"""!
@file test_lan_scan.py
@brief Unit tests for lan-scan probes and output: network detection, ping, TCP probe, reverse DNS, scan_network, parse_ports and print_table.

All system access is mocked (subprocess.check_output, subprocess.run,
socket.create_connection, socket.gethostbyaddr); no packet is sent. Scan
bounds, timeouts and guards are tested in test_lan_scan_safety.py.
"""

import ipaddress
import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from mu2edaq_cluster_tools.lan_scan import (
    ScanResult,
    detect_local_networks,
    parse_ports,
    ping,
    print_table,
    reverse_dns,
    scan_network,
    tcp_probe,
)


# ---------------------------------------------------------------------------
# detect_local_networks
# ---------------------------------------------------------------------------

IP_ADDR_OUTPUT = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0
"""

IFCONFIG_OUTPUT = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.1.200 netmask 0xffffff00 broadcast 192.168.1.255
"""

IPCONFIG_OUTPUT = """\
Wireless LAN adapter Wi-Fi:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 192.168.1.50
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.1.1
"""


class TestDetectLocalNetworksLinux:
    """!
    @brief detect_local_networks() via `ip addr show`.
    """
    def _run(self):
        """!
        @brief Run detection with `ip addr show` returning IP_ADDR_OUTPUT.
        @return List of detected networks.
        """
        with patch("subprocess.check_output") as mock:
            mock.return_value = IP_ADDR_OUTPUT
            return detect_local_networks()

    def test_returns_list(self):
        """!
        @brief The result is a list.
        """
        assert isinstance(self._run(), list)

    def test_eth0_network_detected(self):
        """!
        @brief eth0's /24 is detected.
        """
        assert ipaddress.IPv4Network("192.168.1.0/24") in self._run()


class TestDetectLocalNetworksMacOS:
    """!
    @brief detect_local_networks() falls back to ifconfig.
    """
    def _run(self):
        """!
        @brief Run detection with `ip` missing and ifconfig returning IFCONFIG_OUTPUT.
        @return List of detected networks.
        """
        def side_effect(cmd, **kwargs):
            if cmd == ["ip", "addr", "show"]:
                raise FileNotFoundError("ip not found")
            return IFCONFIG_OUTPUT

        with patch("subprocess.check_output", side_effect=side_effect):
            return detect_local_networks()

    def test_en0_hex_netmask_parsed(self):
        """!
        @brief A hex netmask yields the right /24.
        """
        assert ipaddress.IPv4Network("192.168.1.0/24") in self._run()


class TestDetectLocalNetworksWindows:
    """!
    @brief detect_local_networks() falls back to ipconfig.
    """
    def _run(self):
        """!
        @brief Run detection with ip and ifconfig missing and ipconfig returning IPCONFIG_OUTPUT.
        @return List of detected networks.
        """
        # Simulate Windows: neither ip nor ifconfig exists, so ipconfig output is parsed.
        def side_effect(cmd, **kwargs):
            if cmd in (["ip", "addr", "show"], ["ifconfig"]):
                raise FileNotFoundError("not found")
            return IPCONFIG_OUTPUT

        with patch("subprocess.check_output", side_effect=side_effect):
            return detect_local_networks()

    def test_ipconfig_parsed(self):
        """!
        @brief The IPv4 address and subnet mask from ipconfig yield the right /24.
        """
        assert ipaddress.IPv4Network("192.168.1.0/24") in self._run()


class TestDetectLocalNetworksFailures:
    """!
    @brief Behaviour when no interface command is available.
    """
    def test_all_commands_unavailable_returns_empty(self):
        """!
        @brief With no command available the result is empty, not an exception.
        """
        with patch("subprocess.check_output", side_effect=FileNotFoundError()):
            assert detect_local_networks() == []


# ---------------------------------------------------------------------------
# ping / tcp_probe / reverse_dns
# ---------------------------------------------------------------------------

