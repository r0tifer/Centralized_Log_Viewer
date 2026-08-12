"""Marked lines: the service, the gutter, and the export option."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual.widgets import Checkbox, Input, OptionList, Static

from clv.app import LogViewerApp
from clv.services.marks import MarkSet, mark_key
from clv.services.parsing import parse_lines
from clv.storage import SessionState, StateStore
from clv.widgets.export_dialog import ExportDialog
from clv.widgets.log_view import MARK_GLYPH


# --- the service ------------------------------------------------------------


def _entries(count: int = 6):
    return parse_lines(
        [f"2026-08-07 09:25:{index:02d} - INFO - line {index}" for index in range(count)]
    )


def test_a_key_is_stable_for_the_same_line_in_the_same_source() -> None:
    entry = _entries(1)[0]
    source = Path("/var/log/app.log")

    assert mark_key(source, entry) == mark_key(source, entry)


def test_the_same_line_in_a_different_source_is_a_different_mark() -> None:
    """Marks in one log must not show up in another."""

    entry = _entries(1)[0]

    assert mark_key(Path("/var/log/a.log"), entry) != mark_key(Path("/var/log/b.log"), entry)


def test_toggle_adds_then_removes() -> None:
    marks = MarkSet()
    entry = _entries(1)[0]
    source = Path("/var/log/app.log")

    assert marks.toggle(source, entry) is True
    assert marks.contains(source, entry) is True
    assert len(marks) == 1

    assert marks.toggle(source, entry) is False
    assert marks.contains(source, entry) is False
    assert len(marks) == 0


def test_prune_drops_marks_whose_lines_have_been_evicted() -> None:
    """The buffer is bounded; the count on screen has to stay honest."""

    marks = MarkSet()
    source = Path("/var/log/app.log")
    entries = _entries(6)
    for entry in entries[:3]:
        marks.toggle(source, entry)

    # The ring buffer has moved on: only the last four lines remain.
    dropped = marks.prune(source, entries[2:])

    assert dropped == 2
    assert len(marks) == 1
    assert marks.contains(source, entries[2]) is True


def test_prune_leaves_other_sources_alone() -> None:
    marks = MarkSet()
    entries = _entries(2)
    other = Path("/var/log/other.log")
    marks.toggle(other, entries[0])
    marks.toggle(Path("/var/log/app.log"), entries[1])

    marks.prune(Path("/var/log/app.log"), [])

    assert len(marks) == 1
    assert marks.contains(other, entries[0]) is True


def test_count_for_reports_only_the_named_source() -> None:
    marks = MarkSet()
    entries = _entries(3)
    for entry in entries:
        marks.toggle(Path("/var/log/a.log"), entry)
    marks.toggle(Path("/var/log/b.log"), entries[0])

    assert marks.count_for(Path("/var/log/a.log")) == 3
    assert marks.count_for(Path("/var/log/b.log")) == 1


def test_a_markset_offers_no_way_to_serialise_itself() -> None:
    """The privacy constraint, asserted rather than left to a comment.

    A mark digest is derived from log content, and AGENTS.md is explicit that
    session state holds paths and settings only.
    """

    marks = MarkSet()

    for forbidden in ("to_dict", "as_dict", "to_json", "keys", "save"):
        assert not hasattr(marks, forbidden), f"MarkSet grew a {forbidden} escape hatch"
    assert not hasattr(marks, "__dict__"), "MarkSet must stay __slots__-only"
    assert not any("mark" in field for field in SessionState.PERSISTED_FIELDS)


# --- the UI path ------------------------------------------------------------


def _log_file(tmp_path: Path, count: int = 12) -> Path:
    path = tmp_path / "app.log"
    path.write_text(
        "\n".join(
            f"2026-08-07 09:25:{index:02d} - INFO - line {index}" for index in range(count)
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class _Notices:
    def __init__(self, app: LogViewerApp) -> None:
        self.items: list[str] = []
        app.notify = lambda message, **kw: self.items.append(message)

    def text(self) -> str:
        return " | ".join(self.items)


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


def _painted(app: LogViewerApp) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(segment.text for segment in strip) for strip in strips)


async def _open(pilot, app: LogViewerApp, tmp_path: Path, count: int = 12) -> None:
    await pilot.pause()
    app._select_source(_log_file(tmp_path, count), announce=False)
    app.set_focus(app.log_panel)
    await pilot.pause()


def test_m_toggles_a_mark_and_draws_it_in_the_gutter(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)

            await pilot.press("home", "down", "down")
            await pilot.pause()
            marked_entry = app.log_panel.cursor_entry

            await pilot.press("m")
            await pilot.pause()

            assert app._marks.contains(app._selected_source, marked_entry) is True
            assert app.log_panel.rows[app.log_panel.cursor].marked is True
            assert MARK_GLYPH in _painted(app)
            assert "1 marked" in _status(app)

            await pilot.press("m")
            await pilot.pause()

            assert app._marks.contains(app._selected_source, marked_entry) is False
            assert app.log_panel.rows[app.log_panel.cursor].marked is False
            assert MARK_GLYPH not in _painted(app)
            assert "marked" not in _status(app)

    asyncio.run(scenario())


def test_capital_m_cycles_through_marks_in_buffer_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            notices = _Notices(app)

            for row in (1, 5, 9):
                app.log_panel.move_cursor(row)
                await pilot.pause()
                await pilot.press("m")
                await pilot.pause()

            app.log_panel.move_cursor(0)
            await pilot.pause()

            visited = []
            for _ in range(4):
                await pilot.press("M")
                await pilot.pause()
                visited.append(app.log_panel.cursor)

            assert visited == [1, 5, 9, 1], visited
            assert "Wrapped to the first mark" in notices.text()

    asyncio.run(scenario())


def test_marks_survive_a_filter_change_that_keeps_the_line(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)

            await pilot.press("home", "down", "down", "down")
            await pilot.pause()
            entry = app.log_panel.cursor_entry
            assert entry.raw.endswith("line 3")
            await pilot.press("m")
            await pilot.pause()

            app._update_state(query="line 3")
            app._render_log()
            await pilot.pause()

            assert app._marks.contains(app._selected_source, entry) is True
            assert app.log_panel.rows[0].marked is True
            assert MARK_GLYPH in _painted(app)

    asyncio.run(scenario())


def test_a_mark_reappears_when_a_filter_stops_hiding_its_line(tmp_path: Path) -> None:
    """Content-keyed, so there is nothing to restore — it just matches again."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)

            await pilot.press("home", "down", "down", "down")
            await pilot.pause()
            entry = app.log_panel.cursor_entry
            await pilot.press("m")
            await pilot.pause()

            # Hide it.
            app._update_state(query="line 7")
            app._render_log()
            await pilot.pause()
            assert entry not in app.log_panel.entries
            assert not any(row.marked for row in app.log_panel.rows)

            # Bring it back.
            app._update_state(query="")
            app._render_log()
            await pilot.pause()

            index = app.log_panel.entries.index(entry)
            assert app.log_panel.rows[index].marked is True

    asyncio.run(scenario())


