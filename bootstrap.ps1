<#
.SYNOPSIS
    Set up a local Python virtual environment for mu2edaq-cluster-tools
    (ssh-selector) and install its dependencies from pyproject.toml, for running
    the tool in-place during development. PowerShell port of bootstrap.sh.

    For a per-user install (a launcher on PATH), use .\install.ps1 instead.

.PARAMETER Dev
    Editable install with the [dev] extras.

.PARAMETER Version
    Print the package version and exit.
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Version
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Here 'venv'

function Get-PkgVersion {
    $src = Join-Path $Here 'src\mu2edaq_cluster_tools\ssh_selector.py'
    foreach ($line in Get-Content $src) {
        if ($line -match '^__version__ = "(.*)"') { return $Matches[1] }
    }
}

if ($Version) { Write-Host "mu2edaq-cluster-tools $(Get-PkgVersion)"; exit 0 }

# Prefer 'python'; fall back to the py launcher ('python3' on Windows is the
# Microsoft Store alias stub, so it is not used here).
$Python = $env:PYTHON
if (-not $Python) {
    if (Get-Command python -ErrorAction SilentlyContinue) { $Python = 'python' }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py' }
    else { Write-Error 'Python 3.9+ not found on PATH. Install it first.'; exit 1 }
}

$pyver = & $Python -c 'import sys; print("%d.%d" % sys.version_info[:2])'
& $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
if ($LASTEXITCODE -ne 0) { Write-Error "Python >= 3.9 required, found $pyver"; exit 1 }
Write-Host "Using Python $pyver ($Python)"

if (-not (Test-Path $Venv)) {
    Write-Host "Creating virtual environment in $Venv"
    & $Python -m venv $Venv
}

$VenvPy = Join-Path $Venv 'Scripts\python.exe'
& $VenvPy -m pip install --upgrade pip | Out-Null

$spec = '.[dev]'
if ($Dev) {
    Write-Host "Installing (editable): $spec"
    & $VenvPy -m pip install -e $spec
} else {
    Write-Host "Installing: $spec"
    & $VenvPy -m pip install $spec
}

Write-Host ''
Write-Host 'Bootstrap complete. Activate with:  venv\Scripts\Activate.ps1'
Write-Host 'Run the tool with:                  venv\Scripts\ssh-selector.exe'
Write-Host 'For a per-user launcher instead, use .\install.ps1'
