"""Single source of the mu2edaq-cluster-tools version.

Read by pyproject.toml ([tool.setuptools.dynamic]), the package __init__,
ssh-selector and lan-scan. Kept dependency-free so importing it never pulls
in Textual.
"""

__version__ = "1.0.1"
