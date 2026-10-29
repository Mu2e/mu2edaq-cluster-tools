#!/usr/bin/env python3
"""SSH Host Selector - A TUI application for managing SSH connections."""

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

CURRENT_USER = os.environ.get("USER", getpass.getuser())


def resolve_user(user: str) -> str:
    """Resolve the special token 'default' to the current OS user."""
    return CURRENT_USER if user == "default" else user


def display_user(user: str) -> str:
    """Human-readable label for a user entry."""
    return f"{CURRENT_USER} (default)" if user == "default" else user


def _format_age(secs: float) -> str:
    """Return a compact human-readable age string (e.g. '4m', '1h 23m')."""
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


@dataclass
class Host:
    hostname: str
    nickname: str = ""
    users: list[str] = field(default_factory=lambda: ["default"])
    port: int = 22
    proxy_jump: Optional[str] = None
    proxy_user: Optional[str] = None
    group: str = ""

    def __post_init__(self) -> None:
        if not self.nickname:
            self.nickname = self.hostname
        if not self.users:
            self.users = ["default"]

    def proxy_target(self, user: str) -> Optional[str]:
        """Return the ProxyJump target (user@host) for the given connecting user.

        Uses proxy_user if configured; otherwise defaults to the same user
        being used for the final destination host.
        """
        if not self.proxy_jump:
            return None
        if "@" in self.proxy_jump:
            return self.proxy_jump
        proxy_user = self.proxy_user if self.proxy_user else user
        return f"{resolve_user(proxy_user)}@{self.proxy_jump}"

    def ssh_command(self, user: str, skip_proxy: bool = False) -> list[str]:
        """Build the SSH command list for this host and the given user entry."""
        cmd = ["ssh"]
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.proxy_jump and not skip_proxy:
            cmd.extend(["-J", self.proxy_target(user)])
        cmd.extend(["-l", resolve_user(user)])
        cmd.append(self.hostname)
        return cmd

    def tunnel_command(
        self, user: str, local_port: int, remote_port: int, skip_proxy: bool = False
    ) -> list[str]:
        """Build an SSH tunnel command: ssh -N -L local:localhost:remote ..."""
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
    """Per-group defaults that individual host entries can override."""
    users: list[str] = field(default_factory=list)
    proxy_jump: Optional[str] = None
    proxy_user: Optional[str] = None


@dataclass
class Config:
    hosts: list[Host]
    grouplist: list[str]          # ordered group names; controls display order
    groups: dict[str, GroupConfig] = field(default_factory=dict)
    cache_lifetime: int = 600     # seconds before a user-count result is re-fetched
    panel_width: int = 50         # fixed column width of the right-hand info panel
    check_concurrency: int = 8    # max simultaneous ssh processes for "Check Users"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _parse_users(raw: object) -> list[str]:
    """Normalise a users value (scalar or list) into a list of strings."""
    if isinstance(raw, list):
        return [str(u) for u in raw] or ["default"]
    return [str(raw)] if raw else ["default"]


