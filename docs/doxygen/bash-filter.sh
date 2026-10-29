#!/usr/bin/env bash
## @file bash-filter.sh
## @brief Doxygen INPUT_FILTER that lets Doxygen document Bash scripts.
##
## Doxygen has no Bash parser. With EXTENSION_MAPPING sh=Python it parses
## shell scripts as Python, where `##` lines are Doxygen comment blocks.
## This filter makes three rewrites:
##
## 1. Each shell function definition `name() {` becomes a Python-shaped
##    `def name(arg1, args): pass` stub, so Doxygen attaches the preceding
##    `##` block to a function entity.
## 2. Inside `##` blocks, `@param $N` becomes `@param argN` and `@param $*`
##    or `@param $@` becomes `@param args`. Doxygen checks that every
##    documented parameter appears in the signature, and `$1` is not a
##    valid identifier. The stub signature lists exactly the parameters
##    the block documents, in the order they are documented.
## 3. Every other non-comment line is blanked so shell syntax cannot
##    confuse the Python parser.
##
## Line count is preserved so source cross-references stay correct.
##
## @param $1 path of the script Doxygen is processing
## @return filtered text on stdout
##
## Example:
## @code
##   docs/doxygen/bash-filter.sh install.sh | less
## @endcode
set -euo pipefail

# awk rather than sed: the signature depends on the @param lines of the
# block above the function, so state must carry across lines. Only POSIX
# awk features are used so BSD awk (macOS) and gawk (Linux) behave alike.
awk '
# A `##` line: rename positional parameters, remember them for the stub.
/^[[:space:]]*##/ {
    line = $0
    if (match(line, /@param[[:space:]]+\$[0-9]+/)) {
        tok = substr(line, RSTART, RLENGTH)
        n = tok; sub(/.*\$/, "", n)
        sub(/\$[0-9]+/, "arg" n, line)
        params = params (params == "" ? "" : ", ") "arg" n
    } else if (match(line, /@param[[:space:]]+\$[*@]/)) {
        sub(/\$[*@]/, "args", line)
        params = params (params == "" ? "" : ", ") "args"
    }
    print line
    next
}
# A function definition: emit a stub carrying the collected parameters.
/^[[:space:]]*(function[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\(\)[[:space:]]*\{/ {
    name = $0
    sub(/^[[:space:]]*(function[[:space:]]+)?/, "", name)
    sub(/[[:space:]]*\(.*/, "", name)
    print "def " name "(" params "): pass"
    params = ""
    next
}
# Anything else ends the current comment block and is blanked.
{ params = ""; print "" }
' "$1"
