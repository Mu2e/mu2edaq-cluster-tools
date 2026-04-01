"""Tests for the Host dataclass: ssh_command and tunnel_command builders."""

import pytest

from ssh_selector import CURRENT_USER, Host


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plain_host():
    return Host(hostname="node01.example.com", nickname="Node 01", users=["default"])


@pytest.fixture
def host_with_port():
    return Host(hostname="node01.example.com", nickname="Node 01", port=2222, users=["admin"])


@pytest.fixture
def host_with_proxy():
    return Host(
        hostname="node01.example.com",
        nickname="Node 01",
        users=["default"],
        proxy_jump="gateway.example.com",
    )


# ---------------------------------------------------------------------------
# Host construction
# ---------------------------------------------------------------------------

class TestHostDefaults:
    def test_nickname_defaults_to_hostname(self):
        h = Host(hostname="srv.example.com")
        assert h.nickname == "srv.example.com"

    def test_explicit_nickname(self):
        h = Host(hostname="srv.example.com", nickname="My Server")
        assert h.nickname == "My Server"

    def test_users_defaults_to_default(self):
        h = Host(hostname="srv.example.com")
        assert h.users == ["default"]

    def test_empty_users_list_normalised(self):
        h = Host(hostname="srv.example.com", users=[])
        assert h.users == ["default"]

    def test_port_defaults_to_22(self):
        h = Host(hostname="srv.example.com")
        assert h.port == 22

    def test_proxy_jump_defaults_to_none(self):
        h = Host(hostname="srv.example.com")
        assert h.proxy_jump is None


# ---------------------------------------------------------------------------
# Host.ssh_command
# ---------------------------------------------------------------------------

class TestSSHCommand:
    def test_basic_command(self, plain_host):
        cmd = plain_host.ssh_command("alice")
        assert cmd[0] == "ssh"
        assert "-l" in cmd
        assert cmd[cmd.index("-l") + 1] == "alice"
        assert cmd[-1] == "node01.example.com"

    def test_default_user_resolved(self, plain_host):
        cmd = plain_host.ssh_command("default")
        assert CURRENT_USER in cmd

    def test_no_port_flag_at_default(self, plain_host):
        cmd = plain_host.ssh_command("alice")
        assert "-p" not in cmd

    def test_custom_port_included(self, host_with_port):
        cmd = host_with_port.ssh_command("admin")
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "2222"

    def test_proxy_jump_included(self, host_with_proxy):
        cmd = host_with_proxy.ssh_command("alice")
        assert "-J" in cmd
        assert cmd[cmd.index("-J") + 1] == "gateway.example.com"

    def test_proxy_jump_skipped_when_requested(self, host_with_proxy):
        cmd = host_with_proxy.ssh_command("alice", skip_proxy=True)
        assert "-J" not in cmd

    def test_no_proxy_no_j_flag(self, plain_host):
        cmd = plain_host.ssh_command("alice")
        assert "-J" not in cmd

    def test_hostname_is_last_arg(self, plain_host):
        cmd = plain_host.ssh_command("alice")
        assert cmd[-1] == plain_host.hostname

    def test_hostname_last_with_proxy(self, host_with_proxy):
        cmd = host_with_proxy.ssh_command("alice")
        assert cmd[-1] == host_with_proxy.hostname


# ---------------------------------------------------------------------------
# Host.tunnel_command
# ---------------------------------------------------------------------------

class TestTunnelCommand:
    def test_includes_N_flag(self, plain_host):
        cmd = plain_host.tunnel_command("alice", 8080, 8080)
        assert "-N" in cmd

    def test_L_flag_format(self, plain_host):
        cmd = plain_host.tunnel_command("alice", 8080, 9090)
        assert "-L" in cmd
        assert cmd[cmd.index("-L") + 1] == "8080:localhost:9090"

    def test_default_user_resolved(self, plain_host):
        cmd = plain_host.tunnel_command("default", 8080, 8080)
        assert CURRENT_USER in cmd

    def test_custom_port_included(self, host_with_port):
        cmd = host_with_port.tunnel_command("admin", 8080, 8080)
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "2222"

    def test_no_port_flag_at_default(self, plain_host):
        cmd = plain_host.tunnel_command("alice", 8080, 8080)
        assert "-p" not in cmd

    def test_proxy_jump_included(self, host_with_proxy):
        cmd = host_with_proxy.tunnel_command("alice", 8080, 8080)
        assert "-J" in cmd

    def test_proxy_jump_skipped(self, host_with_proxy):
        cmd = host_with_proxy.tunnel_command("alice", 8080, 8080, skip_proxy=True)
        assert "-J" not in cmd

    def test_hostname_is_last_arg(self, plain_host):
        cmd = plain_host.tunnel_command("alice", 8080, 8080)
        assert cmd[-1] == plain_host.hostname

    def test_different_local_remote_ports(self, plain_host):
        cmd = plain_host.tunnel_command("alice", 5000, 6000)
        assert "5000:localhost:6000" in " ".join(cmd)
