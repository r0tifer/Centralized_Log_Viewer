"""Rendering behaviour of the log pane."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Group
from rich.text import Text

from clv.app import LogViewerApp
from clv.services.discovery import DiscoveryReport, DiscoveredFile
from clv.services.parsing import parse_lines
from clv.storage import SessionState
from clv.widgets.log_view import LogView


def _make_app(**state) -> LogViewerApp:
    app = LogViewerApp()
    app.log_panel = MagicMock(spec=LogView)
    # The cursor properties are read before every re-render so it can be put
    # back afterwards; a bare MagicMock would answer with another MagicMock.
    app.log_panel.cursor_entry = None
    app.log_panel.cursor = -1
    app.state = SessionState(auto_scroll=False, **state)
    return app


def _written(app) -> list:
    """Renderables that reached the pane, in the order they were written.

    Read off ``mock_calls`` rather than one method's ``call_args_list`` because
    entries go through ``write_entry`` and messages through ``write``, and a
    render can interleave them.
    """

    return [
        call.args[0]
        for call in app.log_panel.mock_calls
        if call[0] in ("write", "write_entry") and call.args
    ]


def _plain(app) -> list[str]:
    return [getattr(item, "plain", str(item)) for item in _written(app)]


def _styles(text: Text) -> str:
    """stylize() records styles as spans, not on Text.style."""
    return " ".join(str(span.style) for span in text.spans)


def test_lines_render_as_text_preserving_raw_content() -> None:
    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(["first entry", "second entry"]))

    app._render_log()

    assert all(isinstance(item, Text) for item in _written(app))
    assert _plain(app) == ["first entry", "second entry"]


def test_severity_colours_are_applied_per_level() -> None:
    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(
        parse_lines(
            [
                "2026-08-07 09:25:01 - ERROR - broke",
                "2026-08-07 09:25:02 - INFO - fine",
            ]
        )
    )

    app._render_log()

    error_line, info_line = _written(app)
    assert "#f87171" in _styles(error_line)
    assert "#22c55e" in _styles(info_line)


def test_continuation_lines_are_dimmed() -> None:
    """An inherited level must not read as a fresh entry at that severity."""
    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(
        parse_lines(
            [
                "2026-08-07 09:25:01 - ERROR - broke",
                "Traceback (most recent call last):",
            ]
        )
    )

    app._render_log()

    parent, continuation = _written(app)
    assert "dim" not in _styles(parent)
    assert "dim" in _styles(continuation)


def test_summary_is_shown_when_nothing_is_selected() -> None:
    app = _make_app()
    app._report = DiscoveryReport(
        files=[DiscoveredFile(path=Path("/a.log"), root=Path("/"), size=1)],
        roots=[Path("/")],
        directories={Path("/")},
    )

    app._render_log()

    text = "\n".join(_plain(app))
    assert "Log files found: 1" in text
    assert "Select a log from the tree to begin." in text


def test_empty_source_says_so() -> None:
    app = _make_app()
    app._selected_source = Path("/tmp/empty.log")
    app._entries = deque()

    app._render_log()

    assert _plain(app) == ["No log entries in the selected source."]


def test_empty_filter_result_explains_itself() -> None:
    """An empty pane names the filter responsible instead of just saying 'no match'."""
    app = _make_app(severity="error")
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(["a line with no level", "another"]))

    app._render_log()

    message = _plain(app)[0]
    assert "no detected severity" in message


def test_invalid_query_is_reported_not_raised() -> None:
    app = _make_app(query="(unclosed")
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(["anything"]))

    app._render_log()

    assert "Invalid query" in _plain(app)[0]


def test_structured_rendering_wraps_json_payloads() -> None:
    app = _make_app(pretty_rendering=True)
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(['2026-08-07 09:25:01 - INFO - {"status": "ok"}']))

    app._render_log()

    rendered = _written(app)[0]
    assert isinstance(rendered, Group)
    header, panel = rendered.renderables
    assert isinstance(header, Text)
    assert panel.title == "JSON"


def test_structured_rendering_off_yields_plain_text() -> None:
    app = _make_app(pretty_rendering=False)
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(['2026-08-07 09:25:01 - INFO - {"status": "ok"}']))

    app._render_log()

    assert isinstance(_written(app)[0], Text)


def test_append_only_renders_the_new_entries() -> None:
    """Tailing must not redraw the whole pane."""
    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines([f"line {i}" for i in range(50)]))

    app.log_panel.reset_mock()
    app._append_entries(parse_lines(["brand new line"]))

    assert _plain(app) == ["brand new line"]
    app.log_panel.clear.assert_not_called()


def test_csv_formatter_respects_limits() -> None:
    app = _make_app()
    app._config.csv_max_rows = 2
    app._config.csv_max_cols = 2

    result = app._format_csv("col1,col2\n1,2\n3,4\n5,6")

    assert result is not None
    table, label = result
    assert label == "CSV preview"
    assert len(table.columns) == 2
    assert len(table.rows) == 2


# --- the pane paints its own background -------------------------------------


def _rendered_segments(pane: LogView):
    """Every segment the pane would actually put on screen."""

    for y in range(pane.size.height):
        for segment in pane.render_line(y):
            if segment.text:
                yield segment


def test_the_log_pane_paints_its_own_background() -> None:
    """Otherwise the terminal's default background shows through.

    `render_line` builds its strips from Rich renderables, and a `Text` with no
    background of its own produces segments with `bgcolor=None`. Those cells
    are painted by the terminal rather than by the widget — which is invisible
    on a dark terminal and turns the whole log pane white on a light one, no
    matter which Textual theme is selected. Everything else in the app uses
    stock widgets, which is why only this pane was affected.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app._selected_source = Path("/tmp/example.log")
            app._entries = deque(parse_lines(["2026-08-11 10:00:00 - INFO - a line"]))
            app._render_log()
            await pilot.pause()

            pane = app.query_one("#log-stream", LogView)
            unpainted = [
                segment
                for segment in _rendered_segments(pane)
                if segment.style is None or segment.style.bgcolor is None
            ]

            assert not unpainted, (
                f"{len(unpainted)} segments carry no background and would be "
                "painted by the terminal instead of the widget"
            )

    asyncio.run(scenario())


