"""Tests for host-file discovery: SSH_SELECTOR_CONFIG, -c precedence, search order."""

import sys
from pathlib import Path

import pytest

from mu2edaq_cluster_tools import ssh_selector as ss


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """A cwd, a per-user config dir and an install dir, all empty."""
    cwd = tmp_path / "cwd"
    xdg = tmp_path / "xdg"
    inst = tmp_path / "inst"
    for d in (cwd, xdg, inst):
        d.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR", str(inst))
    user = xdg / "mu2edaq" / "cluster-tools"
    return {"cwd": cwd, "user": user, "inst_cfg": inst / "config", "inst": inst}


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hosts: []\n")
    return path


# -- explicit selection ------------------------------------------------------

class TestExplicitConfigPath:
    def test_neither_given(self):
        assert ss.explicit_config_path(None) == (None, "")

    def test_env_var_used(self, monkeypatch, tmp_path):
        f = tmp_path / "env.yaml"
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(f))
        assert ss.explicit_config_path(None) == (f, "SSH_SELECTOR_CONFIG")

    def test_cli_overrides_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(tmp_path / "env.yaml"))
        cli = tmp_path / "cli.yaml"
        assert ss.explicit_config_path(str(cli)) == (cli, "-c")

    def test_empty_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", "")
        assert ss.explicit_config_path(None) == (None, "")

    def test_tilde_expanded(self, monkeypatch):
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", "~/x.yaml")
        path, _ = ss.explicit_config_path(None)
        assert path == Path.home() / "x.yaml"


# -- search order ------------------------------------------------------------

class TestSearchOrder:
    def test_user_dir_follows_xdg(self, dirs):
        assert ss.user_config_dir() == dirs["user"]

    def test_user_dir_default(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert ss.user_config_dir() == Path.home() / ".config" / "mu2edaq" / "cluster-tools"

    def test_tier_order(self, dirs):
        assert ss.config_search_tiers() == [
            [Path("."), Path("config")],
            [dirs["user"]],
            [dirs["inst_cfg"]],
        ]

    def test_cwd_wins_over_user_and_install(self, dirs):
        _touch(dirs["cwd"] / "local.yaml")
        _touch(dirs["user"] / "user.yaml")
        _touch(dirs["inst_cfg"] / "inst.yaml")
        assert [p.name for p in ss.find_config_files()] == ["local.yaml"]

    def test_cwd_and_cwd_config_merge(self, dirs):
        _touch(dirs["cwd"] / "b.yaml")
        _touch(dirs["cwd"] / "config" / "a.yaml")
        assert [p.name for p in ss.find_config_files()] == ["b.yaml", "a.yaml"]

    def test_user_dir_used_when_cwd_empty(self, dirs):
        _touch(dirs["user"] / "user.yaml")
        _touch(dirs["inst_cfg"] / "inst.yaml")
        assert ss.find_config_files() == [dirs["user"] / "user.yaml"]

    def test_install_dir_used_last(self, dirs):
        _touch(dirs["inst_cfg"] / "mu2e_daq.yaml")
        _touch(dirs["inst_cfg"] / "hosts.yaml.example")  # not *.yaml
        assert ss.find_config_files() == [dirs["inst_cfg"] / "mu2e_daq.yaml"]

    def test_nothing_found(self, dirs):
        assert ss.find_config_files() == []


# -- install dir detection ---------------------------------------------------

class TestInstallDir:
    def test_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR", str(tmp_path))
        assert ss.install_dir() == tmp_path

    def test_marker_next_to_venv(self, monkeypatch, tmp_path):
        (tmp_path / "venv").mkdir()
        (tmp_path / ss.INSTALL_MARKER).write_text("")
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
        assert ss.install_dir() == tmp_path

    def test_no_marker_no_env(self, monkeypatch, tmp_path):
        (tmp_path / "venv").mkdir()
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
        assert ss.install_dir() is None
        assert len(ss.config_search_tiers()) == 2


# -- main() wiring -----------------------------------------------------------

class TestMainSelection:
    def test_env_var_missing_file_exits(self, dirs, monkeypatch):
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(dirs["cwd"] / "nope.yaml"))
        monkeypatch.setattr(sys, "argv", ["ssh-selector"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        assert "SSH_SELECTOR_CONFIG" in str(exc.value.code)

    def test_cli_wins_over_env(self, dirs, monkeypatch):
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(dirs["cwd"] / "env.yaml"))
        monkeypatch.setattr(sys, "argv", ["ssh-selector", "-c", "cli.yaml"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        msg = str(exc.value.code)
        assert "(-c)" in msg and "cli.yaml" in msg

    def test_env_var_file_is_loaded(self, dirs, monkeypatch):
        # A file with no hosts stops right after load_config, so main() never
        # reaches the TUI; the recorded path proves which file was read.
        f = _touch(dirs["user"] / "env.yaml")
        _touch(dirs["cwd"] / "local.yaml")
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(f))
        loaded = []
        real = ss.load_config

        def spy(p):
            loaded.append(p)
            return real(p)

        monkeypatch.setattr(ss, "load_config", spy)
        monkeypatch.setattr(sys, "argv", ["ssh-selector"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        assert loaded == [f]
        assert "No hosts" in str(exc.value.code)

    def test_none_found_lists_search_path(self, dirs, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ssh-selector"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        msg = str(exc.value.code)
        assert str(dirs["user"]) in msg and str(dirs["inst_cfg"]) in msg
