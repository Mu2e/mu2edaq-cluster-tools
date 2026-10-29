"""Tests for background tunnel lifetime and local-port checks (GitHub issue #9)."""

import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest

from mu2edaq_cluster_tools import ssh_selector
from mu2edaq_cluster_tools.ssh_selector import (
    ActiveTunnel,
    Config,
    Host,
    QuitTunnelsModal,
    SSHErrorModal,
    SSHSelector,
    TunnelRegistry,
    check_local_port,
)


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def host():
    return Host(hostname="localhost", nickname="Local")


@pytest.fixture
def registry():
    reg = TunnelRegistry()
    yield reg
    reg.terminate_all()


def _spawn(host, local_port, cmd=None):
    proc = subprocess.Popen(cmd or SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ActiveTunnel(
        host=host, user="default", local_port=local_port, remote_port=80,
        process=proc, started_at=time.monotonic(),
    )


def _app(host, registry):
    return SSHSelector(Config(hosts=[host], grouplist=[]), tunnels=registry)


# ---------------------------------------------------------------------------
# ssh command and local port probe
# ---------------------------------------------------------------------------

class TestTunnelCommand:
    def test_exit_on_forward_failure(self, host):
        cmd = host.tunnel_command("alice", 8080, 9090)
        i = cmd.index("ExitOnForwardFailure=yes")
        assert cmd[i - 1] == "-o"
        assert cmd.index("-o") < cmd.index("-L")


class TestCheckLocalPort:
    def test_listening_port_is_reported(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            reason = check_local_port(port)
        finally:
            srv.close()
        assert reason is not None
        assert str(port) in reason and "in use" in reason

    def test_free_port_is_ok(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert check_local_port(port) is None


# ---------------------------------------------------------------------------
# TunnelRegistry
# ---------------------------------------------------------------------------

class TestTunnelRegistry:
    def test_find_local_port_only_live(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)
        assert registry.find_local_port(5001) is t
        assert registry.find_local_port(5002) is None
        t.process.kill()
        t.process.wait()
        assert registry.find_local_port(5001) is None

    def test_reap_dead(self, host, registry):
        live = _spawn(host, 5001)
        dead = _spawn(host, 5002, [sys.executable, "-c", "pass"])
        dead.process.wait()
        registry.add(live)
        registry.add(dead)
        assert registry.reap_dead() == [dead]
        assert registry.tunnels == [live]

    def test_terminate_all_stops_processes(self, host, registry):
        ts = [_spawn(host, 5001), _spawn(host, 5002)]
        for t in ts:
            registry.add(t)
        registry.terminate_all()
        assert registry.tunnels == []
        assert all(t.process.poll() is not None for t in ts)

    def test_shutdown_stops_unless_kept(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)
        registry.keep_on_exit = True
        registry.shutdown()
        assert t.is_alive
        registry.keep_on_exit = False
        registry.shutdown()
        assert not t.is_alive

    def test_remove_deletes_log(self, host, registry, tmp_path):
        t = _spawn(host, 5001)
        t.log_path = tmp_path / "t.log"
        t.log_path.write_text("x")
        registry.add(t)
        registry.remove(t)
        assert not t.is_alive
        assert not t.log_path.exists()
        assert registry.tunnels == []


# ---------------------------------------------------------------------------
# SSHSelector integration
# ---------------------------------------------------------------------------

class TestSelectorTunnels:
    def test_registry_shared_across_instances(self, host, registry):
        """A rebuilt UI (after Enter / n / foreground t) sees earlier tunnels."""
        t = _spawn(host, 5001)
        registry.add(t)
        second = _app(host, registry)
        assert second._tunnels is registry
        assert second._tunnels.tunnels == [t]

    def test_port_held_by_own_tunnel_is_refused(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)
        problem = _app(host, registry)._local_port_problem(5001)
        assert problem is not None
        assert "5001" in problem and str(t.pid) in problem

    def test_port_held_by_other_process_is_refused(self, host, registry):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            problem = _app(host, registry)._local_port_problem(port)
        finally:
            srv.close()
        assert problem is not None and "in use" in problem

    def test_background_tunnel_logs_stderr_and_detaches(self, host, registry, monkeypatch):
        script = "import sys, time; sys.stderr.write('bind failed\\n'); sys.stderr.flush(); time.sleep(60)"
        monkeypatch.setattr(
            Host, "tunnel_command", lambda self, *a, **k: [sys.executable, "-c", script]
        )
        app = _app(host, registry)
        app._refresh_tunnel_panel = lambda: None
        app._start_background_tunnel(host, "default", 5003, 80, False)
        assert len(registry.tunnels) == 1
        t = registry.tunnels[0]
        assert t.log_path is not None and t.log_path.exists()
        deadline = time.monotonic() + 5
        while "bind failed" not in t.read_log_tail() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "bind failed" in t.read_log_tail()
        if sys.platform != "win32":
            assert os.getpgid(t.pid) != os.getpgid(0)   # own session: no terminal SIGINT
        log_path = t.log_path
        registry.terminate_all()
        assert not log_path.exists()


class TestQuitFlow:
    """Drive the real Textual app headlessly."""

    def _run(self, host, registry, keys):
        app = _app(host, registry)

        async def drive():
            async with app.run_test() as pilot:
                await pilot.pause()
                for k in keys:
                    await pilot.press(k)
                    await pilot.pause()
                return app.screen

        screen = asyncio.run(drive())
        return app, screen

    def test_quit_without_tunnels_exits(self, host, registry):
        app, _ = self._run(host, registry, ["q"])
        assert app.return_value is None
        assert not app.is_running

    def test_quit_asks_and_stop_terminates(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)
        app, _ = self._run(host, registry, ["q", "s"])
        assert not app.is_running
        assert t.process.poll() is not None
        assert registry.tunnels == []

    def test_quit_keep_leaves_tunnels(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)
        app, _ = self._run(host, registry, ["q", "k"])
        assert not app.is_running
        assert t.is_alive
        assert registry.keep_on_exit is True

    def test_quit_cancel_stays(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)

        async def drive():
            app = _app(host, registry)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause()
                assert isinstance(app.screen, QuitTunnelsModal)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, QuitTunnelsModal)
                assert app.is_running
                assert t.is_alive

        asyncio.run(drive())

    def test_panel_shows_tunnels_from_earlier_instance(self, host, registry):
        t = _spawn(host, 5001)
        registry.add(t)

        async def drive():
            app = _app(host, registry)
            async with app.run_test() as pilot:
                await pilot.pause()
                items = list(app.query(ssh_selector.TunnelItem))
                assert [i.tunnel for i in items] == [t]

        asyncio.run(drive())

    def test_dead_tunnel_is_reported(self, host, registry, tmp_path):
        t = _spawn(host, 5001, [sys.executable, "-c", "pass"])
        t.process.wait()
        t.log_path = tmp_path / "dead.log"
        t.log_path.write_text("Error: remote port forwarding failed\n")
        registry.add(t)

        async def drive():
            app = _app(host, registry)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, SSHErrorModal)
                assert "forwarding failed" in app.screen.message
                assert app.screen.title_text == "Tunnel Closed"

        asyncio.run(drive())
        assert registry.tunnels == []
