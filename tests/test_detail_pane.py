"""The line cursor and the event detail pane.

Two widgets and the app wiring between them: `LogView` owns the cursor and the
row model, `DetailPane` renders whatever the cursor is on, and the app decides
when following new lines has to stop so the two do not fight.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static, Switch

from clv.app import LogViewerApp
from clv.services.parsing import parse_line, parse_lines
from clv.storage import SessionState, StateStore
from clv.widgets.detail_pane import NO_FORMAT_REASON, DetailPane
from clv.widgets.log_view import GUTTER_WIDTH, LogView

SYSLOG = "Aug  7 09:25:01 web01 sshd[4321]: Accepted publickey for root"
CLF = '10.0.0.1 - alice [07/Aug/2026:09:25:01 +0000] "GET /a HTTP/1.1" 500 123'
JSON_LINE = '{"ts":"2026-08-07T09:25:01Z","level":"error","msg":"boom","svc":{"name":"api"}}'
#: Matches a format outright and still has no fields to show — the case that
#: makes "no properties" the common outcome rather than an edge one.
PY_LOGGING = "2026-08-07 09:25:01,123 - WARNING - slow"


# --- LogView, on its own ----------------------------------------------------


class _ViewHarness(App[None]):
    """Just a LogView, so the cursor can be driven without the whole viewer."""

    def __init__(self, max_rows: int | None = None) -> None:
        super().__init__()
        self.view = LogView(id="log-stream", max_rows=max_rows)
        self.moves: list[int] = []
        self.selected: list[int] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_log_view_cursor_moved(self, message: LogView.CursorMoved) -> None:
        self.moves.append(message.index)

    def on_log_view_entry_selected(self, message: LogView.EntrySelected) -> None:
        self.selected.append(message.index)


def _numbered(count: int) -> list[str]:
    return [f"2026-08-07 09:25:{index:02d} - INFO - line {index}" for index in range(count)]


async def _filled_view(pilot, app: _ViewHarness, lines: list[str]) -> LogView:
    view = app.view
    view.auto_scroll = False
    for entry in parse_lines(lines):
        view.write_entry(Text(entry.raw), entry)
    await pilot.pause()
    app.set_focus(view)
    return view


def test_arrow_keys_step_the_cursor_one_entry_at_a_time() -> None:
    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(30))

            await pilot.press("home")
            await pilot.pause()
            assert view.cursor == 0
            assert view.cursor_entry.raw.endswith("line 0")

            await pilot.press("down", "down", "down")
            await pilot.pause()
            assert view.cursor == 3
            assert view.cursor_entry.raw.endswith("line 3")

            await pilot.press("up")
            await pilot.pause()
            assert view.cursor == 2
            assert app.moves[-1] == 2

    asyncio.run(scenario())


def test_home_and_end_reach_the_ends_and_report_which_end() -> None:
    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(30))

            await pilot.press("end")
            await pilot.pause()
            assert view.cursor_entry.raw.endswith("line 29")
            assert view.cursor_at_end is True

            await pilot.press("home")
            await pilot.pause()
            assert view.cursor_entry.raw.endswith("line 0")
            assert view.cursor_at_end is False

    asyncio.run(scenario())


def test_page_keys_move_about_a_screen_and_stop_at_the_ends() -> None:
    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(60))

            await pilot.press("home")
            await pilot.pause()
            await pilot.press("pagedown")
            await pilot.pause()
            first_page = view.cursor
            assert first_page > 1, "pagedown moved less than a line"
            assert first_page < 59, "pagedown ran past the buffer"

            await pilot.press("pageup")
            await pilot.pause()
            assert view.cursor == 0, "pageup from one page in must reach the top"

            # Clamped rather than wrapped or crashing.
            await pilot.press("pageup")
            await pilot.pause()
            assert view.cursor == 0

    asyncio.run(scenario())


def test_message_rows_are_skipped_by_the_cursor() -> None:
    """`write` puts a summary or an explanation on screen — nothing to inspect."""

    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = app.view
            view.auto_scroll = False
            view.write(Text("Log files found: 3"))
            for entry in parse_lines(_numbered(3)):
                view.write_entry(Text(entry.raw), entry)
            await pilot.pause()
            app.set_focus(view)

            await pilot.press("home")
            await pilot.pause()

            assert view.cursor == 1, "the cursor landed on the summary row"
            assert view.cursor_entry is not None

    asyncio.run(scenario())


def test_enter_reports_the_selected_entry() -> None:
    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(5))

            await pilot.press("home", "down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app.selected == [1]
            assert view.cursor_entry.raw.endswith("line 1")

    asyncio.run(scenario())


def test_clicking_a_line_selects_it() -> None:
    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(10))
            await pilot.pause()

            # Row 2 of the pane, offset for the widget's own border.
            await pilot.click(view, offset=(GUTTER_WIDTH + 2, 2))
            await pilot.pause()

            assert view.cursor >= 0
            assert app.moves, "clicking did not move the cursor"

    asyncio.run(scenario())


def test_appending_costs_the_new_lines_not_the_buffer() -> None:
    """The performance clause: tailing must not become O(buffer) per poll."""

    async def scenario() -> None:
        app = _ViewHarness()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(400))
            await pilot.pause()

            baseline = [id(row.strips) for row in view.rows]
            lines_before = len(view.text_lines)

            for entry in parse_lines(["2026-08-07 09:30:00 - INFO - tailed"]):
                view.write_entry(Text(entry.raw), entry)
            await pilot.pause()

            # Every pre-existing row kept the strips it already had: nothing was
            # re-rendered, and the line map grew by exactly the new row.
            assert [id(row.strips) for row in view.rows][:400] == baseline
            assert len(view.text_lines) == lines_before + 1

    asyncio.run(scenario())


def test_the_row_cap_drops_in_batches_and_keeps_the_line_map_honest() -> None:
    async def scenario() -> None:
        app = _ViewHarness(max_rows=50)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            view = await _filled_view(pilot, app, _numbered(90))
            await pilot.pause()

            assert len(view.rows) <= 50
            # The flat line map and the rows agree, so render_line cannot index
            # into a row that is no longer there.
            assert len(view.text_lines) == sum(len(row.strips) for row in view.rows)
            expected = 0
            for row in view.rows:
                assert row.start_line == expected
                expected += len(row.strips)

            # The newest lines are the ones kept.
            assert "line 89" in view.text_lines[-1]

    asyncio.run(scenario())


# --- DetailPane -------------------------------------------------------------


class _PaneHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.pane = DetailPane(id="detail-pane")

    def compose(self) -> ComposeResult:
        yield self.pane


def _pane_text(app: _PaneHarness) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(segment.text for segment in strip) for strip in strips)


def _show(app: _PaneHarness, line: str) -> None:
    app.pane.add_class("-visible")
    app.pane.show(parse_line(line))


def test_detail_pane_lists_every_field_of_a_syslog_line() -> None:
    async def scenario() -> None:
        app = _PaneHarness()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            _show(app, SYSLOG)
            await pilot.pause()

            painted = _pane_text(app)
            for expected in ("web01", "sshd", "4321", "BSD syslog"):
                assert expected in painted, f"{expected!r} missing from the detail pane"

    asyncio.run(scenario())


def test_detail_pane_lists_every_field_of_an_access_log_line() -> None:
    async def scenario() -> None:
        app = _PaneHarness()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            _show(app, CLF)
            await pilot.pause()

            painted = _pane_text(app)
            for expected in ("10.0.0.1", "alice", "500", "Common Log Format"):
                assert expected in painted, f"{expected!r} missing from the detail pane"

    asyncio.run(scenario())


def test_detail_pane_flattens_json_fields_to_dotted_keys() -> None:
    async def scenario() -> None:
        app = _PaneHarness()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            _show(app, JSON_LINE)
            await pilot.pause()

            painted = _pane_text(app)
            assert "svc.name" in painted
            assert "api" in painted
            assert "ERROR" in painted

    asyncio.run(scenario())


def test_a_line_with_no_fields_explains_itself_rather_than_showing_a_blank_list() -> None:
    """A blank property list reads as a bug. Both no-field cases say which."""

    async def scenario() -> None:
        app = _PaneHarness()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()

            _show(app, "this matched nothing at all")
            await pilot.pause()
            await pilot.pause()
            assert NO_FORMAT_REASON.split(",")[0] in _pane_text(app)
            assert "unrecognised" in _pane_text(app)

            # Not just raw lines: several *matched* formats carry no fields.
            _show(app, PY_LOGGING)
            await pilot.pause()
            await pilot.pause()
            painted = _pane_text(app)
            assert "Python logging" in painted
            assert "nothing else to name" in painted

    asyncio.run(scenario())


def test_detail_pane_says_when_nothing_is_selected() -> None:
    async def scenario() -> None:
        app = _PaneHarness()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.pane.add_class("-visible")
            app.pane.show(None)
            await pilot.pause()

            assert "No line selected" in _pane_text(app)

    asyncio.run(scenario())


# --- the app wiring ---------------------------------------------------------


def _log_file(tmp_path: Path, count: int = 40) -> Path:
    path = tmp_path / "app.log"
    path.write_text("\n".join(_numbered(count)) + "\n", encoding="utf-8")
    return path


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


def test_enter_opens_the_detail_pane_on_the_cursor_line(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            assert app.state.detail_pane is False

            await pilot.press("home")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app.state.detail_pane is True
            assert app.detail_pane.has_class("-visible")
            assert app.detail_pane.entry is app.log_panel.cursor_entry

    asyncio.run(scenario())


def test_d_toggles_the_detail_pane_and_the_drawer_switch_mirrors_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            assert app.state.detail_pane is True
            assert app.advanced_drawer.query_one("#drawer-detail-pane", Switch).value is True

            await pilot.press("d")
            await pilot.pause()
            assert app.state.detail_pane is False
            assert app.advanced_drawer.query_one("#drawer-detail-pane", Switch).value is False

    asyncio.run(scenario())


def test_the_drawer_switch_opens_the_pane(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.advanced_drawer.show()
            await pilot.pause()

            app.advanced_drawer.query_one("#drawer-detail-pane", Switch).toggle()
            await pilot.pause()

            assert app.state.detail_pane is True
            assert app.detail_pane.has_class("-visible")

    asyncio.run(scenario())


def test_the_detail_pane_preference_survives_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(root=tmp_path / "cache")
        store.save(SessionState(detail_pane=True))

        app = LogViewerApp(store=StateStore(root=tmp_path / "cache"))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()

            assert app.state.detail_pane is True
            assert app.detail_pane.has_class("-visible")

    asyncio.run(scenario())


def test_moving_the_cursor_suspends_follow_and_end_resumes_it(tmp_path: Path) -> None:
    """Incoming lines must not drag the view out from under a moved cursor."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            assert app.state.auto_scroll is True

            await pilot.press("end")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()

            assert app.state.auto_scroll is False
            assert app.log_panel.auto_scroll is False
            assert "cursor moved" in _status(app)
            assert "End resumes" in _status(app)

            await pilot.press("end")
            await pilot.pause()

            assert app.state.auto_scroll is True
            assert "following" in _status(app)
            assert "cursor moved" not in _status(app)

    asyncio.run(scenario())


