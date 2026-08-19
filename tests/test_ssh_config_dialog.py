"""The import picker: nothing is imported that nobody picked.

Every assertion is on what the dialog *dismisses with*, because that tuple is
the only thing the app acts on — and on ``#import-hint``, because a complaint
that arrives as a toast is a complaint the operator reads after the dialog has
already gone.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from clv.services.ssh_config import SSHConfigHost
from clv.widgets.ssh_config_dialog import SSHConfigImportDialog


def _run(scenario) -> None:
    asyncio.run(scenario())


WEB01 = SSHConfigHost(name="web01", hostname="10.0.0.9", user="ops", port=22)
DB02 = SSHConfigHost(name="db02", hostname="10.0.0.4", user=None, port=2222)


class _Host(App[None]):
    """Somewhere to push the screen. The dialog is what is under test."""

    def compose(self) -> ComposeResult:
        yield Static("")


def _hint(screen: SSHConfigImportDialog) -> str:
    return str(screen.query_one("#import-hint", Static).render())


def test_nothing_is_ticked_until_the_operator_ticks_it() -> None:
    """Import with an empty selection dismisses None, so the app never writes."""

    results: list[Optional[tuple]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01, DB02]), callback=results.append)
            await pilot.pause()
            screen = app.screen
            assert screen.picked == ()
            screen.query_one("#import-confirm").press()
            await pilot.pause()

    _run(scenario)
    assert results == [None]


def test_space_ticks_and_import_returns_only_the_ticked_hosts() -> None:
    results: list[Optional[tuple]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01, DB02]), callback=results.append)
            await pilot.pause()
            screen = app.screen
            screen.query_one("#import-list", OptionList).highlighted = 1
            await pilot.press("space")
            await pilot.pause()
            assert screen.picked == ("db02",)
            screen.query_one("#import-confirm").press()
            await pilot.pause()

    _run(scenario)
    (imported,) = results
    assert [host.name for host in imported] == ["db02"]
    # The alias is the destination — see services/ssh_config.
    assert imported[0].host == "db02"


def test_space_ticks_and_unticks() -> None:
    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01, DB02]))
            await pilot.pause()
            screen = app.screen
            await pilot.press("space")
            await pilot.pause()
            assert screen.picked == ("web01",)
            await pilot.press("space")
            await pilot.pause()
            assert screen.picked == ()

    _run(scenario)


def test_a_ticks_everything_and_then_nothing() -> None:
    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01, DB02]))
            await pilot.pause()
            screen = app.screen
            await pilot.press("a")
            await pilot.pause()
            assert screen.picked == ("web01", "db02")
            await pilot.press("a")
            await pilot.pause()
            assert screen.picked == ()

    _run(scenario)


def test_the_default_log_dirs_are_var_log_and_visible_in_the_row() -> None:
    """The one field OpenSSH cannot answer is on screen before anything is written."""

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01]))
            await pilot.pause()
            option = app.screen.query_one("#import-list", OptionList).get_option_at_index(0)
            assert "/var/log" in option.prompt.plain

    _run(scenario)


def test_editing_log_dirs_before_import_is_what_comes_back() -> None:
    results: list[Optional[tuple]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01]), callback=results.append)
            await pilot.pause()
            screen = app.screen
            await pilot.press("enter")
            await pilot.pause()
            assert screen.editing
            screen.query_one("#import-dirs", Input).value = "/srv/app/logs, ~/logs"
            await pilot.press("enter")
            await pilot.pause()
            assert not screen.editing
            # Setting a path is how somebody says they want this one.
            assert screen.picked == ("web01",)
            screen.query_one("#import-confirm").press()
            await pilot.pause()

    _run(scenario)
    (imported,) = results
    assert imported[0].log_dirs == ("/srv/app/logs", "~/logs")


def test_a_relative_log_dir_reports_in_the_hint_and_blocks_the_save() -> None:
    """Reported where the operator typed it, and the screen stays up."""

    results: list[Optional[tuple]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01]), callback=results.append)
            await pilot.pause()
            screen = app.screen
            await pilot.press("enter")
            await pilot.pause()
            screen.query_one("#import-dirs", Input).value = "logs"
            await pilot.press("enter")
            await pilot.pause()

            assert screen.editing, "the editor stayed open"
            assert "relative" in _hint(screen)
            assert screen.query_one("#import-hint", Static).has_class("-warning")
            assert app.screen is screen, "no toast, no dismissal"

    _run(scenario)
    assert results == []


def test_escape_in_the_editor_goes_back_rather_than_cancelling() -> None:
    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01]))
            await pilot.pause()
            screen = app.screen
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert not screen.editing
            assert app.screen is screen

    _run(scenario)


def test_escape_cancels_and_returns_none() -> None:
    results: list[Optional[tuple]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01, DB02]), callback=results.append)
            await pilot.pause()
            await pilot.press("space")
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario)
    assert results == [None]


def test_scan_notes_are_shown_in_the_hint() -> None:
    """What the scan could not make sense of is on screen, not swallowed."""

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(
                SSHConfigImportDialog([WEB01], notes=["2 Match block(s) skipped: reasons"])
            )
            await pilot.pause()
            assert "2 Match block(s) skipped" in _hint(app.screen)

    _run(scenario)


def test_letters_are_text_while_the_editor_is_open() -> None:
    """`a` must not tick every row when it is being typed into a path."""

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([WEB01, DB02]))
            await pilot.pause()
            screen = app.screen
            await pilot.press("enter")
            await pilot.pause()
            screen.query_one("#import-dirs", Input).value = ""
            await pilot.press("a")
            await pilot.pause()

            assert screen.query_one("#import-dirs", Input).value == "a"
            assert screen.picked == ()

    _run(scenario)


def test_the_dialog_fits_eighty_by_twenty_four() -> None:
    """Every control an operator must reach is inside the smallest terminal."""

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(
                SSHConfigImportDialog([WEB01, DB02], notes=["a note that takes a line"])
            )
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            for widget_id in (
                "#import-list",
                "#import-hint",
                "#import-all",
                "#import-cancel",
                "#import-confirm",
            ):
                region = screen.query_one(widget_id).region
                assert region.width > 0, f"{widget_id} laid out to nothing"
                assert region.height > 0, f"{widget_id} painted nothing"
                assert region.right <= 80, f"{widget_id} overflows 80 columns"
                assert region.bottom <= 24, f"{widget_id} falls off 24 rows"

    _run(scenario)


def test_an_empty_candidate_list_still_takes_focus() -> None:
    """A disabled OptionList cannot take focus, and then no key reaches on_key."""

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SSHConfigImportDialog([]))
            await pilot.pause()
            screen = app.screen
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen

    _run(scenario)
