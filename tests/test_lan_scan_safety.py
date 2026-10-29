"""Tests for lan-scan scan bounds, probe timeouts, profiles and version (#8, #11, #12, #15).

No test sends a packet: ping, TCP, DNS and interface detection are all mocked.
"""

import ipaddress
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mu2edaq_cluster_tools import lan_scan as ls

REPO = Path(__file__).resolve().parent.parent
PROFILES = REPO / "config" / "lan-scan"

IP_ADDR_OUTPUT = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0
3: docker0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500
    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
"""

IFCONFIG_OUTPUT = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.200 netmask 0xffffff00 broadcast 192.168.1.255
"""

IPCONFIG_OUTPUT = """\
Ethernet adapter Ethernet 2:

   IPv4 Address. . . . . . . . . . . : 10.226.9.40
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
"""


@pytest.fixture
def clean_env(monkeypatch):
    """Remove any LAN_SCAN_* variables from the test environment."""
    for k in list(os.environ):
        if k.startswith("LAN_SCAN_"):
            monkeypatch.delenv(k)
    return monkeypatch


def _run(argv, interfaces=()):
    """Run main() with scanning and detection mocked; return (exit_code, scan_mock)."""
    with patch.object(ls, "scan_network", return_value=[]) as scan, \
         patch.object(ls, "detect_local_interfaces", return_value=list(interfaces)):
        try:
            ls.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, scan


def _iface(name, cidr):
    return ls.LocalInterface(name, ipaddress.IPv4Interface(cidr))


# ---------------------------------------------------------------------------
# Version: single source (#11, #15 item 10)
# ---------------------------------------------------------------------------

