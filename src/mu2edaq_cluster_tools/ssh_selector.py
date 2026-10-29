#!/usr/bin/env python3
"""!
@file ssh_selector.py
@brief SSH Host Selector - A TUI application for managing SSH connections.

`ssh-selector` is the operator-facing Textual TUI used to reach Mu2e DAQ and
Fermilab offline cluster nodes. It reads a YAML host inventory, shows the
hosts grouped and paired two per row, and on selection either hands an
interactive `ssh` session to the terminal, opens one in a new terminal
window, or starts an `ssh -N -L` port-forward tunnel (foreground or as a
managed background process).

Side panels show the operator's Kerberos ticket cache (`klist`), the local
network identity, live DNS resolution per host, an optional logged-in user
count per host ("Check Users", an `ssh ... who` probe), and the list of
background tunnels this process owns.

Layout of this module:
- Data model: Host, GroupConfig, Config, and the YAML loader load_config().
- "Check Users" concurrency limit and its CLI/env/YAML resolution.
- Kerberos and network information panels.
- Modal screens: user picker, About, SSH error, tunnel ports, quit confirm.
- Host list items, connection request types, and background tunnel
  management (ActiveTunnel, TunnelRegistry, TunnelPanel).
- The SSHSelector application and main().

Threading model: almost everything runs on the Textual asyncio event loop.
Blocking work (DNS lookups, `ifconfig`/`ip`, `getfqdn`) is pushed to the
default thread-pool executor with run_in_executor() and the results are
applied back on the event loop. Subprocesses for `klist` and `ssh ... who`
use asyncio subprocess APIs, not threads.

@note Authentication is delegated entirely to OpenSSH: Kerberos/GSSAPI,
      keys, and agent forwarding come from the operator's `~/.ssh/config`
      and credential cache. This tool never handles passwords.
"""

from mu2edaq_cluster_tools._version import __version__

import asyncio
import atexit
import errno
import ipaddress
import os
import re
import socket
import struct
import signal
import sys
import subprocess
import tempfile
import argparse
import getpass
import shlex
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from rich.markup import escape as markup_escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static, Switch
from textual import events, on


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

## Login name substituted for the "default" user token.
#  $USER is preferred over getpass.getuser() because operators sometimes run
#  under `sudo -E` or a shared account shell where $USER reflects the
#  identity they want to present to the remote side; getpass is the fallback
#  for environments (cron, Windows services) where USER is unset.
#  Evaluated once at import time, so changing USER later has no effect.
CURRENT_USER = os.environ.get("USER", getpass.getuser())


def resolve_user(user: str) -> str:
    """!
    @brief Resolve the special token 'default' to the current OS user.

    The YAML inventory uses "default" rather than a literal name so one
    shared config file works for every operator.

    @param user  A user entry from the config: a login name or "default".
    @return The login name to pass to `ssh -l`.
    @see CURRENT_USER
    """
    return CURRENT_USER if user == "default" else user


def display_user(user: str) -> str:
    """!
    @brief Human-readable label for a user entry.

    Shows the resolved name together with a "(default)" marker so the
    operator can see which login will actually be used.

    @param user  A user entry from the config: a login name or "default".
    @return Label text for the user picker.
    """
    return f"{CURRENT_USER} (default)" if user == "default" else user


def _format_age(secs: float) -> str:
    """!
    @brief Return a compact human-readable age string (e.g. '4m', '1h 23m').

    Used for the tunnel age column, where width is scarce; precision drops
    as the age grows because seconds stop mattering after the first minute.

    @param secs  Elapsed time in seconds (fractional part is truncated).
    @return "Ns" below one minute, "Nm" below one hour, else "Hh MMm".
    """
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


@dataclass
class Host:
    """!
    @brief One SSH destination from the YAML inventory.

    Immutable in practice after load_config() returns: the application reads
    these objects from the Textual event loop and from executor threads
    (DNS lookups read hostname only), and never mutates them after
    construction, so no locking is needed.

    Invariants established by __post_init__():
    - nickname is never empty (falls back to hostname).
    - users is never empty (falls back to ["default"]).
    """
    ## DNS name or address passed to ssh as the destination.
    hostname: str
    ## Display name in the host list; defaults to hostname.
    nickname: str = ""
    ## Login entries offered for this host; "default" means
    #  the current OS user (see resolve_user()).
    users: list[str] = field(default_factory=lambda: ["default"])
    ## sshd port on the destination (22 is omitted from argv).
    port: int = 22
    ## Jump host for `ssh -J`, either "host" or `user@host`;
    #  None for a direct connection.
    proxy_jump: Optional[str] = None
    ## Login on the jump host when proxy_jump has no user part;
    #  None means "same user as the destination".
    proxy_user: Optional[str] = None
    ## Group name used for list headings and group defaults.
    group: str = ""

    def __post_init__(self) -> None:
        """!
        @brief Fill in defaults for an empty nickname or user list.

        Hosts may be built directly (tests, future callers) as well as by
        load_config(), so the invariants are enforced here rather than only
        in the loader.
        """
        if not self.nickname:
            self.nickname = self.hostname
        if not self.users:
            self.users = ["default"]

    def proxy_target(self, user: str) -> Optional[str]:
        """!
        @brief Return the ProxyJump target (`user@host`) for the given connecting user.

        Uses proxy_user if configured; otherwise defaults to the same user
        being used for the final destination host.

        @param user  User entry chosen for the destination ("default" allowed).
        @return The `-J` argument, or None if this host has no jump host.
        """
        if not self.proxy_jump:
            return None
        # An explicit login in proxy_jump is the operator's deliberate choice
        # (e.g. a shared gateway account); never override it.
        if "@" in self.proxy_jump:
            return self.proxy_jump
        # Defaulting the jump login to the destination login matches how
        # Fermilab gateways are used: the same Kerberos principal maps to the
        # same account on the gateway and the inner node, so GSSAPI auth
        # succeeds on both hops with a single ticket.
        proxy_user = self.proxy_user if self.proxy_user else user
        return f"{resolve_user(proxy_user)}@{self.proxy_jump}"

    def ssh_command(self, user: str, skip_proxy: bool = False) -> list[str]:
        """!
        @brief Build the SSH command list for this host and the given user entry.

        The jump host, when configured, is passed with `-J` so OpenSSH opens
        the hop itself (a `-W` stdio forward through the gateway). The
        operator's `~/.ssh/config` is still honoured for both hops, which is
        where GSSAPIAuthentication / GSSAPIDelegateCredentials for Kerberos
        are normally set.

        @param user        User entry for the destination ("default" allowed).
        @param skip_proxy  If True, connect directly and ignore proxy_jump;
                           set when the destination is on a local subnet.
        @return argv list suitable for subprocess / os.execvp().
        """
        cmd = ["ssh"]
        # Omit -p for the standard port so a Port directive in ~/.ssh/config
        # is not silently overridden by an explicit 22.
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.proxy_jump and not skip_proxy:
            cmd.extend(["-J", self.proxy_target(user)])
        # -l rather than user@host keeps the destination argument a bare
        # hostname, so Host blocks in ~/.ssh/config still match it.
        cmd.extend(["-l", resolve_user(user)])
        cmd.append(self.hostname)
        return cmd

    def tunnel_command(
        self, user: str, local_port: int, remote_port: int, skip_proxy: bool = False
    ) -> list[str]:
        """!
        @brief Build an SSH tunnel command: `ssh -N -L local:localhost:remote ...`

        The forward target is "localhost" as seen from the destination host,
        so the remote service only needs to listen on its own loopback.

        @param user         User entry for the destination ("default" allowed).
        @param local_port   Port to listen on locally (loopback only).
        @param remote_port  Port on the destination host's loopback.
        @param skip_proxy   If True, connect directly and ignore proxy_jump.
        @return argv list suitable for subprocess.Popen().
        @warning Without ExitOnForwardFailure, ssh would log in successfully
                 and keep running even when the local port could not be
                 bound, leaving the operator with a "running" tunnel that
                 forwards nothing.
        """
        # -N: no remote command, the session exists only to carry the forward.
        # ExitOnForwardFailure=yes: turn a failed bind into a non-zero exit so
        # the background tunnel reaper can report it instead of hiding it.
        cmd = [
            "ssh", "-N", "-o", "ExitOnForwardFailure=yes",
            "-L", f"{local_port}:localhost:{remote_port}",
        ]
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.proxy_jump and not skip_proxy:
            cmd.extend(["-J", self.proxy_target(user)])
        cmd.extend(["-l", resolve_user(user)])
        cmd.append(self.hostname)
        return cmd


@dataclass
class GroupConfig:
    """!
    @brief Per-group defaults that individual host entries can override.

    Built once by load_config() and read-only afterwards.
    """
    ## Default user entries for hosts in the group; an empty
    #  list means "no group default" (fall back to "default").
    users: list[str] = field(default_factory=list)
    ## Default jump host for the group, or None.
    proxy_jump: Optional[str] = None
    ## Default jump-host login for the group, or None.
    proxy_user: Optional[str] = None


@dataclass
class Config:
    """!
    @brief Fully parsed ssh-selector configuration.

    Produced by load_config(); main() may then overwrite check_concurrency
    with the value resolved from the command line and environment before the
    application starts. Not mutated once SSHSelector is running.
    """
    ## Host entries in file order.
    hosts: list[Host]
    ## Ordered group names; controls display order.
    grouplist: list[str]          # ordered group names; controls display order
    ## Per-group defaults keyed by group name.
    groups: dict[str, GroupConfig] = field(default_factory=dict)
    ## Seconds before a user-count result is re-fetched.
    cache_lifetime: int = 600     # seconds before a user-count result is re-fetched
    ## Fixed column width of the right-hand info panel.
    panel_width: int = 50         # fixed column width of the right-hand info panel
    ## Max simultaneous ssh processes for "Check Users".
    check_concurrency: int = 8    # max simultaneous ssh processes for "Check Users"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _parse_users(raw: object) -> list[str]:
    """!
    @brief Normalise a users value (scalar or list) into a list of strings.

    The YAML accepts both `user: alice` and `users: [alice, bob]`; numeric
    logins (YAML ints) are stringified. An empty value yields ["default"] so
    a host is always connectable.

    @param raw  The raw YAML value: list, scalar, or None.
    @return A non-empty list of user entries.
    """
    if isinstance(raw, list):
        return [str(u) for u in raw] or ["default"]
    return [str(raw)] if raw else ["default"]


