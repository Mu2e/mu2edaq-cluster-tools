"""Tests for local network detection (_detect_local_networks_sync)."""

import ipaddress
import subprocess
from unittest.mock import patch

import pytest

from ssh_selector import _detect_local_networks_sync


# Typical `ip addr show` output (Linux)
IP_ADDR_OUTPUT = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0
3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 10.0.0.5/16 brd 10.0.255.255 scope global eth1
"""

# Typical `ifconfig` output (macOS) with hex netmask
IFCONFIG_OUTPUT = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.1.200 netmask 0xffffff00 broadcast 192.168.1.255
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 10.0.0.10 netmask 0xffff0000 broadcast 10.0.255.255
"""

# ifconfig output with dotted-decimal netmask (some Linux ifconfig variants)
IFCONFIG_DOTTED_OUTPUT = """\
eth0      inet 172.16.0.5  netmask 255.255.0.0
"""


class TestDetectLocalNetworksLinux:
    """Uses `ip addr` path (Linux)."""

    def _run(self):
        with patch("subprocess.check_output") as mock:
            mock.return_value = IP_ADDR_OUTPUT
            return _detect_local_networks_sync(), mock

    def test_returns_list(self):
        nets, _ = self._run()
        assert isinstance(nets, list)

    def test_loopback_included(self):
        nets, _ = self._run()
        assert ipaddress.IPv4Network("127.0.0.0/8") in nets

    def test_eth0_network_detected(self):
        nets, _ = self._run()
        assert ipaddress.IPv4Network("192.168.1.0/24") in nets

    def test_eth1_network_detected(self):
        nets, _ = self._run()
        assert ipaddress.IPv4Network("10.0.0.0/16") in nets

    def test_ip_addr_called_first(self):
        _, mock = self._run()
        first_call_args = mock.call_args_list[0][0][0]
        assert first_call_args == ["ip", "addr", "show"]


class TestDetectLocalNetworksMacOS:
    """Falls back to `ifconfig` when `ip` is unavailable."""

    def _run(self, ifconfig_output=IFCONFIG_OUTPUT):
        def side_effect(cmd, **kwargs):
            if cmd == ["ip", "addr", "show"]:
                raise FileNotFoundError("ip not found")
            return ifconfig_output

        with patch("subprocess.check_output", side_effect=side_effect):
            return _detect_local_networks_sync()

    def test_returns_list(self):
        nets = self._run()
        assert isinstance(nets, list)

    def test_en0_hex_netmask_parsed(self):
        nets = self._run()
        assert ipaddress.IPv4Network("192.168.1.0/24") in nets

    def test_en1_hex_netmask_parsed(self):
        nets = self._run()
        assert ipaddress.IPv4Network("10.0.0.0/16") in nets

    def test_dotted_netmask_parsed(self):
        nets = self._run(IFCONFIG_DOTTED_OUTPUT)
        assert ipaddress.IPv4Network("172.16.0.0/16") in nets


class TestDetectLocalNetworksFailures:
    def test_both_commands_unavailable_returns_empty(self):
        with patch("subprocess.check_output", side_effect=FileNotFoundError()):
            nets = _detect_local_networks_sync()
        assert nets == []

    def test_ip_addr_fails_falls_back_to_ifconfig(self):
        def side_effect(cmd, **kwargs):
            if cmd == ["ip", "addr", "show"]:
                raise subprocess.CalledProcessError(1, cmd)
            return IFCONFIG_OUTPUT

        with patch("subprocess.check_output", side_effect=side_effect):
            nets = _detect_local_networks_sync()

        assert ipaddress.IPv4Network("192.168.1.0/24") in nets
