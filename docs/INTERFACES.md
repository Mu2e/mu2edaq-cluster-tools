# mu2edaq-cluster-tools — interfaces

Reviewed 2026-10-06 against `905d344` on branch `mu2e-sept-mega-review`.
Line references are to `src/mu2edaq_cluster_tools/` unless a path is given.
"Verified" means run in this review (macOS, Python 3.12.1 and 3.9.6).

## 1. Command lines

### 1.1 `ssh-selector`

```
ssh-selector [-c FILE]
ssh-selector --version | -h
```

`ssh_selector.py:1623-1673`. `-c FILE` must exist; otherwise every `*.yaml` in
`./` then `./config/` is a candidate (`find_config_files`, `:1508-1519`), one
loads directly, several show a picker. Exit status: 0 on quit (host list or
picker); 1 via `sys.exit(message)` for no candidates, a missing `-c` file, a
load error, or no hosts; 2 for argparse errors.

Key bindings (`:1031-1042`): Enter connect, `n` new terminal, `t` tunnel, `c`/`C`
check users, `/` filter, `r` `kinit -R`, `A` about, `q` quit, Escape (clear the
focused filter, else quit). Tunnels panel: `k`/Delete terminate. User and tunnel
dialogs: Escape cancels.

### 1.2 `lan-scan`

```
lan-scan [-n CIDR] [-c FILE] [-t SECONDS] [-w COUNT] [-p PORTS] [--no-dns] [--json]
lan-scan --version | -h
```

`lan_scan.py:232-326`. Output: a table, or with `--json`
`[{"ip": "...", "hostname": "..."|null}, ...]`, on stdout; progress and notes
on stderr. Exit status: 0 after any completed scan; 1 if no network was given
and none could be detected; 2 for an invalid CIDR or argparse error. A missing
config file, non-numeric environment or file values, a non-numeric port, or
`workers < 1` end in a traceback (status 1).

| Setting | CLI | Environment | YAML key | Default |
|---|---|---|---|---|
| network | `-n/--network` | `LAN_SCAN_NETWORK` | `network` | auto-detect |
| timeout | `-t/--timeout` | `LAN_SCAN_TIMEOUT` | `timeout` | 1.0 |
| workers | `-w/--workers` | `LAN_SCAN_WORKERS` | `workers` | 64 |
| ports | `-p/--ports` | `LAN_SCAN_PORTS` | `ports` (list or CSV) | none |
| no_dns | `--no-dns` (can only set) | — | `no_dns` | false |
| config file | `-c/--config` | — | — | none |

Precedence command line > environment > file > default (`:275-294`).

### 1.3 Installers

| Script | Options | Man page |
|---|---|---|
| `bootstrap.sh` | `--dev` (editable), `--version`, `-h` | `man/man1/bootstrap.sh.1` |
| `install.sh` | first argument only: `--uninstall`, `--version`; anything else installs | `man/man1/install.sh.1` |

`install.sh` reads `INSTALL_DIR` (default `~/.local/share/mu2edaq-cluster-tools`)
and `APP_CMD` (default `ssh-selector`) from the environment
(`install.sh:15-16`); `bootstrap.sh` reads `PYTHON`.

## 2. Topology file schema (ssh-selector)

Full reference: `settings.md`; example: `config/hosts.yaml.example`.

```yaml
cache_lifetime: 600          # s a "Check Users" result stays fresh
panel_width: 50              # right-hand panel width, columns
grouplist: [Gateways, ...]   # display order
groups:
  <name>:
    users: [default, mu2edaq]   # or user: <scalar>
    proxy_jump: host            # or user@host
    proxy_user: name            # jump login; default = destination user
hosts:
  - hostname: mu2e-trk-01.fnal.gov   # required
    nickname: Tracker 01
    group: Tracker
    port: 22
    users: [mu2etrk]                 # present key (even null) overrides the group
    proxy_jump: null                 # null = no jump for this host
    proxy_user: null
```

Resolution (`ssh_selector.py:144-209`): host key present → host value; else
group value; else default (`users: [default]`, no jump). `default` is `$USER`
(`:39-44`). A group used by hosts but missing from `grouplist` is shown after
the listed groups; one missing from `groups` gets no defaults.

## 3. SSH command construction

`ssh_selector.py:78-114`:

| Mode | argv |
|---|---|
| shell (Enter, `n`) | `ssh [-p PORT] [-J J] -l USER HOST` |
| tunnel (`t`) | `ssh -N -L LOCAL:localhost:REMOTE [-p PORT] [-J J] -l USER HOST` |
| check users (`c`) | `ssh -o BatchMode=yes -o ConnectTimeout=5 [-p PORT] [-J J] -l USER0 HOST who` |

`J` is `proxy_jump` if it contains `@`, else `<proxy_user or USER>@<proxy_jump>`.
The jump is dropped when the host's resolved IPv4 address lies inside a local
subnet (`:1183-1194`). `USER0` is the host's first user. The tunnel's remote end
is `localhost` on the destination host. No `ExitOnForwardFailure`, no
`ControlMaster`, no `-t`.

