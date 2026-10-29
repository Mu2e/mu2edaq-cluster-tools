"""!
@file test_host.py
@brief Tests for the ssh-selector Host dataclass: defaults, proxy_target, ssh_command and tunnel_command.

Includes the regression test for GitHub issue #3 (the jump host must use the
destination user unless proxy_user is set). The ExitOnForwardFailure option
added for issue #9 is tested in test_tunnels.py.
"""

import pytest

from mu2edaq_cluster_tools.ssh_selector import CURRENT_USER, Host


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plain_host():
    """!
    @brief Host on port 22 with the `default` user and no jump host.
    @return Host instance.
    """
    return Host(hostname="node01.example.com", nickname="Node 01", users=["default"])


@pytest.fixture
def host_with_port():
    """!
    @brief Host on port 2222 with user `admin`.
    @return Host instance.
    """
    return Host(hostname="node01.example.com", nickname="Node 01", port=2222, users=["admin"])


@pytest.fixture
def host_with_proxy():
    """!
    @brief Host reached through gateway.example.com, no proxy_user.
    @return Host instance.
    """
    return Host(
        hostname="node01.example.com",
        nickname="Node 01",
        users=["default"],
        proxy_jump="gateway.example.com",
    )


@pytest.fixture
def host_with_proxy_user():
    """!
    @brief Host reached through gateway.example.com as proxy_user `proxyacct`.
    @return Host instance.
    """
    return Host(
        hostname="node01.example.com",
        nickname="Node 01",
        users=["default"],
        proxy_jump="gateway.example.com",
        proxy_user="proxyacct",
    )


# ---------------------------------------------------------------------------
# Host construction
# ---------------------------------------------------------------------------

class TestHostDefaults:
    """!
    @brief Field defaults applied in Host.__post_init__.
    """
    def test_nickname_defaults_to_hostname(self):
        """!
        @brief A missing nickname becomes the hostname.
        """
        h = Host(hostname="srv.example.com")
        assert h.nickname == "srv.example.com"

    def test_explicit_nickname(self):
        """!
        @brief An explicit nickname is kept.
        """
        h = Host(hostname="srv.example.com", nickname="My Server")
        assert h.nickname == "My Server"

    def test_users_defaults_to_default(self):
        """!
        @brief Users default to ['default'].
        """
        h = Host(hostname="srv.example.com")
        assert h.users == ["default"]

    def test_empty_users_list_normalised(self):
        """!
        @brief An empty users list is normalised to ['default'].
        """
        h = Host(hostname="srv.example.com", users=[])
        assert h.users == ["default"]

    def test_port_defaults_to_22(self):
        """!
        @brief Port defaults to 22.
        """
        h = Host(hostname="srv.example.com")
        assert h.port == 22

    def test_proxy_jump_defaults_to_none(self):
        """!
        @brief proxy_jump defaults to None.
        """
        h = Host(hostname="srv.example.com")
        assert h.proxy_jump is None

    def test_proxy_user_defaults_to_none(self):
        """!
        @brief proxy_user defaults to None.
        """
        h = Host(hostname="srv.example.com")
        assert h.proxy_user is None


# ---------------------------------------------------------------------------
# Host.proxy_target
# ---------------------------------------------------------------------------

class TestProxyTarget:
    """!
    @brief Host.proxy_target(): the `user@jumphost` passed to `-J`.
    """
    def test_no_proxy_jump_returns_none(self, plain_host):
        """!
        @brief Without proxy_jump there is no target.
        """
        assert plain_host.proxy_target("alice") is None

    def test_defaults_to_connecting_user(self, host_with_proxy):
        """!
        @brief The jump host uses the destination user by default (#3).
        """
        assert host_with_proxy.proxy_target("mu2edaq") == "mu2edaq@gateway.example.com"

    def test_resolves_default_token(self, host_with_proxy):
        """!
        @brief The `default` token is resolved to the current login.
        """
        assert host_with_proxy.proxy_target("default") == f"{CURRENT_USER}@gateway.example.com"

    def test_proxy_user_overrides_connecting_user(self, host_with_proxy_user):
        """!
        @brief proxy_user replaces the destination user on the jump host.
        """
        assert host_with_proxy_user.proxy_target("alice") == "proxyacct@gateway.example.com"

    def test_embedded_at_sign_passed_through_unchanged(self):
        """!
        @brief A legacy `user@host` proxy_jump is used verbatim.
        """
        h = Host(
            hostname="node01.example.com",
            proxy_jump="legacyuser@gateway.example.com",
        )
        assert h.proxy_target("alice") == "legacyuser@gateway.example.com"


# ---------------------------------------------------------------------------
# Host.ssh_command
# ---------------------------------------------------------------------------

