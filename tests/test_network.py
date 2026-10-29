"""!
@file test_network.py
@brief Tests for ssh-selector local network detection (_detect_local_networks_sync).

subprocess.check_output is mocked with canned `ip addr show` (Linux) and
`ifconfig` (macOS hex netmask, Linux dotted netmask) outputs, so no
interface is queried.
"""

import ipaddress
import subprocess
from unittest.mock import patch

import pytest

from mu2edaq_cluster_tools.ssh_selector import _detect_local_networks_sync


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
    """!
    @brief Uses `ip addr` path (Linux).
    """

    def _run(self):
        """!
        @brief Run detection with `ip addr show` returning IP_ADDR_OUTPUT.
        @return Tuple (networks, check_output mock).
        """
        with patch("subprocess.check_output") as mock:
            mock.return_value = IP_ADDR_OUTPUT
            return _detect_local_networks_sync(), mock

    def test_returns_list(self):
        """!
        @brief The result is a list.
        """
        nets, _ = self._run()
        assert isinstance(nets, list)

    def test_loopback_included(self):
        """!
        @brief 127.0.0.0/8 is included (no filtering at this layer).
        """
        nets, _ = self._run()
        assert ipaddress.IPv4Network("127.0.0.0/8") in nets

    def test_eth0_network_detected(self):
        """!
        @brief eth0's /24 is detected.
        """
        nets, _ = self._run()
        assert ipaddress.IPv4Network("192.168.1.0/24") in nets

    def test_eth1_network_detected(self):
        """!
        @brief eth1's /16 is detected.
        """
        nets, _ = self._run()
        assert ipaddress.IPv4Network("10.0.0.0/16") in nets

    def test_ip_addr_called_first(self):
        """!
        @brief `ip addr show` is tried before ifconfig.
        """
        _, mock = self._run()
        first_call_args = mock.call_args_list[0][0][0]
        assert first_call_args == ["ip", "addr", "show"]


class TestDetectLocalNetworksMacOS:
    """!
    @brief Falls back to `ifconfig` when `ip` is unavailable.
    """

    def _run(self, ifconfig_output=IFCONFIG_OUTPUT):
        """!
        @brief Run detection with `ip` missing and ifconfig returning the given text.
        @param ifconfig_output  Canned ifconfig output.
        @return List of detected networks.
        """
        # Simulate macOS: no `ip` binary, so detection must fall back to ifconfig.
        def side_effect(cmd, **kwargs):
            if cmd == ["ip", "addr", "show"]:
                raise FileNotFoundError("ip not found")
            return ifconfig_output

        with patch("subprocess.check_output", side_effect=side_effect):
            return _detect_local_networks_sync()

    def test_returns_list(self):
        """!
        @brief The result is a list.
        """
        nets = self._run()
        assert isinstance(nets, list)

    def test_en0_hex_netmask_parsed(self):
        """!
        @brief Hex netmask 0xffffff00 yields a /24.
        """
        nets = self._run()
        assert ipaddress.IPv4Network("192.168.1.0/24") in nets

    def test_en1_hex_netmask_parsed(self):
        """!
        @brief Hex netmask 0xffff0000 yields a /16.
        """
        nets = self._run()
        assert ipaddress.IPv4Network("10.0.0.0/16") in nets

    def test_dotted_netmask_parsed(self):
        """!
        @brief A dotted-decimal netmask is also accepted.
        """
        nets = self._run(IFCONFIG_DOTTED_OUTPUT)
        assert ipaddress.IPv4Network("172.16.0.0/16") in nets


class TestDetectLocalNetworksFailures:
    """!
    @brief Behaviour when the interface commands fail.
    """
    def test_both_commands_unavailable_returns_empty(self):
        """!
        @brief With neither ip nor ifconfig present the result is empty, not an exception.
        """
        with patch("subprocess.check_output", side_effect=FileNotFoundError()):
            nets = _detect_local_networks_sync()
        assert nets == []

    def test_ip_addr_fails_falls_back_to_ifconfig(self):
        """!
        @brief A failing `ip addr` (non-zero exit) falls back to ifconfig.
        """
        # `ip` exists but fails; detection must still fall back to ifconfig.
        def side_effect(cmd, **kwargs):
            if cmd == ["ip", "addr", "show"]:
                raise subprocess.CalledProcessError(1, cmd)
            return IFCONFIG_OUTPUT

        with patch("subprocess.check_output", side_effect=side_effect):
            nets = _detect_local_networks_sync()

        assert ipaddress.IPv4Network("192.168.1.0/24") in nets
