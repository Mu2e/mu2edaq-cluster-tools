# Configuration File Reference

Config files are YAML. The application searches for `*.yaml` files in `./` and then `./config/` at startup. If exactly one file is found it is loaded automatically; if multiple files are found a selection screen is displayed. You can bypass discovery entirely with `ssh-selector -c path/to/file.yaml`.

---

## Top-level keys

### `hosts`
**Required.** A list of host entries. See [Host fields](#host-fields) below.

```yaml
hosts:
  - hostname: node01.example.com
    nickname: Node 01
```

---

### `grouplist`
An ordered list of group names. Controls the display order of groups in the host list. Groups not mentioned here are appended at the end under their own name; hosts with no group are collected under **Other**.

| | |
|---|---|
| Type | list of strings |
| Default | `[]` (no groups — all hosts shown in a flat list) |

```yaml
grouplist:
  - Gateways
  - DAQ Control
  - Monitoring
```

---

### `groups`
A mapping of group name → group-level defaults. Every host that belongs to the group inherits these values unless it explicitly overrides them. See [Group fields](#group-fields) below.

```yaml
groups:
  DAQ Control:
    proxy_jump: gateway.example.com
    users:
      - default
      - mu2edaq
```

---

### `cache_lifetime`
How long (in seconds) a logged-in user count result is reused before the application re-fetches it from the host when `c` is pressed.

| | |
|---|---|
| Type | integer |
| Default | `600` |
| Unit | seconds |

```yaml
cache_lifetime: 120   # re-check after 2 minutes on a busy cluster
```

---

### `panel_width`
Fixed column width of the right-hand panel (detail, Kerberos tickets, and network info). The host list on the left expands elastically to fill the remaining terminal width.

| | |
|---|---|
| Type | integer |
| Default | `50` |
| Unit | terminal columns |

```yaml
panel_width: 70   # wider if principal names or hostnames are long
```

---

## Group fields

Defined under `groups: <GroupName>:`.

### `users`
Default list of users offered for every host in this group. The first entry is the one used when connecting without a selection (and pre-filled in the detail panel). Hosts can override this list — see [Host fields → users](#users-1).

| | |
|---|---|
| Type | list of strings |
| Default | `[default]` if omitted |

The special value `"default"` resolves to the current OS user (`$USER`) at runtime.

```yaml
groups:
  Tracker:
    users:
      - default     # current OS user
      - mu2etrk
      - mu2eshift
```

---

### `proxy_jump`
Default SSH ProxyJump host for every host in this group. Passed to SSH as `-J <value>`. Hosts can override or disable this — see [Host fields → proxy_jump](#proxy_jump-1).

The application automatically skips the proxy when the destination IP falls within the same local subnet as the client machine.

| | |
|---|---|
| Type | string |
| Default | none |

```yaml
groups:
  Backend:
    proxy_jump: mu2egateway01.fnal.gov
```

---

## Host fields

Each entry in the `hosts` list supports the following fields.

### `hostname`
**Required.** The address passed to `ssh`. Must be resolvable (DNS or `/etc/hosts`). Reachability is checked on startup and shown as a green/red dot in the list.

| | |
|---|---|
| Type | string |

```yaml
- hostname: mu2edaq01.fnal.gov
```

---

### `nickname`
Display name shown in the host list. Defaults to `hostname` if omitted.

| | |
|---|---|
| Type | string |
| Default | value of `hostname` |

```yaml
- hostname: mu2edaq01.fnal.gov
  nickname: DAQ Head 01
```

---

### `group`
Assigns the host to a named group. Must match one of the entries in `grouplist` (or any name in `groups`) for it to inherit group defaults. Hosts without a group are displayed under **Other**.

| | |
|---|---|
| Type | string |
| Default | none |

```yaml
- hostname: mu2etrk01.fnal.gov
  group: Tracker
```

---

### `port`
SSH port number. When not 22 the `-p` flag is added to the SSH command.

| | |
|---|---|
| Type | integer |
| Default | `22` |

```yaml
- hostname: special.example.com
  port: 2222
```

---

### `users`
List of users available for this host. Overrides the group-level `users` list entirely when present. The **first entry** is the default — used when there is only one user and no selection submenu appears. Subsequent entries are offered in the order listed.

| | |
|---|---|
| Type | list of strings |
| Default | inherits from group, or `[default]` |

The special value `"default"` resolves to the current OS user (`$USER`) at runtime.

```yaml
- hostname: special.example.com
  users:
    - mu2eshift      # first = default
    - default
    - mu2edaq
```

> **Scalar form:** `user: alice` is accepted as shorthand for `users: [alice]` (single user, no submenu). Prefer the list form for new configs.

---

### `proxy_jump`
SSH ProxyJump host for this specific entry. Overrides the group-level `proxy_jump`.

Set to `null` to **disable** a group-level proxy for this host (useful when a node has been moved to a directly-reachable subnet).

| | |
|---|---|
| Type | string or `null` |
| Default | inherits from group, or none |

```yaml
# Override: use a different proxy
- hostname: mu2edcs99.fnal.gov
  proxy_jump: mu2edcsgateway.fnal.gov

# Override: disable the group proxy entirely
- hostname: mu2edcs01.fnal.gov
  proxy_jump: null
```

The proxy is also skipped automatically (regardless of configuration) when the destination IP is on the same local subnet as the client.

---

## Override resolution order

For `users` and `proxy_jump` the precedence is, highest first:

1. **Host entry** — any value set directly on the host (including `null` for `proxy_jump`)
2. **Group defaults** — values set under `groups: <name>:`
3. **Built-in default** — `users: [default]`, no proxy

The host entry wins as soon as the key is **present**, even if its value is `null`. This is what allows `proxy_jump: null` to disable a group-level proxy.

---

## Complete annotated example

```yaml
# How long to cache logged-in user counts (seconds)
cache_lifetime: 300

# Width of the right-hand info panel in terminal columns
panel_width: 55

# Display order of groups in the host list
grouplist:
  - Gateways
  - DAQ Control
  - DCS

# Group-level defaults
groups:
  Gateways:
    users:
      - default
      - admin

  DAQ Control:
    proxy_jump: mu2egateway01.fnal.gov
    users:
      - default
      - mu2edaq
      - mu2eshift

  DCS:
    proxy_jump: mu2egateway01.fnal.gov
    users:
      - default
      - mu2edcs
      - mu2eshift

hosts:

  # ── Gateways ─────────────────────────────────────────────────────────────
  - nickname: Gateway 01
    hostname: mu2egateway01.fnal.gov
    group: Gateways

  # ── DAQ Control ──────────────────────────────────────────────────────────
  - nickname: DAQ Head
    hostname: mu2edaqhead01.fnal.gov
    group: DAQ Control

  # Moved to the lab — directly reachable, disable the group proxy
  - nickname: DAQ Workstation (Lab 3)
    hostname: mu2edaqws99.fnal.gov
    group: DAQ Control
    proxy_jump: null

  # ── DCS ──────────────────────────────────────────────────────────────────
  - nickname: DCS Server
    hostname: mu2edcs01.fnal.gov
    group: DCS

  # Non-standard port; different default user for this node
  - nickname: DCS Console
    hostname: mu2edcs02.fnal.gov
    group: DCS
    port: 2222
    users:
      - mu2edcs        # specific default for this box
      - default
      - mu2eshift
```
