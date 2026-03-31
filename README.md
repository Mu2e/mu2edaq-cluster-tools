# mu2edaq-cluster-tools

![SSH Selector screenshot](screenshot.png)

A terminal-based SSH host selector for managing connections to a fleet of remote hosts. Built with [Textual](https://github.com/Textualize/textual) for a keyboard-driven TUI experience.

## Features

- **Host list with groups** — hosts are organized into named groups with configurable display order
- **Two-column layout** — hosts are displayed side by side for compact browsing; use left/right arrows to select within a row
- **Per-group and per-host configuration** — set default users and proxy jump hosts at the group level, with per-host overrides
- **Multiple users per host** — a selection submenu appears when a host has more than one configured user
- **DNS resolution on startup** — each host is resolved in the background; a green/red dot indicates reachability
- **Automatic proxy skip** — if the destination host is on the same local subnet as the client, the configured proxy jump is automatically bypassed
- **SSH tunnel mode** — open a port-forward tunnel (`ssh -N -L`) instead of an interactive shell; prompts for local and remote port numbers
- **Kerberos ticket panel** — displays active tickets with time-to-expiry color coding; `r` renews the principal via `kinit -R`
- **Network info panel** — shows local hostname, IP, domain, and detected subnets
- **SSH error modal** — if a connection fails, the stderr output is displayed before returning to the host list
- **Host filtering** — press `/` to search/filter the host list in real time
- **Multiple config files** — auto-discovers `*.yaml` files in `./` and `./config/`; shows a picker when more than one is found

## Requirements

- Python 3.10+
- [Textual](https://github.com/Textualize/textual) >= 0.47.0
- [PyYAML](https://pyyaml.org/) >= 6.0

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Auto-discover config files in ./ and ./config/
python ssh_selector.py

# Specify a config file explicitly
python ssh_selector.py -c path/to/hosts.yaml
```

### Key bindings

| Key | Action |
|-----|--------|
| `Enter` | Connect (interactive shell) |
| `t` | Open SSH tunnel (prompts for ports) |
| `/` | Filter host list |
| `r` | Renew Kerberos ticket (`kinit -R`) |
| `q` | Quit |
| `←` / `→` | Switch selected host within a two-column row |
| `Escape` | Clear search / dismiss modal |

## Configuration

Config files are YAML. See [`config/hosts.yaml.example`](config/hosts.yaml.example) for a fully annotated example.

### Minimal example

```yaml
hosts:
  - hostname: myserver.example.com
    nickname: My Server
```

### Full structure

```yaml
# Ordered list of group names — controls display order in the TUI
grouplist:
  - Access
  - Backend

# Per-group defaults inherited by every host in the group
groups:
  Access:
    users:
      - default        # resolves to the current OS user at runtime
      - admin
  Backend:
    proxy_jump: gateway.example.com
    users:
      - default
      - deploy

hosts:
  - nickname: Jump Box
    hostname: gateway.example.com
    group: Access

  - nickname: DB Primary
    hostname: db1.internal.example.com
    group: Backend

  - nickname: Web Server
    hostname: web.example.com
    group: Backend
    port: 2222
    proxy_jump: null   # override group proxy — connect directly
    users:
      - deploy
      - default
```

### Host fields

| Field | Required | Description |
|-------|----------|-------------|
| `hostname` | yes | Address passed to `ssh` |
| `nickname` | no | Display name in the TUI (defaults to `hostname`) |
| `group` | no | Must match a `grouplist` entry |
| `port` | no | SSH port (default `22`) |
| `users` | no | List of users; first is the default. Overrides group default. |
| `proxy_jump` | no | ProxyJump host. Set to `null` to disable the group default. |

The special user value `"default"` resolves to the current OS user (`$USER`) at runtime.

### Group fields

| Field | Description |
|-------|-------------|
| `users` | Default user list for all hosts in the group |
| `proxy_jump` | Default ProxyJump for all hosts in the group |

## SSH tunnel mode

Press `t` on any host to open a port-forward tunnel instead of a shell. You will be prompted for:

- **Local port** — port on your machine to listen on
- **Remote port** — port on the destination host to forward to

The resulting command is `ssh -N -L <local>:localhost:<remote> [options] <host>`. Press `Ctrl+C` in the terminal to close the tunnel and return to the host selector.

## Kerberos support

The right panel shows active Kerberos tickets from `klist` with color-coded expiry:

- **Green** — more than 2 hours remaining
- **Yellow** — less than 2 hours remaining
- **Red** — expired

Press `r` to renew the ticket principal via `kinit -R`. Tickets refresh automatically every 60 seconds.
