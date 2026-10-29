"""!
@file test_config_picker.py
@brief Tests for ConfigSelectApp, the picker shown when several host files are found.

Regression for a NameError in ConfigItem.compose(), which referenced an
undefined name `path` instead of `self.path`. The picker crashed as soon as
it drew its list, so ssh-selector could not start whenever two or more
topology files were discovered. The app is driven headlessly through
Textual's run_test().
"""

import asyncio
from pathlib import Path

from textual.widgets import Label

from mu2edaq_cluster_tools.ssh_selector import ConfigItem, ConfigSelectApp


def _drive(paths, keys):
    """!
    @brief Run ConfigSelectApp headlessly, press keys, and collect results.
    @param paths  Config file paths offered by the picker.
    @param keys   Key names pressed in order after the app mounts.
    @return Tuple (labels shown in the list, value the app exited with).
    """
    app = ConfigSelectApp(paths)
    shown = []

    async def drive():
        async with app.run_test() as pilot:
            await pilot.pause()
            # Read the labels while the list is mounted: this is the step
            # that raised NameError before the fix.
            for item in app.query(ConfigItem):
                shown.append(str(item.query_one(Label).render()))
            for key in keys:
                await pilot.press(key)
            await pilot.pause()

    asyncio.run(drive())
    return shown, app.return_value


def test_picker_lists_every_config_file():
    """!@brief Each discovered file appears as one labelled list entry."""
    paths = [Path("config/mu2e_daq.yaml"), Path("config/nova_daq.yaml")]
    shown, _ = _drive(paths, [])
    assert shown == [str(p) for p in paths]


def test_enter_returns_highlighted_file():
    """!@brief Enter on the first entry exits the picker with that path."""
    paths = [Path("a.yaml"), Path("b.yaml")]
    _, chosen = _drive(paths, ["enter"])
    assert chosen == paths[0]


def test_down_then_enter_returns_second_file():
    """!@brief Moving down one entry and pressing Enter selects the second path."""
    paths = [Path("a.yaml"), Path("b.yaml")]
    _, chosen = _drive(paths, ["down", "enter"])
    assert chosen == paths[1]
