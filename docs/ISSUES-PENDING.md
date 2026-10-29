# Staged issues: mu2edaq-cluster-tools

Findings from the September 2026 DAQ mega-review (`mu2e-sept-mega-review`),
reviewed 2026-10-06 against `905d344`.

**Filed 2026-10-29.** All ten entries were filed as GitHub issues on
`Mu2e/mu2edaq-cluster-tools` (#6 through #15, in CLU order, labels
`mega-review` plus category). #6 (CLU-001) was closed the same day by
`79ae6d0`, merged in PR #5. Pre-existing #4 was closed as fixed by `905d344`.

**Why staged.** Per CONTRACT §9.1 no GitHub write was attempted at review time. Each block
below is the §6 issue body, parsed by `docs/mega-review/tools/stage_issues.py`.
Route: `Mu2e/mu2edaq-cluster-tools` (public, issues enabled).

**Dedup.** `gh issue list -R Mu2e/mu2edaq-cluster-tools --state all` returned 2
issues on 2026-10-06, neither with a mega-review fingerprint:

- #4 (open) "src/ layout move broke the entire test suite". Fixed by `905d344`
  (`pytest.ini: pythonpath = src`); 152 tests pass in this review. Not refiled;
  it can be closed.
- #3 (closed) "Jumphost user is not passed correctly". Fixed by `cba4221`
  (`proxy_target()` defaults to the destination user). No overlap.

**Fingerprints** are `sha1("Mu2e/mu2edaq-cluster-tools:<path>:<rule_id>")[:8]`,
no line number. **Labels:** `mega-review` plus the category shown.

**Not filed here.** The MC-2 node-count disagreement across packages (this
repository: Tracker 14, Calorimeter 8) is a Wave 2 cross-package item
(`docs/mega-review/WAVE2-NOTES.md`).

10 entries: 5 × S3, 5 × S4 (CLU-010 is a rollup).

| ID | Issue | Sev | Category | rule_id | Fingerprint | Title |
|---|---|---|---|---|---|---|
| CLU-001 | #6 (closed) | S3 | bug | `mc1-proxy-jump-gateway` | `2c0b170b` | [topology] Farm MC1 jumps through mu2egateway01, but the MC1 enclave's ingress is g2gateway01 |
| CLU-002 | #7 | S3 | bug | `check-users-unbounded-fanout` | `d9acc61d` | [ssh-selector] Check Users opens one SSH connection per configured host at once, all through the same jump host |
| CLU-003 | #8 | S3 | enhancement | `scan-scope-unbounded` | `2df83ccb` | [lan-scan] no bound on scan size or rate, auto-detect scans whichever network is listed first, and a shipped profile targets a routable site subnet |
| CLU-004 | #9 | S3 | bug | `orphaned-background-tunnels` | `f644c26b` | [ssh-selector] background tunnels leave the panel after any connection and survive quit; a reused local port silently keeps the old forward |
| CLU-005 | #10 | S3 | bug | `uninstall-rmrf-generic-env` | `b7bba2a1` | [install] install.sh takes its target from the generic INSTALL_DIR variable and --uninstall deletes it recursively |
| CLU-006 | #11 | S4 | bug | `version-mismatch` | `e39dde4b` | [version] lan-scan --version reports 1.0.0 while the package is 1.0.1 |
| CLU-007 | #12 | S4 | bug | `probe-timeout-units` | `d1d3bf59` | [lan-scan] probe timeouts are not what --timeout says: Linux ping -W gets milliseconds, and the DNS timeout is global and ineffective |
| CLU-008 | #13 | S4 | enhancement | `config-discovery-cwd-only` | `09709a54` | [config] ssh-selector looks for host files only under the current directory, and install.sh installs none |
| CLU-009 | #14 | S4 | enhancement | `ci-matrix-missing-floor` | `0936c4c9` | [ci] CI matrix omits Python 3.9, the declared floor and the AlmaLinux 9 system Python |
| CLU-010 | #15 | S4 | documentation | `minor-rollup` | `9a081a38` | [hygiene] minor findings from the 2026-10 review (rollup) |

---

## CLU-001

