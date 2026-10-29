"""mu2edaq-cluster-tools - SSH host selector and LAN scanner for DAQ clusters."""

# Import only the version module: importing ssh_selector here would load
# Textual for every entry point, including lan-scan, which does not use it.
from ._version import __version__

__all__ = ["__version__"]
