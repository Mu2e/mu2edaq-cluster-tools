"""!
@file _version.py
@brief Single source of the mu2edaq-cluster-tools version.

Read by pyproject.toml ([tool.setuptools.dynamic]), the package __init__,
ssh-selector and lan-scan. Kept dependency-free so importing it never pulls
in Textual.

Keeping the string in one file means a release changes exactly one line:
the wheel metadata (via setuptools' `attr:` lookup), `--version` of both
console scripts and the ssh-selector About box can never disagree.
setuptools first tries to read the literal statically from this file; a
plain string assignment with no imports keeps that path working, so a
build never has to import the package (or have Textual installed).

The value follows the project's major.minor.patch convention; the
matching git release tag is zero-padded with a `t` prefix (e.g.
`t01.00.01` for "1.0.1").
"""

## Package version string, major.minor.patch. Bump here and nowhere else.
__version__ = "1.0.1"
