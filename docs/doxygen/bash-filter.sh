#!/usr/bin/env bash
## @file bash-filter.sh
## @brief Doxygen INPUT_FILTER that lets Doxygen document Bash scripts.
##
## Doxygen has no Bash parser. With EXTENSION_MAPPING sh=Python it parses
## shell scripts as Python, where `##` lines are Doxygen comment blocks.
## This filter rewrites each shell function definition `name() {` into a
## Python-shaped `def name(): pass` stub so Doxygen attaches the preceding
## `##` block to a function entity, and blanks every other non-comment line
## so shell syntax cannot confuse the Python parser. Line count is preserved
## so source cross-references stay correct.
##
## @param $1 path of the script Doxygen is processing
## @return filtered text on stdout
##
## Example:
## @code
##   docs/doxygen/bash-filter.sh install.sh | less
## @endcode
set -euo pipefail
sed -E \
    -e 's/^[[:space:]]*(function[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*\(\)[[:space:]]*\{.*$/def \2(): pass/' \
    -e '/^def [A-Za-z_][A-Za-z0-9_]*\(\): pass$/b' \
    -e '/^[[:space:]]*##/b' \
    -e 's/.*//' \
    "$1"