def load_config(config_path: Path) -> Config:
    """Parse a YAML config file and return a Config object."""
    with open(config_path) as f:
        data = yaml.safe_load(f)

    grouplist: list[str] = [str(g) for g in data.get("grouplist", [])]

    # Parse optional per-group defaults
    groups: dict[str, GroupConfig] = {}
    for gname, gdata in (data.get("groups") or {}).items():
        gdata = gdata or {}
        raw_users = gdata.get("users", gdata.get("user"))
        groups[str(gname)] = GroupConfig(
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

# Below OpenSSH's default MaxStartups start value (10), so one jump host never
# sees more unauthenticated connections from this tool than sshd accepts
# without starting to drop them.
DEFAULT_CHECK_CONCURRENCY = 8
CHECK_CONCURRENCY_ENV = "SSH_SELECTOR_CHECK_CONCURRENCY"
# Wall-clock limit for one `ssh ... who` probe (ConnectTimeout=5 covers TCP only).
CHECK_USERS_TIMEOUT = 12.0


def _parse_check_concurrency(raw: object, source: str) -> int:
    """Validate a check-concurrency value; raise ValueError naming *source*."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{source}: expected a positive integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{source}: expected a positive integer, got {value}")
    return value


def resolve_check_concurrency(
    cli_value: Optional[int], environ: "dict[str, str]", config_value: int
) -> int:
    """Return the effective limit: command line > environment > YAML config > default."""
    if cli_value is not None:
        return _parse_check_concurrency(cli_value, "--check-concurrency")
    env_value = environ.get(CHECK_CONCURRENCY_ENV, "").strip()
    if env_value:
        return _parse_check_concurrency(env_value, CHECK_CONCURRENCY_ENV)
    return _parse_check_concurrency(config_value, "check_concurrency")


# ---------------------------------------------------------------------------
# Kerberos ticket helpers
# ---------------------------------------------------------------------------

# Matches macOS/Heimdal:  "Mar 31 13:58:47 2026  Apr  1 15:58:47 2026  principal"
# Also matches Linux MIT:  "01/15/2024 10:00:00  01/15/2024 18:00:00  principal"
_KLIST_DATE_RE = re.compile(
    r"([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}"   # macOS month-name
    r"|\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"               # Linux MM/DD/YYYY
    r"\s+"
    r"([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}"
    r"|\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"
    r"\s+(.+)"
)
_DATE_FMTS = ("%b %d %H:%M:%S %Y", "%m/%d/%Y %H:%M:%S")


def _parse_klist_date(s: str) -> Optional[datetime]:
    s = " ".join(s.split())  # collapse extra whitespace (e.g. "Apr  1" → "Apr 1")
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _format_klist(output: str) -> str:
    """Convert raw klist stdout into Rich markup for display."""
    now = datetime.now()
    principal = ""
    ticket_lines: list[str] = []

    for line in output.splitlines():
        stripped = line.strip()
        # "Principal: user@REALM" or "Default principal: user@REALM"
        if "rincipal:" in stripped and not re.match(r"[A-Za-z]{3}\s", stripped):
            principal = stripped.split(":", 1)[1].strip()
            continue
        m = _KLIST_DATE_RE.match(stripped)
        if not m:
            continue
        expires = _parse_klist_date(m.group(2))
        service = m.group(3).strip()
        if expires is None:
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
    """Run klist asynchronously and return Rich-markup formatted output."""
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

    if proc.returncode != 0:
        return "[dim]No active Kerberos tickets.[/dim]"
    return _format_klist(stdout.decode())


class KerberosPanel(Widget):
    """Widget that displays active Kerberos tickets, auto-refreshed every 60 s."""

    def compose(self) -> ComposeResult:
        yield Static("Checking Kerberos tickets…", id="kerberos-content")

    def on_mount(self) -> None:
        self.refresh_tickets()
        self.set_interval(60, self.refresh_tickets)

    def refresh_tickets(self) -> None:
        self.run_worker(self._fetch(), exclusive=True)

    def _refresh(self) -> None:
        self.refresh_tickets()

    async def _fetch(self) -> None:
        text = await _get_kerberos_tickets()
        try:
            self.query_one("#kerberos-content", Static).update(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Network info panel
# ---------------------------------------------------------------------------

def _detect_local_networks_sync() -> list[ipaddress.IPv4Network]:
    """Return local IPv4 networks parsed from interface configuration."""
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
    """Async wrapper around _detect_local_networks_sync."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _detect_local_networks_sync)


async def get_local_network_info() -> str:
    """Return Rich-markup text describing the local network interface."""
    loop = asyncio.get_event_loop()
    try:
        hostname = socket.gethostname()
        fqdn = await loop.run_in_executor(None, socket.getfqdn)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            except Exception:
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
    """Widget showing local network identity, detected once on startup."""

    def compose(self) -> ComposeResult:
        yield Static("Detecting network…", id="network-content")

    def on_mount(self) -> None:
        self.run_worker(self._fetch(), exclusive=True)

    async def _fetch(self) -> None:
        text = await get_local_network_info()
        try:
            self.query_one("#network-content", Static).update(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# User selection modal
# ---------------------------------------------------------------------------

class UserItem(ListItem):
    """List item wrapping a user entry string."""

    def __init__(self, user: str) -> None:
        super().__init__()
        self.user = user

    def compose(self) -> ComposeResult:
        yield Label(display_user(self.user))


class UserSelectModal(ModalScreen[Optional[str]]):
    """Modal overlay for picking a user when a host has multiple users."""

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

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    def __init__(self, host: Host) -> None:
        super().__init__()
        self.host = host

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-container"):
            yield Label(self.host.nickname, id="modal-title")
            yield Label("Select user:", id="modal-subtitle")
            yield ListView(
                *[UserItem(u) for u in self.host.users],
                id="user-list",
            )

    def on_mount(self) -> None:
        self.query_one("#user-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, UserItem):
            self.dismiss(event.item.user)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# SSH error modal
# ---------------------------------------------------------------------------

class AboutModal(ModalScreen):
    """Modal overlay showing application info and author contact details."""

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

    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def compose(self) -> ComposeResult:
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
        self.dismiss()


class SSHErrorModal(ModalScreen):
    """Modal that displays an SSH connection error and dismisses on any key."""

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

    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def __init__(self, message: str, title: str = "SSH Connection Failed") -> None:
        super().__init__()
        self.message = message
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="error-container"):
            yield Label(self.title_text, id="error-title")
            yield Static(markup_escape(self.message), id="error-body")
            yield Label("Press any key to return to the host list", id="error-hint")

    def on_key(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Tunnel port modal
# ---------------------------------------------------------------------------

class TunnelPortModal(ModalScreen[Optional[tuple[int, int, bool]]]):
    """Modal overlay for entering local/remote ports and foreground/background mode."""

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

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    def __init__(self, host: Host) -> None:
        super().__init__()
        self.host = host

    def compose(self) -> ComposeResult:
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
        self.query_one("#local-port", Input).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter" and isinstance(self.focused, Switch):
            event.prevent_default()
            event.stop()
            self._try_submit()

    @on(Input.Submitted, "#local-port")
    def _local_submitted(self) -> None:
        self.query_one("#remote-port", Input).focus()

    @on(Input.Submitted, "#remote-port")
    def _remote_submitted(self) -> None:
        self._try_submit()

    def _try_submit(self) -> None:
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
            error_label.update(str(exc) if str(exc) else "Please enter valid port numbers.")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Quit confirmation when background tunnels are running
# ---------------------------------------------------------------------------

class QuitTunnelsModal(ModalScreen[Optional[str]]):
    """Ask whether to stop or keep running background tunnels on quit.

    Dismisses with "stop", "keep", or None (cancel, stay in the UI).
    """

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

    BINDINGS = [
        Binding("s", "choose('stop')", "Stop tunnels and quit", show=True),
        Binding("y", "choose('stop')", show=False),
        Binding("k", "choose('keep')", "Keep tunnels and quit", show=True),
        Binding("escape", "choose('cancel')", "Cancel", show=True),
        Binding("n", "choose('cancel')", show=False),
    ]

    def __init__(self, tunnels: "list[ActiveTunnel]") -> None:
        super().__init__()
        self.tunnels = tunnels

    def compose(self) -> ComposeResult:
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
        self.dismiss(None if choice == "cancel" else choice)


# ---------------------------------------------------------------------------
# Host list items
# ---------------------------------------------------------------------------

class GroupHeader(ListItem):
    """Non-interactive group heading shown in the host list."""

    def __init__(self, name: str) -> None:
        super().__init__(disabled=True)
        self.group_name = name

    def compose(self) -> ComposeResult:
        yield Label(f"  {self.group_name}")


class HostPairItem(ListItem):
    """A list item holding one or two hosts displayed side by side."""

    def __init__(self, hosts: list[Host]) -> None:
        super().__init__()
        self.hosts = hosts  # 1 or 2 entries

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, host in enumerate(self.hosts):
                with Horizontal(classes=f"host-col col-{i}"):
                    yield Label(" ~", classes=f"host-status host-status-{i}")
                    yield Label(host.nickname, classes="host-name")

    def set_status(self, hostname: str, ip: Optional[str]) -> None:
        """Update the DNS indicator for the given hostname."""
        for i, host in enumerate(self.hosts):
            if host.hostname == hostname:
                dot = "[green] ●[/green]" if ip else "[red] ●[/red]"
                self.query_one(f".host-status-{i}", Label).update(dot)

    def set_user_count(self, hostname: str, count: Optional[int]) -> None:
        """Update the logged-in user count shown next to the nickname."""
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
    """Describes the connection the user wants to make."""
    host: "Host"
    user: str
    skip_proxy: bool
    mode: str = "shell"       # "shell" or "tunnel"
    local_port: int = 0
    remote_port: int = 0


@dataclass
class ActiveTunnel:
    """A background SSH port-forward tunnel managed by the application."""
    host: "Host"
    user: str
    local_port: int
    remote_port: int
    process: "subprocess.Popen[bytes]"
    started_at: float   # time.monotonic()
    log_path: Optional[Path] = None   # file receiving the tunnel's ssh stderr

    @property
    def is_alive(self) -> bool:
        return self.process.poll() is None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def label(self) -> str:
        return f"localhost:{self.local_port} -> {self.host.nickname}:{self.remote_port}"

    def read_log_tail(self, limit: int = 2000) -> str:
        """Return the last *limit* bytes of the ssh stderr log, decoded."""
        if self.log_path is None:
            return ""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - limit))
                return f.read().decode(errors="replace").strip()
        except OSError:
            return ""

    def remove_log(self) -> None:
        if self.log_path is not None:
            try:
                self.log_path.unlink()
            except OSError:
                pass


def _detached_popen_kwargs() -> "dict[str, object]":
    """Popen kwargs that keep terminal signals (Ctrl-C in a foreground session)
    from reaching background tunnels; they are stopped explicitly instead."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def check_local_port(port: int) -> Optional[str]:
    """Return why *port* cannot be forwarded on loopback, or None if it is free.

    Mirrors what ``ssh -L`` binds by default (127.0.0.1 and ::1, with
    SO_REUSEADDR on POSIX). A port held on either address counts as in use,
    because ``localhost:PORT`` could then reach the other listener.
    """
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue   # address family not supported on this host
        try:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((addr, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                continue   # e.g. IPv6 loopback not configured
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
                return f"local port {port} is already in use on {addr}"
            return f"cannot bind local port {port} on {addr}: {exc.strerror or exc}"
        finally:
            sock.close()
    return None


class TunnelRegistry:
    """Background tunnels owned by one ssh-selector process.

    Created once in main() and handed to every SSHSelector instance, so tunnels
    survive the UI being rebuilt after a connection, and can be stopped at exit.
    """

    def __init__(self) -> None:
        self.tunnels: list[ActiveTunnel] = []
        self.keep_on_exit = False   # set when the user chose to leave tunnels running

    def add(self, tunnel: ActiveTunnel) -> None:
        self.tunnels.append(tunnel)

    def alive(self) -> list[ActiveTunnel]:
        return [t for t in self.tunnels if t.is_alive]

    def find_local_port(self, port: int) -> Optional[ActiveTunnel]:
        """Return the live tunnel already forwarding local *port*, if any."""
        for t in self.tunnels:
            if t.local_port == port and t.is_alive:
                return t
        return None

    def remove(self, tunnel: ActiveTunnel, terminate: bool = True) -> None:
        if terminate:
            _stop_processes([tunnel.process])
        self.tunnels = [t for t in self.tunnels if t is not tunnel]
        tunnel.remove_log()

    def reap_dead(self) -> list[ActiveTunnel]:
        """Drop tunnels whose ssh exited and return them (logs still on disk)."""
        dead = [t for t in self.tunnels if not t.is_alive]
        if dead:
            self.tunnels = [t for t in self.tunnels if t.is_alive]
        return dead

    def terminate_all(self) -> None:
        _stop_processes([t.process for t in self.tunnels])
        for t in self.tunnels:
            t.remove_log()
        self.tunnels = []

    def shutdown(self) -> None:
        """atexit hook: stop all tunnels unless the user asked to keep them."""
        if not self.keep_on_exit:
            self.terminate_all()


def _stop_processes(procs: "list[subprocess.Popen[bytes]]", grace: float = 2.0) -> None:
    """SIGTERM every live process, wait up to *grace* seconds, then SIGKILL."""
    live = [p for p in procs if p.poll() is None]
    for p in live:
        try:
            p.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + grace
    for p in live:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


# ---------------------------------------------------------------------------
# Active tunnel list items and panel
# ---------------------------------------------------------------------------

class TunnelItem(ListItem):
    """A list item representing one active background tunnel."""

    def __init__(self, tunnel: ActiveTunnel) -> None:
        super().__init__()
        self.tunnel = tunnel

    def compose(self) -> ComposeResult:
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
    """Widget listing active background tunnels; supports kill via k / Delete."""

    class KillRequested(Message):
        """Posted when the user wants to terminate a tunnel."""
        def __init__(self, tunnel: ActiveTunnel) -> None:
            super().__init__()
            self.tunnel = tunnel

    BINDINGS = [
        Binding("k", "kill_selected", "Kill", show=True),
        Binding("delete", "kill_selected", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield ListView(id="tunnel-list")

    def on_mount(self) -> None:
        self.refresh_tunnels([])

    def refresh_tunnels(self, tunnels: list[ActiveTunnel]) -> None:
        lv = self.query_one("#tunnel-list", ListView)
        lv.clear()
        if not tunnels:
            lv.append(ListItem(Label("[dim]No active tunnels[/dim]")))
        else:
            for t in tunnels:
                lv.append(TunnelItem(t))

    def action_kill_selected(self) -> None:
        lv = self.query_one("#tunnel-list", ListView)
        if isinstance(lv.highlighted_child, TunnelItem):
            self.post_message(self.KillRequested(lv.highlighted_child.tunnel))


# ---------------------------------------------------------------------------
# Main TUI application
# ---------------------------------------------------------------------------

# App result: a ConnectionRequest, or None if the user quit.
AppResult = Optional[ConnectionRequest]


class SSHSelector(App[AppResult]):
    """!
    @brief Interactive Textual TUI that lets an operator pick a DAQ host and
           connect to it (shell, new terminal window, or port-forward tunnel).

    @par State
    - `all_hosts` / `grouplist`: immutable host inventory and group ordering
      taken from the loaded Config.
    - `filtered_hosts`: subset of `all_hosts` matching the search bar; the
      ListView is rebuilt from it on every keystroke.
    - `_selected_host` / `_column`: the host under the cursor.  Rows are
      HostPairItem pairs, so the selection is (row, column) and left/right
      move between the two columns.
    - `_host_ips`: DNS cache, hostname -> IPv4 string, None when the lookup
      failed, key absent while the lookup is still pending.  Filled once per
      app instance at mount.
    - `_local_networks`: IPv4 subnets of the local interfaces, used to decide
      whether the proxy jump can be skipped.
    - `_user_counts`: Check Users cache, hostname -> (count or None,
      time.monotonic() of the probe).  Entries younger than
      `_cache_lifetime` seconds are "fresh" and not re-probed.
    - `_check_concurrency`: upper bound on simultaneous `ssh ... who` probes,
      enforced by an asyncio.Semaphore created per Check Users wave.
    - `_checks_in_flight`: hostnames with a probe currently running, so a
      second press of c cannot start duplicate probes.
    - `_tunnels`: the TunnelRegistry of background `ssh -N -L` processes.
      It is owned by main(), not by this instance.

    @par Lifecycle
    main() runs this app in a loop.  The app never execs ssh itself for an
    interactive session: it calls exit() with an AppResult (a
    ConnectionRequest, or None for quit).  main() then tears down the
    Textual screen, runs the foreground ssh / tunnel / new-terminal launch
    on the real TTY, and when that returns constructs a fresh SSHSelector
    (passing the previous ssh error text and the same TunnelRegistry).
    Hence all per-instance caches (DNS, user counts, local networks) are
    rebuilt on every return to the selector, while background tunnels
    persist across rebuilds.

    @par Thread affinity
    Every method of this class runs on the Textual asyncio event loop.  The
    workers started with run_worker() are async coroutines (not @work
    threads), so they may touch widgets and instance state directly without
    call_from_thread().  Blocking calls are pushed off the loop explicitly:
    gethostbyname via loop.run_in_executor(), and ssh/kinit via
    asyncio.create_subprocess_exec().  Any future thread worker must use
    call_from_thread() for widget updates.

    @note Background tunnels are started detached (new session on POSIX,
          CREATE_NEW_PROCESS_GROUP on Windows) so Ctrl-C in a later
          foreground ssh session does not kill them.
    @see main(), TunnelRegistry, ConnectionRequest, _install_tunnel_shutdown()
    """

    # Layout: search bar on top; host list (flexible width) on the left; a
    # fixed-width right column (width overridden from config.panel_width in
    # on_mount) stacking detail / Kerberos / network / tunnel panels.  The
    # panel heights are percentages of the right column and sum to 100%.
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

    # App-level key bindings shown in the Footer.  Bindings on a focused
    # widget (e.g. TunnelPanel's k/Delete, Input editing keys) take
    # precedence, which is why "q" and "c" are inert while typing in the
    # search bar.
    BINDINGS = [
        # Open an interactive ssh session in this terminal (app exits first).
        Binding("enter", "connect", "Connect", show=True),
        # Launch ssh in a new terminal window; the selector stays usable.
        Binding("n", "new_terminal", "New Terminal"),
        # Port-forward: foreground tunnel, or background tunnel in the registry.
        Binding("t", "tunnel", "Tunnel"),
        # Probe logged-in user counts on all hosts (bounded concurrency).
        Binding("c", "check_users", "Check Users"),
        # Shift-c alias so a caps-lock/shifted press still works; hidden.
        Binding("C", "check_users", show=False),
        # Move focus to the filter Input.
        Binding("/", "focus_search", "Search"),
        # kinit -R: renew the existing TGT without a password prompt.
        Binding("r", "renew_kerberos", "Renew Kerberos"),
        # Capital A to avoid accidental activation while navigating.
        Binding("A", "about", "About"),
        # Quit; asks first if background tunnels are alive.
        Binding("q", "quit", "Quit"),
        # Leave the search bar (clearing it), or quit from the list.
        Binding("escape", "escape_search", "Back to list", show=False),
    ]

    def __init__(
        self, config: Config, error: str = "", tunnels: Optional[TunnelRegistry] = None
    ) -> None:
        """!
        @brief Build a selector instance for one pass of main()'s UI loop.

        @param config   Loaded host Config (hosts, grouplist, cache_lifetime,
                        panel_width, check_concurrency already resolved
                        against CLI/env overrides by main()).
        @param error    stderr tail of the previous foreground ssh that exited
                        with status 255; shown in an SSHErrorModal at mount.
                        Empty string for no error.
        @param tunnels  Registry shared across rebuilds.  None creates a
                        private registry (used by tests).
        """
        super().__init__()
        self.all_hosts = config.hosts
        self.grouplist = config.grouplist
        self.filtered_hosts: list[Host] = list(config.hosts)
        self._selected_host: Optional[Host] = config.hosts[0] if config.hosts else None
        self._startup_error = error
        self._column: int = 0   # 0 = left, 1 = right (two-column list)
        # Three-state DNS cache: the "absent" state drives the "resolving…"
        # text in the detail panel, distinct from a failed lookup.
        # hostname → resolved IP (None = lookup failed, key absent = not yet checked)
        self._host_ips: dict[str, Optional[str]] = {}
        self._local_networks: list[ipaddress.IPv4Network] = []
        self._cache_lifetime: int = config.cache_lifetime
        self._panel_width: int = config.panel_width
        # hostname → (count, monotonic timestamp); key absent = never fetched
        # count None = probe failed (unreachable, auth refused, timeout).
        # time.monotonic() so wall-clock jumps (NTP, DST) cannot make an
        # entry look fresh forever or expire instantly.
        self._user_counts: dict[str, tuple[Optional[int], float]] = {}
        # Bounded below sshd's MaxStartups (default 10:30:100) because all
        # probes to hosts behind the same proxy_jump arrive at that gateway's
        # sshd as concurrent unauthenticated connections; above the start
        # value sshd begins randomly dropping them.  See
        # DEFAULT_CHECK_CONCURRENCY.
        self._check_concurrency: int = config.check_concurrency
        self._checks_in_flight: set[str] = set()
        # Shared with main() so tunnels outlive this UI instance: the app is
        # torn down and rebuilt around every foreground ssh session, but a
        # background port-forward must keep running through that.
        self._tunnels: TunnelRegistry = tunnels if tunnels is not None else TunnelRegistry()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """!
        @brief Build the widget tree (Textual compose hook, called once at start).

        The host list is populated from `all_hosts`; status dots and user
        counts are filled in later by the mount-time workers.

        @return Generator of widgets forming the screen layout.
        """
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
        """!
        @brief Textual Mount handler: apply config, start background lookups,
               and arm the tunnel watchdog.

        Runs once per app instance, i.e. after every return from a
        foreground ssh session.
        """
        # CSS cannot read config; panel_width is applied programmatically.
        self.query_one("#right-panel").styles.width = self._panel_width
        self._update_detail(self._selected_host)
        self.query_one("#list").focus()
        if self._startup_error:
            # Deferred until after the first refresh so the modal stacks on
            # top of a fully laid-out screen rather than racing compose.
            self.call_after_refresh(self.push_screen, SSHErrorModal(self._startup_error))
        # Both are async workers on the event loop; exclusive=False so they
        # run side by side and are not cancelled by each other.
        self.run_worker(self._resolve_all_hosts(), exclusive=False)
        self.run_worker(self._load_local_networks(), exclusive=False)
        # Tunnels started by a previous instance are already in the shared
        # registry; show them and report any that died while the UI was
        # down (e.g. during a foreground ssh session).
        self.call_after_refresh(self._refresh_tunnel_panel)
        self.call_after_refresh(self._cleanup_dead_tunnels)
        # 5 s polling: cheap Popen.poll() per tunnel, and fast enough that
        # an operator learns a forward dropped before relying on it.
        self.set_interval(5, self._cleanup_dead_tunnels)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pair_hosts(group_hosts: list[Host]) -> list[HostPairItem]:
        """!
        @brief Convert a flat host list into two-column HostPairItem rows.

        @param group_hosts  Hosts of one group, in display order.
        @return Rows of two hosts each; the last row holds one host when the
                count is odd.
        """
        return [
            HostPairItem(group_hosts[i: i + 2])
            for i in range(0, len(group_hosts), 2)
        ]

    def _build_list_items(self, hosts: list[Host]) -> list[ListItem]:
        """!
        @brief Build the host ListView contents, grouped with GroupHeader rows.

        Order: groups named in `grouplist` (in that order, empty ones
        omitted), then groups present on hosts but not listed (first-seen
        order), then hosts with no group under "Other".  Without a
        grouplist the hosts are paired flat with no headers.

        @param hosts  Hosts to display (usually `filtered_hosts`).
        @return ListItems ready to append to the ListView.
        """
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
        """!
        @brief Rebuild the host ListView from `filtered_hosts` and reset the
               selection to the first match.
        """
        lv = self.query_one("#list", ListView)
        lv.clear()
        for item in self._build_list_items(self.filtered_hosts):
            lv.append(item)
        # Re-apply any already-cached DNS and user-count status to freshly created items
        # (new HostPairItems start with the "~" placeholder; without this
        # every keystroke in the search bar would blank the status columns).
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
        """!
        @brief Resolve every host's DNS concurrently and update status dots.

        Async worker started from on_mount().  The dot next to each host
        means: "~" lookup pending, green "●" name resolves, red "●" name
        does not resolve from this machine (typo, private DAQ zone not
        visible from here, or VPN down).  A green dot says nothing about
        whether sshd is reachable.
        """
        loop = asyncio.get_event_loop()

        async def _one(host: Host) -> None:
            """!
            @brief Resolve one host and push the result into the UI.
            @param host  Host whose hostname is looked up.
            """
            try:
                # gethostbyname blocks (resolver timeouts can be seconds);
                # run it in the default executor so the UI stays live.
                ip: Optional[str] = await loop.run_in_executor(
                    None, socket.gethostbyname, host.hostname
                )
            except Exception:
                # Any resolver error (gaierror, herror, UnicodeError) is
                # simply "not resolvable" for display purposes.
                ip = None
            self._host_ips[host.hostname] = ip
            # The row holding this host is not tracked, and the list may
            # have been rebuilt by a search, so update every HostPairItem.
            for item in self.query(HostPairItem):
                item.set_status(host.hostname, ip)
            if self._selected_host and self._selected_host.hostname == host.hostname:
                # The proxy-skip decision depends on the IP, so the detail
                # panel must be redrawn once it is known.
                self._update_detail(self._selected_host)

        # Unbounded fan-out is fine here: DNS queries go to the local
        # resolver, not to the DAQ hosts, and the executor caps threads.
        await asyncio.gather(*(_one(h) for h in self.all_hosts))

    async def _load_local_networks(self) -> None:
        """!
        @brief Detect local IPv4 subnets, cache them, and redraw the detail panel.

        Async worker started from on_mount().
        @note Uses `ip addr` on Linux and `ifconfig` on macOS (see
              _detect_local_networks_sync()); on Windows neither exists, the
              list stays empty, and the proxy jump is never skipped.
        """
        self._local_networks = await detect_local_networks()
        if self._selected_host:
            self._update_detail(self._selected_host)

    def _should_skip_proxy(self, host: Host) -> bool:
        """!
        @brief Decide whether the proxy jump can be bypassed for *host*.

        True when the host's resolved IPv4 address lies inside one of this
        machine's directly attached subnets, i.e. the operator is already
        inside the DAQ enclave and the gateway hop would only add latency
        and a second authentication.

        @param host  Candidate destination.
        @return True to connect directly, False to honour proxy_jump.
        """
        # Fail safe: with no proxy configured, or subnets / IP not known
        # yet, keep the configured route.  Skipping the gateway wrongly
        # would fail to connect from outside the enclave.
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
        """!
        @brief Redraw the detail panel for *host*.

        Shows hostname, resolved IP, port, effective proxy jump, cached
        logged-in user count (with age and stale marker), configured users,
        and the ssh command line for the default user.

        @param host  Host to describe, or None when the filter matches nothing.
        """
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
        # Stale counts are still displayed (with their age) rather than
        # hidden: an old number is better than none, and the operator can
        # decide whether to press c again.
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
                # Yellow = someone is logged in; a hint to coordinate
                # before restarting DAQ processes on that node.
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
        """!
        @brief Exit the app with a shell ConnectionRequest for *host*.

        Shows UserSelectModal only when the host lists more than one user.
        main() runs the ssh session in the foreground after exit.

        @param host  Destination host.
        """
        # Decided now, while the DNS/subnet caches are populated; main()
        # has no access to them after the app exits.
        skip_proxy = self._should_skip_proxy(host)
        if len(host.users) == 1:
            self.exit(ConnectionRequest(host=host, user=host.users[0], skip_proxy=skip_proxy))
        else:
            def on_user_chosen(user: Optional[str]) -> None:
                """!
                @brief UserSelectModal callback.
                @param user  Chosen user entry, or None if the modal was cancelled.
                """
                if user is not None:
                    self.exit(ConnectionRequest(host=host, user=user, skip_proxy=skip_proxy))

            self.push_screen(UserSelectModal(host), on_user_chosen)

    def _choose_new_terminal(self, host: Host) -> None:
        """!
        @brief Exit the app with a new_terminal ConnectionRequest for *host*.

        main() spawns the ssh command in a new terminal window and
        immediately rebuilds the selector.

        @param host  Destination host.
        @note The terminal emulator used is platform specific; see
              _open_in_new_terminal().
        """
        skip_proxy = self._should_skip_proxy(host)
        if len(host.users) == 1:
            self.exit(ConnectionRequest(
                host=host, user=host.users[0], skip_proxy=skip_proxy, mode="new_terminal"
            ))
        else:
            def on_user_chosen(user: Optional[str]) -> None:
                """!
                @brief UserSelectModal callback.
                @param user  Chosen user entry, or None if the modal was cancelled.
                """
                if user is not None:
                    self.exit(ConnectionRequest(
                        host=host, user=user, skip_proxy=skip_proxy, mode="new_terminal"
                    ))

            self.push_screen(UserSelectModal(host), on_user_chosen)

    def _choose_tunnel(self, host: Host) -> None:
        """!
        @brief Run the tunnel flow: pick a user if needed, then ask for ports.

        A foreground tunnel exits the app with mode="tunnel" (main() runs
        `ssh -N -L` until Ctrl-C).  A background tunnel is started in-process
        and registered, and the selector stays open.

        @param host  Destination host for the port forward.
        @see TunnelPortModal, _start_background_tunnel()
        """
        skip_proxy = self._should_skip_proxy(host)

        def show_port_modal(user: str) -> None:
            """!
            @brief Push TunnelPortModal for *user*.
            @param user  Login user for the tunnel's ssh.
            """
            def on_ports_chosen(result: Optional[tuple[int, int, bool]]) -> None:
                """!
                @brief TunnelPortModal callback; validates and starts the tunnel.
                @param result  (local_port, remote_port, background), or None
                               if the modal was cancelled.
                """
                if result is None:
                    return
                local_port, remote_port, background = result
                # Pre-check the local bind in the UI.  ssh would also fail
                # (tunnel_command sets ExitOnForwardFailure=yes), but only
                # after the TUI has been torn down (foreground) or as a
                # "Tunnel Closed" popup seconds later (background).
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
                """!
                @brief UserSelectModal callback; continues to the port modal.
                @param user  Chosen user entry, or None if the modal was cancelled.
                """
                if user is not None:
                    show_port_modal(user)

            self.push_screen(UserSelectModal(host), on_user_chosen)

    def _local_port_problem(self, local_port: int) -> Optional[str]:
        """!
        @brief Explain why *local_port* cannot be used for a new tunnel.

        @param local_port  Loopback port the operator wants to forward.
        @return Operator-facing message, or None if the port is usable.
        """
        # Our own tunnels are checked first so the message can name the
        # offending tunnel and point at the k key, rather than a generic
        # "in use" from the bind probe.
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
        """!
        @brief Launch `ssh -N -L` as a detached background process and register it.

        Failures to start (ssh missing, temp dir unwritable) are shown in an
        SSHErrorModal rather than raised.

        @param host         Destination host.
        @param user         Login user entry for ssh.
        @param local_port   Loopback port to listen on.
        @param remote_port  Port on *host* to forward to.
        @param skip_proxy   True to bypass the host's proxy_jump.
        @warning The tunnel's ssh runs with stdin closed, so it cannot prompt.
                 It needs a valid Kerberos ticket or agent key; otherwise it
                 exits at once and is reported by _cleanup_dead_tunnels().
        """
        cmd = host.tunnel_command(user, local_port, remote_port, skip_proxy=skip_proxy)
        log_path: Optional[Path] = None
        try:
            # stderr goes to a per-tunnel temp file so that, when the tunnel
            # dies later, the "Tunnel Closed" dialog can show ssh's reason
            # (auth failure, remote port refused, network drop).
            fd, name = tempfile.mkstemp(prefix=f"ssh-selector-tunnel-{local_port}-", suffix=".log")
            log_path = Path(name)
            # The parent's copy of the fd is closed on leaving the with
            # block; the child keeps its own inherited copy.
            with os.fdopen(fd, "wb") as log:
                proc = subprocess.Popen(
                    cmd,
                    # DEVNULL stdin: Textual owns the TTY; a prompting ssh
                    # would corrupt the screen and steal keystrokes.
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    # Own session / process group: Ctrl-C in a later
                    # foreground ssh must not reach background tunnels.
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
        """!
        @brief Redraw the Tunnels panel from the shared registry.
        """
        self.query_one(TunnelPanel).refresh_tunnels(self._tunnels.tunnels)

    def _cleanup_dead_tunnels(self) -> None:
        """!
        @brief Reap background tunnels whose ssh has exited and report why.

        Called after mount and every 5 s by the interval timer.  Each dead
        tunnel's exit status and stderr tail are collected into one
        "Tunnel Closed" modal, then its log file is deleted.
        """
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
        """!
        @brief Highlight only the active column within a pair row.

        @param item  Row whose column `_column` should carry .col-selected.
        """
        for i, col in enumerate(item.query(".host-col")):
            if i == self._column:
                col.add_class("col-selected")
            else:
                col.remove_class("col-selected")

    @on(ListView.Highlighted, "#list")
    def handle_highlighted(self, event: ListView.Highlighted) -> None:
        """!
        @brief Track the cursor row in the host list (up/down keys, mouse).

        Resets the column to the left host of the new row.

        @param event  Highlighted event; item is None when the list is
                      empty, and GroupHeader rows are ignored.
        """
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
        """!
        @brief Connect when a host row is activated (Enter on the list, or click).

        @param event  Selected event for the activated row.
        """
        # Prefer _selected_host so the right-hand column is honoured; the
        # row's first host is only a fallback.
        if event.item and isinstance(event.item, HostPairItem):
            self._choose_and_exit(self._selected_host or event.item.hosts[0])

    def on_key(self, event) -> None:
        """!
        @brief Left/right arrow keys switch the active column within a pair row.

        @param event  Textual Key event; stopped when consumed so the
                      ListView / Input do not also act on it.
        """
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
        """!
        @brief Terminate a background tunnel (k or Delete in the Tunnels panel).

        @param event  Message carrying the ActiveTunnel to stop.
        @note Stopping waits up to the _stop_processes() grace period
              (SIGTERM then SIGKILL) on the event loop; the UI may pause
              briefly for a stubborn ssh.
        """
        self._tunnels.remove(event.tunnel, terminate=True)
        self._refresh_tunnel_panel()

    @on(Input.Changed, "#search-bar")
    def handle_search(self, event: Input.Changed) -> None:
        """!
        @brief Filter the host list on every edit of the search bar.

        Case-insensitive substring match on nickname or hostname.

        @param event  Input.Changed event from #search-bar.
        """
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
        """!
        @brief Enter: open an interactive ssh session to the selected host.
        """
        if self._selected_host:
            self._choose_and_exit(self._selected_host)

    def action_new_terminal(self) -> None:
        """!
        @brief n: open the selected host in a new terminal window.
        """
        if self._selected_host:
            self._choose_new_terminal(self._selected_host)

    def action_tunnel(self) -> None:
        """!
        @brief t: start the port-forward flow for the selected host.
        """
        if self._selected_host:
            self._choose_tunnel(self._selected_host)

    def action_about(self) -> None:
        """!
        @brief A: show the About modal.
        """
        self.push_screen(AboutModal())

    def action_quit(self) -> None:
        """!
        @brief q (or Esc from the list): quit, asking first about live
               background tunnels.

        QuitTunnelsModal offers stop (terminate all, then exit), keep (set
        keep_on_exit so neither the atexit hook nor the SIGHUP/SIGTERM
        handler kills them; main() prints their PIDs), or cancel.

        @warning Choosing "keep" leaves orphaned ssh processes holding local
                 ports after ssh-selector exits; they must be killed by PID.
        @see _install_tunnel_shutdown(), _report_kept_tunnels()
        """
        # Pressing q again while the question is showing must not stack a
        # second modal.
        if isinstance(self.screen, QuitTunnelsModal):
            return   # already asking
        live = self._tunnels.alive()
        if not live:
            self.exit(None)
            return

        def on_choice(choice: Optional[str]) -> None:
            """!
            @brief QuitTunnelsModal callback.
            @param choice  "stop", "keep", or None for cancel (stay in the app).
            """
            if choice == "stop":
                self._tunnels.terminate_all()
                self.exit(None)
            elif choice == "keep":
                self._tunnels.keep_on_exit = True
                self.exit(None)

        self.push_screen(QuitTunnelsModal(live), on_choice)

    def action_check_users(self) -> None:
        """!
        @brief c / C: probe logged-in user counts on all hosts.

        exclusive=False so a repeated press does not cancel probes already
        in progress; duplicates are filtered by freshness and
        `_checks_in_flight` instead.
        """
        self.run_worker(self._check_all_user_counts(), exclusive=False)

    async def _check_all_user_counts(self) -> None:
        """!
        @brief Count logged-in users on every host, at most check_concurrency at a time.

        Hosts with a fresh cached count or a probe already running are skipped,
        so pressing c again does not add a second wave of connections.

        @warning Raising check_concurrency above the gateway's sshd
                 MaxStartups start value (OpenSSH default 10) makes sshd
                 drop connections at random; probes then report
                 "unreachable" for hosts that are fine.
        """
        # A new semaphore per wave: two overlapping waves can each run up to
        # the limit, but _checks_in_flight prevents them probing the same
        # host, and freshness skips most of the second wave.
        limit = asyncio.Semaphore(self._check_concurrency)

        async def _limited(host: Host) -> None:
            """!
            @brief Probe one host while holding a semaphore slot.
            @param host  Host to probe.
            """
            # Checked before acquiring so cached hosts do not occupy a slot.
            if self._user_count_is_fresh(host.hostname):
                return
            async with limit:
                await self._check_user_count_one(host)

        await asyncio.gather(*[_limited(h) for h in self.all_hosts])

    def _user_count_is_fresh(self, hostname: str) -> bool:
        """!
        @brief Test whether a cached user count is younger than cache_lifetime.

        Failed probes (count None) are cached too, so an unreachable host is
        not hammered on every press within the lifetime.

        @param hostname  Host key into `_user_counts`.
        @return True if a cached count exists and has not yet expired.
        """
        if hostname not in self._user_counts:
            return False
        _, fetched_at = self._user_counts[hostname]
        return (time.monotonic() - fetched_at) < self._cache_lifetime

    async def _check_user_count_one(self, host: Host) -> None:
        """!
        @brief Probe one host unless cached or already in flight, then cache
               and display the result.

        @param host  Host to probe.
        """
        # Re-checked after the semaphore wait: the count may have been
        # filled by an overlapping wave while this task was queued.
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
        """!
        @brief Run `ssh ... who` on *host* and count its non-blank output lines.

        Uses the host's first (default) user and the same proxy decision as
        an interactive connect.

        @param host  Host to probe.
        @return Number of login sessions reported by who, or None if ssh
                failed, authentication was refused, or the probe exceeded
                CHECK_USERS_TIMEOUT.
        @note Needs non-interactive auth (a valid Kerberos ticket for
              GSSAPI, or an agent key).  With an expired ticket every host
              shows "unreachable"; renew with r (kinit -R) or a fresh kinit.
        """
        skip_proxy = self._should_skip_proxy(host)
        base = host.ssh_command(host.users[0], skip_proxy=skip_proxy)
        # Insert non-interactive options right after 'ssh'
        # BatchMode=yes: never prompt (no TTY is available to a worker; a
        # password prompt would hang until the timeout).  ConnectTimeout
        # bounds only the TCP connect; the wait_for below bounds the whole
        # probe including authentication and the proxy hop.
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
            # who prints one line per login session (tty/pts), so this is a
            # session count, not a distinct-user count.
            lines = [l for l in stdout.decode().splitlines() if l.strip()]
            return len(lines)
        except Exception:
            # Covers asyncio.TimeoutError, missing ssh binary, and decode
            # errors: all mean "count unknown" to the operator.
            return None
        finally:
            # A timed-out probe must not keep holding a jump-host connection.
            # (It would also keep counting against the gateway's MaxStartups
            # after its semaphore slot is released.)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    def _show_user_count(self, hostname: str, count: Optional[int]) -> None:
        """!
        @brief Push a new user count into the list rows and detail panel.

        @param hostname  Host whose count changed.
        @param count     Session count, or None if the probe failed.
        """
        # Update every list item that contains this host
        for item in self.query(HostPairItem):
            item.set_user_count(hostname, count)
        # Refresh the detail panel if this host is selected
        if self._selected_host and self._selected_host.hostname == hostname:
            self._update_detail(self._selected_host)

    def action_renew_kerberos(self) -> None:
        """!
        @brief r: renew the Kerberos TGT with `kinit -R`.

        exclusive=True cancels a renewal already running, so repeated
        presses cannot pile up kinit workers.
        """
        self.run_worker(self._do_renew_kerberos(), exclusive=True)

    async def _do_renew_kerberos(self) -> None:
        """!
        @brief Run `kinit -R` and refresh the Kerberos panel afterwards.

        Errors are deliberately swallowed: the panel refresh (klist) shows
        the true ticket state, which is what the operator acts on.

        @note kinit -R only extends a ticket that is still valid and
              renewable (Fermilab tickets are normally issued renewable).
              An expired ticket needs a full `kinit` in a shell; this TUI
              has no TTY for a password prompt.
        @note macOS ships Heimdal kinit; on Windows, MIT Kerberos for
              Windows provides kinit.  If none is on PATH this is a no-op.
        """
        panel = self.query_one(KerberosPanel)
        try:
            panel.query_one("#kerberos-content", Static).update("Renewing…")
        except Exception:
            # Panel content not mounted; the refresh below still runs.
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
        """!
        @brief /: move keyboard focus to the search bar.
        """
        self.query_one("#search-bar", Input).focus()

    def action_escape_search(self) -> None:
        """!
        @brief Esc: leave the search bar (clearing the filter), or quit when
               the list already has focus.

        Clearing the value fires Input.Changed, which restores the full list.
        """
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
