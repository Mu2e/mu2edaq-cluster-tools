"""!
@file test_tunnels.py
@brief Tests for ssh-selector background tunnel lifetime and local-port checks (GitHub issue #9).

Before #9, background tunnels vanished from the panel when the UI was
rebuilt, survived quit, and a reused local port silently kept the old
forward. Covers ExitOnForwardFailure in the tunnel argv, check_local_port(),
TunnelRegistry (lookup, reaping, terminate, keep-on-exit, log cleanup),
sharing the registry across SSHSelector instances, stderr logging and
session detachment, and the quit dialog driven headlessly through Textual's
run_test(). Tunnels are long-sleeping Python processes in place of ssh.
"""

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


# Stand-in for `ssh -N`: a long-lived child that the registry can terminate.
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def host():
    """!
    @brief Host pointing at localhost; never contacted because ssh is replaced.
    @return Host instance.
    """
    return Host(hostname="localhost", nickname="Local")


@pytest.fixture
def registry():
    """!
    @brief Fresh TunnelRegistry that terminates every child process on teardown.
    @return (yielded) TunnelRegistry instance; terminate_all() runs after the test.
    """
    reg = TunnelRegistry()
    yield reg
    reg.terminate_all()


def _spawn(host, local_port, cmd=None):
    """!
    @brief Start a stand-in tunnel process and wrap it in an ActiveTunnel.
    @param host        Host the tunnel nominally belongs to.
    @param local_port  Local port recorded on the tunnel (not actually bound).
    @param cmd         argv to run; defaults to a 60 s sleeper.
    @return ActiveTunnel for the running process.
    """
    proc = subprocess.Popen(cmd or SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ActiveTunnel(
        host=host, user="default", local_port=local_port, remote_port=80,
        process=proc, started_at=time.monotonic(),
    )


def _app(host, registry):
    """!
    @brief Build an SSHSelector sharing the given registry.
    @param host      The only configured host.
    @param registry  TunnelRegistry to inject.
    @return SSHSelector instance.
    """
    return SSHSelector(Config(hosts=[host], grouplist=[]), tunnels=registry)


# ---------------------------------------------------------------------------
# ssh command and local port probe
# ---------------------------------------------------------------------------

class TestTunnelCommand:
    """!
    @brief Tunnel argv fails fast if the forward cannot be set up.
    """
    def test_exit_on_forward_failure(self, host):
        """!
        @brief `-o ExitOnForwardFailure=yes` precedes `-L`.
        """
        cmd = host.tunnel_command("alice", 8080, 9090)
        i = cmd.index("ExitOnForwardFailure=yes")
        assert cmd[i - 1] == "-o"
        assert cmd.index("-o") < cmd.index("-L")


class TestCheckLocalPort:
    """!
    @brief check_local_port() detects ports already bound by any process.
    """
    def test_listening_port_is_reported(self):
        """!
        @brief A listening port yields a reason mentioning the port and `in use`.
        """
        # Bind and listen on an ephemeral port so it is genuinely in use by another socket.
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
        """!
        @brief A free port yields None.
        """
        # Ask the kernel for a free ephemeral port, then release it before checking.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert check_local_port(port) is None


# ---------------------------------------------------------------------------
# TunnelRegistry
# ---------------------------------------------------------------------------

class TestTunnelRegistry:
    """!
    @brief TunnelRegistry bookkeeping of tunnel processes.
    """
    def test_find_local_port_only_live(self, host, registry):
        """!
        @brief find_local_port() returns only live tunnels on that port.
        """
        t = _spawn(host, 5001)
        registry.add(t)
        assert registry.find_local_port(5001) is t
        assert registry.find_local_port(5002) is None
        t.process.kill()
        t.process.wait()
        assert registry.find_local_port(5001) is None

    def test_reap_dead(self, host, registry):
        """!
        @brief reap_dead() removes and returns exited tunnels only.
        """
        live = _spawn(host, 5001)
        dead = _spawn(host, 5002, [sys.executable, "-c", "pass"])
        dead.process.wait()
        registry.add(live)
        registry.add(dead)
        assert registry.reap_dead() == [dead]
        assert registry.tunnels == [live]

    def test_terminate_all_stops_processes(self, host, registry):
        """!
        @brief terminate_all() stops every process and empties the registry.
        """
        ts = [_spawn(host, 5001), _spawn(host, 5002)]
        for t in ts:
            registry.add(t)
        registry.terminate_all()
        assert registry.tunnels == []
        assert all(t.process.poll() is not None for t in ts)

    def test_shutdown_stops_unless_kept(self, host, registry):
        """!
        @brief shutdown() leaves tunnels running only when keep_on_exit is set.
        """
        t = _spawn(host, 5001)
        registry.add(t)
        registry.keep_on_exit = True
        registry.shutdown()
        assert t.is_alive
        registry.keep_on_exit = False
        registry.shutdown()
        assert not t.is_alive

    def test_remove_deletes_log(self, host, registry, tmp_path):
        """!
        @brief remove() stops the tunnel, deletes its log and drops it.
        """
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
    """!
    @brief SSHSelector use of the shared registry.
    """
    def test_registry_shared_across_instances(self, host, registry):
        """!
        @brief A rebuilt UI (after Enter / n / foreground t) sees earlier tunnels.
        """
        t = _spawn(host, 5001)
        registry.add(t)
        second = _app(host, registry)
        assert second._tunnels is registry
        assert second._tunnels.tunnels == [t]

    def test_port_held_by_own_tunnel_is_refused(self, host, registry):
        """!
        @brief A port held by one of our tunnels is refused, naming the port and PID.
        """
        t = _spawn(host, 5001)
        registry.add(t)
        problem = _app(host, registry)._local_port_problem(5001)
        assert problem is not None
        assert "5001" in problem and str(t.pid) in problem

    def test_port_held_by_other_process_is_refused(self, host, registry):
        """!
        @brief A port held by another process is refused as `in use`.
        """
        # A foreign listener holds the port; it is not in the registry.
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
        """!
        @brief Background tunnel stderr goes to a log file, the process gets its own session, and the log is removed on terminate.
        """
        script = "import sys, time; sys.stderr.write('bind failed\\n'); sys.stderr.flush(); time.sleep(60)"
        # Replace ssh with a child that writes an error to stderr and stays alive, to check log capture.
        monkeypatch.setattr(
            Host, "tunnel_command", lambda self, *a, **k: [sys.executable, "-c", script]
        )
        app = _app(host, registry)
        # No DOM outside a running app.
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
    """!
    @brief Drive the real Textual app headlessly.
    """

    def _run(self, host, registry, keys):
        """!
        @brief Run the app under run_test(), press the given keys and return.
        @param host      The only configured host.
        @param registry  Shared TunnelRegistry.
        @param keys      Key names pressed in order.
        @return Tuple (app, final screen).
        """
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
        """!
        @brief `q` with no tunnels exits immediately.
        """
        app, _ = self._run(host, registry, ["q"])
        assert app.return_value is None
        assert not app.is_running

    def test_quit_asks_and_stop_terminates(self, host, registry):
        """!
        @brief `q` then `s` stops the tunnels and exits.
        """
        t = _spawn(host, 5001)
        registry.add(t)
        app, _ = self._run(host, registry, ["q", "s"])
        assert not app.is_running
        assert t.process.poll() is not None
        assert registry.tunnels == []

    def test_quit_keep_leaves_tunnels(self, host, registry):
        """!
        @brief `q` then `k` exits, leaves tunnels running and sets keep_on_exit.
        """
        t = _spawn(host, 5001)
        registry.add(t)
        app, _ = self._run(host, registry, ["q", "k"])
        assert not app.is_running
        assert t.is_alive
        assert registry.keep_on_exit is True

    def test_quit_cancel_stays(self, host, registry):
        """!
        @brief `q` then Escape closes the dialog and keeps the app and tunnel running.
        """
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
        """!
        @brief The tunnel panel lists tunnels registered before the app started.
        """
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
        """!
        @brief A tunnel that died is reaped and its log tail is shown in a `Tunnel Closed` dialog.
        """
        t = _spawn(host, 5001, [sys.executable, "-c", "pass"])
        t.process.wait()
        t.log_path = tmp_path / "dead.log"
        # A pre-exited tunnel with a log, as ssh would leave after ExitOnForwardFailure.
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
