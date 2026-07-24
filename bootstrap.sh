#!/usr/bin/env bash
# bootstrap.sh -- set up a local Python virtual environment for
# mu2edaq-cluster-tools (ssh-selector) and install/update its dependencies
# from pyproject.toml, for running the tool in-place during development.
#
# For a system-wide install (a `ssh-selector` launcher on PATH), use
# ./install.sh instead.
#
# Usage: ./bootstrap.sh [--dev] [--version] [-h|--help]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/venv"
DEV=0

version() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' \
        "$HERE/src/mu2edaq_cluster_tools/ssh_selector.py"
}

for arg in "$@"; do
    case "$arg" in
        --dev) DEV=1 ;;
        --version)
            echo "mu2edaq-cluster-tools $(version)"
            exit 0 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: python3 not found. Install Python 3.9+ first." >&2
    exit 1
fi

PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
    echo "error: Python >= 3.9 required, found $PYVER" >&2; exit 1;
}
echo "Using Python $PYVER at $(command -v "$PYTHON")"

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment in $VENV"
    "$PYTHON" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip >/dev/null

SPEC=".[dev]"
if [ "$DEV" = 1 ]; then
    echo "Installing (editable): $SPEC"
    pip install -e "$SPEC"
else
    echo "Installing: $SPEC"
    pip install "$SPEC"
fi

echo ""
echo "Bootstrap complete. Activate with:  source venv/bin/activate"
echo "Run the tool with:                  ssh-selector"
echo "For a system-wide launcher instead, use ./install.sh"