def test_the_pane_background_follows_the_active_theme() -> None:
    """Hardcoding the colour would just move the bug somewhere else."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app._selected_source = Path("/tmp/example.log")
            app._entries = deque(parse_lines(["2026-08-11 10:00:00 - INFO - a line"]))

            seen = {}
            for theme in ("textual-dark", "textual-light"):
                app.theme = theme
                app._render_log()
                await pilot.pause()
                pane = app.query_one("#log-stream", LogView)
                expected = pane.rich_style.bgcolor
                backgrounds = {
                    segment.style.bgcolor for segment in _rendered_segments(pane)
                }
                assert backgrounds == {expected}, (
                    f"{theme}: pane painted {backgrounds}, widget style says {expected}"
                )
                seen[theme] = expected

            # And the two themes must actually differ, or the assertion above
            # would pass on a pane that ignored the theme entirely.
            assert seen["textual-dark"] != seen["textual-light"]

    asyncio.run(scenario())


def test_the_cursor_and_watch_highlights_still_win_over_the_background() -> None:
    """The background goes on underneath, so nothing above it is erased."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app._selected_source = Path("/tmp/example.log")
            app._entries = deque(
                parse_lines([f"2026-08-11 10:00:0{i} - INFO - line {i}" for i in range(3)])
            )
            app._render_log()
            await pilot.pause()

            pane = app.query_one("#log-stream", LogView)
            plain = pane.rich_style.bgcolor

            pane.move_cursor(1)
            await pilot.pause()
            cursor_row = {
                segment.style.bgcolor for segment in pane.render_line(1) if segment.text
            }
            assert cursor_row != {plain}, "the cursor row lost its highlight"

            pane.set_row_watched(2, True)
            await pilot.pause()
            watched_row = {
                segment.style.bgcolor for segment in pane.render_line(2) if segment.text
            }
            assert watched_row != {plain}, "the watched row lost its highlight"
            assert watched_row != cursor_row, "watch and cursor became indistinguishable"

    asyncio.run(scenario())
