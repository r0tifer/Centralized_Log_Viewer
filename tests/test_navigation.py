"""Match navigation (`n` / `N`) and jump-to-timestamp (`g`)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from clv.app import LogViewerApp
from clv.services.filtering import parse_moment
from clv.widgets.goto_dialog import GotoDialog


# --- parse_moment -----------------------------------------------------------

NOW = datetime(2026, 8, 7, 12, 0, 0)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("-15m", NOW - timedelta(minutes=15)),
        ("15m", NOW - timedelta(minutes=15)),  # bare means the past
        ("-6h", NOW - timedelta(hours=6)),
        ("-2d", NOW - timedelta(days=2)),
        ("+1h", NOW + timedelta(hours=1)),
    ],
)
def test_relative_offsets_resolve_against_now(text: str, expected: datetime) -> None:
    assert parse_moment(text, now=NOW) == expected


def test_an_absolute_timestamp_is_taken_as_written() -> None:
    assert parse_moment("2026-08-07 09:25:01", now=NOW) == datetime(2026, 8, 7, 9, 25, 1)
    assert parse_moment("2026-08-07T09:25:01", now=NOW) == datetime(2026, 8, 7, 9, 25, 1)


@pytest.mark.parametrize("text", ["", "   ", "banana", "15x", "2026-13-45", "-"])
def test_unreadable_input_returns_none_rather_than_raising(text: str) -> None:
    """This is prompt input; the caller reports it to whoever typed it."""

    assert parse_moment(text, now=NOW) is None


# --- the UI path ------------------------------------------------------------


def _mixed_log(tmp_path: Path) -> Path:
    """Twenty lines: every fifth is a WARN, every seventh an ERROR."""

    lines = []
    for index in range(20):
        if index % 7 == 0 and index:
            level = "ERROR"
        elif index % 5 == 0:
            level = "WARN"
        else:
            level = "INFO"
        lines.append(f"2026-08-07 09:{index:02d}:00 - {level} - event {index}")
    path = tmp_path / "mixed.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _undated_log(tmp_path: Path) -> Path:
    path = tmp_path / "undated.log"
    path.write_text("\n".join(f"nothing parseable here {i}" for i in range(6)), encoding="utf-8")
    return path


class _Notices:
    def __init__(self, app: LogViewerApp) -> None:
        self.items: list[str] = []
        app.notify = lambda message, **kw: self.items.append(message)

    def text(self) -> str:
        return " | ".join(self.items)


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


def _counter(app: LogViewerApp) -> str:
    return app.query_bar.query_one("#match-count", Static).render().plain


def test_n_steps_between_warnings_and_worse_when_no_query_is_active(
    tmp_path: Path,
) -> None:
    """With severity on `all`, stepping every entry would just be the down key."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("home")
            await pilot.pause()

            seen = []
            for _ in range(4):
                await pilot.press("n")
                await pilot.pause()
                seen.append(app.log_panel.cursor_entry.level)

            assert seen == ["WARN", "ERROR", "WARN", "ERROR"], seen

    asyncio.run(scenario())