**Title:** `[topology] Farm MC1 jumps through mu2egateway01, but the MC1 enclave's ingress is g2gateway01`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `config/mu2e_daq.yaml:61`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
The committed topology routes the `Farm MC1` group (`mu2e-mc1-worker-01..18`)
through `mu2egateway01.fnal.gov`. The 2026-10-02 access survey of the MC1
trigger cluster found its only working ingress to be `g2gateway01.fnal.gov`.
A local, uncommitted edit in the reviewer's working tree already makes that
change; it was made by another session and has not been committed.

## Evidence
```
$ git show HEAD:config/mu2e_daq.yaml | sed -n '60,61p'
  Farm MC1:
    proxy_jump: mu2egateway01.fnal.gov
$ git diff -- config/mu2e_daq.yaml      # working tree, not committed
-    proxy_jump: mu2egateway01.fnal.gov
+    proxy_jump: g2gateway01.fnal.gov
```
`mu2edaq-trigger-cluster/docs/FINDINGS.md:16-36` (2026-10-02): gateways
`g2gateway01` (131.225.246.25) and `g2gateway02`; workers reached "through a
working g2gateway01 jump". `mu2edaq-trigger-cluster/config/mc1-cluster.yaml:18`
lists `g2gateway01.fnal.gov`.

## Why it matters
Selecting an MC1 worker in `ssh-selector` builds
`ssh -J <user>@mu2egateway01.fnal.gov ... mu2e-mc1-worker-NN`, which does not
follow the documented ingress, so the connection fails or depends on a path
nobody has verified. "Check Users" reports every MC1 worker as unreachable.
Filed at S3: the failure is visible and `ssh -J mu2edaq@g2gateway01` works by
hand. The same file is vendored in
`mu2edaq-config/config-*/mu2edaq-cluster-tools/mu2e_daq.yaml` (three tags),
all with `mu2egateway01`.

## Suggested fix
Commit the one-line change to `g2gateway01.fnal.gov` (and set `proxy_user`
to `mu2edaq` or `mu2eshift`, since the survey found `root` refused at the
gateway). Confirm whether `MC1 Control Room` (`mu2e-mc1-cr-01..04`, also via
`mu2egateway01`) sits behind the same gateways. Update the `mu2edaq-config`
copies.

## Related
- `mu2edaq-trigger-cluster/docs/FINDINGS.md`

<!-- mega-review-fingerprint: 2c0b170b -->

---

## CLU-002

**Title:** `[ssh-selector] Check Users opens one SSH connection per configured host at once, all through the same jump host`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_cluster_tools/ssh_selector.py:1432`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Pressing `c` starts `ssh -o BatchMode=yes -o ConnectTimeout=5 ... who` for
every host in the loaded file simultaneously, with no concurrency limit and no
connection sharing. Each one opens its own SSH connection to the jump host
before reaching the target.

## Evidence
```
$ sed -n '1430,1432p' src/mu2edaq_cluster_tools/ssh_selector.py
    async def _check_all_user_counts(self) -> None:
        """SSH into every host concurrently and count logged-in users via `who`."""
        await asyncio.gather(*[self._check_user_count_one(h) for h in self.all_hosts])
```
Fan-out on the shipped files (counted with `load_config()`):

| File | ssh processes | through one jump host |
|---|---|---|
| `config/mu2e_daq.yaml` | 75 | 62 via `mu2egateway01.fnal.gov` |
| `config/nova_daq.yaml` | 222 | 207 via `novadaq-far-gateway-01.fnal.gov` |

The README describes `c` as checking "the selected host" (`README.md:153`).

## Why it matters
OpenSSH's default `MaxStartups 10:30:100` starts dropping unauthenticated
connections once 10 are pending, so a burst of 62 or 207 GSSAPI logins on one
gateway makes many checks report "unreachable" and can refuse logins by other
people on that gateway for the duration. The gateways' actual `sshd`
settings were not inspected; the consequence is filed at S3 for that reason.

## Suggested fix
Bound concurrency (an `asyncio.Semaphore`, e.g. 8) and multiplex per jump host
(`-o ControlMaster=auto -o ControlPath=... -o ControlPersist=60` on the `-J`
leg, or one `ssh jump 'for h in ...; do ssh $h who; done'`). Offer "check this
host" as the default and "check all" as a separate key, and correct the README.

## Related
- CLU-001

<!-- mega-review-fingerprint: d9acc61d -->

---

## CLU-003

**Title:** `[lan-scan] no bound on scan size or rate, auto-detect scans whichever network is listed first, and a shipped profile targets a routable site subnet`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** enhancement
**Location:** `src/mu2edaq_cluster_tools/lan_scan.py:165`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`lan-scan` probes every address of the chosen network with ICMP and, when
ports are configured, TCP connects, 64 at a time, with no delay, rate limit,
size check or confirmation. With no `--network` it scans the first
non-loopback IPv4 network that `ip addr show` (or `ifconfig`) lists. One of
the three shipped profiles targets `131.225.245.0/24` with TCP 22, 80 and 443.

## Evidence
```
$ sed -n '165p;180p;303p;310p' src/mu2edaq_cluster_tools/lan_scan.py
    addresses = list(network.hosts())
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        detected = [n for n in detect_local_networks() if not n.is_loopback]
        network = detected[0]
