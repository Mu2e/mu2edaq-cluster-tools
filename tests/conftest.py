"""Shared pytest fixtures for ssh_selector tests."""

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
    """Write de-dented YAML content to a temp file and return its Path."""
    p = tmp_path / "hosts.yaml"
    p.write_text(textwrap.dedent(content))
    return p


@pytest.fixture
def minimal_config(tmp_path):
    """Single host, no groups, no extras."""
    return write_yaml(tmp_path, """
        hosts:
          - hostname: test.example.com
            nickname: Test Host
    """)


@pytest.fixture
def grouped_config(tmp_path):
    """Two groups with group-level defaults."""
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
