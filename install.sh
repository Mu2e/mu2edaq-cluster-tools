#!/usr/bin/env bash
# install.sh -- per-user install of mu2edaq-cluster-tools (ssh-selector and
# lan-scan) into a private venv, with launcher scripts in a bin directory.
#
# Usage:
#   ./install.sh [--prefix DIR] [--bin-dir DIR] [--app-cmd NAME]
#   ./install.sh --uninstall [--prefix DIR]
#   ./install.sh --version | -h | --help
#
# Options (each overrides the matching environment variable):
#   --prefix DIR, --install-dir DIR
#                     install directory      (MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR,
#                     default ~/.local/share/mu2edaq-cluster-tools)
#   --bin-dir DIR     launcher directory     (MU2EDAQ_CLUSTER_TOOLS_BIN_DIR,
#                     default: first existing of ~/.local/bin, ~/bin;
#                     ~/.local/bin is created if neither exists)
#   --app-cmd NAME    ssh-selector launcher name (MU2EDAQ_CLUSTER_TOOLS_APP_CMD,
#                     default ssh-selector)
#   --uninstall       remove only what a previous install recorded in its
#                     manifest (DIR/.mu2edaq-cluster-tools-install)
#   --version         print the package version and exit
#   -h, --help        print this help and exit
#
# Precedence: command line > environment > built-in default.
# The generic INSTALL_DIR and APP_CMD variables are no longer read.

# NOTE: the comment block above (line 2 up to the first empty line) IS the
# --help text: usage() prints it with sed. Edit it only to change --help, and
# keep the empty line that terminates it. The Doxygen ("##") documentation
# lives below that empty line so that it never leaks into --help output.

## @file install.sh
## @brief Per-user installer and uninstaller for mu2edaq-cluster-tools
##        (ssh-selector and lan-scan).
##
## Creates a private Python virtual environment under an install directory,
## pip-installs this checkout into it (non-editable), copies the bundled host
## topology files into DIR/config/, and writes two small launcher scripts
## (ssh-selector, or the --app-cmd name, and lan-scan) into a bin directory
## that is normally on the operator's PATH. No root access is needed and
## nothing outside DIR and the two launcher paths is touched.
##
## Every path the install creates is recorded in a manifest,
## DIR/.mu2edaq-cluster-tools-install. --uninstall removes exactly the
## entries listed there and nothing else, and refuses to run at all on a
## directory that has no manifest. This makes a mistyped --prefix harmless:
## the script never recursively deletes a directory it did not create.
##
## @par Synopsis
## @code
##   ./install.sh [--prefix DIR] [--bin-dir DIR] [--app-cmd NAME]
##   ./install.sh --uninstall [--prefix DIR]
##   ./install.sh --version | -h | --help
## @endcode
##
## @par Options
## - `--prefix DIR`, `--install-dir DIR` (also `--prefix=DIR`):
##   install directory. Default `~/.local/share/mu2edaq-cluster-tools`.
##   A leading `~` is expanded by the script, relative paths are made
##   absolute against the current directory, and paths containing `..`
##   are refused.
## - `--bin-dir DIR` (also `--bin-dir=DIR`): directory for the launchers.
##   Default: the first existing of `~/.local/bin`, `~/bin`; `~/.local/bin`
##   is created if neither exists.
## - `--app-cmd NAME` (also `--app-cmd=NAME`): file name of the ssh-selector
##   launcher. Default `ssh-selector`. Must match `[A-Za-z0-9._-]+`.
##   The lan-scan launcher is always named `lan-scan`.
## - `--uninstall`: remove what the manifest in DIR lists, then remove
##   DIR/config and DIR if they are empty.
## - `--version`: print `mu2edaq-cluster-tools <version>` and exit 0.
## - `-h`, `--help`: print the usage banner at the top of the script and
##   exit 0.
##
## @par Environment variables
## - `MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR`: install directory (see --prefix).
## - `MU2EDAQ_CLUSTER_TOOLS_BIN_DIR`: launcher directory (see --bin-dir).
## - `MU2EDAQ_CLUSTER_TOOLS_APP_CMD`: ssh-selector launcher name (see --app-cmd).
## - `HOME`: base of all defaults; `$HOME` itself is refused as DIR.
## - `PATH`: searched for a Python 3.9+ interpreter (python3, python3.12 ...
##   python3.9, python), and checked for BIN_DIR, with a hint printed if
##   BIN_DIR is missing from it.
## - `INSTALL_DIR`, `APP_CMD`: read by install.sh <= 1.0.1. Now ignored with
##   a warning, because such generic names are often set by unrelated tooling.
##
## @par Precedence
## command line > environment (MU2EDAQ_CLUSTER_TOOLS_*) > built-in default.
## The script has no configuration file and does not read a .env file.
##
## @par Exit status
## - 0: install or uninstall finished, --help or --version printed, or
##   --uninstall found no install directory (nothing to remove).
## - 1: refused or impossible operation, reported via die(): unsafe or
##   invalid install directory or launcher name, foreign non-empty
##   directory, missing manifest on --uninstall, existing non-launcher
##   file at a launcher path, or no Python 3.9+ on PATH.
## - 2: command-line usage error (unknown option, missing option argument).
## - other non-zero: a failing command (python -m venv, pip, cp, mkdir ...)
##   aborted the script through `set -e`.
##
## @note Platforms. Linux (AlmaLinux 9) and macOS are supported natively.
##       The script avoids GNU-only tools (no `realpath -m`, no
##       `readlink -f`; sed and awk expressions work with the BSD versions)
##       and uses only constructs available in Bash 3.2, the version macOS
##       ships as /bin/bash. On Windows 11 run it inside WSL. Under Git Bash
##       with a native Windows Python the venv has `Scripts/` instead of
##       `bin/`, so the `venv/bin/...` paths used for pip and the launchers
##       do not exist there.
##
## @warning --uninstall deletes DIR/venv recursively. Safety rests on the
##          manifest check, on check_safe_dir() and on the validation of
##          every manifest entry; see the uninstall section of the source.
##
## Example:
## @code
##   # default per-user install, launchers in ~/.local/bin
##   ./install.sh
##   # shared install with a custom launcher name
##   ./install.sh --prefix /opt/mu2e/cluster-tools --bin-dir ~/bin --app-cmd mu2e-ssh
##   # the same install directory selected through the environment
##   MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR=/opt/mu2e/cluster-tools ./install.sh
##   # remove it again (the --prefix must match the install)
##   ./install.sh --uninstall --prefix /opt/mu2e/cluster-tools
## @endcode

