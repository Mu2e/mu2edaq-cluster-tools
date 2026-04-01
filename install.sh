#!/usr/bin/env bash
# install.sh — installs ssh-selector into ~/.local/share/mu2edaq-cluster-tools
# and creates a launcher script in the user's bin directory.
#
# Usage:
#   ./install.sh              # install with defaults
#   ./install.sh --uninstall  # remove everything this script installed

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override with environment variables if desired
# ---------------------------------------------------------------------------
APP_CMD="${APP_CMD:-ssh-selector}"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.local/share/mu2edaq-cluster-tools}"

# ---------------------------------------------------------------------------
# Resolve the directory this script lives in (works even if called via symlink)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "  $*"; }
success() { echo "✓ $*"; }
warn()    { echo "! $*" >&2; }
die()     { echo "Error: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling mu2edaq-cluster-tools..."
    removed=0
    # Remove app directory
    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        success "Removed ${INSTALL_DIR}"
        removed=1
    fi
    # Remove launcher from any known bin dirs
    for bin_dir in "${HOME}/.local/bin" "${HOME}/bin"; do
        launcher="${bin_dir}/${APP_CMD}"
        if [[ -f "${launcher}" ]]; then
            rm -f "${launcher}"
            success "Removed ${launcher}"
            removed=1
        fi
    done
    [[ ${removed} -eq 0 ]] && echo "Nothing to remove."
    exit 0
fi

# ---------------------------------------------------------------------------
# Find Python 3.10+
# ---------------------------------------------------------------------------
PYTHON=""
for candidate in python3 python3.12 python3.11 python3.10 python; do
    if cmd="$(command -v "${candidate}" 2>/dev/null)"; then
        ok=$("${cmd}" -c "import sys; print('ok' if sys.version_info >= (3,10) else '')" 2>/dev/null || true)
        if [[ "${ok}" == "ok" ]]; then
            PYTHON="${cmd}"
            break
        fi
    fi
done
[[ -n "${PYTHON}" ]] || die "Python 3.10 or newer is required but was not found on PATH."

PY_VERSION=$("${PYTHON}" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

# ---------------------------------------------------------------------------
# Find (or create) a bin directory
# ---------------------------------------------------------------------------
BIN_DIR=""
for candidate in "${HOME}/.local/bin" "${HOME}/bin"; do
    if [[ -d "${candidate}" ]]; then
        BIN_DIR="${candidate}"
        break
    fi
done
if [[ -z "${BIN_DIR}" ]]; then
    BIN_DIR="${HOME}/.local/bin"
    mkdir -p "${BIN_DIR}"
    success "Created ${BIN_DIR}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "Installing mu2edaq-cluster-tools"
echo "─────────────────────────────────────────────"
info "Source  : ${SCRIPT_DIR}"
info "App dir : ${INSTALL_DIR}"
info "Command : ${BIN_DIR}/${APP_CMD}"
info "Python  : ${PYTHON} (${PY_VERSION})"
echo

# ---------------------------------------------------------------------------
# Copy application files
# ---------------------------------------------------------------------------
echo "Copying application files..."
mkdir -p "${INSTALL_DIR}/config"
cp "${SCRIPT_DIR}/ssh_selector.py"   "${INSTALL_DIR}/ssh_selector.py"
cp "${SCRIPT_DIR}/requirements.txt"  "${INSTALL_DIR}/requirements.txt"
cp "${SCRIPT_DIR}/config/hosts.yaml.example" "${INSTALL_DIR}/config/hosts.yaml.example"
success "Files copied to ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Create / update the virtual environment
# ---------------------------------------------------------------------------
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    echo "Creating virtual environment..."
    "${PYTHON}" -m venv "${INSTALL_DIR}/venv"
    success "Virtual environment created"
else
    info "Virtual environment already exists — updating"
fi

echo "Installing Python dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
success "Dependencies installed"

# ---------------------------------------------------------------------------
# Write the launcher script
# ---------------------------------------------------------------------------
LAUNCHER="${BIN_DIR}/${APP_CMD}"
cat > "${LAUNCHER}" <<LAUNCHER_EOF
#!/usr/bin/env bash
# Launcher for mu2edaq-cluster-tools / ssh-selector
# Generated by install.sh — do not edit directly.
exec "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/ssh_selector.py" "\$@"
LAUNCHER_EOF
chmod +x "${LAUNCHER}"
success "Launcher written to ${LAUNCHER}"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo
echo "Installation complete."
echo "Run the application with:  ${APP_CMD}"
echo "To uninstall:              ${SCRIPT_DIR}/install.sh --uninstall"
echo

# Warn if the bin directory is not yet on PATH
if ! printf '%s\n' "${PATH//:/$'\n'}" | grep -qx "${BIN_DIR}"; then
    echo "─────────────────────────────────────────────"
    warn "${BIN_DIR} is not on your PATH."
    echo
    echo "Add this line to your shell config (~/.bashrc or ~/.zshrc):"
    echo
    echo "    export PATH=\"\${HOME}/.local/bin:\${PATH}\""
    echo
fi
