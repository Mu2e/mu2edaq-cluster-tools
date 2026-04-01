#!/usr/bin/env python3
"""SSH Host Selector - A TUI application for managing SSH connections."""

import asyncio
import ipaddress
import os
import re
import socket
import struct
import sys
import subprocess
import argparse
import getpass
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
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static
from textual import on


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
    group: str = ""

    def __post_init__(self) -> None:
        if not self.nickname:
            self.nickname = self.hostname
        if not self.users:
            self.users = ["default"]

    def ssh_command(self, user: str, skip_proxy: bool = False) -> list[str]:
        """Build the SSH command list for this host and the given user entry."""
        cmd = ["ssh"]
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.proxy_jump and not skip_proxy:
            cmd.extend(["-J", self.proxy_jump])
        cmd.extend(["-l", resolve_user(user)])
        cmd.append(self.hostname)
        return cmd

    def tunnel_command(
        self, user: str, local_port: int, remote_port: int, skip_proxy: bool = False
    ) -> list[str]:
        """Build an SSH tunnel command: ssh -N -L local:localhost:remote ..."""
        cmd = ["ssh", "-N", "-L", f"{local_port}:localhost:{remote_port}"]
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.proxy_jump and not skip_proxy:
            cmd.extend(["-J", self.proxy_jump])
        cmd.extend(["-l", resolve_user(user)])
        cmd.append(self.hostname)
        return cmd


@dataclass
class GroupConfig:
    """Per-group defaults that individual host entries can override."""
    users: list[str] = field(default_factory=list)
    proxy_jump: Optional[str] = None