class TestVersion:
    def test_single_source(self):
        from mu2edaq_cluster_tools import __version__ as pkg_version
        from mu2edaq_cluster_tools import ssh_selector
        from mu2edaq_cluster_tools._version import __version__ as src_version
        assert ls.__version__ == pkg_version == src_version == ssh_selector.__version__

    def test_lan_scan_version_flag(self, capsys):
        from mu2edaq_cluster_tools._version import __version__
        with pytest.raises(SystemExit) as exc:
            ls.main(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == f"lan-scan {__version__}"

    def test_no_other_version_literal(self):
        src = REPO / "src" / "mu2edaq_cluster_tools"
        hits = [p.name for p in src.glob("*.py")
                if any(line.startswith("__version__ =") for line in p.read_text().splitlines())]
        assert hits == ["_version.py"]

    def test_pyproject_points_at_version_module(self):
        text = (REPO / "pyproject.toml").read_text()
        assert 'attr = "mu2edaq_cluster_tools._version.__version__"' in text

    def test_lan_scan_import_does_not_load_textual(self):
        code = (
            "import sys, mu2edaq_cluster_tools.lan_scan; "
            "sys.exit(1 if any(m == 'textual' or m.startswith('textual.') "
            "for m in sys.modules) else 0)"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# Profiles live in config/lan-scan/ (#15 item 6, #8)
# ---------------------------------------------------------------------------

class TestProfiles:
    def test_no_lan_scan_profiles_in_config_root(self):
        assert not list((REPO / "config").glob("lan_scan*"))

    def test_profiles_present_and_valid(self):
        profiles = sorted(PROFILES.glob("lan_scan*"))
        assert len(profiles) == 4
        for p in profiles:
            cfg = ls.load_config(p)
            ipaddress.IPv4Network(cfg["network"])

    def test_routable_profile_does_not_self_authorise(self):
        cfg = ls.load_config(PROFILES / "lan_scan-mu2edaq.yaml")
        assert ipaddress.IPv4Network(cfg["network"]).is_global
        assert not cfg.get("allow_routable")
        assert not cfg.get("ports")


# ---------------------------------------------------------------------------
# Ping timeout units per platform (#12)
# ---------------------------------------------------------------------------

class TestPingCommand:
    def test_linux_seconds(self):
        cmd, wait = ls.ping_command("10.0.0.1", 1.0, "Linux")
        assert cmd == ["ping", "-c", "1", "-W", "1", "10.0.0.1"]
        assert wait == 1.0

    def test_linux_rounds_up(self):
        cmd, wait = ls.ping_command("10.0.0.1", 2.3, "Linux")
        assert cmd[cmd.index("-W") + 1] == "3" and wait == 3.0

    def test_linux_subsecond_is_one_second(self):
        cmd, wait = ls.ping_command("10.0.0.1", 0.2, "Linux")
        assert cmd[cmd.index("-W") + 1] == "1" and wait == 1.0

    def test_macos_milliseconds(self):
        cmd, wait = ls.ping_command("10.0.0.1", 0.25, "Darwin")
        assert cmd == ["ping", "-c", "1", "-W", "250", "10.0.0.1"]
        assert wait == 0.25

    def test_windows_milliseconds(self):
        cmd, wait = ls.ping_command("10.0.0.1", 1.5, "Windows")
        assert cmd == ["ping", "-n", "1", "-w", "1500", "10.0.0.1"]
        assert wait == 1.5

    def test_subprocess_kill_tracks_requested_wait(self):
        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=1)
            ls.ping("10.0.0.1", 1.0)
        assert mock.call_args[1]["timeout"] == pytest.approx(1.0 + ls.PING_GRACE)


# ---------------------------------------------------------------------------
# Reverse DNS timeout is per lookup (#12)
# ---------------------------------------------------------------------------

class TestReverseDnsTimeout:
    def test_slow_lookup_is_bounded(self):
        release = threading.Event()

        def slow(ip):
            release.wait(5)
            return ("late.example.com", [], [ip])

        try:
            with patch("socket.gethostbyaddr", side_effect=slow):
                t0 = time.monotonic()
                assert ls.reverse_dns("10.0.0.1", 0.1) is None
                assert time.monotonic() - t0 < 1.0
        finally:
            release.set()

    def test_does_not_touch_global_socket_timeout(self):
        with patch("socket.setdefaulttimeout") as sdt, \
             patch("socket.gethostbyaddr", return_value=("h.example.com", [], [])):
            assert ls.reverse_dns("10.0.0.1", 0.5) == "h.example.com"
        sdt.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limiting, concurrency and lazy address generation (#8)
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_spacing(self):
        sleeps = []
        rl = ls.RateLimiter(10.0, clock=lambda: 100.0, sleep=sleeps.append)
        for _ in range(4):
            rl.acquire()
        assert sleeps == pytest.approx([0.1, 0.2, 0.3])

    def test_no_backlog_after_idle(self):
        now = [100.0]
        sleeps = []
        rl = ls.RateLimiter(10.0, clock=lambda: now[0], sleep=sleeps.append)
        rl.acquire()
        now[0] = 200.0
        rl.acquire()
        assert sleeps == []

    def test_disabled(self):
        sleeps = []
        rl = ls.RateLimiter(0, sleep=sleeps.append)
        for _ in range(5):
            rl.acquire()
        assert sleeps == []


class TestScanBounds:
    def test_concurrency_never_exceeds_workers(self):
        active = [0]
        peak = [0]
        lock = threading.Lock()

        def fake_ping(ip, timeout):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.005)
            with lock:
                active[0] -= 1
            return False

        net = ipaddress.IPv4Network("10.0.0.0/26")
        with patch.object(ls, "ping", side_effect=fake_ping):
            res = ls.scan_network(net, 1.0, workers=3, ports=[], resolve_dns=False, rate=0)
        assert res == []
        assert 1 <= peak[0] <= 3

    def test_every_host_probed_once(self):
        seen = []
        lock = threading.Lock()

        def fake_ping(ip, timeout):
            with lock:
                seen.append(ip)
            return ip.endswith(".7")

        net = ipaddress.IPv4Network("10.0.0.0/27")
        with patch.object(ls, "ping", side_effect=fake_ping):
            res = ls.scan_network(net, 1.0, workers=4, ports=[], resolve_dns=False, rate=0)
        assert sorted(seen) == sorted(str(a) for a in net.hosts())
        assert [r.ip for r in res] == ["10.0.0.7"]

    def test_rate_limiter_applied(self):
        net = ipaddress.IPv4Network("10.0.0.0/29")
        with patch.object(ls, "ping", return_value=False), \
             patch.object(ls.RateLimiter, "acquire") as acq:
            ls.scan_network(net, 1.0, workers=2, ports=[], resolve_dns=False, rate=5)
        assert acq.call_count == 6

    def test_slash32_and_slash31(self):
        assert list(ls.iter_addresses(ipaddress.IPv4Network("10.0.0.5/32"))) == \
            [ipaddress.IPv4Address("10.0.0.5")]
        assert len(list(ls.iter_addresses(ipaddress.IPv4Network("10.0.0.4/31")))) == 2

    def test_host_count(self):
        assert ls.host_count(ipaddress.IPv4Network("10.0.0.0/22")) == 1022
        assert ls.host_count(ipaddress.IPv4Network("10.0.0.0/21")) == 2046
        assert ls.host_count(ipaddress.IPv4Network("10.0.0.0/32")) == 1


# ---------------------------------------------------------------------------
# Auto-detection is deterministic and explicit (#8)
# ---------------------------------------------------------------------------

class TestAutoDetect:
    def test_interface_names_linux(self):
        with patch("subprocess.check_output", return_value=IP_ADDR_OUTPUT):
            found = ls.detect_local_interfaces()
        pairs = [(i.name, str(i.network)) for i in found]
        assert ("eth0", "192.168.1.0/24") in pairs
        assert ("docker0", "172.17.0.0/16") in pairs

    def test_interface_names_macos(self):
        def side_effect(cmd, **kwargs):
            if cmd[0] == "ip":
                raise FileNotFoundError
            return IFCONFIG_OUTPUT
        with patch("subprocess.check_output", side_effect=side_effect):
            found = ls.detect_local_interfaces()
        assert ("en0", "192.168.1.0/24") in [(i.name, str(i.network)) for i in found]

    def test_interface_names_windows(self):
        def side_effect(cmd, **kwargs):
            if cmd[0] in ("ip", "ifconfig"):
                raise FileNotFoundError
            return IPCONFIG_OUTPUT
        with patch("subprocess.check_output", side_effect=side_effect):
            found = ls.detect_local_interfaces()
        assert [(i.name, str(i.network)) for i in found] == [("Ethernet 2", "10.226.9.0/24")]

    def test_scannable_is_sorted_filtered_and_order_independent(self):
        ifs = [_iface("docker0", "172.17.0.1/16"), _iface("lo", "127.0.0.1/8"),
               _iface("eth1", "10.226.9.6/24"), _iface("eth9", "169.254.1.1/16"),
               _iface("eth0", "10.226.9.5/24")]
        out = ls.scannable_interfaces(ifs)
        assert [(i.name, str(i.network)) for i in out] == \
            [("eth0", "10.226.9.0/24"), ("docker0", "172.17.0.0/16")]
        assert ls.scannable_interfaces(list(reversed(ifs))) == out

    def test_multiple_networks_refused(self, clean_env, capsys):
        ifs = [_iface("eth0", "10.226.9.5/24"), _iface("eth1", "192.168.246.5/24")]
        code, scan = _run([], interfaces=ifs)
        assert code == ls.EXIT_NO_NETWORK
        scan.assert_not_called()
        err = capsys.readouterr().err
        assert "10.226.9.0/24" in err and "192.168.246.0/24" in err and "eth1" in err

    def test_single_network_is_named(self, clean_env, capsys):
        ifs = [_iface("lo", "127.0.0.1/8"), _iface("eth0", "10.226.9.5/24")]
        code, scan = _run(["--no-dns"], interfaces=ifs)
        assert code == 0
        assert scan.call_args[0][0] == ipaddress.IPv4Network("10.226.9.0/24")
        assert "eth0" in capsys.readouterr().err

    def test_nothing_detected(self, clean_env):
        code, scan = _run([], interfaces=[_iface("lo", "127.0.0.1/8")])
        assert code == ls.EXIT_NO_NETWORK and not scan.called

    def test_autodetected_large_network_still_bounded(self, clean_env):
        code, scan = _run([], interfaces=[_iface("docker0", "172.17.0.1/16")])
        assert code == ls.EXIT_REFUSED and not scan.called


# ---------------------------------------------------------------------------
# Size and routable guards (#8)
# ---------------------------------------------------------------------------

class TestGuards:
    def test_slash22_allowed(self, clean_env):
        code, scan = _run(["-n", "10.0.0.0/22"])
        assert code == 0 and scan.called

    def test_slash21_refused(self, clean_env, capsys):
        code, scan = _run(["-n", "10.0.0.0/21"])
        assert code == ls.EXIT_REFUSED and not scan.called
        assert "--allow-large" in capsys.readouterr().err

    def test_slash16_typo_refused(self, clean_env):
        code, scan = _run(["-n", "10.226.0.0/16"])
        assert code == ls.EXIT_REFUSED and not scan.called

    def test_allow_large(self, clean_env):
        code, scan = _run(["-n", "10.0.0.0/21", "--allow-large"])
        assert code == 0 and scan.called

    def test_max_hosts_lowered(self, clean_env):
        code, _ = _run(["-n", "10.0.0.0/24", "--max-hosts", "100"])
        assert code == ls.EXIT_REFUSED

    def test_max_hosts_env(self, clean_env):
        clean_env.setenv("LAN_SCAN_MAX_HOSTS", "4096")
        code, scan = _run(["-n", "10.0.0.0/21"])
        assert code == 0 and scan.called

    def test_allow_large_env(self, clean_env):
        clean_env.setenv("LAN_SCAN_ALLOW_LARGE", "1")
        code, _ = _run(["-n", "10.0.0.0/21"])
        assert code == 0

    def test_routable_refused(self, clean_env, capsys):
        code, scan = _run(["-n", "131.225.245.0/24"])
        assert code == ls.EXIT_REFUSED and not scan.called
        assert "--allow-routable" in capsys.readouterr().err

    def test_routable_allowed(self, clean_env):
        code, scan = _run(["-n", "131.225.245.0/24", "--allow-routable"])
        assert code == 0 and scan.called

    def test_shipped_routable_profile(self, clean_env):
        prof = str(PROFILES / "lan_scan-mu2edaq.yaml")
        code, scan = _run(["-c", prof])
        assert code == ls.EXIT_REFUSED and not scan.called
        code, scan = _run(["-c", prof, "--allow-routable"])
        assert code == 0
        args, kwargs = scan.call_args
        assert args[2] == 16 and args[3] == [] and kwargs["rate"] == 20

    @pytest.mark.parametrize("name", ["lan_scan-mu2edaq-data.yaml", "lan_scan-mu2edaq-ipmi.yaml"])
    def test_private_profiles_accepted(self, clean_env, name):
        code, scan = _run(["-c", str(PROFILES / name)])
        assert code == 0 and scan.called


# ---------------------------------------------------------------------------
# Precedence: commandline > environment > config file > default
# ---------------------------------------------------------------------------

class TestPrecedence:
    def _cfg(self, tmp_path, text):
        p = tmp_path / "scan.yaml"
        p.write_text(text)
        return str(p)

    def test_cli_over_env_over_config(self, clean_env, tmp_path):
        cfg = self._cfg(tmp_path, "network: 10.1.0.0/24\nworkers: 8\nrate: 5\ntimeout: 2\n")
        clean_env.setenv("LAN_SCAN_WORKERS", "12")
        clean_env.setenv("LAN_SCAN_RATE", "7")
        code, scan = _run(["-c", cfg, "-r", "9"])
        assert code == 0
        args, kwargs = scan.call_args
        assert args[0] == ipaddress.IPv4Network("10.1.0.0/24")
        assert args[1] == 2.0        # config
        assert args[2] == 12         # env over config
        assert kwargs["rate"] == 9   # commandline over env

    def test_defaults(self, clean_env):
        code, scan = _run(["-n", "10.0.0.0/24"])
        assert code == 0
        args, kwargs = scan.call_args
        assert args[1] == ls.DEFAULT_TIMEOUT and args[2] == ls.DEFAULT_WORKERS
        assert args[3] == [] and args[4] is True and kwargs["rate"] == ls.DEFAULT_RATE

    def test_no_dns_env(self, clean_env):
        clean_env.setenv("LAN_SCAN_NO_DNS", "true")
        _, scan = _run(["-n", "10.0.0.0/24"])
        assert scan.call_args[0][4] is False

    def test_config_no_dns_overridden_by_env(self, clean_env, tmp_path):
        cfg = self._cfg(tmp_path, "no_dns: true\n")
        clean_env.setenv("LAN_SCAN_NO_DNS", "0")
        _, scan = _run(["-c", cfg, "-n", "10.0.0.0/24"])
        assert scan.call_args[0][4] is True

    def test_empty_env_falls_through(self, clean_env, tmp_path):
        cfg = self._cfg(tmp_path, "workers: 8\n")
        clean_env.setenv("LAN_SCAN_WORKERS", "")
        _, scan = _run(["-c", cfg, "-n", "10.0.0.0/24"])
        assert scan.call_args[0][2] == 8

    @pytest.mark.parametrize("argv,env", [
        (["-n", "10.0.0.0/24", "-w", "0"], {}),
        (["-n", "10.0.0.0/24", "-w", "999"], {}),
        (["-n", "10.0.0.0/24", "-r", "-1"], {}),
        (["-n", "10.0.0.0/24", "-t", "0"], {}),
        (["-n", "10.0.0.0/24", "-p", "22,99999"], {}),
        (["-n", "not-a-net"], {}),
        (["-n", "10.0.0.0/24"], {"LAN_SCAN_TIMEOUT": "abc"}),
        (["-n", "10.0.0.0/24"], {"LAN_SCAN_ALLOW_LARGE": "maybe"}),
    ])
    def test_bad_values_exit_2(self, clean_env, capsys, argv, env):
        for k, v in env.items():
            clean_env.setenv(k, v)
        code, scan = _run(argv)
        assert code == ls.EXIT_USAGE and not scan.called
        assert "error:" in capsys.readouterr().err

    def test_missing_config_exit_2(self, clean_env, tmp_path):
        code, _ = _run(["-c", str(tmp_path / "nope.yaml"), "-n", "10.0.0.0/24"])
        assert code == ls.EXIT_USAGE
