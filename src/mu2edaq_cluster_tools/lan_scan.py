#!/usr/bin/env python3
"""!
@file lan_scan.py
@brief LAN Scanner - probes a local network and reports responding hosts.

`lan-scan` sweeps an IPv4 network with one ICMP echo per address (falling
back to TCP connects on operator-chosen ports for hosts that filter ICMP),
optionally reverse-resolves the responders, and prints a table or JSON.

Because it runs on production Mu2e DAQ networks, the scanner is deliberately
conservative by default:
  - Size limit: networks with more than DEFAULT_MAX_HOSTS (1024) probe
    addresses are refused unless `--allow-large` is given.
  - Routable refusal: globally routable (non-RFC 1918, non-reserved) space
    is refused unless `--allow-routable` is given.
  - Rate limiting: at most DEFAULT_RATE (50) addresses are started per
    second across all workers (RateLimiter).
  - Bounded outstanding work: at most `workers` probes in flight and at
    most 2 * workers futures queued; addresses are generated lazily.
  - Auto-detect only when unambiguous: with no `--network`, exactly one
    scannable local network must exist, otherwise the operator must choose.

Every option resolves with precedence commandline > environment
(LAN_SCAN_*) > YAML config file (`-c`) > built-in default.

Exit status: 0 on a completed scan (even if no host answers);
EXIT_NO_NETWORK (1) when no network can be auto-detected or the choice is
ambiguous; EXIT_USAGE (2) for invalid options or an unreadable config file
(argparse also exits 2 on usage errors); EXIT_REFUSED (3) when a safety
limit refuses the scan.

@warning A sweep is visible to site network monitoring. Scan only networks
         you are responsible for; on DAQ subnets keep the default rate so
         that front-end boards and switches are not flooded with ICMP/ARP.
@see ssh_selector.py for the interactive host selector in the same package.
"""

import argparse
import concurrent.futures
import ipaddress
import json
import math
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import yaml

from mu2edaq_cluster_tools._version import __version__

## Per-probe timeout in seconds. One second is ample on a LAN (RTT well under
## 1 ms) and is also the smallest value Linux iputils can express (see
# ping_command), so a smaller default would buy nothing there.
DEFAULT_TIMEOUT = 1.0
## Concurrent probes. A probe is mostly a blocked ping child process, so the
## cost is processes, not CPU; 64 keeps a /24 sweep to a few seconds.
DEFAULT_WORKERS = 64
## Hard ceiling on workers: each worker can own a ping child process, and
## hundreds of simultaneous forks hit per-user process limits on shared hosts.
MAX_WORKERS = 256
## Addresses started per second across all workers (0 = unlimited).
## 50/s finishes a /24 in about 5 s while keeping the ARP/ICMP burst small
# enough not to trip switch storm control or IDS sweep detectors.
DEFAULT_RATE = 50.0
## Largest scan accepted without --allow-large: 1024 probed addresses (a /22
## minus network and broadcast is 1022, so a /22 passes and a /21 does not).
DEFAULT_MAX_HOSTS = 1024
## Extra time given to the ping subprocess (start-up, teardown) before it is killed.
PING_GRACE = 0.5

## Exit codes are part of the CLI contract (scripts and the man page rely on
## them), so they are named once here rather than written as literals.
EXIT_NO_NETWORK = 1
## Exit status for an invalid option value from any source (CLI, environment, YAML).
EXIT_USAGE = 2
## Exit status when a scan is refused by the size or routable-network safety limits.
EXIT_REFUSED = 3

## Accepted spellings for boolean environment variables and config values.
## Anything else is rejected rather than read as False, so a typo such as
# LAN_SCAN_ALLOW_LARGE=ture is reported instead of silently ignored.
_TRUE_STRINGS = {"1", "true", "yes", "on"}
## Accepted spellings of false; see _TRUE_STRINGS.
_FALSE_STRINGS = {"0", "false", "no", "off"}


@dataclass
class ScanResult:
    """!
    @brief One responding host found by scan_network().

    Plain value object. Instances are created on worker threads but only
    read after the executor has been joined, so no locking is needed.
    """
    ## Dotted-quad IPv4 address of the responder.
    ip: str
    ## Reverse-DNS name, or None if DNS was skipped, failed, or timed out.
    hostname: Optional[str]


@dataclass(frozen=True)
class LocalInterface:
    """!
    @brief One IPv4 address configured on a local interface.

    Frozen so instances are hashable and immutable; an interface with
    several addresses yields several LocalInterface objects.
    """
    ## OS interface name or address label (e.g. "eth0", "eth0:1", "en0").
    name: str
    ## Address together with its configured prefix length.
    interface: ipaddress.IPv4Interface

    @property
    def network(self) -> ipaddress.IPv4Network:
        """!
        @brief The network the address belongs to (host bits cleared).
        @return the IPv4Network containing this interface address.
        """
        return self.interface.network


