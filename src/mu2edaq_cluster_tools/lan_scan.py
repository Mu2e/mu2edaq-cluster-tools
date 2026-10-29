#!/usr/bin/env python3
"""LAN Scanner - probes a local network and reports responding hosts."""

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

DEFAULT_TIMEOUT = 1.0
DEFAULT_WORKERS = 64
MAX_WORKERS = 256
# Addresses started per second across all workers (0 = unlimited).
DEFAULT_RATE = 50.0
# Largest scan accepted without --allow-large: 1024 probed addresses (a /22
# minus network and broadcast is 1022, so a /22 passes and a /21 does not).
DEFAULT_MAX_HOSTS = 1024
# Extra time given to the ping subprocess (start-up, teardown) before it is killed.
PING_GRACE = 0.5

EXIT_NO_NETWORK = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


@dataclass
class ScanResult:
    ip: str
    hostname: Optional[str]


@dataclass(frozen=True)
class LocalInterface:
    """One IPv4 address configured on a local interface."""
    name: str
    interface: ipaddress.IPv4Interface

    @property
    def network(self) -> ipaddress.IPv4Network:
        return self.interface.network


# ---------------------------------------------------------------------------
# Local network detection
# ---------------------------------------------------------------------------

def _parse_ip_addr(out: str) -> List[LocalInterface]:
    """Parse Linux `ip addr show` output."""
    found: List[LocalInterface] = []
    current = "?"
    for line in out.splitlines():
        hdr = re.match(r"^\d+:\s+([^:@\s]+)", line)
        if hdr:
            current = hdr.group(1)
            continue
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        # The address label (last token, e.g. "eth0" or "eth0:1") is more
        # specific than the link name when present.
        label = line.split()[-1] if " scope " in line else current
        try:
            found.append(LocalInterface(label, ipaddress.IPv4Interface(m.group(1))))
        except ValueError:
            pass
    return found


def _parse_ifconfig(out: str) -> List[LocalInterface]:
    """Parse macOS/BSD `ifconfig` output (netmask in hex or dotted quad)."""
    found: List[LocalInterface] = []
    current = "?"
    for line in out.splitlines():
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
                mask_str = socket.inet_ntoa(struct.pack(">I", int(mask_raw, 16)))
            else:
                mask_str = mask_raw
            found.append(LocalInterface(current, ipaddress.IPv4Interface(f"{ip_str}/{mask_str}")))
        except (ValueError, OSError, struct.error):
            pass
    return found


def _parse_ipconfig(out: str) -> List[LocalInterface]:
    """Parse Windows `ipconfig` output (paired IPv4 Address / Subnet Mask lines)."""
    found: List[LocalInterface] = []
    current = "?"
    pending_ip = None
    for line in out.splitlines():
        hdr = re.match(r"^\S.*adapter (.+):\s*$", line)
        if hdr:
            current = hdr.group(1).strip()
            pending_ip = None
            continue
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
    """Return local IPv4 interface addresses with their interface names.

    Tries, in order: Linux (`ip addr show`), macOS/BSD (`ifconfig`),
    Windows (`ipconfig`); the first command that yields addresses wins.
    """
    for cmd, parser in (
        (["ip", "addr", "show"], _parse_ip_addr),
        (["ifconfig"], _parse_ifconfig),
        (["ipconfig"], _parse_ipconfig),
    ):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            continue
        found = parser(out)
        if found:
            return found
    return []


def detect_local_networks() -> List[ipaddress.IPv4Network]:
    """Return local IPv4 networks parsed from interface configuration."""
    return [i.network for i in detect_local_interfaces()]


def scannable_interfaces(interfaces: List[LocalInterface]) -> List[LocalInterface]:
    """Drop loopback and link-local addresses, dedupe by network, sort deterministically.

    The result is ordered by network address (ties broken by interface name),
    so it does not depend on the order in which the OS lists interfaces.
    """
    by_net = {}
    for iface in interfaces:
        net = iface.network
        if net.is_loopback or net.is_link_local:
            continue
        prev = by_net.get(net)
        if prev is None or iface.name < prev.name:
            by_net[net] = iface
    return [by_net[n] for n in sorted(by_net)]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def ping_command(ip: str, timeout: float, system: Optional[str] = None) -> Tuple[List[str], float]:
    """Build the one-echo `ping` argv for this platform; return (argv, wait_seconds).

    The reply-wait option has different units per platform:
      * Windows `ping -w`: milliseconds.
      * macOS / FreeBSD `ping -W`: milliseconds.
      * Linux iputils (and BusyBox) `ping -W`: seconds. Older iputils accept
        integers only, so the timeout is rounded up to whole seconds (>= 1).
    wait_seconds is the wait actually requested; it sizes the subprocess kill.
    """
    system = (system or platform.system()).lower()
    timeout_ms = max(1, int(math.ceil(timeout * 1000)))
    if system == "windows":
        return ["ping", "-n", "1", "-w", str(timeout_ms), ip], timeout_ms / 1000.0
    if system in ("darwin", "freebsd"):
        return ["ping", "-c", "1", "-W", str(timeout_ms), ip], timeout_ms / 1000.0
    wait_s = max(1, int(math.ceil(timeout)))
    return ["ping", "-c", "1", "-W", str(wait_s), ip], float(wait_s)