def load_config(config_path: Path) -> Config:
    """!
    @brief Parse a YAML config file and return a Config object.

    Precedence for each host field is: key present on the host entry (even
    if null) > the host's group defaults in `groups:` > built-in default.
    `user` is accepted as a synonym of `users` at both levels.

    @param config_path  Path to the YAML inventory.
    @return The parsed Config, with check_concurrency validated.
    @throws OSError     If the file cannot be opened.
    @throws yaml.YAMLError  If the file is not valid YAML.
    @throws ValueError  If a host lacks `hostname`, or a numeric field
                        (port, cache_lifetime, panel_width,
                        check_concurrency) is not a valid integer.
    @throws AttributeError  If the top level of the file is not a mapping
                            (e.g. an empty file, where safe_load returns None).
    """
    with open(config_path) as f:
        # safe_load: the inventory is shared between operators; never allow
        # YAML tags that construct arbitrary Python objects.
        data = yaml.safe_load(f)

    grouplist: list[str] = [str(g) for g in data.get("grouplist", [])]

    # Parse optional per-group defaults
    groups: dict[str, GroupConfig] = {}
    for gname, gdata in (data.get("groups") or {}).items():
        # A group key with no body ("mu2e-daq:") parses as None.
        gdata = gdata or {}
        raw_users = gdata.get("users", gdata.get("user"))
        groups[str(gname)] = GroupConfig(
            # Keep "no group users" distinct from ["default"] so the
            # host-level fallback below can tell them apart.
            users=_parse_users(raw_users) if raw_users is not None else [],
            proxy_jump=gdata.get("proxy_jump") or None,
            proxy_user=gdata.get("proxy_user") or None,
        )

    hosts: list[Host] = []
    for entry in data.get("hosts", []):
        if "hostname" not in entry:
            raise ValueError(f"Host entry missing required 'hostname' field: {entry}")

        group_name = str(entry.get("group", ""))
        grp = groups.get(group_name, GroupConfig())

        # users: host entry wins if key present, else fall back to group, else default
        if "users" in entry or "user" in entry:
            users = _parse_users(entry.get("users", entry.get("user")))
        else:
            users = grp.users if grp.users else ["default"]

        # proxy_jump: host entry wins if key is present (even if null), else group default
        # An explicit null is how an operator exempts one host (e.g. the
        # gateway itself) from its group's jump host.
        if "proxy_jump" in entry:
            proxy_jump = entry["proxy_jump"] or None
        else:
            proxy_jump = grp.proxy_jump

        # proxy_user: host entry wins if key is present (even if null), else group default.
        # If left unset entirely, ssh_command()/tunnel_command() fall back to the same
        # user used for the final destination host.
        if "proxy_user" in entry:
            proxy_user = entry["proxy_user"] or None
        else:
            proxy_user = grp.proxy_user

        hosts.append(Host(
            hostname=entry["hostname"],
            nickname=entry.get("nickname", entry["hostname"]),
            users=users,
            port=int(entry.get("port", 22)),
            proxy_jump=proxy_jump,
            proxy_user=proxy_user,
            group=group_name,
        ))

    cache_lifetime = int(data.get("cache_lifetime", 600))
    panel_width = int(data.get("panel_width", 50))
    # Validate here so a bad YAML value is reported at startup, naming the
    # key, rather than surfacing later as an asyncio.Semaphore error.
    check_concurrency = _parse_check_concurrency(
        data.get("check_concurrency", DEFAULT_CHECK_CONCURRENCY), "check_concurrency"
    )
    return Config(
        hosts=hosts,
        grouplist=grouplist,
        groups=groups,
        cache_lifetime=cache_lifetime,
        panel_width=panel_width,
        check_concurrency=check_concurrency,
    )


# ---------------------------------------------------------------------------
# "Check Users" concurrency limit
# ---------------------------------------------------------------------------

## Default cap on simultaneous `ssh ... who` probes for "Check Users".
#  Below OpenSSH's default MaxStartups start value (10), so one jump host never
#  sees more unauthenticated connections from this tool than sshd accepts
#  without starting to drop them.
#  sshd's default MaxStartups is "10:30:100": once 10 connections are in the
#  pre-authentication phase, sshd randomly drops 30% of new ones, rising to
#  100% at 100. Every probe through a shared gateway counts against that
#  gateway's budget, and other operators' logins share it too, so the
#  default leaves headroom.
#  @warning Raising this above 10 for hosts behind one jump host can make
#           sshd drop connections, including other operators' interactive
#           logins to the same gateway.
DEFAULT_CHECK_CONCURRENCY = 8
## Environment variable overriding the YAML check_concurrency value.
CHECK_CONCURRENCY_ENV = "SSH_SELECTOR_CHECK_CONCURRENCY"
## Wall-clock limit for one `ssh ... who` probe (ConnectTimeout=5 covers TCP only).
#  ConnectTimeout bounds the TCP connect, not the SSH banner exchange, GSSAPI
#  negotiation, or a jump host that accepts TCP but then stalls (e.g. under
#  MaxStartups pressure). Without a total deadline a stalled probe would
#  hold a concurrency slot indefinitely and starve the rest of the check.
CHECK_USERS_TIMEOUT = 12.0


def _parse_check_concurrency(raw: object, source: str) -> int:
    """!
    @brief Validate a check-concurrency value; raise ValueError naming *source*.

    @param raw     Value from the CLI, environment, or YAML (any type;
                   stringified and stripped before int conversion).
    @param source  Name of the setting shown in the error, e.g.
                   "--check-concurrency" or the environment variable name.
    @return The value as a positive int.
    @throws ValueError  If the value is not an integer or is below 1.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        # "from None": the int() traceback adds nothing for the operator;
        # the message already names the offending setting and value.
        raise ValueError(f"{source}: expected a positive integer, got {raw!r}") from None
    # Zero would deadlock the probe semaphore; negatives are meaningless.
    if value < 1:
        raise ValueError(f"{source}: expected a positive integer, got {value}")
    return value


def resolve_check_concurrency(
    cli_value: Optional[int], environ: "dict[str, str]", config_value: int
) -> int:
    """!
    @brief Return the effective limit: command line > environment > YAML config > default.

    The YAML default is already folded into config_value by load_config().
    Each source is validated with its own name so the error points at the
    setting the operator actually used.

    @param cli_value     Value of --check-concurrency, or None if not given.
    @param environ       Environment mapping (os.environ, or a dict in tests).
    @param config_value  Value from the YAML config (or its default).
    @return The positive integer limit to use.
    @throws ValueError  If the winning source holds an invalid value.
    @see CHECK_CONCURRENCY_ENV, DEFAULT_CHECK_CONCURRENCY
    """
    if cli_value is not None:
        return _parse_check_concurrency(cli_value, "--check-concurrency")
    # A set-but-blank variable is treated as unset, matching common shell
    # usage like `SSH_SELECTOR_CHECK_CONCURRENCY= ssh-selector`.
    env_value = environ.get(CHECK_CONCURRENCY_ENV, "").strip()
    if env_value:
        return _parse_check_concurrency(env_value, CHECK_CONCURRENCY_ENV)
    return _parse_check_concurrency(config_value, "check_concurrency")


# ---------------------------------------------------------------------------
# Kerberos ticket helpers
# ---------------------------------------------------------------------------

## Regex for one ticket line of `klist` output: start, expiry, principal.
#  Matches macOS/Heimdal:  "Mar 31 13:58:47 2026  Apr  1 15:58:47 2026  principal"
#  Also matches Linux MIT:  "01/15/2024 10:00:00  01/15/2024 18:00:00  principal"
#  Groups: 1 = issue time, 2 = expiry time, 3 = service principal.
#  @note MIT klist prints dates in the C locale's %x format; under a
#        non-US locale (e.g. DD/MM/YY) the line will not match and the
#        ticket is silently omitted from the panel.
_KLIST_DATE_RE = re.compile(
    r"([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}"   # macOS month-name
    r"|\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"               # Linux MM/DD/YYYY
    r"\s+"
    r"([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}"
    r"|\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"
    r"\s+(.+)"
)
## strptime formats tried in order: Heimdal month-name form, then MIT numeric form.
_DATE_FMTS = ("%b %d %H:%M:%S %Y", "%m/%d/%Y %H:%M:%S")


def _parse_klist_date(s: str) -> Optional[datetime]:
    """!
    @brief Parse one klist timestamp in either supported format.

    klist prints local time without a zone, so the result is a naive
    datetime that is compared against datetime.now() (also local, naive).

    @param s  Timestamp text captured by _KLIST_DATE_RE.
    @return The parsed naive local datetime, or None if no format matches.
    """
    # Heimdal pads single-digit days with a space ("Apr  1"); strptime's %d
    # accepts "1" but not the double space, so normalise first.
    s = " ".join(s.split())  # collapse extra whitespace (e.g. "Apr  1" → "Apr 1")
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _format_klist(output: str) -> str:
    """!
    @brief Convert raw klist stdout into Rich markup for display.

    Each ticket is shown with its remaining lifetime, colour-coded: red when
    expired, yellow under two hours (time to run kinit before a shift
    hand-over), green otherwise.

    @param output  Decoded stdout of `klist`.
    @return Rich markup for the Kerberos panel; never empty.
    """
    now = datetime.now()
    principal = ""
    ticket_lines: list[str] = []

    for line in output.splitlines():
        stripped = line.strip()
        # "Principal: user@REALM" or "Default principal: user@REALM"
        # The match on "rincipal:" covers both capitalisations; the date
        # guard stops a ticket line whose service name happens to contain
        # "principal:" from being taken as the header.
        if "rincipal:" in stripped and not re.match(r"[A-Za-z]{3}\s", stripped):
            principal = stripped.split(":", 1)[1].strip()
            continue
        m = _KLIST_DATE_RE.match(stripped)
        if not m:
            # Headers, "renew until" lines, and flags lines are not tickets.
            continue
        expires = _parse_klist_date(m.group(2))
        service = m.group(3).strip()
        if expires is None:
            # Still list the service so the operator sees it exists, aligned
            # under the status column.
            ticket_lines.append(f"           {service}")
            continue
        secs = (expires - now).total_seconds()
        if secs < 0:
            tag, status = "red", "EXPIRED"
        elif secs < 7200:
            tag = "yellow"
            h, mn = divmod(int(secs) // 60, 60)
            status = f"{h}h {mn:02d}m"
        else:
            tag, status = "green", f"{int(secs)//3600}h"
        ticket_lines.append(f"[{tag}]{status:>8}[/{tag}]  {service}")

    parts: list[str] = []
    if principal:
        parts.append(f"[dim]Principal:[/dim] {principal}\n")
    if ticket_lines:
        parts.extend(ticket_lines)
    else:
        parts.append("[dim]No tickets found.[/dim]")
    return "\n".join(parts)


async def _get_kerberos_tickets() -> str:
    """!
    @brief Run klist asynchronously and return Rich-markup formatted output.

    Uses an asyncio subprocess so the Textual event loop is never blocked,
    even if the KDC-backed credential cache (e.g. KCM) is slow to answer.

    @return Rich markup for the Kerberos panel. All errors are converted to
            a human-readable message; nothing is raised.
    @note On Windows klist is usually the Microsoft LSA tool, whose output
          format differs; it will run but show "No tickets found."
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "klist",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return "[dim]klist not available on this system.[/dim]"
    except Exception as exc:
        return f"[red]Error running klist: {exc}[/red]"

    # klist exits non-zero when there is no credential cache at all, which is
    # the normal state before kinit; it is not an error worth showing in red.
    if proc.returncode != 0:
        return "[dim]No active Kerberos tickets.[/dim]"
    return _format_klist(stdout.decode())


class KerberosPanel(Widget):
    """!
    @brief Widget that displays active Kerberos tickets, auto-refreshed every 60 s.

    Holds no state of its own beyond the child Static. All methods run on the
    Textual event loop; the klist call is an async worker on that same loop.

    Kerberos matters here because Fermilab gateways authenticate with
    GSSAPI: an expired ticket is the most common reason an `ssh -J` login
    falls back to a password prompt or fails outright, so the panel lets the
    operator see that before connecting.
    """

    def compose(self) -> ComposeResult:
        """!
        @brief Build the placeholder shown until the first klist completes.
        @return Generator of child widgets.
        """
        yield Static("Checking Kerberos tickets…", id="kerberos-content")

    def on_mount(self) -> None:
        """!
        @brief Textual Mount handler: fetch tickets now and every 60 seconds.

        Periodic refresh keeps the remaining-lifetime column honest while the
        TUI is left open for a whole shift, and picks up a kinit run in
        another terminal.
        """
        self.refresh_tickets()
        self.set_interval(60, self.refresh_tickets)

    def refresh_tickets(self) -> None:
        """!
        @brief Start a klist worker, cancelling any one still running.

        exclusive=True means a hung klist cannot pile up a new subprocess
        every 60 seconds; the previous worker is cancelled first.
        """
        self.run_worker(self._fetch(), exclusive=True)

    def _refresh(self) -> None:
        """!
        @brief Alias of refresh_tickets() kept for backward compatibility.
        """
        self.refresh_tickets()

    async def _fetch(self) -> None:
        """!
        @brief Worker body: run klist and update the panel text.
        """
        text = await _get_kerberos_tickets()
        try:
            self.query_one("#kerberos-content", Static).update(text)
        except Exception:
            # The panel may have been unmounted (app exiting or the screen
            # rebuilt after a connection) while klist was running.
            pass


