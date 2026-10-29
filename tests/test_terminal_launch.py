"""Tests for new-terminal command construction and foreground ssh handling."""

import sys
from unittest import mock

import pytest

from mu2edaq_cluster_tools import ssh_selector as sel

SSH_CMD = ["ssh", "-J", "jump@gw", "-l", "mu2edaq", "mu2e-daq-01"]


def _which_only(*present):
    """Return a fake shutil.which that finds only the named executables."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


# ---------------------------------------------------------------------------
# _terminal_command
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exe, flag", [
    ("gnome-terminal", "--"),
    ("xfce4-terminal", "-x"),
    ("konsole", "-e"),
    ("xterm", "-e"),
])
def test_terminal_command_passes_argv_after_flag(exe, flag):
    with mock.patch.object(sel.shutil, "which", side_effect=_which_only(exe)):
        assert sel._terminal_command(SSH_CMD) == [exe, flag] + SSH_CMD


def test_xfce4_uses_x_not_e():
    with mock.patch.object(sel.shutil, "which", side_effect=_which_only("xfce4-terminal")):
        argv = sel._terminal_command(SSH_CMD)
    assert argv[:2] == ["xfce4-terminal", "-x"]


def test_terminal_command_preference_order():
    with mock.patch.object(sel.shutil, "which",
                           side_effect=_which_only("xterm", "konsole", "xfce4-terminal")):
        assert sel._terminal_command(SSH_CMD)[0] == "xfce4-terminal"


def test_terminal_command_none_found():
    with mock.patch.object(sel.shutil, "which", return_value=None):
        assert sel._terminal_command(SSH_CMD) is None


def test_terminal_lookup_does_not_run_which_program():
    with mock.patch.object(sel.shutil, "which", return_value=None), \
         mock.patch.object(sel.subprocess, "run") as run, \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._terminal_command(SSH_CMD)
    run.assert_not_called()
    popen.assert_not_called()


# ---------------------------------------------------------------------------
# _open_in_new_terminal
# ---------------------------------------------------------------------------

def test_open_linux_launches_first_terminal(monkeypatch):
    monkeypatch.setattr(sel.sys, "platform", "linux")
    with mock.patch.object(sel.shutil, "which", side_effect=_which_only("xfce4-terminal")), \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(SSH_CMD, "daq01")
    popen.assert_called_once_with(["xfce4-terminal", "-x"] + SSH_CMD)


def test_open_linux_no_terminal_prints_command(monkeypatch, capsys):
    monkeypatch.setattr(sel.sys, "platform", "linux")
    with mock.patch.object(sel.shutil, "which", return_value=None), \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(SSH_CMD)
    popen.assert_not_called()
    assert "no supported terminal emulator" in capsys.readouterr().out


def test_open_windows_new_console(monkeypatch):
    monkeypatch.setattr(sel.sys, "platform", "win32")
    monkeypatch.setattr(sel.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    with mock.patch.object(sel.shutil, "which") as which, \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(SSH_CMD)
    popen.assert_called_once_with(SSH_CMD, creationflags=0x10)
    which.assert_not_called()


def test_open_macos_uses_osascript(monkeypatch):
    monkeypatch.setattr(sel.sys, "platform", "darwin")
    cmd = ["ssh", "-l", "u", 'h"x']
    with mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(cmd)
    argv = popen.call_args[0][0]
    assert argv[:2] == ["osascript", "-e"]
    assert argv[2].startswith('tell application "Terminal" to do script "')
    assert '\\"' in argv[2]


# ---------------------------------------------------------------------------
# _run_foreground_ssh
# ---------------------------------------------------------------------------

def _py(code):
    return [sys.executable, "-c", code]


def test_foreground_ssh_error_status_returns_stderr(capfd):
    status, err = sel._run_foreground_ssh(_py(
        "import sys; sys.stderr.write('Permission denied (gssapi-with-mic).\\n'); sys.exit(255)"))
    assert status == 255
    assert err == "Permission denied (gssapi-with-mic)."
    # Forwarded live to the terminal as well.
    assert "Permission denied" in capfd.readouterr().err


def test_foreground_ssh_remote_status_no_dialog(capfd):
    status, err = sel._run_foreground_ssh(_py(
        "import sys; sys.stderr.write('warning: something\\n'); sys.exit(1)"))
    assert status == 1
    assert err == ""
    assert "warning: something" in capfd.readouterr().err


def test_foreground_ssh_success():
    assert sel._run_foreground_ssh(_py("pass")) == (0, "")


def test_foreground_ssh_keeps_only_tail():
    n = sel._STDERR_TAIL_BYTES * 3
    status, err = sel._run_foreground_ssh(_py(
        f"import sys; sys.stderr.write('a' * {n} + 'END'); sys.exit(255)"))
    assert status == 255
    assert err.endswith("END")
    assert len(err) <= sel._STDERR_TAIL_BYTES
