#!/usr/bin/env python3
"""LAN Scanner - probes the local network and reports responding hosts."""

__version__ = "1.0.0"

import argparse
import concurrent.futures
import ipaddress
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_TIMEOUT = 1.0
DEFAULT_WORKERS = 64


@dataclass
class ScanResult:
    ip: str
    hostname: Optional[str]


# ---------------------------------------------------------------------------
# Local network detection
# ---------------------------------------------------------------------------

def detect_local_networks() -> list[ipaddress.IPv4Network]:
    """Return local IPv4 networks parsed from interface configuration.

    Tries, in order: Linux (`ip addr show`), macOS/BSD (`ifconfig`),
    Windows (`ipconfig`).
    """
    networks: list[ipaddress.IPv4Network] = []

    # Linux: ip addr show -> "inet x.x.x.x/prefix"
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

    # macOS / BSD fallback: ifconfig -> "inet x.x.x.x netmask 0xhhhhhhhh|d.d.d.d"
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
        if networks:
            return networks
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Windows fallback: ipconfig -> paired "IPv4 Address" / "Subnet Mask" lines
    try:
        out = subprocess.check_output(
            ["ipconfig"], stderr=subprocess.DEVNULL, text=True
        )
        pending_ip = None
        for line in out.splitlines():
            ip_m = re.search(r"IPv4 Address[.\s]*:\s*([\d.]+)", line)
            mask_m = re.search(r"Subnet Mask[.\s]*:\s*([\d.]+)", line)
            if ip_m:
                pending_ip = ip_m.group(1)
            elif mask_m and pending_ip:
                try:
                    networks.append(
                        ipaddress.IPv4Interface(f"{pending_ip}/{mask_m.group(1)}").network
                    )
                except ValueError:
                    pass
                pending_ip = None
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    return networks


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def ping(ip: str, timeout: float) -> bool:
    """Return True if `ip` answers a single ICMP echo within `timeout` seconds."""
    timeout_ms = max(1, int(timeout * 1000))
    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_ms), ip]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def tcp_probe(ip: str, ports: list[int], timeout: float) -> bool:
    """Return True if a TCP connection succeeds on any of `ports` (for hosts that filter ICMP)."""
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def reverse_dns(ip: str, timeout: float) -> Optional[str]:
    """Look up the DNS name for `ip`; returns None if it doesn't resolve."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        return name
    except (socket.herror, socket.gaierror, OSError):
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


def scan_network(
    network: ipaddress.IPv4Network,
    timeout: float,
    workers: int,
    ports: list[int],
    resolve_dns: bool,
) -> list[ScanResult]:
    """Probe every host address in `network` concurrently and return the ones that respond."""
    addresses = list(network.hosts())
    if not addresses:
        addresses = [network.network_address]

    def probe(ip_obj: ipaddress.IPv4Address) -> Optional[ScanResult]:
        ip = str(ip_obj)
        alive = ping(ip, timeout)
        if not alive and ports:
            alive = tcp_probe(ip, ports, timeout)
        if not alive:
            return None
        hostname = reverse_dns(ip, timeout) if resolve_dns else None
        return ScanResult(ip=ip, hostname=hostname)

    results: list[ScanResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(probe, addresses):
            if result is not None:
                results.append(result)

    results.sort(key=lambda r: ipaddress.IPv4Address(r.ip))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(results: list[ScanResult]) -> None:
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


def print_json(results: list[ScanResult]) -> None:
    print(json.dumps([{"ip": r.ip, "hostname": r.hostname} for r in results], indent=2))


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------

def load_config(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def parse_ports(raw) -> list[int]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [int(p) for p in raw]
    return [int(p.strip()) for p in str(raw).split(",") if p.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lan-scan",
        description=(
            "Probe the local LAN and print a table of responding hosts "
            "with their DNS names and IP addresses."
        ),
    )
    parser.add_argument(
        "-n", "--network",
        help="CIDR network to scan, e.g. 192.168.1.0/24 (default: auto-detect)",
    )
    parser.add_argument(
        "-c", "--config", type=Path,
        help="YAML config file providing defaults (see config/lan_scan.yaml.example)",
    )
    parser.add_argument(
        "-t", "--timeout", type=float,
        help=f"Per-host probe timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "-w", "--workers", type=int,
        help=f"Number of concurrent probe threads (default: {DEFAULT_WORKERS})",
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
    args = parser.parse_args()

    config = load_config(args.config)

    # Precedence: commandline > environment > config file > default
    network_str = (
        args.network
        or os.environ.get("LAN_SCAN_NETWORK")
        or config.get("network")
    )
    timeout = (
        args.timeout
        if args.timeout is not None
        else float(os.environ.get("LAN_SCAN_TIMEOUT", config.get("timeout", DEFAULT_TIMEOUT)))
    )
    workers = (
        args.workers
        if args.workers is not None
        else int(os.environ.get("LAN_SCAN_WORKERS", config.get("workers", DEFAULT_WORKERS)))
    )
    ports = parse_ports(
        args.ports or os.environ.get("LAN_SCAN_PORTS") or config.get("ports")
    )
    resolve_dns = not (args.no_dns or config.get("no_dns", False))

    if network_str:
        try:
            network = ipaddress.IPv4Network(network_str, strict=False)
        except ValueError as exc:
            print(f"error: invalid network '{network_str}': {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        detected = [n for n in detect_local_networks() if not n.is_loopback]
        if not detected:
            print(
                "error: could not auto-detect a local network; specify one with --network",
                file=sys.stderr,
            )
            sys.exit(1)
        network = detected[0]
        if len(detected) > 1:
            print(
                f"note: multiple networks detected ({', '.join(str(n) for n in detected)}); "
                f"scanning {network} (use --network to pick another)",
                file=sys.stderr,
            )

    host_count = network.num_addresses - 2 if network.num_addresses > 2 else network.num_addresses
    print(f"Scanning {network} ({host_count} addresses)...", file=sys.stderr)

    results = scan_network(network, timeout, workers, ports, resolve_dns)

    if args.json:
        print_json(results)
    else:
        print_table(results)


if __name__ == "__main__":
    main()