# Strict mode, with consequences relied upon throughout this script:
#  -e  any failing simple command aborts the install (pip, venv, cp, ...).
#      Commands that may legitimately fail are guarded with "|| true" or used
#      as the condition of if/&&/||, where -e does not fire.
#  -u  expanding an unset variable is fatal, hence the "${INSTALL_DIR:-}" and
#      "${MU2EDAQ_CLUSTER_TOOLS_BIN_DIR:-}" style expansions for variables
#      that the operator may or may not have exported.
#  -o pipefail  a pipeline fails if any stage fails, not only the last one;
#      grep returning 1 for "no match" therefore needs "|| true" where its
#      result is captured.
set -euo pipefail

# Absolute directory of this checkout, independent of the caller's cwd, so
# that config/ and src/ are found and "pip install" gets the right tree.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Manifest file name inside INSTALL_DIR. Its presence is what marks a
# directory as owned by this script (see the install and uninstall sections).
MARKER_NAME=".mu2edaq-cluster-tools-install"
# Written as a whole line into every launcher. Re-installs and --uninstall
# only touch a launcher that carries this exact line (see is_our_launcher).
LAUNCHER_TAG="# Generated by mu2edaq-cluster-tools install.sh"
# Tag written by install.sh <= 1.0.1, recognised so old launchers can be replaced.
LEGACY_LAUNCHER_TAG="# Generated by install.sh — do not edit directly."
# Topology files installed into DIR/config/ for ssh-selector discovery.
# lan-scan profiles are deliberately not installed as host files.
HOST_CONFIGS=(mu2e_daq.yaml mu2e_offline.yaml nova_daq.yaml fnal_gpvm.yaml)
# Copied next to the host files as a template for personal host files; the
# .example suffix keeps it from being loaded as a topology.
REFERENCE_FILES=(hosts.yaml.example)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
## @brief Print an indented informational message to stdout.
## @param $* message text
## @return 0
info()    { echo "  $*"; }
## @brief Print a check-marked success message to stdout.
## @param $* message text
## @return 0
success() { echo "✓ $*"; }
## @brief Print a warning to stderr; execution continues.
## @param $* message text
## @return 0
warn()    { echo "! $*" >&2; }
## @brief Print an error to stderr and terminate the script.
## @param $* message text, printed after "Error: "
## @return does not return; exits the script with status 1
die()     { echo "Error: $*" >&2; exit 1; }
## @brief Print the --help text.
##
## The help text is the leading `#` comment banner of this file: sed takes
## lines 2 up to the first empty line, keeps only lines starting with `#`,
## and strips the `#` plus one optional space. The `;` before `}` is
## required by BSD sed on macOS. Keeping the help in the banner means the
## header comment and --help cannot disagree.
## @return 0 (status of sed)
usage()   { sed -n '2,/^$/{/^#/s/^# \{0,1\}//p;}' "${BASH_SOURCE[0]}"; }
## @brief Print the package version.
##
## Reads `__version__` from src/mu2edaq_cluster_tools/_version.py in this
## checkout, the single source of truth for the version. sed is used rather
## than Python so that --version works before an interpreter is located.
## @return 0; prints nothing if the version file has no matching line
version() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' \
        "${SCRIPT_DIR}/src/mu2edaq_cluster_tools/_version.py"
}
## @brief Check that an option received a non-empty argument.
##
## Called with the full remaining argv ("$@") for `--opt VALUE`, or with the
## option name and the parsed value for `--opt=VALUE`.
## @param $1 option name, used in the error message
## @param $2 option argument; missing or empty is an error
## @return 0 if the argument is present; otherwise exits the script with
##         status 2 (usage error)
need_arg() { [[ $# -ge 2 && -n "$2" ]] || { echo "Error: $1 requires an argument" >&2; exit 2; }; }

# Make a path absolute and lexically normalised without requiring it to exist.
## @brief Make a path absolute and lexically normalised without requiring
##        it to exist.
##
## Expands a leading literal `~`, prefixes relative paths with $PWD, and
## collapses `//`, `/./` and a trailing `/` or `/.`. A path containing a
## `..` component is refused rather than resolved: lexical `..` removal is
## wrong when the parent is a symlink, and `realpath -m` (resolution without
## requiring existence) is GNU-only and absent on macOS.
## @param $1 path to normalise (may be relative, may start with `~`)
## @return 0 with the normalised path on stdout; exits the script with
##         status 1 via die() if the path contains `..`
## @warning The result is not symlink-resolved; check_safe_dir() compares
##          it lexically.
abspath() {
    local p="$1"
    # The shell does not tilde-expand "--prefix=~/x" (the word is not a
    # variable assignment), nor a "~" stored in an environment variable, so
    # a literal leading tilde is expanded here.
    # shellcheck disable=SC2088  # matching a literal "~" from --prefix=~/...
    [[ "${p}" == "~" || "${p}" == "~/"* ]] && p="${HOME}${p#\~}"
    [[ "${p}" == /* ]] || p="${PWD}/${p}"
    # Collapse //, /./ and trailing /; refuse .. rather than resolve it.
    # The loops are needed because one ${//} pass turns "///" into "//".
    while [[ "${p}" == *//* ]]; do p="${p//\/\//\/}"; done
    while [[ "${p}" == */./* ]]; do p="${p//\/.\//\/}"; done
    p="${p%/.}"
    [[ "${p}" == */../* || "${p}" == */.. ]] && die "path must not contain '..': $1"
    # Strip a trailing slash so string comparisons against $HOME, the
    # refused-path list and manifest entries are exact; "/" stays "/".
    [[ "${p}" != "/" ]] && p="${p%/}"
    printf '%s\n' "${p}"
}

# Refuse directories that must never be installed into or uninstalled.
## @brief Refuse directories that must never be installed into or
##        uninstalled from.
##
## Applied to INSTALL_DIR for both install and --uninstall. Rejects:
## - an empty path;
## - paths containing `"`, `$`, backtick, backslash, tab or newline, because
##   INSTALL_DIR is embedded inside double quotes in the generated launchers
##   and must stay inert there (tab would also corrupt the tab-separated
##   manifest);
## - `/` and `$HOME` itself, the two most damaging plausible typos;
## - the system directories `/bin`, `/sbin`, `/usr`, `/usr/bin`,
##   `/usr/sbin`, `/usr/local`, `/usr/local/bin`, `/usr/local/sbin` (every
##   match of the pattern `/usr/` + `*bin`), `/etc`, `/opt`, `/tmp`, `/var`,
##   and the home-directory parents `/Users` (macOS) and `/home` (Linux).
##
## Only these exact paths are refused; subdirectories such as
## `/opt/mu2e/cluster-tools` remain allowed for shared installs.
## @param $1 absolute, normalised directory (output of abspath())
## @return 0 if the directory is acceptable; otherwise exits the script with
##         status 1 via die()
## @warning This is a last-line guard against catastrophic typos, not a
##          sandbox; the manifest check is the primary uninstall protection.
check_safe_dir() {
    local d="$1" home
    # Normalise $HOME the same way as $1 so that e.g. HOME=/home/op/ matches.
    home="$(abspath "${HOME}")"
    [[ -n "${d}" ]]             || die "install directory is empty"
    # The path is embedded in the generated launchers; keep it shell-inert.
    [[ "${d}" != *[\"\$\`\\]* && "${d}" != *$'\n'* && "${d}" != *$'\t'* ]] \
        || die "install directory contains a quote, \$, backtick, backslash, tab or newline: ${d}"
    [[ "${d}" != "/" ]]         || die "refusing to use / as the install directory"
    [[ "${d}" != "${home}" ]]   || die "refusing to use \$HOME (${d}) as the install directory"
    # In a case pattern "*" also matches "/", so /usr/*bin covers /usr/bin,
    # /usr/sbin, /usr/local/bin and /usr/local/sbin.
    case "${d}" in
        /bin|/sbin|/usr|/usr/*bin|/usr/local|/etc|/opt|/tmp|/var|/Users|/home)
            die "refusing to use system directory ${d} as the install directory" ;;
    esac
}

# True if $1 is a launcher this script (current or legacy) wrote for INSTALL_DIR.
## @brief Test whether a file is a launcher this script (current or legacy
##        version) wrote for the current INSTALL_DIR.
##
## All three conditions must hold:
## 1. it is a regular file and not a symlink (a symlink may point at
##    something the operator owns elsewhere);
## 2. it contains LAUNCHER_TAG or LEGACY_LAUNCHER_TAG as a complete line;
## 3. it execs `"INSTALL_DIR/venv/bin/...`, so a launcher belonging to a
##    second install under another --prefix is never overwritten or
##    removed by this one.
##
## Reads the globals INSTALL_DIR, LAUNCHER_TAG and LEGACY_LAUNCHER_TAG.
## @param $1 path of the candidate launcher
## @return 0 if the file is our launcher for INSTALL_DIR, 1 otherwise
is_our_launcher() {
    [[ -f "$1" && ! -L "$1" ]] || return 1
    # -x whole line, -F fixed string: the tag must not match as a substring
    # of some other comment.
    grep -qxF -e "${LAUNCHER_TAG}" -e "${LEGACY_LAUNCHER_TAG}" "$1" || return 1
    grep -qF "\"${INSTALL_DIR}/venv/bin/" "$1"
}

# True if INSTALL_DIR has exactly the layout install.sh <= 1.0.1 left behind
# (venv/ and config/hosts.yaml.example, no manifest), so it can be adopted.
## @brief Test whether INSTALL_DIR is an install made by install.sh <= 1.0.1.
##
## Releases up to 1.0.1 wrote no manifest, so their directories would be
## refused as foreign by the ownership check. To let operators upgrade in
## place, a manifest-less directory is adopted only if it has exactly the
## old layout: `venv/` with an executable `venv/bin/ssh-selector`, an
## optional `config/` holding nothing but `hosts.yaml.example`, and no
## other entries (hidden files included). Any extra entry means the
## directory may hold operator data, and it is refused instead.
##
## Reads the global INSTALL_DIR.
## @return 0 if the legacy layout matches exactly, 1 otherwise
is_legacy_install() {
    local e
    [[ -d "${INSTALL_DIR}/venv" && -x "${INSTALL_DIR}/venv/bin/ssh-selector" ]] || return 1
    # "*" plus ".[!.]*" lists visible and hidden entries without "." and
    # "..", and without depending on shopt dotglob/nullglob. An unmatched
    # glob stays literal, which the -e/-L test filters out.
    for e in "${INSTALL_DIR}"/* "${INSTALL_DIR}"/.[!.]*; do
        [[ -e "${e}" || -L "${e}" ]] || continue
        case "${e##*/}" in venv|config) ;; *) return 1 ;; esac
    done
    for e in "${INSTALL_DIR}"/config/* "${INSTALL_DIR}"/config/.[!.]*; do
        [[ -e "${e}" || -L "${e}" ]] || continue
        [[ "${e##*/}" == hosts.yaml.example ]] || return 1
    done
}

# ---------------------------------------------------------------------------
# Command line (overrides environment, which overrides defaults)
# ---------------------------------------------------------------------------
# Options are first collected into OPT_* and merged with the environment
# and defaults afterwards, so the order of options does not matter.
ACTION=install
OPT_PREFIX=""
OPT_BIN_DIR=""
OPT_APP_CMD=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        # "--opt VALUE" form: need_arg gets "$@" so it can check that $2 exists.
        # "--opt=VALUE" form: split on the first "="; an empty VALUE is an error.
        --prefix|--install-dir) need_arg "$@"; OPT_PREFIX="$2"; shift 2 ;;
        --prefix=*|--install-dir=*) OPT_PREFIX="${1#*=}"; need_arg "${1%%=*}" "${OPT_PREFIX}"; shift ;;
        --bin-dir)  need_arg "$@"; OPT_BIN_DIR="$2"; shift 2 ;;
        --bin-dir=*) OPT_BIN_DIR="${1#*=}"; need_arg "${1%%=*}" "${OPT_BIN_DIR}"; shift ;;
        --app-cmd)  need_arg "$@"; OPT_APP_CMD="$2"; shift 2 ;;
        --app-cmd=*) OPT_APP_CMD="${1#*=}"; need_arg "${1%%=*}" "${OPT_APP_CMD}"; shift ;;
        --uninstall) ACTION=uninstall; shift ;;
        # --version and --help exit at once, before any path validation, so
        # they work whatever the environment holds.
        --version)  echo "mu2edaq-cluster-tools $(version)"; exit 0 ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "Error: unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

