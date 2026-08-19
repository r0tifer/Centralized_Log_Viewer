"""Add Source: a path, or the way to the machines that are not paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from clv.app import LogViewerApp
from clv.services.refs import normalize_ref
from clv.widgets.add_source_dialog import REMOTE_HOSTS, AddSourceDialog
from clv.widgets.remote_hosts_dialog import RemoteHostsDialog


def _run(scenario) -> None:
    asyncio.run(scenario())


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("")


def test_the_dialog_returns_the_typed_path() -> None:
    results: list[Optional[str]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(AddSourceDialog(), callback=results.append)
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = "/var/log/syslog"
            await pilot.press("enter")
            await pilot.pause()

    _run(scenario)
    assert results == ["/var/log/syslog"]


def test_escape_cancels() -> None:
    results: list[Optional[str]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(AddSourceDialog(), callback=results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario)
    assert results == [None]


def test_the_remote_hosts_button_dismisses_with_the_sentinel() -> None:
    results: list[Optional[str]] = []

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(AddSourceDialog(), callback=results.append)
            await pilot.pause()
            app.screen.query_one("#remote-hosts-add-source").press()
            await pilot.pause()

    _run(scenario)
    assert results == [REMOTE_HOSTS]


def test_the_sentinel_can_never_be_a_path() -> None:
    """NUL is what makes the sentinel safe rather than merely unlikely.

    ``services/refs`` records that a ref string carries neither a comma nor
    NUL, so nothing an operator can type arrives here looking like this.
    """

    assert "\x00" in REMOTE_HOSTS
    assert "\x00" not in str(normalize_ref("/var/log/syslog"))


def test_the_hint_names_remote_machines() -> None:
    """The dialog says the other door exists; `R` alone was never discoverable."""

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(AddSourceDialog())
            await pilot.pause()
            hint = str(app.screen.query_one("#dialog-hint", Static).render())
            assert "Remote hosts" in hint

    _run(scenario)


def test_the_three_buttons_share_a_row_and_fit_eighty_columns() -> None:
    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(AddSourceDialog())
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            rows = set()
            for widget_id in (
                "#remote-hosts-add-source",
                "#cancel-add-source",
                "#confirm-add-source",
            ):
                region = screen.query_one(widget_id).region
                assert region.width > 0, f"{widget_id} laid out to nothing"
                assert region.height > 0, f"{widget_id} painted nothing"
                assert region.right <= 80, f"{widget_id} overflows 80 columns"
                assert region.bottom <= 24, f"{widget_id} falls off 24 rows"
                rows.add(region.y)
            assert len(rows) == 1, "the third button started a second row"

    _run(scenario)


def test_choosing_remote_hosts_opens_the_host_dialog() -> None:
    """The whole point: Add Source is now a way in to the machine list."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.action_add_source()
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, AddSourceDialog)

            app.screen.query_one("#remote-hosts-add-source").press()
            for _ in range(10):
                await pilot.pause()
                if isinstance(app.screen, RemoteHostsDialog):
                    break
            assert isinstance(app.screen, RemoteHostsDialog), app.screen

    _run(scenario)


def test_a_typed_path_still_adds_a_source(tmp_path: Path) -> None:
    """The regression the new branch could have caused: paths still work."""

    log = tmp_path / "app.log"
    log.write_text("hello\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.action_add_source()
            await pilot.pause()
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(log)
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if app._source_manager.added_paths:
                    break
            assert [str(path) for path in app._source_manager.added_paths] == [str(log)]

    _run(scenario)