# ---------------------------------------------------------------------------
# Network info panel
# ---------------------------------------------------------------------------

def _detect_local_networks_sync() -> list[ipaddress.IPv4Network]:
    """!
    @brief Return local IPv4 networks parsed from interface configuration.

    The result drives the "skip the jump host" decision: if a destination's
    address is on a directly attached subnet (e.g. the operator is already
    inside the DAQ enclave), going through the gateway is unnecessary and
    may even be impossible from the inside.

    Blocking (runs external commands); call from an executor thread via
    detect_local_networks().

    @return Networks of every IPv4 interface found, loopback included;
            empty if neither tool is available or both fail.
    @note Linux uses `ip addr show`; macOS and BSD fall back to `ifconfig`,
          whose netmask is printed in hex (0xffffff00) on macOS and dotted
          form elsewhere. Windows has neither, so the list is empty and the
          jump host is always used.
    """
    networks: list[ipaddress.IPv4Network] = []

    # Linux: ip addr show  →  "inet x.x.x.x/prefix"
    try:
        out = subprocess.check_output(
            ["ip", "addr", "show"], stderr=subprocess.DEVNULL, text=True
        )
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out):
            try:
                networks.append(ipaddress.IPv4Interface(m.group(1)).network)
            except ValueError:
                pass
        # Only stop here if ip gave usable output; some macOS installs ship
        # an `ip` shim with a different format, so fall through to ifconfig.
        if networks:
            return networks
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # macOS / fallback: ifconfig  →  "inet x.x.x.x netmask 0xhhhhhhhh"
    #                             or "inet x.x.x.x netmask d.d.d.d"
    try:
        out = subprocess.check_output(
            ["ifconfig"], stderr=subprocess.DEVNULL, text=True
        )
        for m in re.finditer(
            r"inet (\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)",
            out,
        ):
            ip_str, mask_raw = m.group(1), m.group(2)
            try:
                if mask_raw.lower().startswith("0x"):
                    # Hex mask -> 4 network-order bytes -> dotted quad.
                    mask_str = socket.inet_ntoa(struct.pack(">I", int(mask_raw, 16)))
                else:
                    mask_str = mask_raw
                networks.append(
                    ipaddress.IPv4Interface(f"{ip_str}/{mask_str}").network
                )
            except (ValueError, OSError):
                pass
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    return networks