# install.sh <= 1.0.1 read INSTALL_DIR and APP_CMD. Those names are generic
# enough to be exported by unrelated build systems, CI jobs or other
# installers; honouring them could install into, or worse --uninstall from,
# a directory the operator never meant. They are therefore ignored, with a
# warning so that anyone relying on the old behaviour notices. The ":-"
# keeps "set -u" from aborting when they are unset.
if [[ -n "${INSTALL_DIR:-}" ]]; then
    warn "INSTALL_DIR is set in the environment but is ignored (too generic to trust);"
    warn "use --prefix or MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR instead."
fi
if [[ -n "${APP_CMD:-}" ]]; then
    warn "APP_CMD is ignored; use --app-cmd or MU2EDAQ_CLUSTER_TOOLS_APP_CMD instead."
fi

# Resolve settings: command line > MU2EDAQ_CLUSTER_TOOLS_* environment >
# default. INSTALL_DIR and APP_CMD are reused as the script's own variables,
# overwriting whatever the environment held.
INSTALL_DIR="${OPT_PREFIX:-${MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR:-${HOME}/.local/share/mu2edaq-cluster-tools}}"
INSTALL_DIR="$(abspath "${INSTALL_DIR}")"
APP_CMD="${OPT_APP_CMD:-${MU2EDAQ_CLUSTER_TOOLS_APP_CMD:-ssh-selector}}"
# APP_CMD becomes a file name in BIN_DIR; restricting it to a plain name
# rules out "/" (writing outside BIN_DIR), ".." and shell metacharacters.
[[ "${APP_CMD}" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid launcher name: ${APP_CMD}"
MARKER="${INSTALL_DIR}/${MARKER_NAME}"
# Runs before either action, so a dangerous directory is refused for
# install and --uninstall alike.
check_safe_dir "${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Uninstall: remove exactly the entries the manifest lists, nothing else
# ---------------------------------------------------------------------------
# Why a manifest: an uninstaller that ran "rm -rf $PREFIX" would turn a
# mistyped --prefix (say, a project directory) into data loss. Instead the
# install records each path it creates, and only those are removed. A
# directory without the manifest was not created by this script, so nothing
# at all is deleted from it. Each entry is still re-validated, because the
# manifest is a plain user-writable file that may have been edited or
# corrupted.
if [[ "${ACTION}" == uninstall ]]; then
    echo "Uninstalling mu2edaq-cluster-tools from ${INSTALL_DIR}"
    if [[ ! -d "${INSTALL_DIR}" ]]; then
        echo "Nothing to remove (${INSTALL_DIR} does not exist)."
        exit 0
    fi
    [[ -f "${MARKER}" ]] || die "${INSTALL_DIR} has no ${MARKER_NAME} manifest; \
it was not created by this script, so nothing is removed."

    # Manifest lines are "<kind><TAB><absolute path>"; "#" lines are comments.
    # IFS=TAB keeps spaces inside paths intact; -r keeps backslashes literal.
    while IFS=$'\t' read -r kind path; do
        [[ -z "${kind}" || "${kind}" == \#* ]] && continue
        case "${kind}" in
            venv|file)
                # venv must be exactly DIR/venv; files strictly inside DIR.
                # The venv is the only recursive delete, so its path is
                # pinned exactly rather than merely required to be inside DIR.
                if [[ "${kind}" == venv && "${path}" != "${INSTALL_DIR}/venv" ]]; then
                    warn "skipping venv entry ${path}: not ${INSTALL_DIR}/venv"; continue
                fi
                # "/?*" requires at least one character after "DIR/", so DIR
                # itself can never be an entry.
                case "${path}" in
                    "${INSTALL_DIR}"/?*) ;;
                    *) warn "skipping ${path}: outside ${INSTALL_DIR}"; continue ;;
                esac
                # DIR/../x would pass the prefix test but escape DIR.
                [[ "${path}" == */../* ]] && { warn "skipping ${path}: contains .."; continue; }
                # Never rm -rf through a symlink named venv: only a real
                # directory is removed recursively. A file entry may be a
                # symlink; rm -f removes the link, not its target.
                if [[ "${kind}" == venv && -d "${path}" && ! -L "${path}" ]]; then
                    rm -rf -- "${path}"; success "Removed ${path}"
                elif [[ "${kind}" == file && ( -f "${path}" || -L "${path}" ) ]]; then
                    rm -f -- "${path}"; success "Removed ${path}"
                fi ;;
            launcher)
                # Only launchers this script generated for this install directory.
                # Launchers live outside DIR (in BIN_DIR), so the content
                # check of is_our_launcher replaces the "inside DIR" test; a
                # file the operator has since replaced is kept.
                if is_our_launcher "${path}"; then
                    rm -f -- "${path}"; success "Removed ${path}"
                elif [[ -e "${path}" ]]; then
                    warn "kept ${path}: not a launcher generated for ${INSTALL_DIR}"
                fi ;;
            # Kinds written by some future install.sh are left alone rather
            # than guessed at.
            *) warn "ignoring unknown manifest entry: ${kind}" ;;
        esac
    done < "${MARKER}"

    rm -f -- "${MARKER}"
    # rmdir (not rm -r) only removes empty directories: anything the operator
    # added to DIR or DIR/config survives, and DIR is then kept.
    rmdir "${INSTALL_DIR}/config" 2>/dev/null || true
    if rmdir "${INSTALL_DIR}" 2>/dev/null; then
        success "Removed ${INSTALL_DIR}"
    else
        warn "kept ${INSTALL_DIR}: it still holds files this script did not install"
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Install: refuse a non-empty directory that this script does not own
# ---------------------------------------------------------------------------
# Ownership rules: a missing or empty directory is taken; a directory with
# our manifest is a re-install or upgrade; a manifest-less directory with
# exactly the install.sh <= 1.0.1 layout is adopted (a manifest is written
# below, after which it is managed like any other install); anything else
# might be operator data and is refused, because a later --uninstall would
# otherwise be entitled to delete DIR/venv inside it.
if [[ -d "${INSTALL_DIR}" && ! -f "${MARKER}" ]] \
        && [[ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]]; then
    if is_legacy_install; then
        info "Adopting an install made by an older install.sh in ${INSTALL_DIR}"
    else
        die "${INSTALL_DIR} exists, is not empty and has no ${MARKER_NAME} manifest; \
refusing to install into it. Choose another --prefix."
    fi
fi

# Find Python 3.9+
# python3 first (the usual name on Linux and macOS), then explicit minor
# versions for hosts where python3 is an older system Python, then plain
# "python", which is Python 3 on some distributions and in conda
# environments. The version test runs in the candidate itself; "|| true"
# stops set -e from aborting on Python 2 or a broken interpreter, which
# simply yield an empty result.
PYTHON=""
for candidate in python3 python3.12 python3.11 python3.10 python3.9 python; do
    if cmd="$(command -v "${candidate}" 2>/dev/null)"; then
        ok=$("${cmd}" -c "import sys; print('ok' if sys.version_info >= (3,9) else '')" 2>/dev/null || true)
        if [[ "${ok}" == "ok" ]]; then
            PYTHON="${cmd}"
            break
        fi
    fi
done
[[ -n "${PYTHON}" ]] || die "Python 3.9 or newer is required but was not found on PATH."
PY_VERSION=$("${PYTHON}" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

# Find (or create) the bin directory
# Prefer a directory that already exists: ~/.local/bin is on PATH by default
# in many Linux login shells (e.g. AlmaLinux's ~/.bash_profile), ~/bin is
# the traditional location. On macOS neither is on PATH by default, which
# the PATH check at the end reports.
BIN_DIR="${OPT_BIN_DIR:-${MU2EDAQ_CLUSTER_TOOLS_BIN_DIR:-}}"
if [[ -z "${BIN_DIR}" ]]; then
    for candidate in "${HOME}/.local/bin" "${HOME}/bin"; do
        if [[ -d "${candidate}" ]]; then BIN_DIR="${candidate}"; break; fi
    done
    BIN_DIR="${BIN_DIR:-${HOME}/.local/bin}"
fi
BIN_DIR="$(abspath "${BIN_DIR}")"

# Refuse to clobber an existing command of the same name (another
# ssh-selector, a lan-scan from a different package, ...). Only a launcher
# tagged by this script for this INSTALL_DIR may be overwritten; the legacy
# tag is accepted so a 1.0.1 launcher is upgraded in place. This check runs
# before anything is written, so a refusal leaves no partial install.
LAUNCHER="${BIN_DIR}/${APP_CMD}"
LAN_SCAN_LAUNCHER="${BIN_DIR}/lan-scan"
for l in "${LAUNCHER}" "${LAN_SCAN_LAUNCHER}"; do
    if [[ -e "${l}" || -L "${l}" ]] && ! is_our_launcher "${l}"; then
        die "${l} exists and is not a launcher this script wrote for ${INSTALL_DIR}; refusing to overwrite it."
    fi
done

echo "Installing mu2edaq-cluster-tools $(version)"
echo "─────────────────────────────────────────────"
info "Source   : ${SCRIPT_DIR}"
info "App dir  : ${INSTALL_DIR}"
info "Configs  : ${INSTALL_DIR}/config"
info "Commands : ${LAUNCHER}, ${LAN_SCAN_LAUNCHER}"
info "Python   : ${PYTHON} (${PY_VERSION})"
echo

mkdir -p "${INSTALL_DIR}/config" "${BIN_DIR}"

# Write the manifest first so an interrupted install can still be uninstalled.
# Format: one "<kind>\t<absolute path>" per line; kinds venv, file, launcher.
# Launchers recorded by an earlier install (e.g. another --bin-dir) are kept.
# The manifest is written to a temporary file and renamed into place: mv
# within one directory is an atomic rename, so a crash never leaves a
# truncated manifest that would make --uninstall forget entries.
OLD_LAUNCHERS=""
# "|| true": grep exits 1 when the old manifest lists no launchers, which
# would otherwise abort the script under set -e.
[[ -f "${MARKER}" ]] && OLD_LAUNCHERS="$(grep $'^launcher\t' "${MARKER}" || true)"
{
    echo "# mu2edaq-cluster-tools install manifest -- read by install.sh --uninstall"
    echo "# version $(version), source ${SCRIPT_DIR}"
    printf 'venv\t%s\n' "${INSTALL_DIR}/venv"
    for f in "${HOST_CONFIGS[@]}" "${REFERENCE_FILES[@]}"; do
        printf 'file\t%s\n' "${INSTALL_DIR}/config/${f}"
    done
    # New launchers plus the old ones; awk drops duplicate lines while
    # keeping first-seen order (portable to BSD awk on macOS).
    { printf 'launcher\t%s\n' "${LAUNCHER}" "${LAN_SCAN_LAUNCHER}"
      if [[ -n "${OLD_LAUNCHERS}" ]]; then printf '%s\n' "${OLD_LAUNCHERS}"; fi
    } | awk '!seen[$0]++'
} > "${MARKER}.tmp"
mv -f "${MARKER}.tmp" "${MARKER}"

# Host files are overwritten on every install so that topology updates
# shipped with a new release reach the operator. Personal host files belong
# in ~/.config/mu2edaq/cluster-tools/, which this script never touches.
echo "Copying host and reference configs..."
for f in "${HOST_CONFIGS[@]}" "${REFERENCE_FILES[@]}"; do
    cp "${SCRIPT_DIR}/config/${f}" "${INSTALL_DIR}/config/${f}"
done
success "Configs copied to ${INSTALL_DIR}/config"

# An existing venv (re-install, upgrade or adopted legacy install) is reused
# and upgraded in place by pip below. A venv records the absolute path of
# its interpreter; if that Python has since been removed, run --uninstall
# and install again.
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    echo "Creating virtual environment..."
    "${PYTHON}" -m venv "${INSTALL_DIR}/venv"
    success "Virtual environment created"
else
    info "Virtual environment already exists — updating"
fi

# pip is always run as "venv python -m pip", so the venv's own pip is used
# regardless of PATH or an activated environment. The install from
# ${SCRIPT_DIR} is deliberately non-editable: the package is copied into the
# venv's site-packages, so the checkout can be moved, updated or deleted
# without breaking the installed commands (bootstrap.sh --dev provides the
# editable developer install). The venv/bin paths are the POSIX venv layout
# of Linux, macOS and WSL; a native Windows venv uses venv/Scripts instead.
echo "Installing mu2edaq-cluster-tools package..."
"${INSTALL_DIR}/venv/bin/python" -m pip install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/python" -m pip install --quiet "${SCRIPT_DIR}"
success "Package installed"

## @brief Write one executable launcher script that execs a console entry
##        point from the install's venv.
##
## The launcher is a small wrapper rather than a symlink to venv/bin/ENTRY:
## it carries LAUNCHER_TAG, which lets re-installs and --uninstall prove it
## is ours (see is_our_launcher()), and it embeds the absolute INSTALL_DIR,
## which check_safe_dir() has verified to be free of quotes and shell
## metacharacters. `exec` replaces the wrapper shell, so signals and the
## exit status pass straight through. The venv need not be activated: its
## console scripts already point at the venv interpreter.
##
## Reads the globals INSTALL_DIR and LAUNCHER_TAG.
## @param $1 launcher path to create or overwrite (in BIN_DIR)
## @param $2 entry-point name in INSTALL_DIR/venv/bin (ssh-selector, lan-scan)
## @return 0 on success; a failed write or chmod aborts the script via set -e
## @warning Overwrites $1 unconditionally. The caller must already have
##          checked it with is_our_launcher(), which the install does
##          before anything is written.
write_launcher() {
    local path="$1" entry="$2"
    # The unquoted LAUNCHER_EOF delimiter makes ${INSTALL_DIR}, ${entry} and
    # ${LAUNCHER_TAG} expand now, at install time; "\$@" is escaped so the
    # launcher forwards its own arguments at run time. Do not add comments
    # inside the here-document: they would be copied into every launcher.
    cat > "${path}" <<LAUNCHER_EOF
#!/usr/bin/env bash
${LAUNCHER_TAG}
# Launcher for mu2edaq-cluster-tools / ${entry} -- do not edit directly.
exec "${INSTALL_DIR}/venv/bin/${entry}" "\$@"
LAUNCHER_EOF
    chmod +x "${path}"
    success "Launcher written to ${path}"
}
write_launcher "${LAUNCHER}" ssh-selector
write_launcher "${LAN_SCAN_LAUNCHER}" lan-scan

# Print the exact uninstall command; --prefix is only needed when the
# install directory differs from the default.
uninstall_cmd="${SCRIPT_DIR}/install.sh --uninstall"
if [[ "${INSTALL_DIR}" != "$(abspath "${HOME}/.local/share/mu2edaq-cluster-tools")" ]]; then
    uninstall_cmd+=" --prefix ${INSTALL_DIR}"
fi
echo
echo "Installation complete."
echo "Run the application with:  ${APP_CMD}"
echo "Scan your LAN with:        lan-scan"
echo "Personal host files go in: ~/.config/mu2edaq/cluster-tools/"
echo "To uninstall:              ${uninstall_cmd}"
echo

# Split PATH on ":" into lines and look for BIN_DIR as an exact whole line;
# a substring match would be fooled by e.g. /home/op/.local/bin2. Only a
# hint is printed; the operator's shell startup files are never edited.
if ! printf '%s\n' "${PATH//:/$'\n'}" | grep -qxF "${BIN_DIR}"; then
    echo "─────────────────────────────────────────────"
    warn "${BIN_DIR} is not on your PATH."
    echo
    echo "Add this line to your shell config (~/.bashrc or ~/.zshrc):"
    echo
    echo "    export PATH=\"${BIN_DIR}:\${PATH}\""
    echo
fi