def test_w_also_resumes_following_after_the_cursor_suspended_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("end")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert app.state.auto_scroll is False

            await pilot.press("w")
            await pilot.pause()

            assert app.state.auto_scroll is True
            assert "cursor moved" not in _status(app)

    asyncio.run(scenario())


def test_the_cursor_survives_a_filter_change_that_keeps_the_line(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("home")
            await pilot.press("down", "down", "down")
            await pilot.pause()
            selected = app.log_panel.cursor_entry
            assert selected.raw.endswith("line 3")

            # "line 3" still matches, so the cursor stays on it.
            app.query_bar.set_query_value("line 3")
            app._update_state(query="line 3")
            app._render_log()
            await pilot.pause()

            assert app.log_panel.cursor_entry == selected

    asyncio.run(scenario())


def test_the_cursor_moves_to_the_nearest_line_when_its_own_is_filtered_out(
    tmp_path: Path,
) -> None:
    """Never back to the top: the nearest surviving line is the contract."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("home")
            for _ in range(20):
                await pilot.press("down")
            await pilot.pause()
            assert app.log_panel.cursor_entry.raw.endswith("line 20")

            # A query that keeps only lines 30-39; the selected line is gone.
            app.query_bar.set_query_value("line 3[0-9]")
            app._update_state(query="line 3[0-9]")
            app._render_log()
            await pilot.pause()

            assert app.log_panel.cursor >= 0, "the cursor was dropped entirely"
            surviving = app.log_panel.cursor_entry
            assert surviving is not None
            assert "line 3" in surviving.raw
            # Nearest by position, which for a truncated-from-the-front view is
            # the top of what is left rather than a reset to index 0 by luck.
            assert app.log_panel.cursor == min(20, len(app.log_panel.entries) - 1)

    asyncio.run(scenario())


# --- layout -----------------------------------------------------------------


def test_the_detail_pane_sits_beside_the_log_when_wide(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app._set_detail_pane(True)
            await pilot.pause()

            log = app.log_panel.region
            pane = app.detail_pane.region

            assert app.has_class("-wide")
            assert log.right <= pane.x, "the panes overlap instead of sharing the row"
            assert log.y == pane.y, "side by side means the same vertical band"
            assert pane.right <= 140

    asyncio.run(scenario())


def test_the_detail_pane_stacks_below_the_log_when_narrow(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app._set_detail_pane(True)
            await pilot.pause()

            log = app.log_panel.region
            pane = app.detail_pane.region

            assert app.has_class("-narrow")
            assert log.bottom <= pane.y, "stacked means the pane starts below the log"
            assert pane.bottom <= 30
            assert pane.width > 0

    asyncio.run(scenario())


def test_the_detail_pane_takes_the_viewer_at_eighty_columns(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app._set_detail_pane(True)
            await pilot.pause()

            assert app.has_class("-compact")
            pane = app.detail_pane.region
            assert pane.width > 0 and pane.height > 0, "the pane is not on screen"
            assert pane.right <= 80, "the pane runs off an 80 column terminal"
            assert pane.bottom <= 24
            # The log is behind it rather than squeezed alongside.
            assert app.log_panel.region.width == 0

    asyncio.run(scenario())


def test_the_detail_pane_actually_paints_at_eighty_columns(tmp_path: Path) -> None:
    """Laying out at the right coordinates is not the same as being readable."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            await pilot.press("home")
            await pilot.press("enter")
            await pilot.pause()

            strips = app.screen._compositor.render_strips()
            painted = "\n".join(
                "".join(segment.text for segment in strip) for strip in strips
            )
            assert "Event detail" in painted
            assert "Timestamp" in painted

    asyncio.run(scenario())


def test_copy_mode_hides_the_detail_pane(tmp_path: Path) -> None:
    """Copy mode exists to leave log text and nothing else on screen."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log_file(tmp_path), announce=False)
            app._set_detail_pane(True)
            await pilot.pause()
            assert app.detail_pane.region.width > 0

            await pilot.press("ctrl+l")
            await pilot.pause()

            assert app.detail_pane.region.width == 0

    asyncio.run(scenario())