# ---------------------------------------------------------------------------
# Local network detection
# ---------------------------------------------------------------------------

def _parse_ip_addr(out: str) -> List[LocalInterface]:
    """!
    @brief Parse Linux `ip addr show` output.

    Link header lines look like `2: eth0: <BROADCAST,...>`, or
    `3: eth0.100@eth0: ...` for VLAN devices (the `@parent` suffix is
    dropped). Address lines look like
    `inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0`; the trailing
    token is the address label and is used in preference to the link name.

    @param out  Text captured from `ip addr show`.
    @return the IPv4 addresses found, in listing order; unparsable addresses
            are skipped.
    """
    found: List[LocalInterface] = []
    # Placeholder in case an inet line precedes any link header; keeps the
    # parser total on truncated or unexpected input.
    current = "?"
    for line in out.splitlines():
        hdr = re.match(r"^\d+:\s+([^:@\s]+)", line)
        if hdr:
            current = hdr.group(1)
            continue
        # `\binet ` does not match `inet6` lines: only IPv4 is scanned.
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        # The address label (last token, e.g. "eth0" or "eth0:1") is more
        # specific than the link name when present.
        label = line.split()[-1] if " scope " in line else current
        try:
            found.append(LocalInterface(label, ipaddress.IPv4Interface(m.group(1))))
        except ValueError:
            # One malformed address must not abort detection of the others.
            pass
    return found


def _parse_ifconfig(out: str) -> List[LocalInterface]:
    """!
    @brief Parse macOS/BSD `ifconfig` output (netmask in hex or dotted quad).

    Interface blocks start at column 0 with `en0: flags=...`; address lines
    are indented, e.g. `inet 10.0.0.5 netmask 0xffffff00 broadcast ...`.
    macOS and the BSDs print the netmask in hexadecimal; newer Linux
    net-tools builds print it dotted, so both forms are accepted.

    @param out  Text captured from `ifconfig`.
    @return the IPv4 addresses found, in listing order.
    @note Old Linux net-tools `ifconfig` prints `inet addr:... Mask:...`,
          which this parser does not match; on Linux `ip addr` is tried
          first, so this only matters on hosts without iproute2.
    """
    found: List[LocalInterface] = []
    current = "?"
    for line in out.splitlines():
        # No `continue` after a header match: the same line is still checked
        # for an `inet` clause, which costs nothing and tolerates formats
        # that put the first address on the header line.
        hdr = re.match(r"^([^\s:]+):", line)
        if hdr:
            current = hdr.group(1)
        m = re.search(
            r"inet (\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)",
            line,
        )
        if not m:
            continue
        ip_str, mask_raw = m.group(1), m.group(2)
        try:
            if mask_raw.lower().startswith("0x"):
                # IPv4Interface does not accept a hex mask: convert
                # 0xffffff00 to 255.255.255.0 via network byte order.
                mask_str = socket.inet_ntoa(struct.pack(">I", int(mask_raw, 16)))
            else:
                mask_str = mask_raw
            found.append(LocalInterface(current, ipaddress.IPv4Interface(f"{ip_str}/{mask_str}")))
        except (ValueError, OSError, struct.error):
            # Non-contiguous or out-of-range masks are skipped, not fatal.
            pass
    return found


def _parse_ipconfig(out: str) -> List[LocalInterface]:
    """!
    @brief Parse Windows `ipconfig` output (paired IPv4 Address / Subnet Mask lines).

    Adapter sections start at column 0 with `Ethernet adapter Ethernet 2:`.
    Inside a section the address and mask appear on separate dotted-leader
    lines (`IPv4 Address. . . : 10.0.0.5` followed by
    `Subnet Mask . . . : 255.255.255.0`), so the address is held in
    `pending_ip` until its mask arrives.

    @param out  Text captured from `ipconfig`.
    @return the IPv4 addresses found, in listing order.
    @note Only English-locale `ipconfig` labels are recognised. On a
          localised Windows install auto-detection finds nothing and the
          operator must pass `--network`.
    """
    found: List[LocalInterface] = []
    current = "?"
    pending_ip = None
    for line in out.splitlines():
        hdr = re.match(r"^\S.*adapter (.+):\s*$", line)
        if hdr:
            current = hdr.group(1).strip()
            # Never pair an address from one adapter with a mask from the next.
            pending_ip = None
            continue
        # `[.\s]*` absorbs the dotted leader. Windows may append "(Preferred)"
        # to the address; the `[\d.]+` capture stops before it.
        ip_m = re.search(r"IPv4 Address[.\s]*:\s*([\d.]+)", line)
        mask_m = re.search(r"Subnet Mask[.\s]*:\s*([\d.]+)", line)
        if ip_m:
            pending_ip = ip_m.group(1)
        elif mask_m and pending_ip:
            try:
                found.append(LocalInterface(
                    current, ipaddress.IPv4Interface(f"{pending_ip}/{mask_m.group(1)}")))
            except ValueError:
                pass
            pending_ip = None
    return found