def ping(ip: str, timeout: float) -> bool:
    """Return True if `ip` answers a single ICMP echo within `timeout` seconds."""
    cmd, wait_s = ping_command(ip, timeout)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=wait_s + PING_GRACE,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def tcp_probe(ip: str, ports: List[int], timeout: float) -> bool:
    """Return True if a TCP connection succeeds on any of `ports` (for hosts that filter ICMP)."""
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def reverse_dns(ip: str, timeout: float) -> Optional[str]:
    """Return the DNS name for `ip`, or None if it doesn't resolve within `timeout` s.

    gethostbyaddr() blocks in the C resolver and ignores socket timeouts, so
    it runs in its own daemon thread and the caller waits at most `timeout`.
    An overrunning lookup is abandoned; being a daemon thread it never blocks
    interpreter exit. No process-global socket state is touched.
    """
    result: dict = {}

    def lookup() -> None:
        try:
            result["name"] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):  # herror and gaierror are OSError subclasses
            result["name"] = None

    t = threading.Thread(target=lookup, name=f"rdns-{ip}", daemon=True)
    t.start()
    t.join(timeout)
    return result.get("name")


class RateLimiter:
    """Thread-safe pacing: successive acquire() calls are released >= 1/rate s apart.

    rate <= 0 disables limiting.
    """

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / rate if rate and rate > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next: Optional[float] = None

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = self._clock()
            slot = now if self._next is None or self._next < now else self._next
            self._next = slot + self.interval
        if slot > now:
            self._sleep(slot - now)


def host_count(network: ipaddress.IPv4Network) -> int:
    """Number of addresses scan_network() will probe in `network`."""
    n = network.num_addresses
    return n - 2 if n > 2 else n


def iter_addresses(network: ipaddress.IPv4Network) -> Iterator[ipaddress.IPv4Address]:
    """Yield the addresses to probe lazily (never a list of the whole network)."""
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
    """Probe every host address in `network` and return the ones that respond.

    At most `workers` addresses are probed at once and at most `rate`
    addresses are started per second. Addresses are generated lazily and at
    most 2*workers futures are outstanding at any time.
    """
    workers = max(1, workers)
    limiter = RateLimiter(rate)

    def probe(ip_obj: ipaddress.IPv4Address) -> Optional[ScanResult]:
        limiter.acquire()
        ip = str(ip_obj)
        alive = ping(ip, timeout)
        if not alive and ports:
            alive = tcp_probe(ip, ports, timeout)
        if not alive:
            return None
        hostname = reverse_dns(ip, timeout) if resolve_dns else None
        return ScanResult(ip=ip, hostname=hostname)

    results: List[ScanResult] = []

    def collect(futures) -> None:
        for f in futures:
            r = f.result()
            if r is not None:
                results.append(r)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pending = set()
        for addr in iter_addresses(network):
            if len(pending) >= 2 * workers:
                done, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                collect(done)
            pending.add(executor.submit(probe, addr))
        collect(concurrent.futures.as_completed(pending))

    results.sort(key=lambda r: ipaddress.IPv4Address(r.ip))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(results: List[ScanResult]) -> None:
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
    print(json.dumps([{"ip": r.ip, "hostname": r.hostname} for r in results], indent=2))


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """Invalid option value from the command line, environment or config file."""


def load_config(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def parse_ports(raw) -> List[int]:
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
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def resolve_option(cli, env_name: str, config: dict, key: str, default, convert):
    """Pick one option value with precedence commandline > environment > config > default.

    An unset or empty environment variable and a missing or null config key
    fall through to the next level.
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
        raise ConfigError(f"invalid value {raw!r} from {source}: {exc}") from None


def resolve_flag(cli: bool, env_name: str, config: dict, key: str) -> bool:
    """Boolean option: a given flag wins, else environment, else config, else False."""
    return resolve_option(True if cli else None, env_name, config, key, False, parse_bool)


def _positive(conv):
    def convert(raw):
        v = conv(raw)
        if v <= 0:
            raise ValueError("must be > 0")
        return v
    return convert


def _non_negative_float(raw) -> float:
    v = float(raw)
    if v < 0:
        raise ValueError("must be >= 0")
    return v


def _workers(raw) -> int:
    v = int(raw)
    if not 1 <= v <= MAX_WORKERS:
        raise ValueError(f"must be between 1 and {MAX_WORKERS}")
    return v


def _network(raw) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(str(raw).strip(), strict=False)


def build_parser() -> argparse.ArgumentParser:
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
    print(f"error: {msg}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> None:
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
        _err(str(exc))
        sys.exit(EXIT_USAGE)

    if network is None:
        candidates = scannable_interfaces(detect_local_interfaces())
        if not candidates:
            _err("could not auto-detect a local network; specify one with --network")
            sys.exit(EXIT_NO_NETWORK)
        if len(candidates) > 1:
            _err("multiple local networks detected; choose one with --network:")
            for c in candidates:
                print(f"  {c.network}  ({c.name}, {c.interface.ip})", file=sys.stderr)
            sys.exit(EXIT_NO_NETWORK)
        chosen = candidates[0]
        network = chosen.network
        print(f"note: auto-detected {network} on interface {chosen.name} "
              f"({chosen.interface.ip})", file=sys.stderr)

    count = host_count(network)
    if count > max_hosts and not allow_large:
        _err(f"{network} has {count} addresses to probe, more than the limit of "
             f"{max_hosts}; narrow the network, raise --max-hosts, or pass --allow-large")
        sys.exit(EXIT_REFUSED)
    if network.is_global and not allow_routable:
        _err(f"{network} is globally routable address space; a sweep of it may be "
             "treated as hostile by site network monitoring. Pass --allow-routable "
             "if you are authorised to scan it")
        sys.exit(EXIT_REFUSED)

    rate_txt = f"{rate:g}/s" if rate > 0 else "unlimited"
    print(f"Scanning {network} ({count} addresses, {workers} workers, rate {rate_txt})...",
          file=sys.stderr)

    results = scan_network(network, timeout, workers, ports, not no_dns, rate=rate)

    if args.json:
        print_json(results)
    else:
        print_table(results)


if __name__ == "__main__":
    main()
