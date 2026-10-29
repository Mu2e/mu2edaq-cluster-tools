#!/usr/bin/env bash
# bootstrap.sh -- set up a local Python virtual environment (venv/ in this
# checkout) for mu2edaq-cluster-tools (ssh-selector, lan-scan) and
# install/update the package and its dependencies from pyproject.toml.
# The script works from its own directory, so it may be run from anywhere.
#
#   (default)  regular install of the package, runtime dependencies only
#   --dev      editable install (pip install -e) plus the dev extra (pytest)
#
# For a per-user install (launchers on PATH), use ./install.sh instead.
#
# Usage: ./bootstrap.sh [--dev] [--version] [-h|--help]
set -euo pipefail

# NOTE: the comment block above (line 2 up to the first line that does not
# start with "#", i.e. the "set" line) IS the --help text, printed by sed in
# the -h|--help branch below. Every comment placed above "set" would appear
# in --help, so the Doxygen ("##") documentation lives here instead.

## @file bootstrap.sh
## @brief Create or update the developer virtual environment venv/ inside
##        this checkout and install mu2edaq-cluster-tools into it.
##
## Intended for working on the code and running the test suite. It creates
## ./venv (if missing) with the first suitable Python, upgrades pip, and
## pip-installs the package from pyproject.toml in this checkout:
## - default: a regular (non-editable) install with runtime dependencies
##   only. The package is copied into venv's site-packages, so source edits
##   take effect only after the script is run again.
## - `--dev`: an editable install (`pip install -e ".[dev]"`) plus the
##   `dev` extra (pytest). The venv imports the package straight from
##   src/, so source edits take effect immediately; this is what the test
##   suite expects.
##
## Re-running the script is safe: an existing venv is reused and the
## package and its dependencies are upgraded in place. For an operator
## install with launchers on PATH use install.sh instead.
##
## @par Synopsis
## @code
##   ./bootstrap.sh [--dev] [--version] [-h|--help]
## @endcode
##
## @par Options
## - `--dev`: editable install with the dev extra (see above).
## - `--version`: print `mu2edaq-cluster-tools <version>` and exit 0.
## - `-h`, `--help`: print the usage banner at the top of the script and
##   exit 0.
##
## Options may be given in any order. --version and --help act as soon as
## they are reached, so options after them are not examined.
##
## @par Environment variables
## - `PYTHON`: interpreter used to create the venv (default `python3`). It
##   must be Python 3.9 or newer. Only consulted when venv/ does not yet
##   exist; afterwards the venv's own python is used.
##
## @par Precedence
## command line > environment > built-in default. Only PYTHON is
## configurable through the environment and it has no command-line
## equivalent; there is no configuration file.
##
## @par Exit status
## - 0: bootstrap finished, or --help or --version printed.
## - 1: the PYTHON interpreter was not found or is older than 3.9.
## - 2: unknown command-line option.
## - other non-zero: a failing command (python -m venv, pip) aborted the
##   script through `set -e`.
##
## @note Platforms. Linux and macOS are supported natively, as is WSL on
##       Windows 11. The script sources venv/bin/activate, the POSIX venv
##       layout; under Git Bash with a native Windows Python the venv has
##       venv/Scripts/activate instead and the script stops at that step.
##       On Windows without WSL create the venv by hand, e.g.
##       `py -3 -m venv venv` and `venv\Scripts\pip install -e ".[dev]"`.
##
## Example:
## @code
##   # developer setup, then run the tests
##   ./bootstrap.sh --dev
##   source venv/bin/activate
##   python -m pytest -q
##   # use a specific interpreter for a fresh venv
##   rm -rf venv && PYTHON=python3.11 ./bootstrap.sh --dev
## @endcode

# Strict mode: -e aborts on the first failing command (venv creation, pip);
# -u makes a mistyped variable name fatal, hence "${PYTHON:-python3}" for
# the optional PYTHON override; -o pipefail makes a pipeline fail if any of
# its stages fails.

# Absolute directory of this checkout, independent of the caller's cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The venv always lives in the checkout, never in the caller's directory.
VENV="$HERE/venv"
DEV=0

## @brief Print the package version.
##
## Reads `__version__` from src/mu2edaq_cluster_tools/_version.py in this
## checkout, the single source of truth for the version. sed is used rather
## than Python so that --version works before a venv or interpreter exists.
## @return 0; prints nothing if the version file has no matching line
version() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' \
        "$HERE/src/mu2edaq_cluster_tools/_version.py"
}

# Options are flags without arguments, so a simple loop over "$@" suffices.
for arg in "$@"; do
    case "$arg" in
        --dev) DEV=1 ;;
        --version)
            echo "mu2edaq-cluster-tools $(version)"
            exit 0 ;;
        -h|--help)
            # Print the leading "#" banner: lines 2 up to the first line not
            # starting with "#", with "# " stripped. The ";" before "}" is
            # required by BSD sed on macOS.
            sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "$HERE/bootstrap.sh"
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# PYTHON may be overridden from the environment, e.g. PYTHON=python3.11, on
# hosts where "python3" is an older system interpreter.
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: python3 not found. Install Python 3.9+ first." >&2
    exit 1
fi

# The version check is done in the interpreter itself; the sys.exit status
# is consumed by "||", so set -e does not abort before the message prints.
PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
    echo "error: Python >= 3.9 required, found $PYVER" >&2; exit 1;
}
echo "Using Python $PYVER at $(command -v "$PYTHON")"

# An existing venv is reused as is, even if PYTHON now names a different
# interpreter; delete venv/ to rebuild it with another Python.
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment in $VENV"
    "$PYTHON" -m venv "$VENV"
fi

# Install the package from this checkout, wherever the script was run from.
# pip resolves "." and ".[dev]" against the current directory, so without
# this cd running e.g. "../bootstrap.sh" would install whatever project the
# caller happens to be in, or fail. HERE was computed from BASH_SOURCE, so
# the cd works from any cwd and through a relative invocation path.
cd "$HERE"

# Activating puts venv/bin first on PATH, so the plain "python" below is the
# venv interpreter and pip installs into the venv, not the system Python.
# The activation only affects this script's process, not the caller's
# shell. SC1091: activate does not exist until runtime, so shellcheck
# cannot follow it.
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip >/dev/null

# Editable (-e): the venv references src/ in place, so code changes are
# live without reinstalling, as needed for development and pytest. The
# default non-editable install copies a snapshot of the package into the
# venv, matching what install.sh deploys for operators.
if [ "$DEV" = 1 ]; then
    echo "Installing (editable, with dev extra): .[dev]"
    python -m pip install -e ".[dev]"
else
    echo "Installing: ."
    python -m pip install .
fi

echo ""
echo "Bootstrap complete. Activate with:  source $VENV/bin/activate"
echo "Run the tool with:                  ssh-selector"
echo "For a system-wide launcher instead, use ./install.sh"
