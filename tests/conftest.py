"""!
@file conftest.py
@brief Shared pytest fixtures and sample data for the mu2edaq-cluster-tools test suite.

Provides the YAML host-file builders used by the ssh-selector configuration
tests, canned `klist` outputs (macOS Heimdal and Linux MIT formats) for the
Kerberos parsing tests, and an autouse fixture that isolates config
discovery (GitHub issue #13) from the operator's real environment.
"""

import getpass
import os
import textwrap
from pathlib import Path

import pytest


CURRENT_USER = os.environ.get("USER", getpass.getuser())


# ---------------------------------------------------------------------------
# YAML config helpers
# ---------------------------------------------------------------------------

def write_yaml(tmp_path: Path, content: str) -> Path:
    """!
    @brief Write de-dented YAML text to `hosts.yaml` under a temporary directory.

    @param tmp_path  Directory to write into (normally the pytest tmp_path).
    @param content   YAML text; common leading indentation is removed.
    @return Path of the written file.
    """
    p = tmp_path / "hosts.yaml"
    p.write_text(textwrap.dedent(content))
    return p


@pytest.fixture
def minimal_config(tmp_path):
    """!
    @brief Host file with a single host and no groups or optional fields.

    @param tmp_path  pytest per-test temporary directory.
    @return Path to the YAML file; exercises the load_config defaults.
    """
    return write_yaml(tmp_path, """
        hosts:
          - hostname: test.example.com
            nickname: Test Host
    """)


@pytest.fixture
def grouped_config(tmp_path):
    """!
    @brief Host file with two groups, group-level defaults and host overrides.

    Group `Access` sets users only; group `Backend` sets a proxy_jump and users.
    Host `db2` overrides users and nulls proxy_jump to test per-host precedence.

    @param tmp_path  pytest per-test temporary directory.
    @return Path to the YAML file (cache_lifetime 120).
    """
    return write_yaml(tmp_path, """
        cache_lifetime: 120

        grouplist:
          - Access
          - Backend

        groups:
          Access:
            users:
              - default
              - admin
          Backend:
            proxy_jump: gw.example.com
            users:
              - default
              - deploy

        hosts:
          - hostname: gateway.example.com
            nickname: Gateway
            group: Access

          - hostname: db1.example.com
            nickname: DB Primary
            group: Backend

          - hostname: db2.example.com
            nickname: DB Replica
            group: Backend
            users:
              - dbadmin
              - default
            proxy_jump: null
    """)


# ---------------------------------------------------------------------------
# Sample klist output
# ---------------------------------------------------------------------------

KLIST_MACOS = """\
Credentials cache: API:DEADBEEF
        Principal: testuser@EXAMPLE.ORG

  Issued                Expires               Principal
Apr  1 10:00:00 2026  Apr  2 10:00:00 2026  krbtgt/EXAMPLE.ORG@EXAMPLE.ORG
Apr  1 10:00:00 2026  Apr  2 10:00:00 2026  afs/example.org@EXAMPLE.ORG
"""

KLIST_LINUX = """\
Ticket cache: FILE:/tmp/krb5cc_1000
Default principal: testuser@EXAMPLE.ORG

Valid starting       Expires              Service principal
04/01/2026 10:00:00  04/02/2026 10:00:00  krbtgt/EXAMPLE.ORG@EXAMPLE.ORG
04/01/2026 10:00:00  04/02/2026 10:00:00  afs/example.org@EXAMPLE.ORG
"""

KLIST_EMPTY = ""

KLIST_EXPIRED = """\
        Principal: testuser@EXAMPLE.ORG

  Issued                Expires               Principal
Jan  1 10:00:00 2020  Jan  2 10:00:00 2020  krbtgt/EXAMPLE.ORG@EXAMPLE.ORG
"""


# ---------------------------------------------------------------------------
# Config discovery isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_config_discovery(tmp_path_factory, monkeypatch):
    """!
    @brief Autouse fixture that keeps the operator's real ~/.config and environment out of every test.

    Points XDG_CONFIG_HOME at an empty temporary directory and removes
    SSH_SELECTOR_CONFIG and MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR, so config
    discovery (issue #13) never finds a developer's own host files or install
    tree. Tests that need those variables set them again via monkeypatch.

    @param tmp_path_factory  Factory for the empty XDG directory.
    @param monkeypatch       Restores the environment after each test.
    @return None; the effect is the patched environment.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    monkeypatch.delenv("SSH_SELECTOR_CONFIG", raising=False)
    monkeypatch.delenv("MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR", raising=False)