def detect_local_interfaces() -> List[LocalInterface]:
    """!
    @brief Return local IPv4 interface addresses with their interface names.

    Tries, in order: Linux (`ip addr show`), macOS/BSD (`ifconfig`),
    Windows (`ipconfig`); the first command that yields addresses wins.

    Trying commands instead of branching on platform.system() keeps the
    function working where the "native" tool is missing (minimal containers,
    hosts with only one of the tools installed) and keeps it testable with a
    mocked subprocess on any platform.

    @return all IPv4 addresses found, including loopback; an empty list if
            no tool is available or none produced parsable output.
    @note The tools are found via PATH and need no elevated privileges.
    @see scannable_interfaces() for the filtering applied before auto-detect.
    """
    for cmd, parser in (
        (["ip", "addr", "show"], _parse_ip_addr),
        (["ifconfig"], _parse_ifconfig),
        (["ipconfig"], _parse_ipconfig),
    ):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            # Missing or failing tool: fall through to the next platform's tool.
            continue
        found = parser(out)
        if found:
            return found
    return []


def detect_local_networks() -> List[ipaddress.IPv4Network]:
    """!
    @brief Return local IPv4 networks parsed from interface configuration.

    Unfiltered convenience wrapper for callers that need only the networks.

    @return one network per configured address (may contain duplicates and
            loopback).
    """
    return [i.network for i in detect_local_interfaces()]


def scannable_interfaces(interfaces: List[LocalInterface]) -> List[LocalInterface]:
    """!
    @brief Drop loopback and link-local addresses, dedupe by network, sort deterministically.

    The result is ordered by network address (ties broken by interface name),
    so it does not depend on the order in which the OS lists interfaces.

    Loopback (127/8) is never worth scanning, and link-local (169.254/16,
    65 534 addresses) would only trip the size limit. Two addresses on the
    same network (aliases, or two NICs on one subnet) count as one
    candidate, so they do not make auto-detection ambiguous.

    @param interfaces  Addresses from detect_local_interfaces().
    @return at most one LocalInterface per distinct network, sorted.
    """
    by_net = {}
    for iface in interfaces:
        net = iface.network
        if net.is_loopback or net.is_link_local:
            continue
        prev = by_net.get(net)
        # Keep the lexically smallest name so the reported interface is
        # stable across runs regardless of OS listing order.
        if prev is None or iface.name < prev.name:
            by_net[net] = iface
    return [by_net[n] for n in sorted(by_net)]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def ping_command(ip: str, timeout: float, system: Optional[str] = None) -> Tuple[List[str], float]:
    """!
    @brief Build the one-echo `ping` argv for this platform; return (argv, wait_seconds).

    The reply-wait option has different units per platform:
      - Windows `ping -w`: milliseconds.
      - macOS / FreeBSD `ping -W`: milliseconds.
      - Linux iputils (and BusyBox) `ping -W`: seconds. Older iputils accept
        integers only, so the timeout is rounded up to whole seconds (at least 1).

    wait_seconds is the wait actually requested; it sizes the subprocess kill.

    @param ip       Destination IPv4 address as a string.
    @param timeout  Requested reply wait in seconds (fractions allowed).
    @param system   Platform name as returned by platform.system(); None
                    means the running platform. Tests pass it explicitly to
                    check every platform's argv on one machine.
    @return (argv, wait_seconds); argv sends exactly one echo request.
    @note The count option is `-n` on Windows and `-c` elsewhere.
    @note Any platform not recognised as Windows, macOS or FreeBSD gets the
          iputils form, the most widespread one.
    """
    system = (system or platform.system()).lower()
    # Round up, never down: a sub-millisecond timeout must not become 0,
    # which some ping implementations treat as "no timeout".
    timeout_ms = max(1, int(math.ceil(timeout * 1000)))
    if system == "windows":
        return ["ping", "-n", "1", "-w", str(timeout_ms), ip], timeout_ms / 1000.0
    if system in ("darwin", "freebsd"):
        return ["ping", "-c", "1", "-W", str(timeout_ms), ip], timeout_ms / 1000.0
    # Linux: -W is whole seconds. Return the rounded value so the caller's
    # subprocess timeout does not kill ping before its own wait expires.
    wait_s = max(1, int(math.ceil(timeout)))
    return ["ping", "-c", "1", "-W", str(wait_s), ip], float(wait_s)