$ sed -n '13,19p' config/lan_scan-mu2edaq.yaml
network: 131.225.245.0/24
...
ports:
  - 22
  - 80
  - 443
$ host mu2egateway01.fnal.gov
mu2egateway01.fnal.gov has address 131.225.245.6
```

## Why it matters
A typo (`/16` for `/24`) or an auto-detected container bridge or site network
turns a subnet check into a sweep of tens of thousands of addresses (a /16
takes about 35 minutes at the defaults and builds 65534 address objects first;
a /8 about 16.7 million). A sweep of routable site address space, including the
shipped `131.225.245.0/24` profile, can be indistinguishable from
reconnaissance to the site's network monitoring. Whether Fermilab policy
permits it from these hosts is outside this review.

## Suggested fix
Refuse networks larger than a configurable limit (e.g. /22) unless
`--allow-large` is given; never auto-scan a network that was not named, or
require confirmation; add `--rate` (probes per second); generate addresses
lazily. Mark site-routable profiles as such, or drop the TCP fallback from
them.

## Related
- CLU-007

<!-- mega-review-fingerprint: 2df83ccb -->

---

## CLU-004

**Title:** `[ssh-selector] background tunnels leave the panel after any connection and survive quit; a reused local port silently keeps the old forward`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_cluster_tools/ssh_selector.py:1676`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Background tunnels are child `ssh -N -L` processes recorded in the running
`SSHSelector` instance. `main()` builds a new instance after every Enter, `n`
or foreground `t`, so earlier tunnels disappear from the panel and can no
longer be stopped from the UI; on quit nothing stops them. The tunnels are
started without `ExitOnForwardFailure`, and their stderr is piped and never
read.

## Evidence
```
$ sed -n '1675,1676p' src/mu2edaq_cluster_tools/ssh_selector.py
    while True:
        app = SSHSelector(config, error=ssh_error)
$ sed -n '1059p' src/mu2edaq_cluster_tools/ssh_selector.py
        self._active_tunnels: list[ActiveTunnel] = []
$ sed -n '1322p' src/mu2edaq_cluster_tools/ssh_selector.py
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
$ sed -n '104p' src/mu2edaq_cluster_tools/ssh_selector.py
        cmd = ["ssh", "-N", "-L", f"{local_port}:localhost:{remote_port}"]
```

## Why it matters
An operator who opens `localhost:3075 → host A` in the background, connects
somewhere, and later opens `localhost:3075 → host B` gets a second `ssh` whose
bind fails with only a warning; it stays connected, is listed as alive, and
`localhost:3075` still reaches host A. The operator then works against the
wrong host with no indication. Orphaned tunnels also hold ports and Kerberos
sessions after the tool exits.

## Suggested fix
Keep the tunnel list in `main()` and pass it into each `SSHSelector`; start
tunnels with `-o ExitOnForwardFailure=yes`; send stderr to a log file instead
of an unread pipe; on quit, list running tunnels and ask whether to stop them.

## Related
- none

<!-- mega-review-fingerprint: f644c26b -->

---

## CLU-005

**Title:** `[install] install.sh takes its target from the generic INSTALL_DIR variable and --uninstall deletes it recursively`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `install.sh:16`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`install.sh` uses `$INSTALL_DIR` from the environment as its install directory
and `--uninstall` runs `rm -rf "${INSTALL_DIR}"` without confirmation.
`INSTALL_DIR` is a generic name that build environments commonly export for
their own purposes.

## Evidence
```
$ sed -n '15,16p;50,51p' install.sh
APP_CMD="${APP_CMD:-ssh-selector}"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.local/share/mu2edaq-cluster-tools}"
    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
```
Only the first argument is checked (`install.sh:38`, `:46`); any other value,
including a mistyped `--uninstal`, runs a normal install.