def test_a_mark_on_an_evicted_line_is_dropped_without_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path, count=6)

            await pilot.press("home")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            assert len(app._marks) == 1

            # The ring buffer moved on and the marked line is no longer in it.
            app._entries.clear()
            app._entries.extend(parse_lines([f"2026-08-07 10:00:{i:02d} - INFO - later {i}" for i in range(4)]))
            app._render_log()
            await pilot.pause()

            assert len(app._marks) == 0
            assert "marked" not in _status(app)

    asyncio.run(scenario())


def test_marks_are_never_written_to_the_session_file(tmp_path: Path) -> None:
    """The privacy guard: a mark digest is derived from log content."""

    async def scenario() -> None:
        store = StateStore(root=tmp_path / "cache")
        app = LogViewerApp(store=store)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)

            await pilot.press("home")
            await pilot.press("m")
            await pilot.pause()
            marked = app.log_panel.cursor_entry
            assert len(app._marks) == 1

            store.save(app.state)

        payload = json.loads(store.path.read_text(encoding="utf-8"))

        assert "marks" not in payload
        assert not any("mark" in key for key in payload)
        blob = json.dumps(payload)
        assert marked.raw not in blob
        assert mark_key(app._selected_source, marked).split("\0")[1] not in blob
        # And the dataclass itself has no field for them.
        assert not any("mark" in field for field in SessionState.PERSISTED_FIELDS)

    asyncio.run(scenario())