def ping(ip: str, timeout: float) -> bool:
    """!
    @brief Return True if `ip` answers a single ICMP echo within `timeout` seconds.

    Uses the system ping binary (setuid or CAP_NET_RAW) rather than a raw
    socket, so lan-scan itself needs no root privileges.

    @param ip       Destination IPv4 address.
    @param timeout  Reply wait in seconds; see ping_command() for rounding.
    @return True on exit status 0 (a reply was received); False otherwise,
            including when ping is missing or overruns its wait.
    """
    cmd, wait_s = ping_command(ip, timeout)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Backstop kill: a ping that ignores its own -W/-w must not pin
            # a worker thread indefinitely.
            timeout=wait_s + PING_GRACE,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Every failure counts as "no answer": one bad probe must not abort the sweep.
        return False


def tcp_probe(ip: str, ports: List[int], timeout: float) -> bool:
    """!
    @brief Return True if a TCP connection succeeds on any of `ports` (for hosts that filter ICMP).

    Only a completed handshake counts. A refused connection (RST) is
    treated as "not alive" even though it proves a host exists, which keeps
    the result conservative behind firewalls that answer with resets.

    @param ip       Destination IPv4 address.
    @param ports    TCP ports tried in order; the first success short-circuits.
    @param timeout  Per-port connect timeout in seconds.
    @return True if any port accepted a connection.
    @warning A full connect is logged by the target service (e.g. sshd);
             choose ports whose owners expect the probe.
    """
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def reverse_dns(ip: str, timeout: float) -> Optional[str]:
    """!
    @brief Return the DNS name for `ip`, or None if it doesn't resolve within `timeout` s.

    gethostbyaddr() blocks in the C resolver and ignores socket timeouts, so
    it runs in its own daemon thread and the caller waits at most `timeout`.
    An overrunning lookup is abandoned; being a daemon thread it never blocks
    interpreter exit. No process-global socket state is touched.

    The alternative, socket.setdefaulttimeout(), would change every socket
    in the process (including concurrent tcp_probe() calls) and still would
    not bound the resolver, which is why a thread is used.

    @param ip       IPv4 address to reverse-resolve.
    @param timeout  Maximum seconds to wait for the answer.
    @return the primary hostname, or None on failure or timeout.
    @note Abandoned lookup threads keep running until the resolver returns.
          With an unreachable DNS server they can accumulate; use `--no-dns`
          in that situation.
    """
    # A dict rather than a local so the nested function can publish its result.
    result: dict = {}

    def lookup() -> None:
        """!
        @brief Thread body: store the resolved name, or None, in `result`.
        """
        try:
            result["name"] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):  # herror and gaierror are OSError subclasses
            result["name"] = None

    t = threading.Thread(target=lookup, name=f"rdns-{ip}", daemon=True)
    t.start()
    t.join(timeout)
    # A missing key means the lookup is still running: report None.
    return result.get("name")


class RateLimiter:
    """!
    @brief Thread-safe pacing: successive acquire() calls are released at least 1/rate s apart.

    rate <= 0 disables limiting.

    The limiter hands out time slots: each acquire() reserves the next free
    slot under the lock, then sleeps outside it, so waiting threads do not
    serialise on the lock. When the limiter has been idle the next slot is
    "now"; no burst credit accumulates, so the instantaneous probe rate stays
    bounded after a quiet period.

    One instance is shared by all worker threads of a scan_network() call.
    """

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        """!
        @brief Create a limiter releasing at most `rate` callers per second.
        @param rate   Releases per second; 0, negative or None disables pacing.
        @param clock  Monotonic time source in seconds (injected by tests).
        @param sleep  Sleep function (injected by tests to avoid real waits).
        """
        ## Seconds between successive releases; 0.0 disables pacing.
        self.interval = 1.0 / rate if rate and rate > 0 else 0.0
        ## Monotonic time source in seconds.
        self._clock = clock
        ## Sleep function used to wait for the next release slot.
        self._sleep = sleep
        ## Serializes slot reservation across worker threads.
        self._lock = threading.Lock()
        ## Time at which the next caller may be released; None before first use.
        self._next: Optional[float] = None

    def acquire(self) -> None:
        """!
        @brief Block until the caller's rate slot arrives.
        """
        if not self.interval:
            return
        with self._lock:
            now = self._clock()
            slot = now if self._next is None or self._next < now else self._next
            self._next = slot + self.interval
        # Sleep after releasing the lock so other threads can reserve later slots.
        if slot > now:
            self._sleep(slot - now)


