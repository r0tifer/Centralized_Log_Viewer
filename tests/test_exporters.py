"""Built-in exporters, atomic writing, and the Ctrl+E path through the UI.

The service-level tests pin the file formats; the app-level ones pin the two
things that are easy to get wrong once a filter is involved: that an export
writes the *filtered set* rather than the visible window, and that nothing is
written until the operator confirms.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from textual.widgets import Button, Input, OptionList

from clv.app import LogViewerApp
from clv.plugins import Exporter, ExportResult
from clv.services.export import (
    BUILTIN_FORMATS,
    CSV_COLUMNS,
    builtin_format,
    default_stem,
    describe_formats,
    write_atomically,
    write_csv,
    write_jsonl,
    write_text,
)
from clv.services.parsing import parse_lines
from clv.widgets.export_dialog import ExportDialog

LINES = [
    "Aug  7 09:25:01 web01 sshd[4242]: Accepted password for root",
    '2026-08-07 09:25:02 - ERROR - {"status": 500, "path": "/api"}',
    "  at some.package.Thing.method(Thing.java:42)",
]


def _render(writer, entries) -> str:
    handle = io.StringIO()
    writer(entries, handle)
    return handle.getvalue()


# --- built-in formats -------------------------------------------------------


def test_jsonl_round_trips_an_entry_including_fields() -> None:
    entries = parse_lines(LINES)

    payloads = [json.loads(line) for line in _render(write_jsonl, entries).splitlines()]

    assert len(payloads) == 3
    syslog = payloads[0]
    assert syslog["raw"] == LINES[0]
    assert syslog["format"] == "syslog"
    assert syslog["fields"] == {"host": "web01", "tag": "sshd", "pid": "4242"}
    # A continuation keeps its inherited level, and reports itself as one.
    assert payloads[2]["continuation"] is True
    assert payloads[2]["level"] == "ERROR"
    assert payloads[2]["fields"] == {}


def test_csv_is_rectangular_and_quotes_embedded_delimiters() -> None:
    entries = parse_lines(['2026-08-07 09:25:02 - INFO - a "quoted", comma line'])

    rendered = _render(write_csv, entries)
    rows = list(csv.reader(io.StringIO(rendered)))

    assert rows[0] == list(CSV_COLUMNS)
    assert len(rows) == 2
    assert len(rows[1]) == len(CSV_COLUMNS)
    # The awkward characters survive the round trip intact.
    assert rows[1][CSV_COLUMNS.index("raw")] == entries[0].raw
    assert '"' in rendered and "\r\n" in rendered


def test_csv_carries_fields_as_one_json_column() -> None:
    entries = parse_lines([LINES[0]])

    rows = list(csv.reader(io.StringIO(_render(write_csv, entries))))

    fields = json.loads(rows[1][CSV_COLUMNS.index("fields")])
    assert fields == {"host": "web01", "pid": "4242", "tag": "sshd"}


def test_plain_text_is_byte_identical_to_the_raw_lines() -> None:
    entries = parse_lines(LINES)

    assert _render(write_text, entries) == "".join(f"{line}\n" for line in LINES)


def test_every_builtin_has_a_distinct_key_and_extension() -> None:
    keys = [fmt.key for fmt in BUILTIN_FORMATS]

    assert len(keys) == len(set(keys)) == 3
    assert builtin_format("csv") is not None
    assert builtin_format("nope") is None


# --- atomic writing ---------------------------------------------------------


def test_write_atomically_writes_the_file_and_removes_its_temp(tmp_path: Path) -> None:
    destination = tmp_path / "out.jsonl"

    written = write_atomically(destination, parse_lines(LINES), write_jsonl)

    assert written == 3
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["out.jsonl"]


def test_a_failed_write_leaves_the_destination_and_no_temp_behind(tmp_path: Path) -> None:
    destination = tmp_path / "out.log"
    destination.write_text("previous export\n", encoding="utf-8")

    def exploding(entries, handle) -> None:
        handle.write("partial\n")
        raise OSError("disk full")

    with pytest.raises(OSError):
        write_atomically(destination, parse_lines(LINES), exploding)

    assert destination.read_text(encoding="utf-8") == "previous export\n"
    assert [p.name for p in tmp_path.iterdir()] == ["out.log"]


def test_write_to_a_missing_directory_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write_atomically(tmp_path / "nope" / "out.log", parse_lines(LINES), write_text)


def test_default_stem_names_the_source_and_the_moment() -> None:
    moment = datetime(2026, 8, 11, 14, 25, 30)

    assert default_stem(Path("/var/log/syslog"), now=moment) == "syslog-20260811-142530"
    assert default_stem(Path("/var/log/app.log.1"), now=moment).startswith("app.log.1-")
    assert default_stem(None, now=moment) == "clv-export-20260811-142530"


def test_describe_formats_lists_what_is_available() -> None:
    assert describe_formats(["CSV", "mine"]) == "Exporters: CSV, mine"
    assert describe_formats([]) == "Exporters: none"


# --- the UI path ------------------------------------------------------------


class _Recorder(Exporter):
    name = "recorder"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def export(self, entries, context) -> ExportResult:
        self.calls.append(len(entries))
        return ExportResult(ok=True, detail=f"sent {len(entries)}", destination=None)


class _Exploding(Exporter):
    name = "exploding-exporter"

    def export(self, entries, context) -> ExportResult:
        raise RuntimeError("boom")


def _log_file(tmp_path: Path, count: int = 30) -> Path:
    path = tmp_path / "app.log"
    path.write_text(
        "\n".join(
            f"2026-08-07 09:25:{index:02d} - INFO - "
            f"{'alpha' if index % 2 else 'beta'} line {index}"
            for index in range(count)
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class _Session:
    """An open source, a captured notification list, and the export dialog."""

    def __init__(self, app: LogViewerApp) -> None:
        self.app = app
        self.notices: list[tuple[str, str]] = []

    def capture(self) -> None:
        self.app.notify = lambda message, **kw: self.notices.append(
            (message, kw.get("severity", ""))
        )

    def messages(self) -> str:
        return " | ".join(message for message, _ in self.notices)


async def _open(tmp_path: Path, pilot_size=(120, 30), count: int = 30):
    app = LogViewerApp()
    session = _Session(app)
    context = app.run_test(size=pilot_size)
    pilot = await context.__aenter__()
    app._select_source(_log_file(tmp_path, count), announce=False)
    # Focus the pane, not the query input: Textual's Input binds ctrl+e to
    # end-of-line, so the export binding is unreachable from inside it. That is
    # a deliberate trade (see the BINDINGS comment) and has its own test below.
    app.set_focus(app.log_panel)
    session.capture()
    await pilot.pause()
    return app, session, pilot, context


async def _open_dialog(app, pilot) -> ExportDialog:
    app.action_export_view()
    await pilot.pause()
    await pilot.pause()
    assert isinstance(app.screen, ExportDialog)
    return app.screen


async def _confirm(pilot, dialog: ExportDialog) -> None:
    dialog.query_one("#confirm-export", Button).press()
    await pilot.pause()
    await pilot.pause()


def test_ctrl_e_opens_the_dialog_and_escape_cancels_without_writing(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path)
        try:
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, ExportDialog)

            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()

            assert not isinstance(app.screen, ExportDialog)
            assert "canceled" in session.messages()
            # Nothing but the source itself was written.
            assert [p.name for p in tmp_path.iterdir()] == ["app.log"]
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_export_writes_the_whole_filtered_set_not_the_visible_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=30)
        try:
            # The pane is showing five lines; the export is not about the pane.
            app._show_lines = 5
            destination = tmp_path / "full.log"

            dialog = await _open_dialog(app, pilot)
            assert "30 entries" in str(dialog.query_one("#export-count").content)
            dialog.query_one("#export-format", OptionList).highlighted = 2  # plain text
            dialog.query_one("#export-path", Input).value = str(destination)
            await _confirm(pilot, dialog)

            written = destination.read_text(encoding="utf-8").splitlines()
            assert len(written) == 30
            assert "Exported 30 entries" in session.messages()
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_export_respects_the_active_query(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=30)
        try:
            app._update_state(query="alpha")
            destination = tmp_path / "alpha.log"

            dialog = await _open_dialog(app, pilot)
            assert "15 entries" in str(dialog.query_one("#export-count").content)
            dialog.query_one("#export-format", OptionList).highlighted = 2
            dialog.query_one("#export-path", Input).value = str(destination)
            await _confirm(pilot, dialog)

            written = destination.read_text(encoding="utf-8").splitlines()
            assert len(written) == 15
            assert all("alpha" in line for line in written)
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_overwriting_an_existing_file_takes_a_second_press(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=4)
        try:
            destination = tmp_path / "existing.log"
            destination.write_text("do not clobber me\n", encoding="utf-8")

            dialog = await _open_dialog(app, pilot)
            dialog.query_one("#export-format", OptionList).highlighted = 2
            dialog.query_one("#export-path", Input).value = str(destination)
            await _confirm(pilot, dialog)

            # Still open, still untouched, and the dialog says why.
            assert isinstance(app.screen, ExportDialog)
            assert "exists" in str(dialog.query_one("#export-hint").content)
            assert destination.read_text(encoding="utf-8") == "do not clobber me\n"

            await _confirm(pilot, dialog)
            assert not isinstance(app.screen, ExportDialog)
            assert len(destination.read_text(encoding="utf-8").splitlines()) == 4
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_an_unwritable_destination_is_reported_not_raised(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=4)
        try:
            dialog = await _open_dialog(app, pilot)
            dialog.query_one("#export-format", OptionList).highlighted = 2
            dialog.query_one("#export-path", Input).value = str(
                tmp_path / "missing-dir" / "out.log"
            )
            await _confirm(pilot, dialog)

            assert "Export failed" in session.messages()
            assert app.is_running
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_a_plugin_exporter_is_offered_and_reports_its_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=6)
        try:
            recorder = _Recorder()
            app._plugins.exporters.append(recorder)

            dialog = await _open_dialog(app, pilot)
            # Built-ins first, then the plugin; it supplies its own destination,
            # so the path input is disabled for it.
            dialog.query_one("#export-format", OptionList).highlighted = 3
            await pilot.pause()
            assert dialog.query_one("#export-path", Input).disabled is True

            await _confirm(pilot, dialog)

            assert recorder.calls == [6]
            assert "sent 6" in session.messages()
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_a_raising_exporter_is_recorded_and_the_app_survives(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=6)
        try:
            app._plugins.exporters.append(_Exploding())

            dialog = await _open_dialog(app, pilot)
            dialog.query_one("#export-format", OptionList).highlighted = 3
            await _confirm(pilot, dialog)

            assert any("raised" in error.message for error in app._plugins.errors)
            assert "exploding-exporter" in session.messages()
            assert app.is_running
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_the_whole_export_is_reachable_from_the_keyboard(tmp_path: Path) -> None:
    """Down to the format, Enter to accept it, type the path, Enter to write."""

    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=4)
        try:
            destination = tmp_path / "keyboard.csv"

            await pilot.press("ctrl+e")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ExportDialog)

            await pilot.press("down")  # JSON Lines -> CSV
            await pilot.press("enter")  # accept the format, move to the path
            await pilot.pause()
            assert app.focused is dialog.query_one("#export-path", Input)
            # The default name followed the format the cursor landed on.
            assert dialog.query_one("#export-path", Input).value.endswith(".csv")

            dialog.query_one("#export-path", Input).value = str(destination)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            rows = list(csv.reader(io.StringIO(destination.read_text(encoding="utf-8"))))
            assert rows[0] == list(CSV_COLUMNS)
            assert len(rows) == 5  # header + four entries
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_app_bindings_do_not_reach_through_the_dialog(tmp_path: Path) -> None:
    """`q` must not quit, and `a` must not stack the add-source dialog, from a modal."""

    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=4)
        try:
            dialog = await _open_dialog(app, pilot)
            for key in ("q", "a", "y"):
                await pilot.press(key)
                await pilot.pause()
                assert app.is_running
                assert app.screen is dialog
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_ctrl_e_stays_out_of_the_query_input(tmp_path: Path) -> None:
    """Textual's Input owns ctrl+e (end-of-line); the export binding yields to it.

    Pinned as a test rather than left to be rediscovered: the alternative is a
    `priority` binding that steals a text-editing key, which is the worse trade.
    """

    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, count=4)
        try:
            app.action_focus_query()
            await pilot.pause()
            await pilot.press("ctrl+e")
            await pilot.pause()

            assert not isinstance(app.screen, ExportDialog)
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_export_without_a_source_explains_itself(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            notices: list[str] = []
            app.notify = lambda message, **kw: notices.append(message)
            app.action_export_view()
            await pilot.pause()
            await pilot.pause()

            assert not isinstance(app.screen, ExportDialog)
            assert any("Open a log" in message for message in notices)

    asyncio.run(scenario())


def test_the_drawer_lists_the_available_exporters(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._plugins.exporters.append(_Recorder())
            app._refresh_plugin_status()
            await pilot.pause()

            text = str(app.advanced_drawer.query_one("#export-status").content)
            assert "CSV" in text
            assert "recorder" in text

    asyncio.run(scenario())


def test_the_export_dialog_fits_eighty_columns(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, session, pilot, context = await _open(tmp_path, pilot_size=(80, 24), count=4)
        try:
            dialog = await _open_dialog(app, pilot)
            container = dialog.query_one("#export-dialog")
            actions = dialog.query_one("#dialog-actions")

            assert container.region.right <= 80
            assert container.region.bottom <= 24
            # The confirm button is the last thing in the dialog: if it is on
            # screen, nothing above it has been pushed off.
            assert actions.region.bottom <= 24
        finally:
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_read_only_directory_surfaces_as_an_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(OSError):
            write_atomically(locked / "out.log", parse_lines(LINES), write_text)
    finally:
        locked.chmod(0o700)