class TestSSHCommand:
    """!
    @brief Host.ssh_command() argv for interactive sessions.
    """
    def test_basic_command(self, plain_host):
        """!
        @brief argv starts with ssh, passes the user with `-l` and ends with the hostname.
        """
        cmd = plain_host.ssh_command("alice")
        assert cmd[0] == "ssh"
        assert "-l" in cmd
        assert cmd[cmd.index("-l") + 1] == "alice"
        assert cmd[-1] == "node01.example.com"

    def test_default_user_resolved(self, plain_host):
        """!
        @brief `default` is resolved to the current login.
        """
        cmd = plain_host.ssh_command("default")
        assert CURRENT_USER in cmd

    def test_no_port_flag_at_default(self, plain_host):
        """!
        @brief Port 22 adds no `-p`.
        """
        cmd = plain_host.ssh_command("alice")
        assert "-p" not in cmd

    def test_custom_port_included(self, host_with_port):
        """!
        @brief A non-default port is passed with `-p`.
        """
        cmd = host_with_port.ssh_command("admin")
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "2222"

    def test_proxy_jump_included(self, host_with_proxy):
        """!
        @brief A jump host adds `-J`.
        """
        cmd = host_with_proxy.ssh_command("alice")
        assert "-J" in cmd

    def test_proxy_jump_defaults_to_connecting_user(self, host_with_proxy):
        """!
        @brief Regression test for #3: jump host should use the same user as the destination.
        """
        cmd = host_with_proxy.ssh_command("mu2edaq")
        assert cmd[cmd.index("-J") + 1] == "mu2edaq@gateway.example.com"

    def test_proxy_jump_default_resolves_default_user(self, host_with_proxy):
        """!
        @brief `-J` resolves the `default` token to the current login.
        """
        cmd = host_with_proxy.ssh_command("default")
        assert cmd[cmd.index("-J") + 1] == f"{CURRENT_USER}@gateway.example.com"

    def test_proxy_user_overrides_connecting_user(self, host_with_proxy_user):
        """!
        @brief `-J` uses proxy_user when set.
        """
        cmd = host_with_proxy_user.ssh_command("alice")
        assert cmd[cmd.index("-J") + 1] == "proxyacct@gateway.example.com"

    def test_proxy_jump_skipped_when_requested(self, host_with_proxy):
        """!
        @brief skip_proxy=True omits `-J`.
        """
        cmd = host_with_proxy.ssh_command("alice", skip_proxy=True)
        assert "-J" not in cmd

    def test_no_proxy_no_j_flag(self, plain_host):
        """!
        @brief A host without proxy_jump gets no `-J`.
        """
        cmd = plain_host.ssh_command("alice")
        assert "-J" not in cmd

    def test_hostname_is_last_arg(self, plain_host):
        """!
        @brief The hostname is the last argument.
        """
        cmd = plain_host.ssh_command("alice")
        assert cmd[-1] == plain_host.hostname

    def test_hostname_last_with_proxy(self, host_with_proxy):
        """!
        @brief The hostname stays last when `-J` is present.
        """
        cmd = host_with_proxy.ssh_command("alice")
        assert cmd[-1] == host_with_proxy.hostname


# ---------------------------------------------------------------------------
# Host.tunnel_command
# ---------------------------------------------------------------------------

class TestTunnelCommand:
    """!
    @brief Host.tunnel_command() argv for `-N -L` port forwards.
    """
    def test_includes_N_flag(self, plain_host):
        """!
        @brief Tunnels run with `-N` (no remote command).
        """
        cmd = plain_host.tunnel_command("alice", 8080, 8080)
        assert "-N" in cmd

    def test_L_flag_format(self, plain_host):
        """!
        @brief `-L` is `local:localhost:remote`.
        """
        cmd = plain_host.tunnel_command("alice", 8080, 9090)
        assert "-L" in cmd
        assert cmd[cmd.index("-L") + 1] == "8080:localhost:9090"

    def test_default_user_resolved(self, plain_host):
        """!
        @brief `default` is resolved to the current login.
        """
        cmd = plain_host.tunnel_command("default", 8080, 8080)
        assert CURRENT_USER in cmd

    def test_custom_port_included(self, host_with_port):
        """!
        @brief A non-default SSH port is passed with `-p`.
        """
        cmd = host_with_port.tunnel_command("admin", 8080, 8080)
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "2222"

    def test_no_port_flag_at_default(self, plain_host):
        """!
        @brief Port 22 adds no `-p`.
        """
        cmd = plain_host.tunnel_command("alice", 8080, 8080)
        assert "-p" not in cmd

    def test_proxy_jump_included(self, host_with_proxy):
        """!
        @brief A jump host adds `-J`.
        """
        cmd = host_with_proxy.tunnel_command("alice", 8080, 8080)
        assert "-J" in cmd

    def test_proxy_jump_defaults_to_connecting_user(self, host_with_proxy):
        """!
        @brief `-J` uses the destination user by default (#3).
        """
        cmd = host_with_proxy.tunnel_command("mu2edaq", 8080, 8080)
        assert cmd[cmd.index("-J") + 1] == "mu2edaq@gateway.example.com"

    def test_proxy_user_overrides_connecting_user(self, host_with_proxy_user):
        """!
        @brief `-J` uses proxy_user when set.
        """
        cmd = host_with_proxy_user.tunnel_command("alice", 8080, 8080)
        assert cmd[cmd.index("-J") + 1] == "proxyacct@gateway.example.com"

    def test_proxy_jump_skipped(self, host_with_proxy):
        """!
        @brief skip_proxy=True omits `-J`.
        """
        cmd = host_with_proxy.tunnel_command("alice", 8080, 8080, skip_proxy=True)
        assert "-J" not in cmd

    def test_hostname_is_last_arg(self, plain_host):
        """!
        @brief The hostname is the last argument.
        """
        cmd = plain_host.tunnel_command("alice", 8080, 8080)
        assert cmd[-1] == plain_host.hostname

    def test_different_local_remote_ports(self, plain_host):
        """!
        @brief Distinct local and remote ports appear in the forward spec.
        """
        cmd = plain_host.tunnel_command("alice", 5000, 6000)
        assert "5000:localhost:6000" in " ".join(cmd)