def host_count(network: ipaddress.IPv4Network) -> int:
    """!
    @brief Number of addresses scan_network() will probe in `network`.

    Mirrors iter_addresses(): the network and broadcast addresses are
    excluded except for /31 and /32, where every address is a host.

    @param network  Network to be scanned.
    @return the probe count, compared against `--max-hosts`.
    """
    n = network.num_addresses
    return n - 2 if n > 2 else n


def iter_addresses(network: ipaddress.IPv4Network) -> Iterator[ipaddress.IPv4Address]:
    """!
    @brief Yield the addresses to probe lazily (never a list of the whole network).

    Laziness matters with `--allow-large`: a /8 would otherwise materialise
    16 M address objects before the first probe is sent.

    @param network  Network to enumerate.
    @return an iterator over host addresses in ascending order.
    """
    if network.num_addresses <= 2:
        # /31 and /32: every address is a host (RFC 3021). hosts() of a /32
        # differs between Python versions, so enumerate directly.
        for i in range(network.num_addresses):
            yield network.network_address + i
    else:
        yield from network.hosts()


def scan_network(
    network: ipaddress.IPv4Network,
    timeout: float,
    workers: int,
    ports: List[int],
    resolve_dns: bool,
    rate: float = DEFAULT_RATE,
) -> List[ScanResult]:
    """!
    @brief Probe every host address in `network` and return the ones that respond.

    At most `workers` addresses are probed at once and at most `rate`
    addresses are started per second. Addresses are generated lazily and at
    most 2*workers futures are outstanding at any time.

    Each probe tries ICMP first, then the TCP `ports` if ICMP fails, then
    (responders only) reverse DNS. Safety checks (size, routability) are
    the caller's job; this function scans whatever it is given.

    @param network      Network to sweep.
    @param timeout      Per-probe timeout in seconds (ping, each TCP port, DNS).
    @param workers      Maximum concurrent probes; values below 1 are raised to 1.
    @param ports        TCP fallback ports; empty disables the fallback.
    @param resolve_dns  If True, reverse-resolve each responder.
    @param rate         Maximum addresses started per second; 0 = unlimited.
    @return responders sorted numerically by address.
    @throws Exception an unexpected error inside a probe is re-raised by
            Future.result(); expected failures are absorbed by ping(),
            tcp_probe() and reverse_dns().
    @warning Even rate-limited, a sweep generates ARP and ICMP for every
             address. Coordinate before scanning DAQ subnets during data
             taking.
    @see main() for the safety limits applied before this is called.
    """
    workers = max(1, workers)
    limiter = RateLimiter(rate)

    def probe(ip_obj: ipaddress.IPv4Address) -> Optional[ScanResult]:
        """!
        @brief Worker body: probe one address under the rate limit.
        @param ip_obj  Address to probe.
        @return a ScanResult for a responder, else None.
        """
        # Pace at the start of the probe so the limit governs how fast new
        # addresses are touched, independent of how long slow ones take.
        limiter.acquire()
        ip = str(ip_obj)
        alive = ping(ip, timeout)
        if not alive and ports:
            alive = tcp_probe(ip, ports, timeout)
        if not alive:
            return None
        hostname = reverse_dns(ip, timeout) if resolve_dns else None
        return ScanResult(ip=ip, hostname=hostname)

    # Only the submitting thread appends (via collect), so no lock is needed.
    results: List[ScanResult] = []

    def collect(futures) -> None:
        """!
        @brief Harvest finished futures into `results`.
        @param futures  Iterable of completed futures.
        @throws Exception re-raised from a probe that failed unexpectedly.
        """
        for f in futures:
            r = f.result()
            if r is not None:
                results.append(r)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pending = set()
        for addr in iter_addresses(network):
            # Back-pressure: submitting a whole /16 up front would queue 65k
            # futures in memory. 2*workers keeps every worker fed while
            # bounding memory regardless of network size.
            if len(pending) >= 2 * workers:
                done, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                collect(done)
            pending.add(executor.submit(probe, addr))
        collect(concurrent.futures.as_completed(pending))

    # Completion order is nondeterministic; sort numerically (string order
    # would put .10 before .9).
    results.sort(key=lambda r: ipaddress.IPv4Address(r.ip))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(results: List[ScanResult]) -> None:
    """!
    @brief Print results as an aligned two-column table on stdout.

    Column widths are computed from the data so long FQDNs do not break
    alignment. Hosts without a DNS name show "-".

    @param results  Responders, already sorted.
    """
    if not results:
        print("No hosts found.")
        return

    ip_width = max(len("IP Address"), max(len(r.ip) for r in results))
    name_width = max(len("DNS Name"), max(len(r.hostname or "-") for r in results))

    header = f"{'IP Address':<{ip_width}}  {'DNS Name':<{name_width}}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(f"{r.ip:<{ip_width}}  {(r.hostname or '-'):<{name_width}}")
    print(f"\n{len(results)} host(s) found.")