class TestPing:
    """!
    @brief ping() wraps the system ping and maps outcomes to bool.
    """
    def test_success_returns_true(self):
        """!
        @brief Exit status 0 means alive.
        """
        with patch("subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0)
            assert ping("192.168.1.1", 1.0) is True

    def test_nonzero_returncode_returns_false(self):
        """!
        @brief A non-zero exit status means not alive.
        """
        with patch("subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=1)
            assert ping("192.168.1.1", 1.0) is False

    def test_timeout_returns_false(self):
        """!
        @brief A subprocess timeout means not alive.
        """
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ping", 1.0)):
            assert ping("192.168.1.1", 1.0) is False

    def test_missing_binary_returns_false(self):
        """!
        @brief A missing ping binary means not alive, not an exception.
        """
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert ping("192.168.1.1", 1.0) is False

    def test_windows_uses_dash_n(self):
        """!
        @brief On Windows the argv uses `-n` and `-w`.
        """
        with patch("platform.system", return_value="Windows"), \
             patch("subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0)
            ping("192.168.1.1", 1.0)
            cmd = mock.call_args[0][0]
            assert "-n" in cmd and "-w" in cmd

    def test_posix_uses_dash_c(self):
        """!
        @brief On POSIX the argv uses `-c` and `-W`.
        """
        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0)
            ping("192.168.1.1", 1.0)
            cmd = mock.call_args[0][0]
            assert "-c" in cmd and "-W" in cmd


class TestTcpProbe:
    """!
    @brief tcp_probe() connect fallback.
    """
    def test_connect_success_returns_true(self):
        """!
        @brief A successful connect on any port means alive.
        """
        with patch("socket.create_connection") as mock:
            # create_connection is used as a context manager; make the mock support `with`.
            mock.return_value.__enter__ = MagicMock()
            mock.return_value.__exit__ = MagicMock(return_value=False)
            assert tcp_probe("192.168.1.1", [22, 80], 1.0) is True

    def test_all_ports_refused_returns_false(self):
        """!
        @brief All connects failing means not alive.
        """
        with patch("socket.create_connection", side_effect=OSError()):
            assert tcp_probe("192.168.1.1", [22, 80], 1.0) is False

    def test_no_ports_returns_false(self):
        """!
        @brief An empty port list means not alive without any connect.
        """
        assert tcp_probe("192.168.1.1", [], 1.0) is False


class TestReverseDns:
    """!
    @brief reverse_dns() result mapping.
    """
    def test_resolves_hostname(self):
        """!
        @brief A PTR answer returns the hostname.
        """
        with patch("socket.gethostbyaddr", return_value=("host.example.com", [], ["192.168.1.1"])):
            assert reverse_dns("192.168.1.1", 1.0) == "host.example.com"

    def test_unresolvable_returns_none(self):
        """!
        @brief socket.herror yields None.
        """
        with patch("socket.gethostbyaddr", side_effect=socket.herror()):
            assert reverse_dns("192.168.1.1", 1.0) is None


# ---------------------------------------------------------------------------
# scan_network
# ---------------------------------------------------------------------------

class TestScanNetwork:
    """!
    @brief scan_network() result assembly with ping, TCP and DNS mocked.
    """
    def test_only_responding_hosts_returned(self):
        """!
        @brief Only hosts that answer appear, with their resolved name.
        """
        # Only .1 answers ping; reverse DNS is mocked so no lookup leaves the host.
        network = ipaddress.IPv4Network("192.168.1.0/30")  # .1, .2 usable

        def fake_ping(ip, timeout):
            return ip == "192.168.1.1"

        with patch("mu2edaq_cluster_tools.lan_scan.ping", side_effect=fake_ping), \
             patch("mu2edaq_cluster_tools.lan_scan.reverse_dns", return_value="host1.example.com"):
            results = scan_network(network, timeout=1.0, workers=4, ports=[], resolve_dns=True)

        assert len(results) == 1
        assert results[0] == ScanResult(ip="192.168.1.1", hostname="host1.example.com")

    def test_no_dns_skips_lookup(self):
        """!
        @brief resolve_dns=False performs no lookups and leaves hostnames None.
        """
        network = ipaddress.IPv4Network("192.168.1.0/30")
        with patch("mu2edaq_cluster_tools.lan_scan.ping", return_value=True), \
             patch("mu2edaq_cluster_tools.lan_scan.reverse_dns") as mock_dns:
            results = scan_network(network, timeout=1.0, workers=4, ports=[], resolve_dns=False)

        mock_dns.assert_not_called()
        assert all(r.hostname is None for r in results)

    def test_tcp_fallback_used_when_ping_fails(self):
        """!
        @brief Hosts that ignore ping but accept TCP are reported.
        """
        network = ipaddress.IPv4Network("192.168.1.0/30")
        with patch("mu2edaq_cluster_tools.lan_scan.ping", return_value=False), \
             patch("mu2edaq_cluster_tools.lan_scan.tcp_probe", return_value=True), \
             patch("mu2edaq_cluster_tools.lan_scan.reverse_dns", return_value=None):
            results = scan_network(network, timeout=1.0, workers=4, ports=[22], resolve_dns=True)

        assert len(results) == 2


# ---------------------------------------------------------------------------
# parse_ports / print_table
# ---------------------------------------------------------------------------

class TestParsePorts:
    """!
    @brief parse_ports() input forms.
    """
    def test_none_returns_empty(self):
        """!
        @brief None yields an empty list.
        """
        assert parse_ports(None) == []

    def test_csv_string_parsed(self):
        """!
        @brief A comma-separated string with spaces is parsed to ints.
        """
        assert parse_ports("22, 80,443") == [22, 80, 443]

    def test_list_passed_through(self):
        """!
        @brief A list of ints is returned unchanged.
        """
        assert parse_ports([22, 80]) == [22, 80]


class TestPrintTable:
    """!
    @brief print_table() text output.
    """
    def test_empty_results(self, capsys):
        """!
        @brief No results prints `No hosts found.`.
        """
        print_table([])
        assert "No hosts found." in capsys.readouterr().out

    def test_table_contains_ip_and_hostname(self, capsys):
        """!
        @brief Each row shows IP and hostname.
        """
        print_table([ScanResult(ip="192.168.1.1", hostname="host1.example.com")])
        out = capsys.readouterr().out
        assert "192.168.1.1" in out
        assert "host1.example.com" in out

    def test_missing_hostname_shown_as_dash(self, capsys):
        """!
        @brief A missing hostname is shown as `-`.
        """
        print_table([ScanResult(ip="192.168.1.1", hostname=None)])
        out = capsys.readouterr().out
        assert "-" in out
