"""!
@file test_terminal_launch.py
@brief Tests for ssh-selector new-terminal launch and foreground ssh stderr handling (GitHub issue #15, items 7 and 8).

Covers _terminal_command() (argv-safe flags per emulator, preference order,
shutil.which instead of running `which`), _open_in_new_terminal() on Linux,
Windows and macOS, and _run_foreground_ssh(), which forwards ssh's stderr
live and keeps only a bounded tail for the error dialog. Terminal emulators
are never launched: shutil.which and subprocess.Popen are mocked. The
foreground tests run the Python interpreter in place of ssh.
"""

import sys
from unittest import mock

import pytest

from mu2edaq_cluster_tools import ssh_selector as sel

SSH_CMD = ["ssh", "-J", "jump@gw", "-l", "mu2edaq", "mu2e-daq-01"]


def _which_only(*present):
    """!
    @brief Return a fake shutil.which that finds only the named executables.
    @param present  Executable names to report as installed.
    @return Callable name -> path or None.
    """
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
    """!
    @brief Each emulator receives the ssh argv as separate arguments after its exec flag.
    @param exe   Parametrized emulator name.
    @param flag  Flag that takes the rest of argv for that emulator.
    """
    with mock.patch.object(sel.shutil, "which", side_effect=_which_only(exe)):
        assert sel._terminal_command(SSH_CMD) == [exe, flag] + SSH_CMD


def test_xfce4_uses_x_not_e():
    """!
    @brief xfce4-terminal uses `-x`; its `-e` takes a single string (#15 item 7).
    """
    with mock.patch.object(sel.shutil, "which", side_effect=_which_only("xfce4-terminal")):
        argv = sel._terminal_command(SSH_CMD)
    assert argv[:2] == ["xfce4-terminal", "-x"]


def test_terminal_command_preference_order():
    """!
    @brief xfce4-terminal is preferred over konsole and xterm when several are installed.
    """
    with mock.patch.object(sel.shutil, "which",
                           side_effect=_which_only("xterm", "konsole", "xfce4-terminal")):
        assert sel._terminal_command(SSH_CMD)[0] == "xfce4-terminal"


def test_terminal_command_none_found():
    """!
    @brief With no known emulator installed the result is None.
    """
    with mock.patch.object(sel.shutil, "which", return_value=None):
        assert sel._terminal_command(SSH_CMD) is None


def test_terminal_lookup_does_not_run_which_program():
    """!
    @brief Lookup uses shutil.which and never spawns a `which` process (absent on Windows).
    """
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
    """!
    @brief On Linux the detected emulator is launched with the ssh argv.
    """
    monkeypatch.setattr(sel.sys, "platform", "linux")
    with mock.patch.object(sel.shutil, "which", side_effect=_which_only("xfce4-terminal")), \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(SSH_CMD, "daq01")
    popen.assert_called_once_with(["xfce4-terminal", "-x"] + SSH_CMD)


def test_open_linux_no_terminal_prints_command(monkeypatch, capsys):
    """!
    @brief On Linux without an emulator nothing is launched and a message is printed.
    """
    monkeypatch.setattr(sel.sys, "platform", "linux")
    with mock.patch.object(sel.shutil, "which", return_value=None), \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(SSH_CMD)
    popen.assert_not_called()
    assert "no supported terminal emulator" in capsys.readouterr().out


def test_open_windows_new_console(monkeypatch):
    """!
    @brief On Windows ssh is started directly with CREATE_NEW_CONSOLE and no emulator lookup.
    """
    monkeypatch.setattr(sel.sys, "platform", "win32")
    # CREATE_NEW_CONSOLE exists only on Windows builds of subprocess; inject it on other platforms.
    monkeypatch.setattr(sel.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    with mock.patch.object(sel.shutil, "which") as which, \
         mock.patch.object(sel.subprocess, "Popen") as popen:
        sel._open_in_new_terminal(SSH_CMD)
    popen.assert_called_once_with(SSH_CMD, creationflags=0x10)
    which.assert_not_called()


def test_open_macos_uses_osascript(monkeypatch):
    """!
    @brief On macOS Terminal.app is driven via osascript with double quotes escaped.
    """
    monkeypatch.setattr(sel.sys, "platform", "darwin")
    # A double quote in the argv checks escaping inside the AppleScript string literal.
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
    """!
    @brief Build an argv that runs a Python snippet, used in place of ssh.
    @param code  Python source passed to `-c`.
    @return argv list.
    """
    return [sys.executable, "-c", code]


def test_foreground_ssh_error_status_returns_stderr(capfd):
    """!
    @brief Exit status 255 (ssh's own failure) returns the stderr text and still echoes it live.
    @param capfd  Captures the file-descriptor-level stderr the child writes to.
    """
    status, err = sel._run_foreground_ssh(_py(
        "import sys; sys.stderr.write('Permission denied (gssapi-with-mic).\\n'); sys.exit(255)"))
    assert status == 255
    assert err == "Permission denied (gssapi-with-mic)."
    # Forwarded live to the terminal as well.
    assert "Permission denied" in capfd.readouterr().err


def test_foreground_ssh_remote_status_no_dialog(capfd):
    """!
    @brief A non-255 status (remote command) returns no error text, so no dialog is shown.
    """
    status, err = sel._run_foreground_ssh(_py(
        "import sys; sys.stderr.write('warning: something\\n'); sys.exit(1)"))
    assert status == 1
    assert err == ""
    assert "warning: something" in capfd.readouterr().err


def test_foreground_ssh_success():
    """!
    @brief A clean exit returns (0, '').
    """
    assert sel._run_foreground_ssh(_py("pass")) == (0, "")


def test_foreground_ssh_keeps_only_tail():
    """!
    @brief Only the last _STDERR_TAIL_BYTES of stderr are kept.
    """
    # Write three times the tail size so truncation must occur.
    n = sel._STDERR_TAIL_BYTES * 3
    status, err = sel._run_foreground_ssh(_py(
        f"import sys; sys.stderr.write('a' * {n} + 'END'); sys.exit(255)"))
    assert status == 255
    assert err.endswith("END")
    assert len(err) <= sel._STDERR_TAIL_BYTES
