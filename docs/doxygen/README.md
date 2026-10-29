# Doxygen API documentation

This directory holds the Doxygen configuration for mu2edaq-cluster-tools.
Doxygen extracts the comments from the Python package and the Bash
installers into a browsable HTML reference.

## Building

Install Doxygen 1.9 or later, then run from the repository root:

```sh
mkdir -p build/doxygen    # Doxygen creates only the last path level
doxygen docs/doxygen/Doxyfile
open build/doxygen/html/index.html        # macOS
xdg-open build/doxygen/html/index.html    # Linux
```

Warnings are written to `build/doxygen/warnings.log`. To audit comment
coverage, rerun with `EXTRACT_ALL = NO`; each "is not documented" line is a gap. `build/` is git-ignored.

| Platform | Install Doxygen |
|---|---|
| AlmaLinux 9 | `sudo dnf install doxygen` |
| macOS | `brew install doxygen` |
| Windows 11 | `winget install DimitriVanHeesch.Doxygen` |

## Comment conventions

### Python

Every module, class, method, and function carries a docstring that opens
with `"""!`. The `!` tells Doxygen to interpret Doxygen commands inside
the docstring. The docstring stays an ordinary docstring for `help()`.

```python
def ssh_command(self, user: str, skip_proxy: bool = False) -> list[str]:
    """!
    @brief Build the argv that opens an interactive SSH session to this host.

    The jump host, when configured, is passed with `-J` so that OpenSSH
    performs the hop itself and Kerberos credentials are forwarded.

    @param user        Login name on the destination, or "default".
    @param skip_proxy  If True, connect directly and ignore proxy_jump.
    @return argv list suitable for subprocess.run().
    """
```

Required tags:

- Modules use `@file`, `@brief`, and a description of the module's role.
- Classes use `@brief` and describe state, invariants, and thread affinity.
- Functions use `@brief`, one `@param` per argument, `@return` when a value
  is returned, and `@throws` for exceptions that escape.
- Use `@note` for platform differences and `@warning` for operator hazards.

Inline `#` comments explain why a line exists, not what it does.

### Bash

Doxygen has no Bash parser. The Doxyfile maps `.sh` to the Python parser
and runs `bash-filter.sh` as an input filter. The filter turns each
`name() {` definition into a `def name(): pass` stub and blanks other
code lines, so only `##` comment blocks reach Doxygen.

```bash
## @file install.sh
## @brief Install ssh-selector and lan-scan into a private virtualenv.

## @brief Print an informational message to stdout.
## @param $* message text
info() { echo "  $*"; }
```

Top-of-file blocks use `@file`, `@brief`, usage, options, environment
variables, and exit status. Function blocks use `@brief`, `@param` for
positional arguments, and `@return` for the exit status.