def print_json(results: List[ScanResult]) -> None:
    """!
    @brief Print results as a JSON array of {"ip", "hostname"} objects on stdout.

    Machine-readable form for scripts; hostname is null when unresolved.
    Progress messages go to stderr, so stdout stays valid JSON.

    @param results  Responders, already sorted.
    """
    print(json.dumps([{"ip": r.ip, "hostname": r.hostname} for r in results], indent=2))


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """!
    @brief Invalid option value from the command line, environment or config file.

    Subclasses ValueError so generic handlers still catch it, while main()
    maps it to EXIT_USAGE with a message naming the offending source.
    """


def load_config(path: Optional[Path]) -> dict:
    """!
    @brief Load the optional YAML config file.

    yaml.safe_load is used so a config file cannot instantiate arbitrary
    Python objects. An empty file is treated as an empty mapping.

    @param path  Path from `-c/--config`, or None for no config file.
    @return the top-level mapping; empty if `path` is None or the file is empty.
    @throws OSError if the file cannot be opened.
    @throws yaml.YAMLError if the file is not valid YAML.
    @throws ConfigError if the top level is not a mapping.
    """
    if path is None:
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def parse_ports(raw) -> List[int]:
    """!
    @brief Convert a port specification to a list of TCP ports.

    Accepts a YAML list (`ports: [22, 443]`) or a comma-separated string
    (`22,443` from the CLI or environment); blank items are ignored.

    @param raw  List, string, or a falsy value.
    @return the ports in the given order; empty if `raw` is falsy.
    @throws ValueError if an item is not an integer or is outside 1-65535.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        ports = [int(p) for p in raw]
    else:
        ports = [int(p.strip()) for p in str(raw).split(",") if p.strip()]
    for p in ports:
        if not 1 <= p <= 65535:
            raise ValueError(f"port {p} out of range 1-65535")
    return ports


def parse_bool(raw) -> bool:
    """!
    @brief Convert a YAML boolean or an environment string to bool.
    @param raw  bool, or a string in _TRUE_STRINGS / _FALSE_STRINGS (case-insensitive).
    @return the boolean value.
    @throws ValueError for any other value, so typos are reported, not ignored.
    """
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def resolve_option(cli, env_name: str, config: dict, key: str, default, convert):
    """!
    @brief Pick one option value with precedence commandline > environment > config > default.

    An unset or empty environment variable and a missing or null config key
    fall through to the next level.

    Only the winning source is converted, so an invalid value at a lower
    level causes no error when a higher level overrides it. The default is
    returned unconverted and must already have the final type.

    @param cli       Value from argparse, or None if the option was not given.
    @param env_name  Environment variable to consult (e.g. LAN_SCAN_TIMEOUT).
    @param config    Mapping from load_config().
    @param key       Key to look up in `config`.
    @param default   Value used when no source supplies one.
    @param convert   Callable that converts and validates the raw value.
    @return the converted value from the highest-precedence source, or `default`.
    @throws ConfigError if `convert` rejects the value; the message names
            the source so the operator knows where to fix it.
    """
    env_val = os.environ.get(env_name)
    if cli is not None:
        source, raw = "the commandline", cli
    elif env_val:
        source, raw = env_name, env_val
    elif config.get(key) is not None:
        source, raw = f"config key '{key}'", config[key]
    else:
        return default
    try:
        return convert(raw)
    except (TypeError, ValueError) as exc:
        # `from None`: the converter's traceback adds nothing for an operator.
        raise ConfigError(f"invalid value {raw!r} from {source}: {exc}") from None


def resolve_flag(cli: bool, env_name: str, config: dict, key: str) -> bool:
    """!
    @brief Boolean option: a given flag wins, else environment, else config, else False.

    A store_true flag is False when absent, which would be indistinguishable
    from "explicitly off" and would mask the environment and config, so an
    absent flag is passed on as None. Consequently a flag can only switch an
    option on; it cannot override an enabling environment or config value.

    @param cli       Flag value from argparse.
    @param env_name  Environment variable to consult.
    @param config    Mapping from load_config().
    @param key       Key to look up in `config`.
    @return the resolved boolean.
    @throws ConfigError if the environment or config value is not a boolean.
    """
    return resolve_option(True if cli else None, env_name, config, key, False, parse_bool)


def _positive(conv):
    """!
    @brief Wrap a converter so that it also rejects values <= 0.
    @param conv  Base converter, e.g. float or int.
    @return a converter that raises ValueError for non-positive results.
    """
    def convert(raw):
        """!
        @brief Convert `raw` with `conv` and require a positive result.
        @param raw  Raw option value.
        @return the converted value.
        @throws ValueError if conversion fails or the value is <= 0.
        """
        v = conv(raw)
        if v <= 0:
            raise ValueError("must be > 0")
        return v
    return convert


def _non_negative_float(raw) -> float:
    """!
    @brief Converter for `--rate`: a float >= 0 (0 means unlimited).
    @param raw  Raw option value.
    @return the value as float.
    @throws ValueError if not a number or negative.
    """
    v = float(raw)
    if v < 0:
        raise ValueError("must be >= 0")
    return v


def _workers(raw) -> int:
    """!
    @brief Converter for `--workers`: an int in 1..MAX_WORKERS.
    @param raw  Raw option value.
    @return the worker count.
    @throws ValueError if not an integer or out of range.
    """
    v = int(raw)
    if not 1 <= v <= MAX_WORKERS:
        raise ValueError(f"must be between 1 and {MAX_WORKERS}")
    return v


def _network(raw) -> ipaddress.IPv4Network:
    """!
    @brief Converter for `--network`: an IPv4 CIDR string.

    strict=False lets operators paste a host address with its prefix
    (e.g. 192.168.1.37/24); the host bits are cleared.

    @param raw  Raw option value.
    @return the IPv4 network.
    @throws ValueError if the string is not an IPv4 CIDR.
    """
    return ipaddress.IPv4Network(str(raw).strip(), strict=False)


def build_parser() -> argparse.ArgumentParser:
    """!
    @brief Build the `lan-scan` argument parser.

    Options default to None rather than their real defaults so that
    resolve_option() can tell "not given" from "given"; that is what lets
    environment and config values take effect.

    @return the configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="lan-scan",
        description=(
            "Probe a LAN and print a table of responding hosts "
            "with their DNS names and IP addresses."
        ),
    )
    parser.add_argument(
        "-n", "--network",
        help="CIDR network to scan, e.g. 192.168.1.0/24 (default: auto-detect, "
             "only if exactly one candidate network exists)",
    )
    parser.add_argument(
        "-c", "--config", type=Path,
        help="YAML config file providing defaults (see config/lan-scan/lan_scan.yaml.example)",
    )
    parser.add_argument(
        "-t", "--timeout", type=float,
        help=f"Per-host probe timeout in seconds (default: {DEFAULT_TIMEOUT}; "
             "Linux ping rounds up to whole seconds)",
    )
    parser.add_argument(
        "-w", "--workers", type=int,
        help=f"Maximum concurrent probes, 1-{MAX_WORKERS} (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "-r", "--rate", type=float,
        help=f"Maximum addresses started per second, 0 = unlimited (default: {DEFAULT_RATE:g})",
    )
    parser.add_argument(
        "--max-hosts", type=int,
        help=f"Refuse networks with more probe addresses than this unless --allow-large "
             f"(default: {DEFAULT_MAX_HOSTS}, i.e. a /22)",
    )
    parser.add_argument(
        "--allow-large", action="store_true",
        help="Scan even if the network exceeds --max-hosts",
    )
    parser.add_argument(
        "--allow-routable", action="store_true",
        help="Scan even if the network is globally routable (non-private) address space",
    )
    parser.add_argument(
        "-p", "--ports",
        help="Comma-separated TCP ports to try if ICMP ping is filtered, e.g. 22,80,443",
    )
    parser.add_argument(
        "--no-dns", action="store_true",
        help="Skip reverse DNS lookups (faster, IP addresses only)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON instead of a table",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    return parser


def _err(msg: str) -> None:
    """!
    @brief Print an `error:` message on stderr (stdout is reserved for results).
    @param msg  Message text.
    """
    print(f"error: {msg}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> None:
    """!
    @brief Entry point of the `lan-scan` console script.

    Steps: parse arguments; resolve every option (commandline > environment
    > config file > default); auto-detect the network if none was given;
    apply the safety limits; scan; print a table or JSON.

    Environment variables: LAN_SCAN_NETWORK, LAN_SCAN_TIMEOUT,
    LAN_SCAN_WORKERS, LAN_SCAN_RATE, LAN_SCAN_MAX_HOSTS, LAN_SCAN_PORTS,
    LAN_SCAN_ALLOW_LARGE, LAN_SCAN_ALLOW_ROUTABLE, LAN_SCAN_NO_DNS.
    Config keys use the same names in lower case without the prefix.

    Example:
    @code
    lan-scan -n 192.168.10.0/24 -p 22 --json
    @endcode

    @param argv  Argument list without the program name; None means sys.argv[1:].
    @throws SystemExit 0 after `--help` or `--version`; EXIT_USAGE (2) for
            argparse errors, invalid option values or an unreadable config;
            EXIT_NO_NETWORK (1) if auto-detection finds no network or more
            than one; EXIT_REFUSED (3) if the network exceeds the host limit
            or is globally routable without the matching override.
    @warning `--allow-large` and `--allow-routable` (and their environment
             and config equivalents) disable the safety checks. Setting them
             in a shared config file affects every operator who uses it.
    """
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
        # Precedence: commandline > environment > config file > default
        network = resolve_option(args.network, "LAN_SCAN_NETWORK", config, "network",
                                 None, _network)
        timeout = resolve_option(args.timeout, "LAN_SCAN_TIMEOUT", config, "timeout",
                                 DEFAULT_TIMEOUT, _positive(float))
        workers = resolve_option(args.workers, "LAN_SCAN_WORKERS", config, "workers",
                                 DEFAULT_WORKERS, _workers)
        rate = resolve_option(args.rate, "LAN_SCAN_RATE", config, "rate",
                              DEFAULT_RATE, _non_negative_float)
        max_hosts = resolve_option(args.max_hosts, "LAN_SCAN_MAX_HOSTS", config, "max_hosts",
                                   DEFAULT_MAX_HOSTS, _positive(int))
        ports = resolve_option(args.ports, "LAN_SCAN_PORTS", config, "ports", [], parse_ports)
        allow_large = resolve_flag(args.allow_large, "LAN_SCAN_ALLOW_LARGE",
                                   config, "allow_large")
        allow_routable = resolve_flag(args.allow_routable, "LAN_SCAN_ALLOW_ROUTABLE",
                                      config, "allow_routable")
        no_dns = resolve_flag(args.no_dns, "LAN_SCAN_NO_DNS", config, "no_dns")
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        # Every configuration problem is reported before any packet is sent.
        _err(str(exc))
        sys.exit(EXIT_USAGE)

    if network is None:
        candidates = scannable_interfaces(detect_local_interfaces())
        if not candidates:
            _err("could not auto-detect a local network; specify one with --network")
            sys.exit(EXIT_NO_NETWORK)
        # Never guess on a multi-homed host (DAQ servers typically sit on a
        # data network and a control network): scanning the wrong one is
        # worse than asking, so list the candidates and stop.
        if len(candidates) > 1:
            _err("multiple local networks detected; choose one with --network:")
            for c in candidates:
                print(f"  {c.network}  ({c.name}, {c.interface.ip})", file=sys.stderr)
            sys.exit(EXIT_NO_NETWORK)
        chosen = candidates[0]
        network = chosen.network
        # Announce the choice so an unexpected auto-detection is visible.
        print(f"note: auto-detected {network} on interface {chosen.name} "
              f"({chosen.interface.ip})", file=sys.stderr)

    # The safety limits apply to explicit and auto-detected networks alike:
    # an auto-detected /16 on a lab network is refused just the same.
    count = host_count(network)
    if count > max_hosts and not allow_large:
        _err(f"{network} has {count} addresses to probe, more than the limit of "
             f"{max_hosts}; narrow the network, raise --max-hosts, or pass --allow-large")
        sys.exit(EXIT_REFUSED)
    # is_global excludes RFC 1918, CGNAT, loopback, link-local and other
    # reserved ranges, so ordinary internal DAQ subnets pass without the flag.
    if network.is_global and not allow_routable:
        _err(f"{network} is globally routable address space; a sweep of it may be "
             "treated as hostile by site network monitoring. Pass --allow-routable "
             "if you are authorised to scan it")
        sys.exit(EXIT_REFUSED)

    rate_txt = f"{rate:g}/s" if rate > 0 else "unlimited"
    # Progress goes to stderr so `--json` output on stdout stays parseable.
    print(f"Scanning {network} ({count} addresses, {workers} workers, rate {rate_txt})...",
          file=sys.stderr)

    results = scan_network(network, timeout, workers, ports, not no_dns, rate=rate)

    if args.json:
        print_json(results)
    else:
        print_table(results)


if __name__ == "__main__":
    main()
