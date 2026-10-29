"""!
@file __init__.py
@brief mu2edaq-cluster-tools - SSH host selector and LAN scanner for DAQ clusters.

Package root for the two console scripts:
  - `ssh-selector` (ssh_selector.main): Textual TUI for picking a DAQ host
    and opening an SSH session, terminal or tunnel.
  - `lan-scan` (lan_scan.main): rate-limited ICMP/TCP sweep of a local network.

The package root re-exports only `__version__`.

@note This module must not import Textual (directly or via ssh_selector).
      Python runs a package's `__init__` before any of its submodules, so
      anything imported here is paid for by every entry point. lan-scan does
      not use Textual; importing it here would slow lan-scan start-up and
      make lan-scan fail on hosts where Textual is absent or broken.
@see _version.py for the single source of the version string.
"""

# Import only the version module: importing ssh_selector here would load
# Textual for every entry point, including lan-scan, which does not use it.
from ._version import __version__

## Limit `from mu2edaq_cluster_tools import *` to the version, so that a
## wildcard import cannot pull in a submodule (and Textual) by accident.
__all__ = ["__version__"]