async def detect_local_networks() -> list[ipaddress.IPv4Network]:
    """!
    @brief Async wrapper around _detect_local_networks_sync.

    Runs the blocking interface scan in the default thread-pool executor so
    the Textual event loop keeps drawing.

    @return Local IPv4 networks (see _detect_local_networks_sync()).
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _detect_local_networks_sync)


async def get_local_network_info() -> str:
    """!
    @brief Return Rich-markup text describing the local network interface.

    Shows hostname, primary IPv4 address, DNS domain, and attached subnets so
    the operator can tell at a glance whether they are on site, on VPN, or
    inside an enclave (which determines whether jump hosts are needed).

    @return Rich markup for the network panel; any failure yields a short
            "unavailable" message rather than an exception.
    """
    loop = asyncio.get_event_loop()
    try:
        hostname = socket.gethostname()
        # getfqdn() does a reverse DNS lookup that can block for seconds on a
        # broken resolver; keep it off the event loop.
        fqdn = await loop.run_in_executor(None, socket.getfqdn)
        # UDP connect() sends no packets; it only asks the kernel which
        # source address it would route from, which gives the primary
        # interface address even on multi-homed hosts or behind VPNs.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            except Exception:
                # No default route (isolated DAQ network): fall back to the
                # address the hostname resolves to.
                local_ip = await loop.run_in_executor(
                    None, socket.gethostbyname, hostname
                )
        domain_parts = fqdn.split(".")
        domain = ".".join(domain_parts[1:]) if len(domain_parts) > 1 else "(local)"
        # Show non-loopback subnets
        nets = await detect_local_networks()
        net_strs = [str(n) for n in nets if not n.is_loopback]
        net_line = ", ".join(net_strs) if net_strs else "(unknown)"
        return (
            f"[dim]Host   :[/dim]  {hostname}\n"
            f"[dim]IP     :[/dim]  {local_ip}\n"
            f"[dim]Domain :[/dim]  {domain}\n"
            f"[dim]Subnets:[/dim]  {net_line}"
        )
    except Exception:
        return "[dim]Network info unavailable.[/dim]"


class NetworkInfoPanel(Widget):
    """!
    @brief Widget showing local network identity, detected once on startup.

    Not refreshed periodically: interface changes during a session are rare,
    and the information is advisory. Runs entirely on the Textual event loop;
    blocking lookups inside get_local_network_info() go to executor threads.
    """

    def compose(self) -> ComposeResult:
        """!
        @brief Build the placeholder shown until detection completes.
        @return Generator of child widgets.
        """
        yield Static("Detecting network…", id="network-content")

    def on_mount(self) -> None:
        """!
        @brief Textual Mount handler: start the one-shot detection worker.
        """
        self.run_worker(self._fetch(), exclusive=True)

    async def _fetch(self) -> None:
        """!
        @brief Worker body: gather network info and update the panel text.
        """
        text = await get_local_network_info()
        try:
            self.query_one("#network-content", Static).update(text)
        except Exception:
            # Widget unmounted before detection finished.
            pass


# ---------------------------------------------------------------------------
# User selection modal
# ---------------------------------------------------------------------------

class UserItem(ListItem):
    """!
    @brief List item wrapping a user entry string.

    Stores the raw entry ("default" or a login) so the modal can return it
    unresolved; resolution happens when the ssh argv is built.
    """

    def __init__(self, user: str) -> None:
        """!
        @brief Create a row for one user entry.
        @param user  User entry from Host.users.
        """
        super().__init__()
        ## The user entry this row represents.
        self.user = user

    def compose(self) -> ComposeResult:
        """!
        @brief Build the row label, marking the default user.
        @return Generator of child widgets.
        """
        yield Label(display_user(self.user))


class UserSelectModal(ModalScreen[Optional[str]]):
    """!
    @brief Modal overlay for picking a user when a host has multiple users.

    Dismisses with the chosen user entry (unresolved, "default" allowed), or
    None when cancelled with Escape. Runs on the Textual event loop.
    """

    ## Textual CSS for the centred picker box.
    CSS = """
    UserSelectModal {
        align: center middle;
    }

    #modal-container {
        width: 50;
        height: auto;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    #modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }

    #user-list {
        height: auto;
        max-height: 16;
    }
    """

    ## Key bindings: Escape cancels the selection.
    BINDINGS = [
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    def __init__(self, host: Host) -> None:
        """!
        @brief Create the picker for one host.
        @param host  Host whose users list is shown.
        """
        super().__init__()
        ## The host whose users are offered.
        self.host = host

    def compose(self) -> ComposeResult:
        """!
        @brief Build the title, subtitle, and one UserItem per user entry.
        @return Generator of child widgets.
        """
        with Vertical(id="modal-container"):
            yield Label(self.host.nickname, id="modal-title")
            yield Label("Select user:", id="modal-subtitle")
            yield ListView(
                *[UserItem(u) for u in self.host.users],
                id="user-list",
            )

    def on_mount(self) -> None:
        """!
        @brief Textual Mount handler: focus the list so arrows and Enter work at once.
        """
        self.query_one("#user-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """!
        @brief Handle ListView.Selected (Enter or click on a row): return that user.
        @param event  The selection event; event.item is the chosen row.
        """
        if isinstance(event.item, UserItem):
            self.dismiss(event.item.user)

    def action_dismiss_modal(self) -> None:
        """!
        @brief Escape key action: close the picker without choosing a user.
        """
        self.dismiss(None)


# ---------------------------------------------------------------------------
# SSH error modal
# ---------------------------------------------------------------------------

class AboutModal(ModalScreen):
    """!
    @brief Modal overlay showing application info and author contact details.

    Stateless; dismissed by any key. Runs on the Textual event loop.
    """

    ## Textual CSS for the centred About box.
    CSS = """
    AboutModal {
        align: center middle;
    }

    #about-container {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #about-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    #about-body {
        margin-bottom: 1;
    }

    #about-hint {
        color: $text-muted;
        text-style: italic;
    }
    """

    ## Escape closes the modal; listed for completeness since on_key() also
    #  catches every key. Hidden from the footer.
    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def compose(self) -> ComposeResult:
        """!
        @brief Build the title, version/author/licence block, and close hint.
        @return Generator of child widgets.
        """
        with Vertical(id="about-container"):
            yield Label("mu2edaq-cluster-tools", id="about-title")
            yield Static(
                "A keyboard-driven interface for managing SSH connections\n"
                "to Fermilab DAQ and Offline Clusters.\n\n"
                f"[dim]Version :[/dim]  {__version__}\n"
                "[dim]Author  :[/dim]  Andrew J. Norman\n"
                "[dim]Email   :[/dim]  anorman@fnal.gov\n"
                "[dim]Repo    :[/dim]  github.com/Mu2e/mu2edaq-cluster-tools\n"
                "[dim]License :[/dim]  MIT\n",
                id="about-body",
            )
            yield Label("Press any key to close", id="about-hint")

    def on_key(self) -> None:
        """!
        @brief Textual Key handler: any key press closes the modal.
        """
        self.dismiss()


class SSHErrorModal(ModalScreen):
    """!
    @brief Modal that displays an SSH connection error and dismisses on any key.

    Used both for failed interactive sessions (ssh stderr captured by main())
    and for tunnels that could not be started or that exited. Runs on the
    Textual event loop.
    """

    ## Textual CSS for the centred error box (error-coloured border).
    CSS = """
    SSHErrorModal {
        align: center middle;
    }

    #error-container {
        width: 70;
        height: auto;
        max-height: 24;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }

    #error-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }

    #error-body {
        margin-bottom: 1;
    }

    #error-hint {
        color: $text-muted;
        text-style: italic;
    }
    """

    ## Escape closes the modal (on_key() also catches every key). Hidden.
    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def __init__(self, message: str, title: str = "SSH Connection Failed") -> None:
        """!
        @brief Create an error modal.
        @param message  Error text, typically the tail of ssh's stderr.
        @param title    Heading for the modal.
        """
        super().__init__()
        ## Error text to show; displayed literally.
        self.message = message
        # Not "title": Screen already has a reactive `title` attribute.
        ## Heading for the modal.
        self.title_text = title

    def compose(self) -> ComposeResult:
        """!
        @brief Build the heading, the escaped error body, and the close hint.
        @return Generator of child widgets.
        """
        with Vertical(id="error-container"):
            yield Label(self.title_text, id="error-title")
            # ssh stderr routinely contains square brackets (IPv6 addresses,
            # "[localhost]:8080") that Rich would parse as markup and either
            # swallow or raise on; escape so the operator sees the raw text.
            yield Static(markup_escape(self.message), id="error-body")
            yield Label("Press any key to return to the host list", id="error-hint")

    def on_key(self) -> None:
        """!
        @brief Textual Key handler: any key press closes the modal.
        """
        self.dismiss()


# ---------------------------------------------------------------------------
# Tunnel port modal
# ---------------------------------------------------------------------------

class TunnelPortModal(ModalScreen[Optional[tuple[int, int, bool]]]):
    """!
    @brief Modal overlay for entering local/remote ports and foreground/background mode.

    Dismisses with (local_port, remote_port, background) once both ports
    parse as integers in 1-65535, or None on Escape. Invalid input keeps the
    modal open and shows the reason in the inline error label. Runs on the
    Textual event loop.

    Port availability is not checked here; the caller checks the local port
    after dismissal (against both its own tunnels and the OS).
    """

    ## Textual CSS for the tunnel form.
    CSS = """
    TunnelPortModal {
        align: center middle;
    }

    #tunnel-container {
        width: 58;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #tunnel-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    .tunnel-label {
        margin-top: 1;
        color: $text-muted;
    }

    #tunnel-bg-row {
        height: 3;
        margin-top: 1;
        align: left middle;
    }

    #tunnel-bg-label {
        width: auto;
        color: $text-muted;
        margin-right: 1;
    }

    #tunnel-hint {
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }

    #tunnel-error {
        color: $error;
        margin-top: 0;
    }
    """

    ## Key bindings: Escape cancels the tunnel request.
    BINDINGS = [
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    def __init__(self, host: Host) -> None:
        """!
        @brief Create the tunnel form for one host.
        @param host  Destination host for the tunnel.
        """
        super().__init__()
        ## Destination host, used only for the title.
        self.host = host

    def compose(self) -> ComposeResult:
        """!
        @brief Build the local/remote port inputs, error label, and background switch.

        Background defaults to on: the common use (web UIs, EPICS/OTS
        consoles on DAQ nodes) wants the tunnel to keep running while the
        operator returns to the host list.

        @return Generator of child widgets.
        """
        with Vertical(id="tunnel-container"):
            yield Label(f"Open tunnel to {self.host.nickname}", id="tunnel-title")
            yield Label("Local port:", classes="tunnel-label")
            yield Input(placeholder="e.g. 8080", id="local-port")
            yield Label("Remote port (on destination):", classes="tunnel-label")
            yield Input(placeholder="e.g. 8080", id="remote-port")
            yield Label("", id="tunnel-error")
            with Horizontal(id="tunnel-bg-row"):
                yield Label("Run in background:", id="tunnel-bg-label")
                yield Switch(value=True, id="tunnel-bg-switch")
            yield Label(
                "Tab between fields and toggle · Enter on port field to advance · Enter confirms",
                id="tunnel-hint",
            )

    def on_mount(self) -> None:
        """!
        @brief Textual Mount handler: focus the local port field.
        """
        self.query_one("#local-port", Input).focus()

    def on_key(self, event: events.Key) -> None:
        """!
        @brief Textual Key handler: Enter while the Switch has focus submits the form.

        A Switch toggles on Enter by default; the hint promises "Enter
        confirms", so the event is consumed here and submitted instead.

        @param event  The key event.
        """
        if event.key == "enter" and isinstance(self.focused, Switch):
            event.prevent_default()
            event.stop()
            self._try_submit()

    @on(Input.Submitted, "#local-port")
    def _local_submitted(self) -> None:
        """!
        @brief Input.Submitted on the local port (Enter): move to the remote port.
        """
        self.query_one("#remote-port", Input).focus()

    @on(Input.Submitted, "#remote-port")
    def _remote_submitted(self) -> None:
        """!
        @brief Input.Submitted on the remote port (Enter): validate and submit.
        """
        self._try_submit()

    def _try_submit(self) -> None:
        """!
        @brief Validate both ports and dismiss with the result, or show the error.

        Ports below 1024 are accepted: binding them locally needs root on
        POSIX, but that failure is reported by the caller's port check with a
        clear message rather than refused here.
        """
        local_str = self.query_one("#local-port", Input).value.strip()
        remote_str = self.query_one("#remote-port", Input).value.strip()
        error_label = self.query_one("#tunnel-error", Label)
        try:
            local_port = int(local_str)
            remote_port = int(remote_str)
            if not (1 <= local_port <= 65535):
                raise ValueError(f"Local port {local_port} out of range 1–65535")
            if not (1 <= remote_port <= 65535):
                raise ValueError(f"Remote port {remote_port} out of range 1–65535")
            background = self.query_one("#tunnel-bg-switch", Switch).value
            self.dismiss((local_port, remote_port, background))
        except ValueError as exc:
            # int("") / int("abc") raise ValueError with a Python-internal
            # message; show it, or a generic hint if the message is empty.
            error_label.update(str(exc) if str(exc) else "Please enter valid port numbers.")

    def action_dismiss_modal(self) -> None:
        """!
        @brief Escape key action: close the form without starting a tunnel.
        """
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Quit confirmation when background tunnels are running
# ---------------------------------------------------------------------------

class QuitTunnelsModal(ModalScreen[Optional[str]]):
    """!
    @brief Ask whether to stop or keep running background tunnels on quit.

    Dismisses with "stop", "keep", or None (cancel, stay in the UI).

    Shown only when live background tunnels exist. Stop is bound to both s
    and y so a reflexive "yes" does the safe thing; keep requires the
    deliberate k.

    @warning Kept tunnels outlive ssh-selector as orphaned `ssh -N`
             processes holding local ports and authenticated sessions to
             DAQ hosts. They must be stopped by PID later; main() prints
             the PIDs and log paths on exit for that purpose.
    """

    ## Textual CSS for the warning-coloured confirmation box.
    CSS = """
    QuitTunnelsModal {
        align: center middle;
    }

    #quit-container {
        width: 70;
        height: auto;
        max-height: 24;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }

    #quit-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #quit-hint {
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    """

    ## Key bindings: s/y stop tunnels and quit, k keeps them and quits,
    #  Escape/n cancel. y and n are hidden aliases for operators who answer
    #  any prompt with y/n.
    BINDINGS = [
        Binding("s", "choose('stop')", "Stop tunnels and quit", show=True),
        Binding("y", "choose('stop')", show=False),
        Binding("k", "choose('keep')", "Keep tunnels and quit", show=True),
        Binding("escape", "choose('cancel')", "Cancel", show=True),
        Binding("n", "choose('cancel')", show=False),
    ]

    def __init__(self, tunnels: "list[ActiveTunnel]") -> None:
        """!
        @brief Create the confirmation dialog.
        @param tunnels  Live background tunnels to list.
        """
        super().__init__()
        ## Snapshot of the live tunnels listed in the dialog.
        self.tunnels = tunnels

    def compose(self) -> ComposeResult:
        """!
        @brief Build the heading, one line per tunnel with its PID, and the key hint.
        @return Generator of child widgets.
        """
        # Labels contain host nicknames from the YAML; escape them so a
        # nickname with brackets cannot be read as Rich markup.
        lines = "\n".join(
            f"  {markup_escape(t.label)}  [dim]pid {t.pid}[/dim]" for t in self.tunnels
        )
        with Vertical(id="quit-container"):
            yield Label(
                f"{len(self.tunnels)} background tunnel(s) still running", id="quit-title"
            )
            yield Static(lines)
            yield Label(
                "s: stop them and quit · k: keep them running and quit · Esc: cancel",
                id="quit-hint",
            )

    def action_choose(self, choice: str) -> None:
        """!
        @brief Key action for s/y/k/Escape/n: dismiss with the operator's choice.
        @param choice  "stop", "keep", or "cancel" (mapped to None).
        """
        self.dismiss(None if choice == "cancel" else choice)


# ---------------------------------------------------------------------------
# Host list items
# ---------------------------------------------------------------------------

class GroupHeader(ListItem):
    """!
    @brief Non-interactive group heading shown in the host list.

    Constructed disabled so ListView cursor movement skips it and it can
    never be "selected" as a host.
    """

    def __init__(self, name: str) -> None:
        """!
        @brief Create a heading row.
        @param name  Group name to display.
        """
        super().__init__(disabled=True)
        # Not "name": Widget.name is an existing attribute used by Textual.
        ## Group name shown in the heading.
        self.group_name = name

    def compose(self) -> ComposeResult:
        """!
        @brief Build the indented heading label.
        @return Generator of child widgets.
        """
        yield Label(f"  {self.group_name}")


class HostPairItem(ListItem):
    """!
    @brief A list item holding one or two hosts displayed side by side.

    Two columns halve the list height for large clusters; the application
    tracks which column is active. Each column has a status label (DNS
    indicator) and a name label (nickname plus optional user count). The
    set_* methods must be called on the Textual event loop.
    """

    def __init__(self, hosts: list[Host]) -> None:
        """!
        @brief Create a row for one or two hosts.
        @param hosts  One or two hosts, left column first.
        """
        super().__init__()
        ## The one or two hosts in this row, left column first.
        self.hosts = hosts  # 1 or 2 entries

    def compose(self) -> ComposeResult:
        """!
        @brief Build one status label and one name label per host column.

        The status starts as "~" (DNS lookup pending). CSS classes
        host-status-N and col-N let set_status()/set_user_count() find the
        column for a given host.

        @return Generator of child widgets.
        """
        with Horizontal():
            for i, host in enumerate(self.hosts):
                with Horizontal(classes=f"host-col col-{i}"):
                    yield Label(" ~", classes=f"host-status host-status-{i}")
                    yield Label(host.nickname, classes="host-name")

    def set_status(self, hostname: str, ip: Optional[str]) -> None:
        """!
        @brief Update the DNS indicator for the given hostname.

        Green means the name resolved, red means it did not. Resolution is
        not reachability; it is a quick sign of a typo in the inventory or a
        missing VPN / site DNS.

        @param hostname  Host whose column to update; ignored if not in this row.
        @param ip        Resolved address, or None if resolution failed.
        """
        for i, host in enumerate(self.hosts):
            if host.hostname == hostname:
                dot = "[green] ●[/green]" if ip else "[red] ●[/red]"
                self.query_one(f".host-status-{i}", Label).update(dot)

    def set_user_count(self, hostname: str, count: Optional[int]) -> None:
        """!
        @brief Update the logged-in user count shown next to the nickname.

        Lets an operator see which nodes are busy before logging in, e.g.
        to avoid a node another shifter is actively using.

        @param hostname  Host whose column to update; ignored if not in this row.
        @param count     Number of logged-in users, or None to show the
                         bare nickname (probe failed or not yet run).
        """
        for i, host in enumerate(self.hosts):
            if host.hostname == hostname:
                label = self.query_one(f".col-{i} .host-name", Label)
                text = host.nickname if count is None else f"{host.nickname} ({count})"
                label.update(text)


# ---------------------------------------------------------------------------
# Connection result types
# ---------------------------------------------------------------------------

@dataclass
class ConnectionRequest:
    """!
    @brief Describes the connection the user wants to make.

    Returned by SSHSelector via App.exit(). The TUI exits before ssh runs so
    that the interactive session gets the real terminal; main() executes the
    request and then restarts the TUI.
    """
    ## Destination host.
    host: "Host"
    ## User entry ("default" allowed; resolved later).
    user: str
    ## True if the destination is on a local subnet and the
    #  jump host should be bypassed.
    skip_proxy: bool
    ## "shell" for an interactive session in this terminal,
    #  "new_terminal" to open one in a new window, or
    #  "tunnel" for a foreground port forward.
    mode: str = "shell"       # "shell" or "tunnel"
    ## Local port for mode "tunnel", else 0.
    local_port: int = 0
    ## Destination port for mode "tunnel", else 0.
    remote_port: int = 0


@dataclass
class ActiveTunnel:
    """!
    @brief A background SSH port-forward tunnel managed by the application.

    Owned by TunnelRegistry. Created and read on the Textual event loop and,
    at shutdown, from the atexit hook or a signal handler on the main thread;
    there is no concurrent access from worker threads.
    """
    ## Destination host of the forward.
    host: "Host"
    ## User entry the tunnel logged in as.
    user: str
    ## Local loopback port being forwarded.
    local_port: int
    ## Port on the destination's loopback.
    remote_port: int
    ## The detached `ssh -N -L` child process.
    process: "subprocess.Popen[bytes]"
    ## time.monotonic() when the tunnel was started; monotonic
    #  so the displayed age is immune to wall-clock steps.
    started_at: float   # time.monotonic()
    ## Temporary file receiving ssh's stderr, or None. Kept
    #  so an unexpected exit can be explained to the operator.
    log_path: Optional[Path] = None   # file receiving the tunnel's ssh stderr

    @property
    def is_alive(self) -> bool:
        """!
        @brief True while the ssh process has not exited.

        poll() also reaps the child, so dead tunnels do not linger as zombies.

        @return Whether the tunnel process is still running.
        """
        return self.process.poll() is None

    @property
    def pid(self) -> int:
        """!
        @brief Process ID of the ssh child, shown so operators can kill kept tunnels.
        @return The PID.
        """
        return self.process.pid

    @property
    def label(self) -> str:
        """!
        @brief One-line description of the forward for dialogs and messages.
        @return Text of the form "localhost:LOCAL -> nickname:REMOTE".
        """
        return f"localhost:{self.local_port} -> {self.host.nickname}:{self.remote_port}"

    def read_log_tail(self, limit: int = 2000) -> str:
        """!
        @brief Return the last *limit* bytes of the ssh stderr log, decoded.

        The tail is where ssh writes the reason it exited (e.g. "bind:
        Address already in use", "Permission denied (gssapi-with-mic)").
        Seeking from the end bounds the read even if a verbose ssh grew the
        log large.

        @param limit  Maximum number of bytes to read from the end.
        @return Stripped text, or "" if there is no log or it is unreadable.
        """
        if self.log_path is None:
            return ""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - limit))
                # errors="replace": the cut may land inside a multi-byte
                # UTF-8 sequence.
                return f.read().decode(errors="replace").strip()
        except OSError:
            return ""

    def remove_log(self) -> None:
        """!
        @brief Delete the stderr log file, ignoring errors if it is already gone.
        """
        if self.log_path is not None:
            try:
                self.log_path.unlink()
            except OSError:
                pass


def _detached_popen_kwargs() -> "dict[str, object]":
    """!
    @brief Popen kwargs that keep terminal signals (Ctrl-C in a foreground session)
    from reaching background tunnels; they are stopped explicitly instead.

    After a background tunnel is started the operator may open an interactive
    ssh session from the same terminal. Without detaching, a Ctrl-C in that
    session (or the terminal closing, SIGHUP) is delivered to the whole
    foreground process group and would silently kill every tunnel.

    @return Keyword arguments to splat into subprocess.Popen().
    @note POSIX: start_new_session=True calls setsid(), leaving the
          terminal's process group and session. Windows:
          CREATE_NEW_PROCESS_GROUP stops CTRL_C_EVENT propagation; the
          getattr default keeps this importable on non-Windows Pythons.
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def check_local_port(port: int) -> Optional[str]:
    """!
    @brief Return why *port* cannot be forwarded on loopback, or None if it is free.

    Mirrors what `ssh -L` binds by default (127.0.0.1 and ::1, with
    SO_REUSEADDR on POSIX). A port held on either address counts as in use,
    because `localhost:PORT` could then reach the other listener.

    This pre-check exists because a background tunnel's bind failure is
    otherwise only visible after the fact (ExitOnForwardFailure makes ssh
    exit, and the reaper reports it later). Checking first gives the
    operator an immediate, specific message. It is advisory: the port can
    still be taken between this check and ssh's own bind.

    @param port  Local TCP port to test.
    @return A human-readable reason the port is unusable, or None if both
            loopback addresses (where supported) are free.
    @note On Windows SO_REUSEADDR is not set: there it means "allow another
          socket to steal a bound port" rather than POSIX's "ignore
          TIME_WAIT", so setting it would hide real conflicts.
    """
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue   # address family not supported on this host
        try:
            # Match sshd/ssh: a port left in TIME_WAIT by a just-closed
            # tunnel is reusable, so it must not be reported as busy.
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # V6ONLY so binding ::1 tests the IPv6 port alone; without it a
            # dual-stack socket on Linux would also collide with 127.0.0.1.
            if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((addr, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                continue   # e.g. IPv6 loopback not configured
            # Windows reports WSAEADDRINUSE (10048) rather than EADDRINUSE;
            # the -1 default keeps the tuple valid where it is not defined.
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
                return f"local port {port} is already in use on {addr}"
            # Typically EACCES for ports below 1024 without privileges.
            return f"cannot bind local port {port} on {addr}: {exc.strerror or exc}"
        finally:
            sock.close()
    return None


class TunnelRegistry:
    """!
    @brief Background tunnels owned by one ssh-selector process.

    Created once in main() and handed to every SSHSelector instance, so tunnels
    survive the UI being rebuilt after a connection, and can be stopped at exit.

    Thread affinity: mutated only on the main thread, either from the
    Textual event loop or, at shutdown, from the atexit hook or a SIGHUP /
    SIGTERM handler. Methods rebind self.tunnels to a new list instead of
    mutating it in place, so a caller iterating an earlier snapshot is not
    disturbed.

    @warning If the Python process is killed with SIGKILL, neither atexit
             nor the signal handlers run and the tunnels are orphaned.
    """

    def __init__(self) -> None:
        """!
        @brief Create an empty registry that stops tunnels on exit by default.
        """
        ## Tunnels started by this process; may include dead
        #  ones until reap_dead() runs.
        self.tunnels: list[ActiveTunnel] = []
        # Default is to stop: leaving authenticated forwards to DAQ hosts
        # running unnoticed is worse than having to restart one.
        ## True once the operator chose "keep" in
        #  QuitTunnelsModal; suppresses shutdown().
        self.keep_on_exit = False   # set when the user chose to leave tunnels running

    def add(self, tunnel: ActiveTunnel) -> None:
        """!
        @brief Register a newly started tunnel.
        @param tunnel  The tunnel to track.
        """
        self.tunnels.append(tunnel)

    def alive(self) -> list[ActiveTunnel]:
        """!
        @brief Return the tunnels whose ssh process is still running.
        @return New list of live tunnels, in start order.
        """
        return [t for t in self.tunnels if t.is_alive]

    def find_local_port(self, port: int) -> Optional[ActiveTunnel]:
        """!
        @brief Return the live tunnel already forwarding local *port*, if any.

        Checked before check_local_port() so the operator is told which of
        their own tunnels holds the port, not just that it is busy.

        @param port  Local port to look up.
        @return The matching live tunnel, or None.
        """
        for t in self.tunnels:
            if t.local_port == port and t.is_alive:
                return t
        return None

    def remove(self, tunnel: ActiveTunnel, terminate: bool = True) -> None:
        """!
        @brief Stop (optionally) and forget one tunnel, deleting its log.
        @param tunnel     The tunnel to remove; ignored if not registered.
        @param terminate  If True, stop the ssh process first (may block up
                          to the _stop_processes() grace period).
        """
        if terminate:
            _stop_processes([tunnel.process])
        self.tunnels = [t for t in self.tunnels if t is not tunnel]
        tunnel.remove_log()

    def reap_dead(self) -> list[ActiveTunnel]:
        """!
        @brief Drop tunnels whose ssh exited and return them (logs still on disk).

        The logs are left in place so the caller can read the exit reason
        and then call ActiveTunnel.remove_log().

        @return The tunnels that were removed; empty if all are alive.
        """
        dead = [t for t in self.tunnels if not t.is_alive]
        if dead:
            self.tunnels = [t for t in self.tunnels if t.is_alive]
        return dead

    def terminate_all(self) -> None:
        """!
        @brief Stop every registered tunnel, delete their logs, and clear the registry.

        All processes are signalled before any is waited for, so the total
        time is one grace period rather than one per tunnel.
        """
        _stop_processes([t.process for t in self.tunnels])
        for t in self.tunnels:
            t.remove_log()
        self.tunnels = []

    def shutdown(self) -> None:
        """!
        @brief atexit hook: stop all tunnels unless the user asked to keep them.

        Also called from the SIGHUP/SIGTERM handler installed by main(), so
        closing the terminal window does not orphan tunnels.
        """
        if not self.keep_on_exit:
            self.terminate_all()


def _stop_processes(procs: "list[subprocess.Popen[bytes]]", grace: float = 2.0) -> None:
    """!
    @brief SIGTERM every live process, wait up to *grace* seconds, then SIGKILL.

    SIGTERM first lets ssh close the channel cleanly so the remote sshd
    tears down the forward and logs a normal disconnect. SIGKILL is the
    backstop for an ssh stuck on a dead network path. The grace period is a
    shared deadline across all processes, not per process.

    @param procs  Processes to stop; already-exited ones are skipped.
    @param grace  Seconds to wait for SIGTERM to take effect overall.
    @note On Windows terminate() and kill() both call TerminateProcess(), so
          there is no graceful phase there.
    """
    live = [p for p in procs if p.poll() is None]
    for p in live:
        try:
            p.terminate()
        except OSError:
            # Raced with the process exiting on its own.
            pass
    deadline = time.monotonic() + grace
    for p in live:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                # Reap so no zombie is left; bounded so a process in
                # uninterruptible sleep cannot hang shutdown.
                p.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


# ---------------------------------------------------------------------------
# Active tunnel list items and panel
# ---------------------------------------------------------------------------

class TunnelItem(ListItem):
    """!
    @brief A list item representing one active background tunnel.

    The age is rendered once at compose time; the panel is rebuilt by the
    application whenever the tunnel set changes, which refreshes it.
    """

    def __init__(self, tunnel: ActiveTunnel) -> None:
        """!
        @brief Create a row for one tunnel.
        @param tunnel  The tunnel to show.
        """
        super().__init__()
        ## The tunnel this row shows and that k / Delete would kill.
        self.tunnel = tunnel

    def compose(self) -> ComposeResult:
        """!
        @brief Build the row: liveness dot, ports and host, login, age, and PID.

        The PID is shown so an operator can find the process with ps or
        kill it from another shell if the TUI is unavailable.

        @return Generator of child widgets.
        """
        dot = "[green] ●[/green]" if self.tunnel.is_alive else "[red] ●[/red]"
        age = _format_age(time.monotonic() - self.tunnel.started_at)
        user = resolve_user(self.tunnel.user)
        yield Label(
            f"{dot} :{self.tunnel.local_port} → "
            f"{self.tunnel.host.nickname}:{self.tunnel.remote_port}  "
            f"[dim]{user}  {age}  pid {self.tunnel.pid}[/dim]",
            classes="tunnel-item-label",
        )


class TunnelPanel(Widget):
    """!
    @brief Widget listing active background tunnels; supports kill via k / Delete.

    The panel does not own tunnels: it displays the list the application
    passes to refresh_tunnels() and posts KillRequested so the application,
    which owns the TunnelRegistry, does the actual termination. Runs on the
    Textual event loop.
    """

    class KillRequested(Message):
        """!
        @brief Posted when the user wants to terminate a tunnel.

        Handled by SSHSelector.on_tunnel_panel_kill_requested().
        """
        def __init__(self, tunnel: ActiveTunnel) -> None:
            """!
            @brief Create the message.
            @param tunnel  The tunnel the operator selected.
            """
            super().__init__()
            ## The tunnel to terminate.
            self.tunnel = tunnel

    ## Key bindings: k and Delete kill the highlighted tunnel. Only active
    #  while the panel (or its list) has focus, so k in the host list is not
    #  affected.
    BINDINGS = [
        Binding("k", "kill_selected", "Kill", show=True),
        Binding("delete", "kill_selected", show=False),
    ]

    def compose(self) -> ComposeResult:
        """!
        @brief Build the (initially empty) tunnel ListView.
        @return Generator of child widgets.
        """
        yield ListView(id="tunnel-list")

    def on_mount(self) -> None:
        """!
        @brief Textual Mount handler: show the "No active tunnels" placeholder.
        """
        self.refresh_tunnels([])

    def refresh_tunnels(self, tunnels: list[ActiveTunnel]) -> None:
        """!
        @brief Rebuild the list from *tunnels*.

        A full rebuild is cheap for the handful of tunnels an operator runs
        and avoids reconciling stale rows.

        @param tunnels  Tunnels to show, typically TunnelRegistry.tunnels.
        """
        lv = self.query_one("#tunnel-list", ListView)
        lv.clear()
        if not tunnels:
            # A plain ListItem, not a TunnelItem, so k on it does nothing.
            lv.append(ListItem(Label("[dim]No active tunnels[/dim]")))
        else:
            for t in tunnels:
                lv.append(TunnelItem(t))

    def action_kill_selected(self) -> None:
        """!
        @brief k / Delete key action: request termination of the highlighted tunnel.

        Kills immediately without confirmation: the action is scoped to one
        visibly highlighted row, and a tunnel is trivial to restart.
        """
        lv = self.query_one("#tunnel-list", ListView)
        if isinstance(lv.highlighted_child, TunnelItem):
            self.post_message(self.KillRequested(lv.highlighted_child.tunnel))


# ---------------------------------------------------------------------------
# Main TUI application
# ---------------------------------------------------------------------------

# App result: a ConnectionRequest, or None if the user quit.
AppResult = Optional[ConnectionRequest]


class SSHSelector(App[AppResult]):
    """Interactive SSH host selector."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #search-bar {
        height: 3;
        margin: 0 1;
    }

    #main {
        layout: horizontal;
        height: 1fr;
    }

    #host-list-container {
        width: 1fr;
        min-width: 24;
        border: solid $primary;
        padding: 0 1;
    }

    #host-list-container > Label {
        color: $primary;
        text-style: bold;
        padding: 0 0 1 0;
    }

    #right-panel {
        width: 50;
    }

    #detail-container {
        height: 40%;
        border: solid $accent;
        padding: 1 2;
    }

    #detail-content {
        height: 1fr;
    }

    #kerberos-container {
        height: 22%;
        border: solid $secondary;
        padding: 0 1;
    }

    #kerberos-title {
        color: $secondary;
        text-style: bold;
        padding: 0 0 1 0;
    }

    KerberosPanel {
        height: 1fr;
        overflow-y: auto;
    }

    #network-container {
        height: 12%;
        border: solid $primary-darken-2;
        padding: 0 1;
    }

    #network-title {
        color: $primary-darken-2;
        text-style: bold;
        padding: 0 0 1 0;
    }

    NetworkInfoPanel {
        height: 1fr;
    }

    #tunnels-container {
        height: 26%;
        border: solid $warning-darken-2;
        padding: 0 1;
    }

    #tunnels-title {
        color: $warning;
        text-style: bold;
        padding: 0 0 1 0;
    }

    TunnelPanel {
        height: 1fr;
    }

    TunnelPanel ListView {
        height: 1fr;
    }

    .tunnel-item-label {
        width: 1fr;
    }

    HostPairItem > Horizontal {
        height: 1;
    }

    .host-col {
        width: 1fr;
        height: 1;
    }

    .host-status {
        width: 3;
        min-width: 3;
        color: $text-muted;
    }

    .host-name {
        width: 1fr;
    }

    ListView {
        height: 1fr;
    }

    ListItem {
        padding: 0 1;
    }

    ListItem.--highlight {
        background: $accent;
        color: $text;
    }

    HostPairItem.--highlight {
        background: $surface;
    }

    .col-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    GroupHeader {
        background: $boost;
        color: $accent;
        text-style: bold;
        padding: 0 0;
        height: 1;
    }

    GroupHeader.--highlight {
        background: $boost;
        color: $accent;
    }

    Input {
        margin: 0;
    }
    """

    BINDINGS = [
        Binding("enter", "connect", "Connect", show=True),
        Binding("n", "new_terminal", "New Terminal"),
        Binding("t", "tunnel", "Tunnel"),
        Binding("c", "check_users", "Check Users"),
        Binding("C", "check_users", show=False),
        Binding("/", "focus_search", "Search"),
        Binding("r", "renew_kerberos", "Renew Kerberos"),
        Binding("A", "about", "About"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape_search", "Back to list", show=False),
    ]

    def __init__(
        self, config: Config, error: str = "", tunnels: Optional[TunnelRegistry] = None
    ) -> None:
        super().__init__()
        self.all_hosts = config.hosts
        self.grouplist = config.grouplist
        self.filtered_hosts: list[Host] = list(config.hosts)
        self._selected_host: Optional[Host] = config.hosts[0] if config.hosts else None
        self._startup_error = error
        self._column: int = 0   # 0 = left, 1 = right (two-column list)
        # hostname → resolved IP (None = lookup failed, key absent = not yet checked)
        self._host_ips: dict[str, Optional[str]] = {}
        self._local_networks: list[ipaddress.IPv4Network] = []
        self._cache_lifetime: int = config.cache_lifetime
        self._panel_width: int = config.panel_width
        # hostname → (count, monotonic timestamp); key absent = never fetched
        self._user_counts: dict[str, tuple[Optional[int], float]] = {}
        self._check_concurrency: int = config.check_concurrency
        self._checks_in_flight: set[str] = set()
        # Shared with main() so tunnels outlive this UI instance.
        self._tunnels: TunnelRegistry = tunnels if tunnels is not None else TunnelRegistry()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Filter hosts  (press / to focus)", id="search-bar")
        with Horizontal(id="main"):
            with Container(id="host-list-container"):
                yield Label("Hosts")
                yield ListView(*self._build_list_items(self.all_hosts), id="list")
            with Vertical(id="right-panel"):
                with Container(id="detail-container"):
                    yield Static("", id="detail-content")
                with Container(id="kerberos-container"):
                    yield Label("Kerberos Tickets", id="kerberos-title")
                    yield KerberosPanel()
                with Container(id="network-container"):
                    yield Label("Network", id="network-title")
                    yield NetworkInfoPanel()
                with Container(id="tunnels-container"):
                    yield Label("Tunnels", id="tunnels-title")
                    yield TunnelPanel()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#right-panel").styles.width = self._panel_width
        self._update_detail(self._selected_host)
        self.query_one("#list").focus()
        if self._startup_error:
            self.call_after_refresh(self.push_screen, SSHErrorModal(self._startup_error))
        self.run_worker(self._resolve_all_hosts(), exclusive=False)
        self.run_worker(self._load_local_networks(), exclusive=False)
        self.call_after_refresh(self._refresh_tunnel_panel)
        self.call_after_refresh(self._cleanup_dead_tunnels)
        self.set_interval(5, self._cleanup_dead_tunnels)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pair_hosts(group_hosts: list[Host]) -> list[HostPairItem]:
        """Convert a flat host list into two-column HostPairItem rows."""
        return [
            HostPairItem(group_hosts[i: i + 2])
            for i in range(0, len(group_hosts), 2)
        ]

    def _build_list_items(self, hosts: list[Host]) -> list[ListItem]:
        """Return ListItems organized by group, with GroupHeader separators."""
        if not self.grouplist:
            return self._pair_hosts(hosts)

        # Bucket hosts by group, preserving per-group order
        in_grouplist: dict[str, list[Host]] = {g: [] for g in self.grouplist}
        extra: dict[str, list[Host]] = {}
        ungrouped: list[Host] = []

        for host in hosts:
            if host.group in in_grouplist:
                in_grouplist[host.group].append(host)
            elif host.group:
                extra.setdefault(host.group, []).append(host)
            else:
                ungrouped.append(host)

        items: list[ListItem] = []
        for group_name in self.grouplist:
            group_hosts = in_grouplist[group_name]
            if group_hosts:
                items.append(GroupHeader(group_name))
                items.extend(self._pair_hosts(group_hosts))
        for group_name, group_hosts in extra.items():
            items.append(GroupHeader(group_name))
            items.extend(self._pair_hosts(group_hosts))
        if ungrouped:
            items.append(GroupHeader("Other"))
            items.extend(self._pair_hosts(ungrouped))

        return items

    def _repopulate_list(self) -> None:
        lv = self.query_one("#list", ListView)
        lv.clear()
        for item in self._build_list_items(self.filtered_hosts):
            lv.append(item)
        # Re-apply any already-cached DNS and user-count status to freshly created items
        for item in lv.query(HostPairItem):
            for host in item.hosts:
                if host.hostname in self._host_ips:
                    item.set_status(host.hostname, self._host_ips[host.hostname])
                if host.hostname in self._user_counts:
                    count, _ = self._user_counts[host.hostname]
                    item.set_user_count(host.hostname, count)
        self._column = 0
        self._selected_host = self.filtered_hosts[0] if self.filtered_hosts else None
        self._update_detail(self._selected_host)

    async def _resolve_all_hosts(self) -> None:
        """Resolve every host's DNS concurrently and update status indicators."""
        loop = asyncio.get_event_loop()

        async def _one(host: Host) -> None:
            try:
                ip: Optional[str] = await loop.run_in_executor(
                    None, socket.gethostbyname, host.hostname
                )
            except Exception:
                ip = None
            self._host_ips[host.hostname] = ip
            for item in self.query(HostPairItem):
                item.set_status(host.hostname, ip)
            if self._selected_host and self._selected_host.hostname == host.hostname:
                self._update_detail(self._selected_host)

        await asyncio.gather(*(_one(h) for h in self.all_hosts))

    async def _load_local_networks(self) -> None:
        """Detect local subnets and cache them; refresh detail panel after."""
        self._local_networks = await detect_local_networks()
        if self._selected_host:
            self._update_detail(self._selected_host)

    def _should_skip_proxy(self, host: Host) -> bool:
        """Return True if the host's IP is within a local subnet (proxy not needed)."""
        if not host.proxy_jump or not self._local_networks:
            return False
        dest_ip = self._host_ips.get(host.hostname)
        if not dest_ip:
            return False
        try:
            dest = ipaddress.IPv4Address(dest_ip)
            return any(dest in net for net in self._local_networks)
        except ValueError:
            return False

    def _update_detail(self, host: Optional[Host]) -> None:
        panel = self.query_one("#detail-content", Static)
        if host is None:
            panel.update("[dim]No host selected.[/dim]")
            return

        port_str = f"{host.port}" if host.port != 22 else "22 (default)"
        if not host.proxy_jump:
            proxy_str = "[dim]none[/dim]"
        elif self._should_skip_proxy(host):
            proxy_str = f"[dim]{host.proxy_target(host.users[0])} (skipped — same subnet)[/dim]"
        else:
            proxy_str = host.proxy_target(host.users[0])
        if host.hostname not in self._host_ips:
            ip_str = "[dim]resolving…[/dim]"
        elif self._host_ips[host.hostname]:
            ip_str = self._host_ips[host.hostname]
        else:
            ip_str = "[red]not resolvable[/red]"

        # Show users list; mark first as default
        user_lines = []
        for i, u in enumerate(host.users):
            label = display_user(u)
            if i == 0:
                label += " [dim](first / default)[/dim]"
            user_lines.append(f"  {label}")
        users_str = "\n".join(user_lines)

        # Logged-in user count
        if host.hostname not in self._user_counts:
            logged_in_str = "[dim]press c to check[/dim]"
        else:
            count, fetched_at = self._user_counts[host.hostname]
            age = time.monotonic() - fetched_at
            expired = age >= self._cache_lifetime
            stale_tag = " [dim](stale)[/dim]" if expired else ""
            age_tag = f" [dim]({_format_age(age)} ago)[/dim]"
            if count is None:
                logged_in_str = f"[dim]unreachable[/dim]{age_tag}{stale_tag}"
            else:
                color = "green" if count == 0 else "yellow"
                logged_in_str = f"[{color}]{count}[/{color}]{age_tag}{stale_tag}"

        # Preview command using the default (first) user
        cmd_str = " ".join(host.ssh_command(host.users[0]))

        panel.update(
            f"[bold white]{host.nickname}[/bold white]\n\n"
            f"[dim]Hostname   :[/dim]  {host.hostname}\n"
            f"[dim]IP         :[/dim]  {ip_str}\n"
            f"[dim]Port       :[/dim]  {port_str}\n"
            f"[dim]Proxy jump :[/dim]  {proxy_str}\n"
            f"[dim]Logged in  :[/dim]  {logged_in_str}\n\n"
            f"[dim]Users:[/dim]\n{users_str}\n\n"
            f"[dim]Command (default user):[/dim]\n"
            f"  [italic]{cmd_str}[/italic]\n\n"
            f"[dim]Press [bold]Enter[/bold] to connect, [bold]q[/bold] to quit.[/dim]"
        )

    def _choose_and_exit(self, host: Host) -> None:
        """Exit with a shell ConnectionRequest, showing the user modal only when needed."""
        skip_proxy = self._should_skip_proxy(host)
        if len(host.users) == 1:
            self.exit(ConnectionRequest(host=host, user=host.users[0], skip_proxy=skip_proxy))
        else:
            def on_user_chosen(user: Optional[str]) -> None:
                if user is not None:
                    self.exit(ConnectionRequest(host=host, user=user, skip_proxy=skip_proxy))

            self.push_screen(UserSelectModal(host), on_user_chosen)

    def _choose_new_terminal(self, host: Host) -> None:
        """Exit with a new_terminal ConnectionRequest, showing user modal only when needed."""
        skip_proxy = self._should_skip_proxy(host)
        if len(host.users) == 1:
            self.exit(ConnectionRequest(
                host=host, user=host.users[0], skip_proxy=skip_proxy, mode="new_terminal"
            ))
        else:
            def on_user_chosen(user: Optional[str]) -> None:
                if user is not None:
                    self.exit(ConnectionRequest(
                        host=host, user=user, skip_proxy=skip_proxy, mode="new_terminal"
                    ))

            self.push_screen(UserSelectModal(host), on_user_chosen)

    def _choose_tunnel(self, host: Host) -> None:
        """Start tunnel flow: pick user (if needed), then prompt for port numbers."""
        skip_proxy = self._should_skip_proxy(host)

        def show_port_modal(user: str) -> None:
            def on_ports_chosen(result: Optional[tuple[int, int, bool]]) -> None:
                if result is None:
                    return
                local_port, remote_port, background = result
                problem = self._local_port_problem(local_port)
                if problem:
                    self.push_screen(SSHErrorModal(problem, title="Tunnel Not Started"))
                    return
                if background:
                    self._start_background_tunnel(host, user, local_port, remote_port, skip_proxy)
                else:
                    self.exit(ConnectionRequest(
                        host=host,
                        user=user,
                        skip_proxy=skip_proxy,
                        mode="tunnel",
                        local_port=local_port,
                        remote_port=remote_port,
                    ))

            self.push_screen(TunnelPortModal(host), on_ports_chosen)

        if len(host.users) == 1:
            show_port_modal(host.users[0])
        else:
            def on_user_chosen(user: Optional[str]) -> None:
                if user is not None:
                    show_port_modal(user)

            self.push_screen(UserSelectModal(host), on_user_chosen)

    def _local_port_problem(self, local_port: int) -> Optional[str]:
        """Return a message if *local_port* cannot be used for a new tunnel."""
        existing = self._tunnels.find_local_port(local_port)
        if existing is not None:
            return (
                f"Local port {local_port} is already forwarded by the background tunnel\n"
                f"  {existing.label}  (pid {existing.pid}).\n"
                "Kill it in the Tunnels panel (k) or choose another local port."
            )
        reason = check_local_port(local_port)
        if reason:
            return f"{reason[0].upper()}{reason[1:]}.\nChoose another local port."
        return None

    def _start_background_tunnel(
        self, host: Host, user: str, local_port: int, remote_port: int, skip_proxy: bool
    ) -> None:
        """Launch an SSH tunnel as a background process and register it."""
        cmd = host.tunnel_command(user, local_port, remote_port, skip_proxy=skip_proxy)
        log_path: Optional[Path] = None
        try:
            fd, name = tempfile.mkstemp(prefix=f"ssh-selector-tunnel-{local_port}-", suffix=".log")
            log_path = Path(name)
            with os.fdopen(fd, "wb") as log:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    **_detached_popen_kwargs(),
                )
        except Exception as exc:
            if log_path is not None:
                try:
                    log_path.unlink()
                except OSError:
                    pass
            self.push_screen(SSHErrorModal(str(exc), title="Tunnel Not Started"))
            return
        self._tunnels.add(ActiveTunnel(
            host=host,
            user=user,
            local_port=local_port,
            remote_port=remote_port,
            process=proc,
            started_at=time.monotonic(),
            log_path=log_path,
        ))
        self._refresh_tunnel_panel()

    def _refresh_tunnel_panel(self) -> None:
        self.query_one(TunnelPanel).refresh_tunnels(self._tunnels.tunnels)

    def _cleanup_dead_tunnels(self) -> None:
        """Remove background tunnels whose SSH process has exited and report why."""
        dead = self._tunnels.reap_dead()
        if not dead:
            return
        reports = []
        for t in dead:
            detail = t.read_log_tail()
            reports.append(
                f"{t.label} exited with status {t.process.returncode}"
                + (f":\n{detail}" if detail else ".")
            )
            t.remove_log()
        self._refresh_tunnel_panel()
        self.push_screen(SSHErrorModal("\n\n".join(reports), title="Tunnel Closed"))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _apply_column_highlight(self, item: "HostPairItem") -> None:
        """Highlight only the active column within the pair row."""
        for i, col in enumerate(item.query(".host-col")):
            if i == self._column:
                col.add_class("col-selected")
            else:
                col.remove_class("col-selected")

    @on(ListView.Highlighted, "#list")
    def handle_highlighted(self, event: ListView.Highlighted) -> None:
        # Remove column highlight from any previously highlighted row
        for col in self.query(".host-col"):
            col.remove_class("col-selected")
        if event.item and isinstance(event.item, HostPairItem):
            self._column = 0
            self._selected_host = event.item.hosts[0]
            self._apply_column_highlight(event.item)
            self._update_detail(self._selected_host)

    @on(ListView.Selected, "#list")
    def handle_selected(self, event: ListView.Selected) -> None:
        if event.item and isinstance(event.item, HostPairItem):
            self._choose_and_exit(self._selected_host or event.item.hosts[0])

    def on_key(self, event) -> None:
        """Left/right arrows switch the active column within a two-host row."""
        if event.key not in ("left", "right"):
            return
        lv = self.query_one("#list", ListView)
        item = lv.highlighted_child
        if not isinstance(item, HostPairItem) or len(item.hosts) < 2:
            return
        self._column = 1 if event.key == "right" else 0
        self._selected_host = item.hosts[self._column]
        self._apply_column_highlight(item)
        self._update_detail(self._selected_host)
        event.stop()

    def on_tunnel_panel_kill_requested(self, event: TunnelPanel.KillRequested) -> None:
        """Terminate a background tunnel when the user kills it from the panel."""
        self._tunnels.remove(event.tunnel, terminate=True)
        self._refresh_tunnel_panel()

    @on(Input.Changed, "#search-bar")
    def handle_search(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        if query:
            self.filtered_hosts = [
                h for h in self.all_hosts
                if query in h.nickname.lower() or query in h.hostname.lower()
            ]
        else:
            self.filtered_hosts = list(self.all_hosts)
        self._repopulate_list()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_connect(self) -> None:
        if self._selected_host:
            self._choose_and_exit(self._selected_host)

    def action_new_terminal(self) -> None:
        if self._selected_host:
            self._choose_new_terminal(self._selected_host)

    def action_tunnel(self) -> None:
        if self._selected_host:
            self._choose_tunnel(self._selected_host)

    def action_about(self) -> None:
        self.push_screen(AboutModal())

    def action_quit(self) -> None:
        """Quit, asking first whether to stop any running background tunnels."""
        if isinstance(self.screen, QuitTunnelsModal):
            return   # already asking
        live = self._tunnels.alive()
        if not live:
            self.exit(None)
            return

        def on_choice(choice: Optional[str]) -> None:
            if choice == "stop":
                self._tunnels.terminate_all()
                self.exit(None)
            elif choice == "keep":
                self._tunnels.keep_on_exit = True
                self.exit(None)

        self.push_screen(QuitTunnelsModal(live), on_choice)

    def action_check_users(self) -> None:
        self.run_worker(self._check_all_user_counts(), exclusive=False)

    async def _check_all_user_counts(self) -> None:
        """Count logged-in users on every host, at most check_concurrency at a time.

        Hosts with a fresh cached count or a probe already running are skipped,
        so pressing c again does not add a second wave of connections.
        """
        limit = asyncio.Semaphore(self._check_concurrency)

        async def _limited(host: Host) -> None:
            if self._user_count_is_fresh(host.hostname):
                return
            async with limit:
                await self._check_user_count_one(host)

        await asyncio.gather(*[_limited(h) for h in self.all_hosts])

    def _user_count_is_fresh(self, hostname: str) -> bool:
        """Return True if a cached count exists and has not yet expired."""
        if hostname not in self._user_counts:
            return False
        _, fetched_at = self._user_counts[hostname]
        return (time.monotonic() - fetched_at) < self._cache_lifetime

    async def _check_user_count_one(self, host: Host) -> None:
        if self._user_count_is_fresh(host.hostname):
            return  # cached result still valid — nothing to do
        if host.hostname in self._checks_in_flight:
            return  # a probe for this host is already running
        self._checks_in_flight.add(host.hostname)
        try:
            count = await self._probe_user_count(host)
        finally:
            self._checks_in_flight.discard(host.hostname)
        self._user_counts[host.hostname] = (count, time.monotonic())
        self._show_user_count(host.hostname, count)

    async def _probe_user_count(self, host: Host) -> Optional[int]:
        """Run `ssh ... who` on *host*; return the line count or None on failure."""
        skip_proxy = self._should_skip_proxy(host)
        base = host.ssh_command(host.users[0], skip_proxy=skip_proxy)
        # Insert non-interactive options right after 'ssh'
        cmd = (
            base[:1]
            + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
            + base[1:]
            + ["who"]
        )
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CHECK_USERS_TIMEOUT)
            if proc.returncode != 0:
                return None
            lines = [l for l in stdout.decode().splitlines() if l.strip()]
            return len(lines)
        except Exception:
            return None
        finally:
            # A timed-out probe must not keep holding a jump-host connection.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    def _show_user_count(self, hostname: str, count: Optional[int]) -> None:
        # Update every list item that contains this host
        for item in self.query(HostPairItem):
            item.set_user_count(hostname, count)
        # Refresh the detail panel if this host is selected
        if self._selected_host and self._selected_host.hostname == hostname:
            self._update_detail(self._selected_host)

    def action_renew_kerberos(self) -> None:
        self.run_worker(self._do_renew_kerberos(), exclusive=True)

    async def _do_renew_kerberos(self) -> None:
        panel = self.query_one(KerberosPanel)
        try:
            panel.query_one("#kerberos-content", Static).update("Renewing…")
        except Exception:
            pass
        try:
            proc = await asyncio.create_subprocess_exec(
                "kinit", "-R",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            pass
        panel.refresh_tickets()

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def action_escape_search(self) -> None:
        search = self.query_one("#search-bar", Input)
        if search.has_focus:
            search.value = ""
            self.query_one("#list", ListView).focus()
        else:
            self.action_quit()


# ---------------------------------------------------------------------------
# Config file discovery and selection
# ---------------------------------------------------------------------------

#: Environment variable naming a host file; used when -c is not given.
CONFIG_ENV_VAR = "SSH_SELECTOR_CONFIG"
#: Environment variable naming the install.sh install directory (shared with install.sh).
INSTALL_DIR_ENV_VAR = "MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR"
#: Marker/manifest file that install.sh writes at the top of its install directory.
INSTALL_MARKER = ".mu2edaq-cluster-tools-install"


def user_config_dir() -> Path:
    """Per-user host file directory: $XDG_CONFIG_HOME (or ~/.config)/mu2edaq/cluster-tools."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mu2edaq" / "cluster-tools"


def install_dir() -> Optional[Path]:
    """Return the install.sh install directory, or None if not running from one.

    $MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR wins; otherwise the parent of the running
    venv (sys.prefix) counts only if it carries the install.sh marker file.
    """
    env = os.environ.get(INSTALL_DIR_ENV_VAR)
    if env:
        return Path(env).expanduser()
    candidate = Path(sys.prefix).parent
    if (candidate / INSTALL_MARKER).is_file():
        return candidate
    return None


def config_search_tiers() -> list[list[Path]]:
    """Directories searched for host files, as precedence tiers (highest first)."""
    tiers = [[Path("."), Path("config")], [user_config_dir()]]
    inst = install_dir()
    if inst is not None:
        tiers.append([inst / "config"])
    return tiers


def find_config_files() -> list[Path]:
    """Return the *.yaml host files of the first search tier that has any.

    Tiers, in order: ./ and ./config/ (merged); the per-user config directory;
    the install directory's config/. Files are deduplicated by real path.
    """
    seen: set[Path] = set()
    for tier in config_search_tiers():
        found: list[Path] = []
        for d in tier:
            if d.is_dir():
                for p in sorted(d.glob("*.yaml")):
                    resolved = p.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        found.append(p)
        if found:
            return found
    return []


def explicit_config_path(cli_value: Optional[str]) -> tuple[Optional[Path], str]:
    """Return (path, source) for a host file named by -c or $SSH_SELECTOR_CONFIG.

    -c takes precedence over the environment; (None, "") means neither was given
    and discovery should run.
    """
    if cli_value:
        return Path(cli_value).expanduser(), "-c"
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env).expanduser(), CONFIG_ENV_VAR
    return None, ""


class ConfigItem(ListItem):
    """List item wrapping a config file Path."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        yield Label(str(path))


class ConfigSelectApp(App[Optional[Path]]):
    """Pre-flight TUI for picking a config file when multiple are found."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #config-select-container {
        height: 1fr;
        border: solid $primary;
        margin: 1 2;
        padding: 1 2;
    }

    #config-select-title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }

    ListView {
        height: 1fr;
    }

    ListItem {
        padding: 0 1;
    }

    ListItem.--highlight {
        background: $accent;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="config-select-container"):
            yield Label("Multiple config files found — select one:", id="config-select-title")
            yield ListView(*[ConfigItem(p) for p in self.paths], id="config-list")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#config-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ConfigItem):
            self.exit(event.item.path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Terminal emulators tried on Linux/BSD, in preference order.  Each prefix
# must take the *remaining argv* as the command (not a single shell string):
# gnome-terminal "--", xfce4-terminal "-x" ("-e" takes one string),
# konsole "-e" and xterm "-e" (both consume all following arguments).
_TERMINAL_CANDIDATES = (
    ("gnome-terminal", "--"),
    ("xfce4-terminal", "-x"),
    ("konsole", "-e"),
    ("xterm", "-e"),
)


def _terminal_command(cmd: list[str]) -> Optional[list[str]]:
    """Return the argv that runs *cmd* in a new terminal on Linux/BSD, or None.

    Uses shutil.which (no external ``which`` program) to find the first
    installed emulator in _TERMINAL_CANDIDATES.
    """
    for prefix in _TERMINAL_CANDIDATES:
        if shutil.which(prefix[0]):
            return list(prefix) + list(cmd)
    return None


def _open_in_new_terminal(cmd: list[str], title: str = "") -> None:
    """Launch *cmd* in a new terminal window, returning immediately."""
    if sys.platform == "darwin":
        ssh_str = " ".join(shlex.quote(c) for c in cmd)
        # AppleScript requires double-quoted strings; escape any embedded double quotes.
        ssh_str_escaped = ssh_str.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Terminal" to do script "{ssh_str_escaped}"'
        subprocess.Popen(["osascript", "-e", script])
        return

    if sys.platform == "win32":
        # A new console window running ssh.exe directly; no shell involved.
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return

    # Linux/BSD: first installed terminal emulator.
    term_cmd = _terminal_command(cmd)
    if term_cmd is not None:
        subprocess.Popen(term_cmd)
        return

    print(f"Warning: no supported terminal emulator found. Run manually:\n  {' '.join(cmd)}")


# ssh exits with 255 when ssh itself fails (resolution, connect, auth, proxy
# jump); any other status is the remote command's / login shell's own.
SSH_ERROR_STATUS = 255
# Bytes of ssh's stderr kept for the error dialog.
_STDERR_TAIL_BYTES = 8192


def _run_foreground_ssh(cmd: list[str]) -> tuple[int, str]:
    """Run an interactive *cmd* in the foreground and return (status, error).

    ssh's stderr is forwarded to the terminal as it is produced (warnings,
    banners and host-key messages are seen live), while the last
    _STDERR_TAIL_BYTES are kept.  *error* is that tail when ssh exits with
    SSH_ERROR_STATUS, otherwise "", so a remote shell whose last command
    failed does not raise the "SSH Connection Failed" dialog.  Prompts
    (passwords, host-key confirmation) use /dev/tty and are unaffected.
    """
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    tail = bytearray()
    out = getattr(sys.stderr, "buffer", None)

    def _pump() -> None:
        fd = proc.stderr.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            if out is not None:
                out.write(chunk)
                out.flush()
            else:
                sys.stderr.write(chunk.decode(errors="replace"))
                sys.stderr.flush()
            tail.extend(chunk)
            if len(tail) > _STDERR_TAIL_BYTES:
                del tail[:-_STDERR_TAIL_BYTES]

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    status = proc.wait()
    pump.join()
    proc.stderr.close()
    if status == SSH_ERROR_STATUS:
        return status, tail.decode(errors="replace").strip()
    return status, ""


def _install_tunnel_shutdown(tunnels: TunnelRegistry) -> None:
    """Stop background tunnels on any interpreter exit unless the user kept them."""
    atexit.register(tunnels.shutdown)

    def _stop_then_resignal(signum: int, _frame: object) -> None:
        # atexit does not run on a fatal signal: stop the tunnels here, then
        # die with the default action exactly as before this handler existed.
        tunnels.shutdown()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop_then_resignal)
            except (OSError, ValueError):
                pass


def _report_kept_tunnels(tunnels: TunnelRegistry) -> None:
    """Tell the user which tunnels were left running at quit and how to stop them."""
    if not tunnels.keep_on_exit:
        return
    live = tunnels.alive()
    if not live:
        return
    print("Background tunnels left running:")
    for t in live:
        log = f"  log {t.log_path}" if t.log_path else ""
        print(f"  pid {t.pid}  {t.label}{log}")
    print(f"Stop them with: kill {' '.join(str(t.pid) for t in live)}")


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SSH Host Selector — pick a host from a YAML list and connect.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Config file selection:
  1. -c / --config FILE
  2. $SSH_SELECTOR_CONFIG (a file)
  3. Discovery: the first of these tiers that holds any *.yaml file wins
     a. ./*.yaml and ./config/*.yaml
     b. $XDG_CONFIG_HOME/mu2edaq/cluster-tools/*.yaml
        (default ~/.config/mu2edaq/cluster-tools/)
     c. <install dir>/config/*.yaml (install.sh installs only)
  If exactly one file is found it is loaded automatically.
  If multiple files are found a selection screen is shown.
""",
    )
    parser.add_argument(
        "-c", "--config",
        metavar="FILE",
        help=f"Path to a YAML config file. Overrides ${CONFIG_ENV_VAR}; skips auto-discovery.",
    )
    parser.add_argument(
        "--check-concurrency",
        metavar="N",
        type=int,
        default=None,
        help=(
            "Maximum simultaneous ssh connections for Check Users "
            f"(overrides ${CHECK_CONCURRENCY_ENV} and the check_concurrency "
            f"config key; default {DEFAULT_CHECK_CONCURRENCY})."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args()

    explicit, source = explicit_config_path(args.config)
    if explicit is not None:
        config_path = explicit
        if not config_path.exists():
            sys.exit(f"Error: config file not found ({source}): {config_path}")
    else:
        candidates = find_config_files()
        if not candidates:
            searched = ", ".join(str(d) for tier in config_search_tiers() for d in tier)
            sys.exit(
                f"No YAML config files found in: {searched}\n"
                f"Use -c / --config FILE or set {CONFIG_ENV_VAR} to specify one explicitly."
            )
        elif len(candidates) == 1:
            config_path = candidates[0]
        else:
            chosen = ConfigSelectApp(candidates).run()
            if chosen is None:
                sys.exit(0)
            config_path = chosen

    try:
        config = load_config(config_path)
    except Exception as exc:
        sys.exit(f"Error loading config: {exc}")

    if not config.hosts:
        sys.exit("No hosts defined in config file.")

    try:
        config.check_concurrency = resolve_check_concurrency(
            args.check_concurrency, dict(os.environ), config.check_concurrency
        )
    except ValueError as exc:
        sys.exit(f"Error: {exc}")

    tunnels = TunnelRegistry()
    _install_tunnel_shutdown(tunnels)

    ssh_error = ""
    while True:
        app = SSHSelector(config, error=ssh_error, tunnels=tunnels)
        result: AppResult = app.run()

        if result is None:
            _report_kept_tunnels(tunnels)
            sys.exit(0)

        host = result.host
        user_entry = result.user
        skip_proxy = result.skip_proxy

        if skip_proxy and host.proxy_jump:
            print(f"Note: proxy jump skipped (same subnet as {host.proxy_jump})")

        if result.mode == "new_terminal":
            cmd = host.ssh_command(user_entry, skip_proxy=skip_proxy)
            _open_in_new_terminal(cmd, host.nickname)
            ssh_error = ""
        elif result.mode == "tunnel":
            cmd = host.tunnel_command(
                user_entry, result.local_port, result.remote_port, skip_proxy=skip_proxy
            )
            print(f"Opening tunnel: {' '.join(cmd)}")
            print(
                f"  localhost:{result.local_port}  →  "
                f"{host.nickname}:{result.remote_port}"
            )
            print("  Press Ctrl+C to close the tunnel.")
            try:
                subprocess.run(cmd)
            except KeyboardInterrupt:
                print("\nTunnel closed.")
            ssh_error = ""
        else:
            cmd = host.ssh_command(user_entry, skip_proxy=skip_proxy)
            print(f"Connecting: {' '.join(cmd)}")
            _, ssh_error = _run_foreground_ssh(cmd)
        # Session/tunnel ended — loop back to the selection screen


if __name__ == "__main__":
    main()
