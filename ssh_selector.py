#!/usr/bin/env python3
"""SSH Host Selector - A TUI application for managing SSH connections."""

import asyncio
import os
import re
import sys
import subprocess
import argparse
import getpass
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

    def ssh_command(self, user: str) -> list[str]:
        """Build the SSH command list for this host and the given user entry."""
        cmd = ["ssh"]
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.proxy_jump:
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

    return Config(hosts=hosts, grouplist=grouplist, groups=groups)


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
# Host list items
# ---------------------------------------------------------------------------

class GroupHeader(ListItem):
    """Non-interactive group heading shown in the host list."""

    def __init__(self, name: str) -> None:
        super().__init__(disabled=True)
        self.group_name = name

    def compose(self) -> ComposeResult:
        yield Label(f"  {self.group_name}")


class HostItem(ListItem):
    """A list item wrapping a Host."""

    def __init__(self, host: Host) -> None:
        super().__init__()
        self.host = host

    def compose(self) -> ComposeResult:
        yield Label(self.host.nickname)


# ---------------------------------------------------------------------------
# Main TUI application
# ---------------------------------------------------------------------------

# App result: (host, user-entry-string) or None if the user quit.
AppResult = Optional[tuple[Host, str]]


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
        width: 35%;
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
        width: 65%;
    }

    #detail-container {
        height: 60%;
        border: solid $accent;
        padding: 1 2;
    }

    #detail-content {
        height: 1fr;
    }

    #kerberos-container {
        height: 40%;
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
        Binding("enter", "connect", "Connect"),
        Binding("/", "focus_search", "Search"),
        Binding("r", "renew_kerberos", "Renew Kerberos"),
        Binding("escape", "escape_search", "Back to list", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, error: str = "") -> None:
        super().__init__()
        self.all_hosts = config.hosts
        self.grouplist = config.grouplist
        self.filtered_hosts: list[Host] = list(config.hosts)
        self._selected_host: Optional[Host] = config.hosts[0] if config.hosts else None
        self._startup_error = error

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
        yield Footer()

    def on_mount(self) -> None:
        self._update_detail(self._selected_host)
        self.query_one("#list").focus()
        if self._startup_error:
            self.call_after_refresh(self.push_screen, SSHErrorModal(self._startup_error))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_list_items(self, hosts: list[Host]) -> list[ListItem]:
        """Return ListItems organized by group, with GroupHeader separators."""
        if not self.grouplist:
            return [HostItem(h) for h in hosts]

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
                items.extend(HostItem(h) for h in group_hosts)
        for group_name, group_hosts in extra.items():
            items.append(GroupHeader(group_name))
            items.extend(HostItem(h) for h in group_hosts)
        if ungrouped:
            items.append(GroupHeader("Other"))
            items.extend(HostItem(h) for h in ungrouped)

        return items

    def _repopulate_list(self) -> None:
        lv = self.query_one("#list", ListView)
        lv.clear()
        for item in self._build_list_items(self.filtered_hosts):
            lv.append(item)
        self._selected_host = self.filtered_hosts[0] if self.filtered_hosts else None
        self._update_detail(self._selected_host)

    def _update_detail(self, host: Optional[Host]) -> None:
        panel = self.query_one("#detail-content", Static)
        if host is None:
            panel.update("[dim]No host selected.[/dim]")
            return

        port_str = f"{host.port}" if host.port != 22 else "22 (default)"
        proxy_str = host.proxy_jump if host.proxy_jump else "[dim]none[/dim]"

        # Show users list; mark first as default
        user_lines = []
        for i, u in enumerate(host.users):
            label = display_user(u)
            if i == 0:
                label += " [dim](first / default)[/dim]"
            user_lines.append(f"  {label}")
        users_str = "\n".join(user_lines)

        # Preview command using the default (first) user
        cmd_str = " ".join(host.ssh_command(host.users[0]))

        panel.update(
            f"[bold white]{host.nickname}[/bold white]\n\n"
            f"[dim]Hostname   :[/dim]  {host.hostname}\n"
            f"[dim]Port       :[/dim]  {port_str}\n"
            f"[dim]Proxy jump :[/dim]  {proxy_str}\n\n"
            f"[dim]Users:[/dim]\n{users_str}\n\n"
            f"[dim]Command (default user):[/dim]\n"
            f"  [italic]{cmd_str}[/italic]\n\n"
            f"[dim]Press [bold]Enter[/bold] to connect, [bold]q[/bold] to quit.[/dim]"
        )

    def _choose_and_exit(self, host: Host) -> None:
        """Exit with host+user, showing the user modal only when needed."""
        if len(host.users) == 1:
            self.exit((host, host.users[0]))
        else:
            def on_user_chosen(user: Optional[str]) -> None:
                if user is not None:
                    self.exit((host, user))
                # user is None → modal was cancelled; stay on host list

            self.push_screen(UserSelectModal(host), on_user_chosen)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @on(ListView.Highlighted, "#list")
    def handle_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and isinstance(event.item, HostItem):
            self._selected_host = event.item.host
            self._update_detail(self._selected_host)

    @on(ListView.Selected, "#list")
    def handle_selected(self, event: ListView.Selected) -> None:
        if event.item and isinstance(event.item, HostItem):
            self._choose_and_exit(event.item.host)

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

        host, user_entry = result
        cmd = host.ssh_command(user_entry)
        print(f"Connecting: {' '.join(cmd)}")
        proc = subprocess.run(cmd, stderr=subprocess.PIPE)
        if proc.returncode != 0 and proc.stderr:
            ssh_error = proc.stderr.decode(errors="replace").strip()
        else:
            ssh_error = ""
        # SSH session ended — loop back to the selection screen


if __name__ == "__main__":
    main()
