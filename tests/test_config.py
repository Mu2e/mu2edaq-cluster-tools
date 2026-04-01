"""Tests for load_config, Config, GroupConfig, and find_config_files."""

import textwrap
from pathlib import Path

import pytest

from ssh_selector import CURRENT_USER, Config, Host, load_config, find_config_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def yaml_file(tmp_path: Path, content: str, name: str = "hosts.yaml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Minimal configs
# ---------------------------------------------------------------------------

class TestLoadConfigMinimal:
    def test_single_host_loaded(self, minimal_config):
        cfg = load_config(minimal_config)
        assert len(cfg.hosts) == 1

    def test_hostname_correct(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].hostname == "test.example.com"

    def test_nickname_correct(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].nickname == "Test Host"

    def test_default_port(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].port == 22

    def test_default_users(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].users == ["default"]

    def test_no_proxy_jump(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.hosts[0].proxy_jump is None

    def test_default_cache_lifetime(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.cache_lifetime == 600

    def test_empty_grouplist(self, minimal_config):
        cfg = load_config(minimal_config)
        assert cfg.grouplist == []


# ---------------------------------------------------------------------------
# Cache lifetime
# ---------------------------------------------------------------------------

class TestCacheLifetime:
    def test_custom_cache_lifetime(self, tmp_path):
        p = yaml_file(tmp_path, """
            cache_lifetime: 120
            hosts:
              - hostname: srv.example.com
        """)
        cfg = load_config(p)
        assert cfg.cache_lifetime == 120

    def test_default_cache_lifetime_when_absent(self, tmp_path):
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
    def test_grouplist_order_preserved(self, grouped_config):
        cfg = load_config(grouped_config)
        assert cfg.grouplist == ["Access", "Backend"]

    def test_host_count(self, grouped_config):
        cfg = load_config(grouped_config)
        assert len(cfg.hosts) == 3

    def test_group_default_users_applied(self, grouped_config):
        cfg = load_config(grouped_config)
        gateway = next(h for h in cfg.hosts if h.hostname == "gateway.example.com")
        assert gateway.users == ["default", "admin"]

    def test_group_default_proxy_applied(self, grouped_config):
        cfg = load_config(grouped_config)
        db1 = next(h for h in cfg.hosts if h.hostname == "db1.example.com")
        assert db1.proxy_jump == "gw.example.com"

    def test_host_users_override_group(self, grouped_config):
        cfg = load_config(grouped_config)
        db2 = next(h for h in cfg.hosts if h.hostname == "db2.example.com")
        assert db2.users == ["dbadmin", "default"]

    def test_host_null_proxy_overrides_group(self, grouped_config):
        cfg = load_config(grouped_config)
        db2 = next(h for h in cfg.hosts if h.hostname == "db2.example.com")
        assert db2.proxy_jump is None

    def test_no_proxy_group_has_none(self, grouped_config):
        cfg = load_config(grouped_config)
        gateway = next(h for h in cfg.hosts if h.hostname == "gateway.example.com")
        assert gateway.proxy_jump is None


# ---------------------------------------------------------------------------
# Per-host field variations
# ---------------------------------------------------------------------------

class TestLoadConfigHostFields:
    def test_custom_port(self, tmp_path):
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                port: 2222
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].port == 2222

    def test_nickname_defaults_to_hostname(self, tmp_path):
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].nickname == "srv.example.com"

    def test_scalar_user_field(self, tmp_path):
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                user: admin
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].users == ["admin"]

    def test_users_list_field(self, tmp_path):
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
        p = yaml_file(tmp_path, """
            hosts:
              - hostname: srv.example.com
                proxy_jump: gw.example.com
        """)
        cfg = load_config(p)
        assert cfg.hosts[0].proxy_jump == "gw.example.com"

    def test_group_field_stored(self, tmp_path):
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
        p = yaml_file(tmp_path, """
            hosts:
              - nickname: No Hostname
        """)
        with pytest.raises(ValueError, match="hostname"):
            load_config(p)

    def test_multiple_hosts_all_loaded(self, tmp_path):
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
    def test_finds_yaml_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "myhosts.yaml").touch()
        found = find_config_files()
        assert any(p.name == "myhosts.yaml" for p in found)

    def test_finds_yaml_in_config_subdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "cluster.yaml").touch()
        found = find_config_files()
        assert any(p.name == "cluster.yaml" for p in found)

    def test_no_yaml_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        found = find_config_files()
        assert found == []

    def test_deduplication_of_same_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hosts.yaml").touch()
        # Symlink config/ -> . so the same file appears in both search paths
        (tmp_path / "config").symlink_to(tmp_path)
        found = find_config_files()
        names = [p.name for p in found]
        assert names.count("hosts.yaml") == 1

    def test_non_yaml_files_excluded(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hosts.txt").touch()
        (tmp_path / "notes.yml").touch()
        found = find_config_files()
        assert found == []