@dataclass
class Config:
    hosts: list[Host]
    grouplist: list[str]          # ordered group names; controls display order
    groups: dict[str, GroupConfig] = field(default_factory=dict)
    cache_lifetime: int = 600     # seconds before a user-count result is re-fetched
    panel_width: int = 50         # fixed column width of the right-hand info panel


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

        hosts.append(Host(
            hostname=entry["hostname"],
            nickname=entry.get("nickname", entry["hostname"]),
            users=users,
            port=int(entry.get("port", 22)),
            proxy_jump=proxy_jump,
            group=group_name,
        ))

    cache_lifetime = int(data.get("cache_lifetime", 600))
    panel_width = int(data.get("panel_width", 50))
    return Config(
        hosts=hosts,
        grouplist=grouplist,
        groups=groups,
        cache_lifetime=cache_lifetime,
        panel_width=panel_width,
    )


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

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="error-container"):
            yield Label("SSH Connection Failed", id="error-title")
            yield Static(markup_escape(self.message), id="error-body")
            yield Label("Press any key to return to the host list", id="error-hint")

    def on_key(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Tunnel port modal
# ---------------------------------------------------------------------------

class TunnelPortModal(ModalScreen[Optional[tuple[int, int]]]):
    """Modal overlay for entering local and remote port numbers for an SSH tunnel."""

    CSS = """
    TunnelPortModal {
        align: center middle;
    }

    #tunnel-container {
        width: 54;
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
            yield Label(
                "Tab / Enter to move between fields  ·  Enter on second field to confirm",
                id="tunnel-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#local-port", Input).focus()

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
            self.dismiss((local_port, remote_port))
        except ValueError as exc:
            error_label.update(str(exc) if str(exc) else "Please enter valid port numbers.")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


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
# Main TUI application
# ---------------------------------------------------------------------------

@dataclass
class ConnectionRequest:
    """Describes the connection the user wants to make."""
    host: Host
    user: str
    skip_proxy: bool
    mode: str = "shell"       # "shell" or "tunnel"
    local_port: int = 0
    remote_port: int = 0


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
        height: 55%;
        border: solid $accent;
        padding: 1 2;
    }

    #detail-content {
        height: 1fr;
    }

    #kerberos-container {
        height: 28%;
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
        height: 17%;
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
        Binding("t", "tunnel", "Tunnel"),
        Binding("c", "check_users", "Check Users"),
        Binding("C", "check_users", show=False),
        Binding("/", "focus_search", "Search"),
        Binding("r", "renew_kerberos", "Renew Kerberos"),
        Binding("A", "about", "About"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape_search", "Back to list", show=False),
    ]

    def __init__(self, config: Config, error: str = "") -> None:
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
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#right-panel").styles.width = self._panel_width
        self._update_detail(self._selected_host)
        self.query_one("#list").focus()
        if self._startup_error:
            self.call_after_refresh(self.push_screen, SSHErrorModal(self._startup_error))
        self.run_worker(self._resolve_all_hosts(), exclusive=False)
        self.run_worker(self._load_local_networks(), exclusive=False)

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
            proxy_str = f"[dim]{host.proxy_jump} (skipped — same subnet)[/dim]"
        else:
            proxy_str = host.proxy_jump
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

    def _choose_tunnel(self, host: Host) -> None:
        """Start tunnel flow: pick user (if needed), then prompt for port numbers."""
        skip_proxy = self._should_skip_proxy(host)

        def show_port_modal(user: str) -> None:
            def on_ports_chosen(ports: Optional[tuple[int, int]]) -> None:
                if ports is not None:
                    local_port, remote_port = ports
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

    def action_tunnel(self) -> None:
        if self._selected_host:
            self._choose_tunnel(self._selected_host)

    def action_about(self) -> None:
        self.push_screen(AboutModal())

    def action_check_users(self) -> None:
        self.run_worker(self._check_all_user_counts(), exclusive=False)

    async def _check_all_user_counts(self) -> None:
        """SSH into every host concurrently and count logged-in users via `who`."""
        await asyncio.gather(*[self._check_user_count_one(h) for h in self.all_hosts])

    def _user_count_is_fresh(self, hostname: str) -> bool:
        """Return True if a cached count exists and has not yet expired."""
        if hostname not in self._user_counts:
            return False
        _, fetched_at = self._user_counts[hostname]
        return (time.monotonic() - fetched_at) < self._cache_lifetime

    async def _check_user_count_one(self, host: Host) -> None:
        if self._user_count_is_fresh(host.hostname):
            return  # cached result still valid — nothing to do
        skip_proxy = self._should_skip_proxy(host)
        base = host.ssh_command(host.users[0], skip_proxy=skip_proxy)
        # Insert non-interactive options right after 'ssh'
        cmd = (
            base[:1]
            + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
            + base[1:]
            + ["who"]
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
            lines = [l for l in stdout.decode().splitlines() if l.strip()]
            count: Optional[int] = len(lines)
        except Exception:
            count = None
        self._user_counts[host.hostname] = (count, time.monotonic())
        # Update every list item that contains this host
        for item in self.query(HostPairItem):
            item.set_user_count(host.hostname, count)
        # Refresh the detail panel if this host is selected
        if self._selected_host and self._selected_host.hostname == host.hostname:
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

def find_config_files() -> list[Path]:
    """Return *.yaml files found in ./ then ./config/, deduplicated by real path."""
    found: list[Path] = []
    seen: set[Path] = set()
    for d in (Path("."), Path("config")):
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(p)
    return found


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

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SSH Host Selector — pick a host from a YAML list and connect.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Config file search order (when -c is not given):
  1. *.yaml files in the current directory
  2. *.yaml files in ./config/
  If exactly one file is found it is loaded automatically.
  If multiple files are found a selection screen is shown.
""",
    )
    parser.add_argument(
        "-c", "--config",
        metavar="FILE",
        help="Path to a YAML config file. Skips auto-discovery when given.",
    )
    args = parser.parse_args()

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            sys.exit(f"Error: config file not found: {config_path}")
    else:
        candidates = find_config_files()
        if not candidates:
            sys.exit(
                "No YAML config files found in ./ or ./config/.\n"
                "Use -c / --config FILE to specify one explicitly."
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

    ssh_error = ""
    while True:
        app = SSHSelector(config, error=ssh_error)
        result: AppResult = app.run()

        if result is None:
            sys.exit(0)

        host = result.host
        user_entry = result.user
        skip_proxy = result.skip_proxy

        if skip_proxy and host.proxy_jump:
            print(f"Note: proxy jump skipped (same subnet as {host.proxy_jump})")

        if result.mode == "tunnel":
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
            proc = subprocess.run(cmd, stderr=subprocess.PIPE)
            if proc.returncode != 0 and proc.stderr:
                ssh_error = proc.stderr.decode(errors="replace").strip()
            else:
                ssh_error = ""
        # Session/tunnel ended — loop back to the selection screen


if __name__ == "__main__":
    main()
