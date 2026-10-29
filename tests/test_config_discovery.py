"""!
@file test_config_discovery.py
@brief Tests for ssh-selector host-file discovery (GitHub issue #13).

Covers explicit selection (`-c` beats SSH_SELECTOR_CONFIG), the three-tier
search order (cwd and ./config, then the per-user XDG directory
`mu2edaq/cluster-tools`, then `<install>/config`), install-directory
detection via MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR or the install marker
beside the venv, and how main() reports missing or empty files.
"""

import sys
from pathlib import Path

import pytest

from mu2edaq_cluster_tools import ssh_selector as ss


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """!
    @brief Create empty cwd, XDG and install directories and point the process at them.

    chdir()s into the cwd and sets XDG_CONFIG_HOME and
    MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR so that every search tier is a known,
    initially empty temporary directory.

    @param tmp_path     pytest per-test temporary directory.
    @param monkeypatch  Restores cwd and environment afterwards.
    @return dict with keys `cwd`, `user` (per-user config dir, not yet created),
            `inst_cfg` (install config dir, not yet created) and `inst`.
    """
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
    """!
    @brief Create a minimal valid host file (`hosts: []`), making parent directories.
    @param path  File to create.
    @return The same path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hosts: []\n")
    return path


# -- explicit selection ------------------------------------------------------

class TestExplicitConfigPath:
    """!
    @brief explicit_config_path(): `-c` and SSH_SELECTOR_CONFIG handling.
    """
    def test_neither_given(self):
        """!
        @brief Without `-c` or the variable there is no explicit path.
        """
        assert ss.explicit_config_path(None) == (None, "")

    def test_env_var_used(self, monkeypatch, tmp_path):
        """!
        @brief SSH_SELECTOR_CONFIG is used and reported as the source.
        """
        f = tmp_path / "env.yaml"
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(f))
        assert ss.explicit_config_path(None) == (f, "SSH_SELECTOR_CONFIG")

    def test_cli_overrides_env(self, monkeypatch, tmp_path):
        """!
        @brief `-c` wins over SSH_SELECTOR_CONFIG and is reported as the source.
        """
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(tmp_path / "env.yaml"))
        cli = tmp_path / "cli.yaml"
        assert ss.explicit_config_path(str(cli)) == (cli, "-c")

    def test_empty_env_var_ignored(self, monkeypatch):
        """!
        @brief An empty SSH_SELECTOR_CONFIG counts as unset.
        """
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", "")
        assert ss.explicit_config_path(None) == (None, "")

    def test_tilde_expanded(self, monkeypatch):
        """!
        @brief A leading `~` in SSH_SELECTOR_CONFIG is expanded to the home directory.
        """
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", "~/x.yaml")
        path, _ = ss.explicit_config_path(None)
        assert path == Path.home() / "x.yaml"


# -- search order ------------------------------------------------------------

class TestSearchOrder:
    """!
    @brief user_config_dir(), config_search_tiers() and first-non-empty-tier selection.
    """
    def test_user_dir_follows_xdg(self, dirs):
        """!
        @brief The per-user directory lives under XDG_CONFIG_HOME.
        """
        assert ss.user_config_dir() == dirs["user"]

    def test_user_dir_default(self, monkeypatch):
        """!
        @brief Without XDG_CONFIG_HOME the per-user directory is ~/.config/mu2edaq/cluster-tools.
        """
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert ss.user_config_dir() == Path.home() / ".config" / "mu2edaq" / "cluster-tools"

    def test_tier_order(self, dirs):
        """!
        @brief Tiers are [cwd, ./config], then the user dir, then the install config dir.
        """
        assert ss.config_search_tiers() == [
            [Path("."), Path("config")],
            [dirs["user"]],
            [dirs["inst_cfg"]],
        ]

    def test_cwd_wins_over_user_and_install(self, dirs):
        """!
        @brief Files in the cwd hide the user and install tiers entirely.
        """
        _touch(dirs["cwd"] / "local.yaml")
        _touch(dirs["user"] / "user.yaml")
        _touch(dirs["inst_cfg"] / "inst.yaml")
        assert [p.name for p in ss.find_config_files()] == ["local.yaml"]

    def test_cwd_and_cwd_config_merge(self, dirs):
        """!
        @brief cwd and ./config form one tier; cwd files are listed first.
        """
        _touch(dirs["cwd"] / "b.yaml")
        _touch(dirs["cwd"] / "config" / "a.yaml")
        assert [p.name for p in ss.find_config_files()] == ["b.yaml", "a.yaml"]

    def test_user_dir_used_when_cwd_empty(self, dirs):
        """!
        @brief The user tier is used when the cwd tier is empty and hides the install tier.
        """
        _touch(dirs["user"] / "user.yaml")
        _touch(dirs["inst_cfg"] / "inst.yaml")
        assert ss.find_config_files() == [dirs["user"] / "user.yaml"]

    def test_install_dir_used_last(self, dirs):
        """!
        @brief The install tier is used last and only `*.yaml` files match.
        """
        _touch(dirs["inst_cfg"] / "mu2e_daq.yaml")
        _touch(dirs["inst_cfg"] / "hosts.yaml.example")  # not *.yaml
        assert ss.find_config_files() == [dirs["inst_cfg"] / "mu2e_daq.yaml"]

    def test_nothing_found(self, dirs):
        """!
        @brief With all tiers empty the result is an empty list.
        """
        assert ss.find_config_files() == []


# -- install dir detection ---------------------------------------------------

class TestInstallDir:
    """!
    @brief install_dir() detection from the environment or the install marker.
    """
    def test_env_var(self, monkeypatch, tmp_path):
        """!
        @brief MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR is returned as-is.
        """
        monkeypatch.setenv("MU2EDAQ_CLUSTER_TOOLS_INSTALL_DIR", str(tmp_path))
        assert ss.install_dir() == tmp_path

    def test_marker_next_to_venv(self, monkeypatch, tmp_path):
        """!
        @brief The install marker beside sys.prefix's parent venv identifies the install tree.
        """
        (tmp_path / "venv").mkdir()
        (tmp_path / ss.INSTALL_MARKER).write_text("")
        # install_dir() looks for the marker in the parent of the running venv (sys.prefix).
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
        assert ss.install_dir() == tmp_path

    def test_no_marker_no_env(self, monkeypatch, tmp_path):
        """!
        @brief Without marker or variable there is no install dir and only two search tiers.
        """
        (tmp_path / "venv").mkdir()
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
        assert ss.install_dir() is None
        assert len(ss.config_search_tiers()) == 2


# -- main() wiring -----------------------------------------------------------

class TestMainSelection:
    """!
    @brief main() error paths and selection of the file actually loaded.
    """
    def test_env_var_missing_file_exits(self, dirs, monkeypatch):
        """!
        @brief A missing file named by SSH_SELECTOR_CONFIG exits with a message naming the variable.
        """
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(dirs["cwd"] / "nope.yaml"))
        monkeypatch.setattr(sys, "argv", ["ssh-selector"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        assert "SSH_SELECTOR_CONFIG" in str(exc.value.code)

    def test_cli_wins_over_env(self, dirs, monkeypatch):
        """!
        @brief A missing `-c` file is reported with `(-c)` even when the variable is also set.
        """
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(dirs["cwd"] / "env.yaml"))
        monkeypatch.setattr(sys, "argv", ["ssh-selector", "-c", "cli.yaml"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        msg = str(exc.value.code)
        assert "(-c)" in msg and "cli.yaml" in msg

    def test_env_var_file_is_loaded(self, dirs, monkeypatch):
        # A file with no hosts stops right after load_config, so main() never
        # reaches the TUI; the recorded path proves which file was read.
        """!
        @brief SSH_SELECTOR_CONFIG is loaded in preference to a host file in the cwd.
        """
        f = _touch(dirs["user"] / "env.yaml")
        _touch(dirs["cwd"] / "local.yaml")
        monkeypatch.setenv("SSH_SELECTOR_CONFIG", str(f))
        loaded = []
        # Wrap load_config to record which path main() actually loads.
        real = ss.load_config

        def spy(p):
            """!
            @brief Record the path passed to load_config, then delegate to the real function.
            @param p  Path being loaded.
            @return The real load_config result.
            """
            loaded.append(p)
            return real(p)

        monkeypatch.setattr(ss, "load_config", spy)
        monkeypatch.setattr(sys, "argv", ["ssh-selector"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        assert loaded == [f]
        assert "No hosts" in str(exc.value.code)

    def test_none_found_lists_search_path(self, dirs, monkeypatch):
        """!
        @brief When nothing is found the exit message lists the user and install directories searched.
        """
        monkeypatch.setattr(sys, "argv", ["ssh-selector"])
        with pytest.raises(SystemExit) as exc:
            ss.main()
        msg = str(exc.value.code)
        assert str(dirs["user"]) in msg and str(dirs["inst_cfg"]) in msg
