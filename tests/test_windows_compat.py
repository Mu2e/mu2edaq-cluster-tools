"""Windows compatibility tests.

Added in the windows-compat sweep. Locks in:
  * ssh_selector's local-network detection has a Windows (ipconfig) fallback,
    mirroring lan_scan -- without it, auto-detection returned nothing on Windows
    (no `ip`/`ifconfig`),
  * lan_scan's ping uses the Windows flags on Windows, and
  * bootstrap/install ship PowerShell ports.
"""
import pathlib
import shutil
import subprocess
from unittest import mock

import pytest

from mu2edaq_cluster_tools import lan_scan, ssh_selector

REPO = pathlib.Path(__file__).resolve().parent.parent
PWSH = shutil.which("pwsh") or shutil.which("powershell")

WINDOWS_IPCONFIG = """
Windows IP Configuration

Ethernet adapter Ethernet:
   IPv4 Address. . . . . . . . . . . : 192.168.6.42
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.6.1
"""


def _only_ipconfig(cmd, *a, **k):
    # Simulate a Windows host: `ip` and `ifconfig` are absent, `ipconfig` works.
    if cmd[0] == "ipconfig":
        return WINDOWS_IPCONFIG
    raise FileNotFoundError(cmd[0])


def test_ssh_selector_detects_networks_via_ipconfig_on_windows():
    with mock.patch.object(ssh_selector.subprocess, "check_output", _only_ipconfig):
        nets = ssh_selector._detect_local_networks_sync()
    assert any(str(n) == "192.168.6.0/24" for n in nets), nets


def test_lan_scan_detects_networks_via_ipconfig_on_windows():
    with mock.patch.object(lan_scan.subprocess, "check_output", _only_ipconfig):
        nets = lan_scan.detect_local_networks()
    assert any(str(n) == "192.168.6.0/24" for n in nets), nets


def test_ping_uses_windows_flags_on_windows():
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return mock.Mock(returncode=0)

    with mock.patch.object(lan_scan.platform, "system", return_value="Windows"), \
         mock.patch.object(lan_scan.subprocess, "run", fake_run):
        assert lan_scan.ping("192.168.6.1", 1.0) is True
    assert captured["cmd"][:2] == ["ping", "-n"], captured["cmd"]


def test_bootstrap_and_install_have_powershell_ports():
    for stem in ("bootstrap", "install"):
        assert (REPO / f"{stem}.sh").is_file()
        assert (REPO / f"{stem}.ps1").is_file()


@pytest.mark.skipif(not PWSH, reason="PowerShell not available")
@pytest.mark.parametrize("stem", ["bootstrap", "install"])
def test_powershell_scripts_parse(stem):
    path = (REPO / f"{stem}.ps1").as_posix()
    code = (
        "$e=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$null,[ref]$e)|Out-Null;"
        "if($e){$e|ForEach-Object{Write-Error $_};exit 1}else{exit 0}"
    )
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
