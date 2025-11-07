from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, call

from clv.app import DiscoverySummary, LogViewerApp
from clv.storage import SessionState
from rich.console import Group
from rich.text import Text
from textual.widgets import RichLog


def _make_app() -> LogViewerApp:
    app = LogViewerApp()
    app.log_panel = MagicMock(spec=RichLog)
    return app


def test_write_log_line_preserves_plain_text() -> None:
    app = _make_app()
    app._write_log_line("hello world")
    app.log_panel.write.assert_called_once_with("hello world")


def test_write_log_line_does_not_strip_newline() -> None:
    app = _make_app()
    app._write_log_line("already done\n")
    app.log_panel.write.assert_called_once_with("already done\n")


def test_render_log_writes_each_line_with_break() -> None:
    app = _make_app()
    app.log_panel.write = MagicMock()
    app.log_panel.clear = MagicMock()
    app.log_panel.scroll_end = MagicMock()
    app.state = SessionState(auto_scroll=False)
    app._selected_source = Path("/tmp/example.log")
    app._raw_lines = deque(["first entry", "second entry"])

    app._render_log()

    recorded = [entry.args[0] for entry in app.log_panel.write.call_args_list]
    assert all(isinstance(entry, Text) for entry in recorded)
    assert [entry.plain for entry in recorded] == ["first entry", "second entry"]


def test_render_log_shows_summary_without_selection() -> None:
    app = _make_app()
    app.log_panel.write = MagicMock()
    app.log_panel.clear = MagicMock()
    app._discovery_summary = DiscoverySummary(
        source_count=2,
        folder_count=1,
        log_count=5,
    )

    app._render_log()

    assert app.log_panel.write.call_args_list == [
        call("Summary"),
        call("Total log sources: 2"),
        call("Folders containing logs: 1"),
        call("Total logs: 5"),
        call(""),
        call("Select a log from the tree to begin."),
    ]


def test_render_log_handles_empty_source() -> None:
    app = _make_app()
    app.log_panel.write = MagicMock()
    app.log_panel.clear = MagicMock()
    app.state = SessionState(auto_scroll=False)
    app._selected_source = Path("/tmp/empty.log")
    app._raw_lines = deque()

    app._render_log()

    app.log_panel.write.assert_called_once_with(
        "No log entries found in the selected source."
    )


def test_render_log_formats_json_when_enabled() -> None:
    app = _make_app()
    app.log_panel.write = MagicMock()
    app.log_panel.clear = MagicMock()
    app.log_panel.scroll_end = MagicMock()
    line = '2024-01-01 12:00:00 - INFO - {"status": "ok"}'
    app.state = SessionState(auto_scroll=False, pretty_rendering=True)
    app._selected_source = Path("/tmp/example.log")
    app._raw_lines = deque([line])

    app._render_log()

    rendered = app.log_panel.write.call_args_list[0].args[0]
    assert isinstance(rendered, Group)
    header, panel = rendered.renderables
    assert isinstance(header, Text)
    assert header.plain.startswith("2024-01-01")
    assert hasattr(panel, "title") and panel.title == "JSON"


def test_render_log_plain_text_when_pretty_disabled() -> None:
    app = _make_app()
    app.log_panel.write = MagicMock()
    app.log_panel.clear = MagicMock()
    app.state = SessionState(auto_scroll=False, pretty_rendering=False)
    app._selected_source = Path("/tmp/example.log")
    app._raw_lines = deque(
        ['2024-01-01 12:00:00 - INFO - {"status": "ok"}']
    )

    app._render_log()

    rendered = app.log_panel.write.call_args_list[0].args[0]
    assert isinstance(rendered, Text)


def test_csv_formatter_respects_limits() -> None:
    app = _make_app()
    app._config.csv_max_rows = 2
    app._config.csv_max_cols = 2
    payload = "col1,col2\n1,2\n3,4\n5,6"

    result = app._format_csv_payload(payload)

    assert result is not None
    table, label = result
    assert label == "CSV preview"
    assert len(table.columns) == 2
    assert len(table.rows) == 2
