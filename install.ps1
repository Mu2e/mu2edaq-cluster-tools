<#
.SYNOPSIS
    Per-user install of ssh-selector / lan-scan on Windows (PowerShell port of
    install.sh). Installs into %LOCALAPPDATA%\mu2edaq-cluster-tools and writes
    .cmd launchers into a user bin directory on PATH.

.PARAMETER Uninstall
    Remove everything this script installed.

.PARAMETER Version
    Print the package version and exit.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Version
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$AppCmd = if ($env:APP_CMD) { $env:APP_CMD } else { 'ssh-selector' }
$InstallDir = if ($env:INSTALL_DIR) { $env:INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'mu2edaq-cluster-tools' }
$BinDir = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'  # on PATH by default
if (-not (Test-Path $BinDir)) { $BinDir = Join-Path $InstallDir 'bin' }

function Get-PkgVersion {
    $src = Join-Path $ScriptDir 'src\mu2edaq_cluster_tools\ssh_selector.py'
    foreach ($line in Get-Content $src) {
        if ($line -match '^__version__ = "(.*)"') { return $Matches[1] }
    }
}

if ($Version) { Write-Host "mu2edaq-cluster-tools $(Get-PkgVersion)"; exit 0 }

if ($Uninstall) {
    Write-Host 'Uninstalling mu2edaq-cluster-tools...'
    $removed = $false
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir; Write-Host "Removed $InstallDir"; $removed = $true }
    foreach ($cmd in @($AppCmd, 'lan-scan')) {
        $launcher = Join-Path $BinDir "$cmd.cmd"
        if (Test-Path $launcher) { Remove-Item -Force $launcher; Write-Host "Removed $launcher"; $removed = $true }
    }
    if (-not $removed) { Write-Host 'Nothing to remove.' }
    exit 0
}

# Find Python 3.9+
$Python = $null
foreach ($candidate in @('python', 'py')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $ok = & $candidate -c "import sys; print('ok' if sys.version_info >= (3,9) else '')" 2>$null
        if ($ok -eq 'ok') { $Python = $candidate; break }
    }
}
if (-not $Python) { Write-Error 'Python 3.9 or newer is required but was not found on PATH.'; exit 1 }
$pyver = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"

Write-Host 'Installing mu2edaq-cluster-tools'
Write-Host ('-' * 45)
Write-Host "  Source   : $ScriptDir"
Write-Host "  App dir  : $InstallDir"
Write-Host "  Commands : $BinDir\$AppCmd.cmd, $BinDir\lan-scan.cmd"
Write-Host "  Python   : $Python ($pyver)"
Write-Host ''

# Reference config
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'config') | Out-Null
Copy-Item (Join-Path $ScriptDir 'config\hosts.yaml.example') (Join-Path $InstallDir 'config\hosts.yaml.example') -Force
Write-Host "Reference config copied to $InstallDir"

# venv
$VenvDir = Join-Path $InstallDir 'venv'
if (-not (Test-Path $VenvDir)) {
    Write-Host 'Creating virtual environment...'
    & $Python -m venv $VenvDir
} else {
    Write-Host 'Virtual environment already exists -- updating'
}
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet $ScriptDir
Write-Host 'Package installed'

# Launchers (.cmd shims that exec the venv entry points)
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
foreach ($cmd in @($AppCmd, 'lan-scan')) {
    $target = Join-Path $VenvDir "Scripts\$cmd.exe"
    $launcher = Join-Path $BinDir "$cmd.cmd"
    Set-Content -Path $launcher -Encoding ascii -Value @(
        '@echo off',
        "`"$target`" %*"
    )
    Write-Host "Launcher written to $launcher"
}

Write-Host ''
Write-Host 'Installation complete.'
Write-Host "Run the application with:  $AppCmd"
Write-Host 'Scan your LAN with:        lan-scan'
Write-Host "To uninstall:              .\install.ps1 -Uninstall"

# Warn if the bin directory is not on PATH
$onPath = ($env:PATH -split ';') -contains $BinDir
if (-not $onPath) {
    Write-Host ('-' * 45)
    Write-Warning "$BinDir is not on your PATH; add it (User environment variables) to run the commands by name."
}
