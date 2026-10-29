"""!
@file test_config.py
@brief Tests for ssh-selector host-file parsing: load_config, Config, group defaults and find_config_files.

Covers per-host defaults, cache_lifetime, group-level users/proxy_jump/
proxy_user defaults and per-host overrides (including explicit null), the
mandatory hostname field, and cwd/./config file discovery with
de-duplication and the `.yaml`-only filter.
"""

import textwrap
from pathlib import Path

import pytest

from mu2edaq_cluster_tools.ssh_selector import CURRENT_USER, Config, Host, load_config, find_config_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def yaml_file(tmp_path: Path, content: str, name: str = "hosts.yaml") -> Path:
    """!
    @brief Write de-dented YAML text to a temporary file.
    @param tmp_path  Directory to write into.
    @param content   YAML text.
    @param name      File name.
    @return Path of the written file.
    """
    p = tmp_path / name
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Minimal configs
# ---------------------------------------------------------------------------

class TestLoadConfigMinimal:
    """!
    @brief Defaults applied to a single bare host entry.
    """
    def test_single_host_loaded(self, minimal_config):
        """!
        @brief Exactly one host is loaded.
        """
        cfg = load_config(minimal_config)
        assert len(cfg.hosts) == 1

    def test_hostname_correct(self, minimal_config):
        """!
        @brief The hostname is read verbatim.
        """
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].hostname == "test.example.com"

    def test_nickname_correct(self, minimal_config):
        """!
        @brief The nickname is read verbatim.
        """
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].nickname == "Test Host"

    def test_default_port(self, minimal_config):
        """!
        @brief Port defaults to 22.
        """
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].port == 22

    def test_default_users(self, minimal_config):
        """!
        @brief Users default to ['default'].
        """
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].users == ["default"]

    def test_no_proxy_jump(self, minimal_config):
        """!
        @brief proxy_jump defaults to None.
        """
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].proxy_jump is None

    def test_no_proxy_user(self, minimal_config):
        """!
        @brief proxy_user defaults to None.
        """
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].proxy_user is None

    def test_default_cache_lifetime(self, minimal_config):
        """!
        @brief cache_lifetime defaults to 600 s.
        """
        cfg = load_config(minimal_config)
        assert cfg.cache_lifetime == 600

    def test_empty_grouplist(self, minimal_config):
        """!
        @brief grouplist defaults to empty.
        """
        cfg = load_config(minimal_config)
        assert cfg.grouplist == []


# ---------------------------------------------------------------------------
# Cache lifetime
# ---------------------------------------------------------------------------

class TestCacheLifetime:
    """!
    @brief Top-level cache_lifetime key.
    """
    def test_custom_cache_lifetime(self, tmp_path):
        """!
        @brief An explicit cache_lifetime is honoured.
        """
        p = yaml_file(tmp_path, """
            cache_lifetime: 120
            hosts:
              - hostname: srv.example.com
        """)
        cfg = load_config(p)
        assert cfg.cache_lifetime == 120

    def test_default_cache_lifetime_when_absent(self, tmp_path):
        """!
        @brief An absent cache_lifetime yields 600.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
        """)
        cfg = load_config(p)
        assert cfg.cache_lifetime == 600


# ---------------------------------------------------------------------------
# Groups and group defaults
# ---------------------------------------------------------------------------

class TestLoadConfigGroups:
    """!
    @brief Group defaults and per-host overrides using the grouped_config fixture.
    """
    def test_grouplist_order_preserved(self, grouped_config):
        """!
        @brief grouplist keeps file order.
        """
        cfg = load_config(grouped_config)
        assert cfg.grouplist == ["Access", "Backend"]

    def test_host_count(self, grouped_config):
        """!
        @brief All three hosts are loaded.
        """
        cfg = load_config(grouped_config)
        assert len(cfg.hosts) == 3

    def test_group_default_users_applied(self, grouped_config):
        """!
        @brief A host without users inherits the group's users.
        """
        cfg = load_config(grouped_config)
        gateway = next(h for h in cfg.hosts if h.hostname == "gateway.example.com")
        assert gateway.users == ["default", "admin"]

    def test_group_default_proxy_applied(self, grouped_config):
        """!
        @brief A host without proxy_jump inherits the group's jump host.
        """
        cfg = load_config(grouped_config)
        db1 = next(h for h in cfg.hosts if h.hostname == "db1.example.com")
        assert db1.proxy_jump == "gw.example.com"

    def test_host_users_override_group(self, grouped_config):
        """!
        @brief Host-level users replace the group's users.
        """
        cfg = load_config(grouped_config)
        db2 = next(h for h in cfg.hosts if h.hostname == "db2.example.com")
        assert db2.users == ["dbadmin", "default"]

    def test_host_null_proxy_overrides_group(self, grouped_config):
        """!
        @brief An explicit `proxy_jump: null` on a host cancels the group jump host.
        """
        cfg = load_config(grouped_config)
        db2 = next(h for h in cfg.hosts if h.hostname == "db2.example.com")
        assert db2.proxy_jump is None

    def test_no_proxy_group_has_none(self, grouped_config):
        """!
        @brief A group without proxy_jump leaves its hosts without one.
        """
        cfg = load_config(grouped_config)
        gateway = next(h for h in cfg.hosts if h.hostname == "gateway.example.com")
        assert gateway.proxy_jump is None


