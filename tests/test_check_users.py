"""Tests for the "Check Users" fan-out limit (GitHub issue #7)."""

import asyncio

import pytest

from mu2edaq_cluster_tools import ssh_selector
from mu2edaq_cluster_tools.ssh_selector import (
    CHECK_CONCURRENCY_ENV,
    DEFAULT_CHECK_CONCURRENCY,
    Config,
    Host,
    SSHSelector,
    load_config,
    resolve_check_concurrency,
)

from .conftest import write_yaml


def _hosts(n):
    return [Host(hostname=f"node{i:03d}.example.com", proxy_jump="gw.example.com") for i in range(n)]


def _app(n_hosts, concurrency=DEFAULT_CHECK_CONCURRENCY):
    cfg = Config(hosts=_hosts(n_hosts), grouplist=[], check_concurrency=concurrency)
    app = SSHSelector(cfg)
    app._show_user_count = lambda hostname, count: None   # no DOM outside a running app
    return app


# ---------------------------------------------------------------------------
# Configuration: CLI > environment > YAML > default
# ---------------------------------------------------------------------------

class TestCheckConcurrencyConfig:
    def test_default_is_below_sshd_maxstartups(self):
        assert DEFAULT_CHECK_CONCURRENCY == 8
        assert Config(hosts=[], grouplist=[]).check_concurrency == DEFAULT_CHECK_CONCURRENCY

    def test_yaml_default(self, minimal_config):
        assert load_config(minimal_config).check_concurrency == DEFAULT_CHECK_CONCURRENCY

    def test_yaml_value(self, tmp_path):
        p = write_yaml(tmp_path, """
            check_concurrency: 3
            hosts:
              - hostname: a.example.com
        """)
        assert load_config(p).check_concurrency == 3

    @pytest.mark.parametrize("bad", [0, -2, "many"])
    def test_yaml_invalid_rejected(self, tmp_path, bad):
        p = write_yaml(tmp_path, f"""
            check_concurrency: {bad}
            hosts:
              - hostname: a.example.com
        """)
        with pytest.raises(ValueError, match="check_concurrency"):
            load_config(p)

    def test_config_used_when_nothing_else(self):
        assert resolve_check_concurrency(None, {}, 5) == 5

    def test_env_overrides_config(self):
        assert resolve_check_concurrency(None, {CHECK_CONCURRENCY_ENV: "2"}, 5) == 2

    def test_blank_env_ignored(self):
        assert resolve_check_concurrency(None, {CHECK_CONCURRENCY_ENV: " "}, 5) == 5

    def test_cli_overrides_env_and_config(self):
        assert resolve_check_concurrency(4, {CHECK_CONCURRENCY_ENV: "2"}, 5) == 4

    def test_invalid_env_names_variable(self):
        with pytest.raises(ValueError, match=CHECK_CONCURRENCY_ENV):
            resolve_check_concurrency(None, {CHECK_CONCURRENCY_ENV: "0"}, 5)

    def test_invalid_cli_names_option(self):
        with pytest.raises(ValueError, match="--check-concurrency"):
            resolve_check_concurrency(0, {}, 5)

    def test_app_takes_limit_from_config(self):
        assert _app(1, concurrency=3)._check_concurrency == 3


# ---------------------------------------------------------------------------
# Fan-out is bounded
# ---------------------------------------------------------------------------

class TestCheckUsersFanOut:
    @pytest.mark.parametrize("limit", [1, 3, 8])
    def test_never_more_than_limit_in_flight(self, limit):
        app = _app(40, concurrency=limit)
        state = {"now": 0, "peak": 0, "calls": 0}

        async def fake_probe(host):
            state["now"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.002)
            state["now"] -= 1
            return 1

        app._probe_user_count = fake_probe
        asyncio.run(app._check_all_user_counts())
        assert state["calls"] == 40
        assert state["peak"] == limit
        assert len(app._user_counts) == 40

    def test_fresh_hosts_are_not_probed(self):
        app = _app(5)
        probed = []

        async def fake_probe(host):
            probed.append(host.hostname)
            return 0

        app._user_counts["node000.example.com"] = (2, ssh_selector.time.monotonic())
        app._probe_user_count = fake_probe
        asyncio.run(app._check_all_user_counts())
        assert "node000.example.com" not in probed
        assert len(probed) == 4

    def test_repeated_press_does_not_duplicate_probes(self):
        app = _app(6, concurrency=2)
        probed = []

        async def fake_probe(host):
            probed.append(host.hostname)
            await asyncio.sleep(0.005)
            return 0

        app._probe_user_count = fake_probe

        async def press_twice():
            await asyncio.gather(app._check_all_user_counts(), app._check_all_user_counts())

        asyncio.run(press_twice())
        assert sorted(probed) == sorted(h.hostname for h in app.all_hosts)


# ---------------------------------------------------------------------------
# Per-host probe: timeout and failure handling
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._rc = returncode
        self._hang = hang
        self.returncode = None
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(10)
        self.returncode = self._rc
        return self._stdout, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class TestProbe:
    def _run(self, monkeypatch, proc):
        captured = {}

        async def fake_exec(*cmd, **kw):
            captured["cmd"] = cmd
            return proc

        monkeypatch.setattr(ssh_selector.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(ssh_selector, "CHECK_USERS_TIMEOUT", 0.05)
        app = _app(1)
        result = asyncio.run(app._probe_user_count(app.all_hosts[0]))
        return result, captured["cmd"]

    def test_counts_who_lines(self, monkeypatch):
        result, cmd = self._run(monkeypatch, _FakeProc(b"a pts/0\nb pts/1\n\n"))
        assert result == 2
        assert cmd[1:5] == ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")
        assert cmd[-1] == "who"

    def test_nonzero_exit_is_unreachable(self, monkeypatch):
        result, _ = self._run(monkeypatch, _FakeProc(b"", returncode=255))
        assert result is None

    def test_timeout_kills_ssh(self, monkeypatch):
        proc = _FakeProc(hang=True)
        result, _ = self._run(monkeypatch, proc)
        assert result is None
        assert proc.killed