def test_n_honours_the_selected_severity_bucket(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app._update_state(severity="error")
            app._render_log()
            app.set_focus(app.log_panel)
            await pilot.pause()

            targets, label = app._navigation_targets()

            assert label == "error entry"
            # The severity filter already hid everything else, so every visible
            # row is a target — which is the honest answer, not a bug.
            assert len(targets) == len(app.log_panel.entries)
            assert all(entry.level == "ERROR" for entry in app.log_panel.entries)

    asyncio.run(scenario())


def test_n_wraps_at_the_end_and_says_so(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            notices = _Notices(app)

            targets, _ = app._navigation_targets()
            for _ in range(len(targets)):
                await pilot.press("n")
                await pilot.pause()
            assert "Wrapped" not in notices.text()

            await pilot.press("n")
            await pilot.pause()

            assert "Wrapped to the first" in notices.text()
            assert app.log_panel.cursor == targets[0]

    asyncio.run(scenario())


def test_shift_n_walks_back_and_wraps_to_the_last(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            notices = _Notices(app)

            targets, _ = app._navigation_targets()
            await pilot.press("home")
            await pilot.pause()
            await pilot.press("N")
            await pilot.pause()

            assert app.log_panel.cursor == targets[-1]
            assert "Wrapped to the last" in notices.text()

    asyncio.run(scenario())


def test_the_match_position_appears_in_both_the_counter_and_the_status_bar(
    tmp_path: Path,
) -> None:
    """#match-count is hidden at 80 columns; the status bar never is."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.query_bar.set_query_value("event 1")
            app._update_state(query="event 1")
            app._sync_regex_validation()
            app._render_log()
            app.set_focus(app.log_panel)
            await pilot.pause()

            total = len(app._navigation_targets()[0])
            assert total > 1

            await pilot.press("home")
            await pilot.press("n")
            await pilot.pause()

            assert f"of {total} hits" in _counter(app)
            assert f"match 2 of {total}" in _status(app)

    asyncio.run(scenario())


def test_arrowing_off_a_match_clears_the_position_rather_than_leaving_a_stale_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("home")
            await pilot.press("n")
            await pilot.pause()
            assert app._match_position is not None

            # Step onto an INFO line, which is not a navigation target.
            await pilot.press("down")
            await pilot.pause()

            assert app._match_position is None
            assert "warning or worse" not in _status(app)
            assert "hits" not in _counter(app)

    asyncio.run(scenario())


def test_the_position_is_dropped_when_the_filter_changes(tmp_path: Path) -> None:
    """A position into a result set that no longer exists is worse than none."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("home")
            await pilot.press("n")
            await pilot.pause()
            assert app._match_position is not None

            before = app._navigation_targets()[0]

            app._update_state(query="event 1")
            app._render_log()
            await pilot.pause()

            assert app._match_position is None
            assert "warning or worse" not in _status(app)
            # The cached target set was thrown away with it, so the next `n`
            # navigates the new result rather than the old one.
            assert app._navigation_targets()[0] != before

    asyncio.run(scenario())


def test_n_with_no_source_open_explains_itself(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            notices = _Notices(app)

            app.action_next_match()
            await pilot.pause()

            assert "Open a log" in notices.text()

    asyncio.run(scenario())


def test_no_targets_is_a_notification_not_a_dead_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Nothing in this source declares a level, so there is no WARN+.
            app._select_source(_undated_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            notices = _Notices(app)

            await pilot.press("n")
            await pilot.pause()

            assert "No warning or worse to jump to." in notices.text()

    asyncio.run(scenario())


# --- g ----------------------------------------------------------------------


async def _goto(pilot, app: LogViewerApp, text: str) -> None:
    """Press `g`, type into the dialog and submit it."""

    await pilot.press("g")
    await pilot.pause()
    assert isinstance(app.screen, GotoDialog)
    app.screen.query_one("#goto-input", Input).value = text
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()


def test_g_lands_on_the_first_entry_at_or_after_an_absolute_time(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            # 09:07:30 falls between two entries, so the *next* one wins.
            await _goto(pilot, app, "2026-08-07 09:07:30")

            assert app.log_panel.cursor_entry.timestamp == datetime(2026, 8, 7, 9, 8, 0)

    asyncio.run(scenario())


def test_g_accepts_a_relative_offset(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            # Every entry in the fixture is in the past, so "everything since a
            # decade ago" lands on the first one.
            await _goto(pilot, app, "-3650d")

            assert app.log_panel.cursor == 0

    asyncio.run(scenario())


def test_g_reports_how_many_entries_it_had_to_skip(tmp_path: Path) -> None:
    """The "explain what is hidden" rule applied to ordering."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_undated_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            notices = _Notices(app)

            await _goto(pilot, app, "-15m")

            assert "6 with no timestamp skipped" in notices.text()
            assert "No entry at or after" in notices.text()

    asyncio.run(scenario())


def test_g_reports_an_unreadable_time_rather_than_failing_silently(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            notices = _Notices(app)

            await _goto(pilot, app, "next tuesday")

            assert "Could not read" in notices.text()

    asyncio.run(scenario())


def test_escape_cancels_the_goto_dialog_without_moving_the_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            await pilot.press("home")
            await pilot.pause()
            before = app.log_panel.cursor

            await pilot.press("g")
            await pilot.pause()
            assert isinstance(app.screen, GotoDialog)
            await pilot.press("escape")
            await pilot.pause()

            assert not isinstance(app.screen, GotoDialog)
            assert app.log_panel.cursor == before

    asyncio.run(scenario())


def test_the_goto_dialog_fits_eighty_columns(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._select_source(_mixed_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("g")
            await pilot.pause()

            container = app.screen.query_one("#goto-dialog")
            assert container.region.right <= 80
            assert container.region.bottom <= 24
            assert container.region.width > 0

    asyncio.run(scenario())