Process handling (`:1675-1716`, `:1316-1334`): shell sessions run in the
foreground with stderr forwarded live (tail shown in a dialog on ssh exit 255); foreground tunnels run until Ctrl-C; background
tunnels are `Popen` children with stderr piped and never read; "check users"
runs every host's `ssh` concurrently with a 12 s limit each (`:1430-1470`).

**Fan-out of `c` on the shipped topologies** (all at once, through the listed
jump hosts):

| File | Connections | Through one jump host |
|---|---|---|
| `mu2e_daq.yaml` | 75 | 62 via `mu2egateway01` |
| `nova_daq.yaml` | 222 | 207 via `novadaq-far-gateway-01` |
| `mu2e_offline.yaml` | 22 | 7 via `mu2edaq-gateway` |

## 4. `lan-scan` probe behaviour

What it sends, per address of `ipaddress.IPv4Network(...).hosts()` (all
addresses for /31 and /32; network and broadcast excluded otherwise):

1. One ICMP echo by running the system `ping` (`:114-130`):
   `ping -c 1 -W <timeout×1000>` on POSIX, `ping -n 1 -w <ms>` on Windows; the
   subprocess is killed after `timeout + 1` s. Linux iputils reads `-W` in
   seconds, so there the effective wait for a silent address is `timeout + 1` s;
   macOS reads milliseconds (CLU-007).
2. Only if the ping failed and ports are configured: a full TCP connect to each
   port in order, `timeout` s each, closed immediately; stops at the first
   success (`:133-141`). No data is written.
3. Only for responders, unless `--no-dns`: one `gethostbyaddr`. The
   `socket.setdefaulttimeout(timeout)` around it (`:144-154`) does not bound the
   resolver call and is process-global across threads.

Rate and concurrency: a `ThreadPoolExecutor(max_workers=workers)` (`:180-183`),
default 64, so up to 64 `ping` processes at a time; no delay, no rate limit, no
cap on network size, no confirmation. The address list is built in memory first
(`:165`). With defaults on Linux a /24 of mostly silent addresses takes about
8 s; with the shipped profiles' three fallback ports about 20 s; a /16 about
35 minutes.

Scope: the network from `-n`, `LAN_SCAN_NETWORK` or the file; otherwise the
first non-loopback IPv4 network reported by `ip addr show` (else `ifconfig`,
else `ipconfig`), with the others named on stderr (`:303-316`). On a
multi-homed node or one with container bridges the first network need not be
the DAQ subnet, and its size is not checked. Host bits in `-n` are accepted
(`strict=False`).

Shipped profiles: `lan_scan-mu2edaq.yaml` → `131.225.245.0/24`, a routable
Fermilab subnet (it contains `mu2egateway01.fnal.gov`, 131.225.245.6, resolved
in this review); `lan_scan-mu2edaq-data.yaml` → `10.226.9.0/24`;
`lan_scan-mu2edaq-ipmi.yaml` → `192.168.246.0/24`. All three add TCP 22, 80 and
443 (CLU-003).

## 5. Client-side commands invoked

| Command | When | Source |
|---|---|---|
| `ssh` | Enter, `n`, `t`, `c` | `ssh_selector.py:91-114`, `:1441-1470` |
| `klist` | start-up and every 60 s | `:279-295`, `:306` |
| `kinit -R` | `r` | `:1475-1490` |
| `ip addr show`, `ifconfig` | start-up (both tools); `ipconfig` in `lan-scan` | `ssh_selector.py:326-367`, `lan_scan.py:37-107` |
| `osascript`, `gnome-terminal`, `xfce4-terminal`, `konsole`, `xterm` (found via `shutil.which`) | `n` | `ssh_selector._open_in_new_terminal` |
| `ping` | `lan-scan` | `lan_scan.py:114-130` |

## 6. Review facts verified

- `__version__`: `ssh_selector.py:4` = `"1.0.1"`, `lan_scan.py:4` = `"1.0.0"`.
  `pyproject.toml:51` takes the version from `ssh_selector`; `bootstrap.sh:16-19`
  and `install.sh:30-33` read `ssh_selector.py`. `ssh-selector --version` and
  the package metadata say 1.0.1; `lan-scan --version` says 1.0.0. The package
  `__init__.py:3` re-exports `ssh_selector.__version__` (1.0.1).
- Committed `config/mu2e_daq.yaml`: Tracker 14 (`mu2e-trk-01..14`), Calorimeter
  8 (`mu2e-calo-01..08`); `Farm MC1` `proxy_jump: mu2egateway01.fnal.gov`
  (line 61). The working tree's uncommitted change (another session) sets
  `g2gateway01.fnal.gov`; not staged by this review.
- 152 tests pass on Python 3.12.1 and on 3.9.6 (fresh venv, Textual 8.2.8).
  No test reads a repository config file or a man page path.
- CI (`.github/workflows/ci.yml`) runs `pytest` on ubuntu-latest and
  macos-latest with Python 3.10, 3.11 and 3.12 for pushes to main/master and
  for pull requests. It does not run Python 3.9, Windows, a man-page lint or
  the entry points.