# ---------------------------------------------------------------------------
# Per-host field variations
# ---------------------------------------------------------------------------

class TestLoadConfigHostFields:
    """!
    @brief Individual host fields and their validation.
    """
    def test_custom_port(self, tmp_path):
        """!
        @brief A non-default port is honoured.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                port: 2222
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].port == 2222

    def test_nickname_defaults_to_hostname(self, tmp_path):
        """!
        @brief A missing nickname falls back to the hostname.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].nickname == "srv.example.com"

    def test_scalar_user_field(self, tmp_path):
        """!
        @brief A scalar `user:` key becomes a one-element users list.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                user: admin
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].users == ["admin"]

    def test_users_list_field(self, tmp_path):
        """!
        @brief A `users:` list is kept in order.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                users:
                  - alice
                  - bob
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].users == ["alice", "bob"]

    def test_explicit_proxy_jump(self, tmp_path):
        """!
        @brief A host-level proxy_jump is honoured.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                proxy_jump: gw.example.com
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].proxy_jump == "gw.example.com"

    def test_explicit_proxy_user(self, tmp_path):
        """!
        @brief A host-level proxy_user is honoured.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                proxy_jump: gw.example.com
                proxy_user: proxyacct
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].proxy_user == "proxyacct"

    def test_group_default_proxy_user_applied(self, tmp_path):
        """!
        @brief A host inherits the group's proxy_user.
        """
        p = yaml_file(tmp_path, """
            grouplist:
              - Backend
            groups:
              Backend:
                proxy_jump: gw.example.com
                proxy_user: proxyacct
            hosts:
              - hostname: srv.example.com
                group: Backend
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].proxy_user == "proxyacct"

    def test_host_proxy_user_overrides_group(self, tmp_path):
        """!
        @brief A host-level proxy_user replaces the group's.
        """
        p = yaml_file(tmp_path, """
            grouplist:
              - Backend
            groups:
              Backend:
                proxy_jump: gw.example.com
                proxy_user: groupacct
            hosts:
              - hostname: srv.example.com
                group: Backend
                proxy_user: hostacct
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].proxy_user == "hostacct"

    def test_host_null_proxy_user_overrides_group(self, tmp_path):
        """!
        @brief An explicit `proxy_user: null` cancels the group's proxy_user.
        """
        p = yaml_file(tmp_path, """
            grouplist:
              - Backend
            groups:
              Backend:
                proxy_jump: gw.example.com
                proxy_user: groupacct
            hosts:
              - hostname: srv.example.com
                group: Backend
                proxy_user: null
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].proxy_user is None

    def test_group_field_stored(self, tmp_path):
        """!
        @brief The host's group name is stored on the Host.
        """
        p = yaml_file(tmp_path, """
            grouplist:
              - MyGroup
            groups:
              MyGroup:
                users: [default]
            hosts:
              - hostname: srv.example.com
                group: MyGroup
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].group == "MyGroup"

    def test_missing_hostname_raises(self, tmp_path):
        """!
        @brief A host entry without hostname raises ValueError.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - nickname: No Hostname
        """)
        with pytest.raises(ValueError, match="hostname"):
            load_config(p)

    def test_multiple_hosts_all_loaded(self, tmp_path):
        """!
        @brief Every host entry in the list is loaded.
        """
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: a.example.com
              - hostname: b.example.com
              - hostname: c.example.com
        """)
        cfg = load_config(p)
        assert len(cfg.hosts) == 3
        hostnames = {h.hostname for h in cfg.hosts}
        assert hostnames == {"a.example.com", "b.example.com", "c.example.com"}


# ---------------------------------------------------------------------------
# find_config_files
# ---------------------------------------------------------------------------

class TestFindConfigFiles:
    """!
    @brief find_config_files() in the cwd tier.
    """
    def test_finds_yaml_in_cwd(self, tmp_path, monkeypatch):
        """!
        @brief A `*.yaml` file in the cwd is found.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "myhosts.yaml").touch()
        found = find_config_files()
        assert any(p.name == "myhosts.yaml" for p in found)

    def test_finds_yaml_in_config_subdir(self, tmp_path, monkeypatch):
        """!
        @brief A `*.yaml` file in ./config is found.
        """
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "cluster.yaml").touch()
        found = find_config_files()
        assert any(p.name == "cluster.yaml" for p in found)

    def test_no_yaml_returns_empty(self, tmp_path, monkeypatch):
        """!
        @brief An empty cwd yields no files.
        """
        monkeypatch.chdir(tmp_path)
        found = find_config_files()
        assert found == []

    def test_deduplication_of_same_file(self, tmp_path, monkeypatch):
        """!
        @brief A file reachable through both cwd and ./config is listed once.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hosts.yaml").touch()
        # Symlink config/ -> . so the same file appears in both search paths
        (tmp_path / "config").symlink_to(tmp_path)
        found = find_config_files()
        names = [p.name for p in found]
        assert names.count("hosts.yaml") == 1

    def test_non_yaml_files_excluded(self, tmp_path, monkeypatch):
        """!
        @brief `.txt` and `.yml` files are ignored; only `.yaml` matches.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hosts.txt").touch()
        (tmp_path / "notes.yml").touch()
        found = find_config_files()
        assert found == []