## Why it matters
With an inherited `INSTALL_DIR` (for example a build prefix), `./install.sh`
writes `venv/` and `config/` into that directory, and `./install.sh --uninstall`
deletes the whole directory, including content that was never installed by
this script. Filed at S3, not S2, because deletion needs the explicit
`--uninstall` and the install step prints the target directory first.

## Suggested fix
Use a package-prefixed variable (`MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR`), write a
marker file at install, and on uninstall remove only what the marker lists
(`venv/`, `config/hosts.yaml.example`, the two launchers if they are the
generated ones). Reject unknown arguments.

## Related
- Mu2e/mu2edaq-commandline-tools CLT-002 (same pattern)

<!-- mega-review-fingerprint: b7bba2a1 -->

---

## CLU-006

**Title:** `[version] lan-scan --version reports 1.0.0 while the package is 1.0.1`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** bug
**Location:** `src/mu2edaq_cluster_tools/lan_scan.py:4`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
The package has two `__version__` strings. Packaging, `ssh-selector`,
`bootstrap.sh` and `install.sh` use the one in `ssh_selector.py`; `lan-scan`
reports its own.

## Evidence
```
$ grep -n '^__version__' src/mu2edaq_cluster_tools/*.py
src/mu2edaq_cluster_tools/lan_scan.py:4:__version__ = "1.0.0"
src/mu2edaq_cluster_tools/ssh_selector.py:4:__version__ = "1.0.1"
$ grep -n 'version = ' pyproject.toml
51:version = { attr = "mu2edaq_cluster_tools.ssh_selector.__version__" }
```

## Why it matters
A bug report quoting `lan-scan --version` names a release that does not
exist.

## Suggested fix
Define `__version__` once in `__init__.py` (and point `pyproject.toml`,
`bootstrap.sh` and `install.sh` at it); import it in both modules.

## Related
- none

<!-- mega-review-fingerprint: e39dde4b -->

---

## CLU-007

**Title:** `[lan-scan] probe timeouts are not what --timeout says: Linux ping -W gets milliseconds, and the DNS timeout is global and ineffective`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** bug
**Location:** `src/mu2edaq_cluster_tools/lan_scan.py:120`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`ping()` passes the timeout in milliseconds to `-W`, which iputils on Linux
reads as seconds; the probe ends only when the subprocess is killed at
`timeout + 1` s. `reverse_dns()` sets `socket.setdefaulttimeout()`, which does
not bound `gethostbyaddr()` and is shared by all worker threads.

## Evidence
```
$ sed -n '116,120p;126p' src/mu2edaq_cluster_tools/lan_scan.py
    timeout_ms = max(1, int(timeout * 1000))
    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_ms), ip]
            timeout=timeout + 1,
$ sed -n '146,147p' src/mu2edaq_cluster_tools/lan_scan.py
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
```
macOS `ping -W` is in milliseconds, so the value is right there.

## Why it matters
On the Linux DAQ nodes every silent address costs `timeout + 1` s instead of
`timeout`, doubling scan time at the default, and `--timeout 0.2` still waits
1.2 s. A slow reverse lookup is not bounded at all.

## Suggested fix
Pass `-W` in seconds on Linux (`max(1, ceil(timeout))`), milliseconds on macOS
(branch on `platform.system()`). Do reverse lookups in a separate executor with
a `future.result(timeout=...)`, and drop `setdefaulttimeout`.

## Related
- CLU-003

<!-- mega-review-fingerprint: d1d3bf59 -->

---

## CLU-008

**Title:** `[config] ssh-selector looks for host files only under the current directory, and install.sh installs none`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** enhancement
**Location:** `src/mu2edaq_cluster_tools/ssh_selector.py:1512`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Discovery is `./*.yaml` then `./config/*.yaml`, nothing else: no environment
variable, no `~/.config/...`, no install directory. `install.sh` copies only
`config/hosts.yaml.example` (not matched by `*.yaml`), so the installed
`ssh-selector` run from a home directory exits with "No YAML config files
found".

## Evidence
```
$ sed -n '1512p' src/mu2edaq_cluster_tools/ssh_selector.py
    for d in (Path("."), Path("config")):
$ sed -n '118,119p' install.sh
mkdir -p "${INSTALL_DIR}/config"
cp "${SCRIPT_DIR}/config/hosts.yaml.example" "${INSTALL_DIR}/config/hosts.yaml.example"
```