def test_m_with_no_line_selected_explains_itself(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            notices = _Notices(app)

            app.action_toggle_mark()
            await pilot.pause()

            assert "Move the cursor to a line to mark it." in notices.text()

    asyncio.run(scenario())


def test_capital_m_with_nothing_marked_says_how_to_mark(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            notices = _Notices(app)

            await pilot.press("M")
            await pilot.pause()

            assert "press m to mark one" in notices.text()

    asyncio.run(scenario())


# --- export -----------------------------------------------------------------


def test_export_writes_only_the_marked_lines_when_asked(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 34)) as pilot:
            await _open(pilot, app, tmp_path)

            wanted = []
            for row in (2, 4, 6):
                app.log_panel.move_cursor(row)
                await pilot.pause()
                wanted.append(app.log_panel.cursor_entry.raw)
                await pilot.press("m")
                await pilot.pause()

            destination = tmp_path / "marked.log"
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, ExportDialog)

            checkbox = app.screen.query_one("#export-marked-only", Checkbox)
            assert checkbox.disabled is False
            assert "3 lines" in str(checkbox.label)
            checkbox.value = True
            # Plain text: raw lines, byte-identical to what is on screen.
            app.screen.query_one("#export-format", OptionList).highlighted = 2
            app.screen.query_one("#export-path", Input).value = str(destination)
            await pilot.pause()
            app.screen._finalize()
            await pilot.pause()
            await pilot.pause()

            assert destination.read_text(encoding="utf-8").splitlines() == wanted

    asyncio.run(scenario())


def test_the_marked_only_checkbox_is_disabled_and_says_why_when_nothing_is_marked(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 34)) as pilot:
            await _open(pilot, app, tmp_path)

            await pilot.press("ctrl+e")
            await pilot.pause()

            checkbox = app.screen.query_one("#export-marked-only", Checkbox)
            assert checkbox.disabled is True
            assert "nothing marked" in str(checkbox.label)

    asyncio.run(scenario())


def test_export_still_writes_everything_when_the_box_is_left_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 34)) as pilot:
            await _open(pilot, app, tmp_path)

            app.log_panel.move_cursor(2)
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()

            destination = tmp_path / "all.log"
            await pilot.press("ctrl+e")
            await pilot.pause()
            app.screen.query_one("#export-format", OptionList).highlighted = 2
            app.screen.query_one("#export-path", Input).value = str(destination)
            await pilot.pause()
            app.screen._finalize()
            await pilot.pause()
            await pilot.pause()

            assert len(destination.read_text(encoding="utf-8").splitlines()) == 12

    asyncio.run(scenario())


def test_the_export_dialog_still_fits_eighty_by_twenty_four(tmp_path: Path) -> None:
    """The checkbox is one more row inside a dialog that already had to fit."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await _open(pilot, app, tmp_path)

            await pilot.press("ctrl+e")
            await pilot.pause()

            container = app.screen.query_one("#export-dialog")
            assert container.region.right <= 80
            assert container.region.bottom <= 24
            checkbox = app.screen.query_one("#export-marked-only", Checkbox)
            assert checkbox.region.width > 0
            assert checkbox.region.right <= 80

    asyncio.run(scenario())