## Why it matters
Operators must keep a checkout and pass `-c` with a full path. The suite
convention is a config directory with command line > environment > `.env` >
file precedence; `lan-scan` has environment overrides, `ssh-selector` has none.

## Suggested fix
Add `SSH_SELECTOR_CONFIG` (file) and search `~/.config/mu2edaq/cluster-tools/`
and `$INSTALL_DIR/config/` after the current directory; have `install.sh`
copy the topology files there.

## Related
- none

<!-- mega-review-fingerprint: 09709a54 -->

---

## CLU-009

**Title:** `[ci] CI matrix omits Python 3.9, the declared floor and the AlmaLinux 9 system Python`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** enhancement
**Location:** `.github/workflows/ci.yml:16`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
The workflow tests 3.10–3.12 on Ubuntu and macOS. `pyproject.toml` declares
`requires-python = ">=3.9"` and a Windows classifier; neither 3.9 nor Windows
is tested.

## Evidence
```
$ sed -n '15,16p' .github/workflows/ci.yml
        os: [ubuntu-latest, macos-latest]
        python-version: ["3.10", "3.11", "3.12"]
$ grep -n 'requires-python\|Windows' pyproject.toml
9:requires-python = ">=3.9"
19:    "Operating System :: Microsoft :: Windows",
```
The suite passes on 3.9.6 in this review (152 tests), so adding it costs
nothing today.

## Why it matters
The tool runs on AlmaLinux 9 hosts whose system Python is 3.9; a 3.10-only
construct would pass CI and fail there.

## Suggested fix
Add `"3.9"` to the matrix and `windows-latest` (or drop the Windows
classifier).

## Related
- none

<!-- mega-review-fingerprint: 0936c4c9 -->

---

## CLU-010

**Title:** `[hygiene] minor findings from the 2026-10 review (rollup)`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** documentation
**Location:** `README.md:28`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Small items, each too minor for its own issue.

## Evidence
1. `README.md:28` says the green/red dot "indicates reachability"; it shows
   whether DNS resolved (`ssh_selector.py:1158-1175`).
2. `README.md:153` says `c` checks "the selected host"; it checks all (CLU-002).
3. `README.md:163` says GPVM configs will be added "shortly";
   `config/fnal_gpmv.yaml` exists (and its name transposes "gpvm").
4. `config/mu2e_daq.yaml`: `Stopping Target Monitor` is in `grouplist` but
   has no `groups` entry, so `mu2e-stm-01/02` get no jump host and only the
   current user, unlike every other DAQ group.
5. `config/nova_daq.yaml:794` uses group `DCS`, and
   `config/mu2e_offline.yaml:147` uses `DAQ Control`; neither is in its
   file's `grouplist`, so they sort last.
6. The `lan_scan-*.yaml` profiles sit in `config/` beside the host files, so
   `ssh-selector` run in the checkout offers them in its picker and stops with
   "No hosts defined in config file."
7. `ssh_selector.py:1608` runs `xfce4-terminal -e ssh -l ...`; `-e` takes one
   string (`-x` takes the rest). `ssh_selector.py:1614` calls `which`, absent
   on Windows, so `n` raises there.
8. `ssh_selector.py:1711` captures `ssh`'s stderr for the whole interactive
   session; its warnings appear only afterwards, and only on a non-zero exit,
   which a remote shell's last command status can also cause.
9. `bootstrap.sh:55-61` installs `.[dev]` relative to the current directory,
   not the script's; run from elsewhere it installs the wrong project or
   fails. `--dev` selects an editable install; the `dev` extra is installed
   either way.
10. `lan-scan` imports the package `__init__.py`, which imports
    `ssh_selector` and therefore Textual, for no use.

## Why it matters
Each is a small mismatch between documentation and behaviour or a rough edge
for the operator; none affects a DAQ system.

## Suggested fix
Correct the README lines; add a `groups` entry for the STM group (or document
that STM hosts are reached directly); add the missing groups to `grouplist`;
move the lan-scan profiles to `config/lan-scan/`; use `xfce4-terminal -x` and
`shutil.which`; let `ssh` keep its stderr in the foreground; make
`bootstrap.sh` `cd "$HERE"`; drop the `__init__` import.

## Related
- CLU-002

<!-- mega-review-fingerprint: 9a081a38 -->
