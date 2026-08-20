"""The structured columns: what fills them, what is left out, and what it costs.

With `o` on, a row is no longer the raw line with a colour on it — it is a set
of fixed cells built from what the parser recovered, and the message starts at
the same screen column on every row. Three properties matter enough to be
pinned here:

* **The cells replace the prefix.** A row that showed both would be wider than
  the line it replaced, which is the opposite of the point.
* **The layout is planned once per render**, never per row and never on the
  tail-append path. That is the O(new) guarantee, and it is asserted
  structurally rather than with a clock.
* **Chips are an allowlist.** A journald line arrives carrying forty fields,
  and a rule of the form "show the leftovers" makes the pane unreadable.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

from clv.app import LogViewerApp
from clv.plugins.sources.journald import translate
from clv.services.parsing import parse_lines
from clv.storage import SessionState
from clv.widgets import columns as columns_module
from clv.widgets.columns import (
    MIN_MESSAGE,
    NOTHING_PARSED_NOTE,
    ColumnarLine,
    plan_columns,
    render_row,
)
from clv.widgets.log_view import LogView

SYSLOG = [
    "Aug  7 09:25:01 web01 sshd[1123]: Failed password for root from 10.0.0.5 port 22",
    "Aug  7 09:25:02 web01 kernel: TCP: dropping request, ratelimit exceeded",
    "Aug  7 09:25:03 web01 CRON[9021]: (root) CMD (/usr/bin/backup.sh)",
]

ACCESS = [
    '10.0.0.1 - - [07/Aug/2026:09:25:01 +0000] "GET /checkout HTTP/1.1" 404 512',
    '10.0.0.7 - bob [07/Aug/2026:09:25:03 +0000] "POST /pay HTTP/1.1" 500 0',
]


def _make_app(**state) -> LogViewerApp:
    """A real app with a mocked pane — the convention in test_log_rendering.py."""

    app = LogViewerApp()
    app.log_panel = MagicMock(spec=LogView)
    app.log_panel.cursor_entry = None
    app.log_panel.cursor = -1
    app.state = SessionState(auto_scroll=False, pretty_rendering=True, **state)
    return app


def _written(app) -> list:
    return [
        call.args[0]
        for call in app.log_panel.mock_calls
        if call[0] in ("write", "write_entry") and call.args
    ]


def _plan(lines: list[str], *, merged: bool = False, clustering: bool = False):
    entries = parse_lines(lines)
    return entries, plan_columns(entries, merged=merged, clustering=clustering)


def _row(line: str, **kwargs) -> ColumnarLine:
    entries, layout = _plan([line])
    return render_row(entries[0], layout, **kwargs)


# --- the cells replace the prefix -------------------------------------------

def test_the_columns_replace_the_parsed_prefix() -> None:
    """The timestamp and the tag are cells now, not text repeated beside them."""

    entries, layout = _plan(SYSLOG)
    row = render_row(entries[0], layout)

    plain = row.plain
    assert "09:25:01" in plain, "the time cell is filled"
    assert "sshd" in plain, "the source cell is filled"
    assert "Failed password for root from 10.0.0.5 port 22" in plain
    # The consumed prefix is gone from the row...
    assert "Aug  7 09:25:01 web01 sshd[1123]:" not in plain
    # ...and untouched on the entry, which is what the detail pane, `y` and an
    # export all read.
    assert entries[0].raw.startswith("Aug  7 09:25:01 web01 sshd[1123]:")


def test_an_access_log_row_keeps_its_status() -> None:
    """The correctness case, and the reason chips exist at all.

    `_parse_structured` sets an access-log entry's `message` to the request
    line alone, so a row built from the message would show `POST /pay HTTP/1.1`
    and drop the 500 the operator is hunting for.
    """

    entries, layout = _plan(ACCESS)
    rows = [render_row(entry, layout).plain for entry in entries]

    assert "GET /checkout HTTP/1.1" in rows[0]
    assert "status=404" in rows[0]
    assert "status=500" in rows[1]


def test_a_raw_line_is_shown_whole() -> None:
    """No format matched, so `message` is the line and nothing is lost."""

    row = _row("a line no format recognises at all")

    assert row.plain == "a line no format recognises at all"


# --- chips ------------------------------------------------------------------

def test_a_journald_row_does_not_render_forty_chips() -> None:
    """The allowlist, pinned against the source that motivated it."""

    record = {
        "__REALTIME_TIMESTAMP": "1786000000000000",
        "PRIORITY": "3",
        "MESSAGE": "unit entered failed state",
        "_SYSTEMD_UNIT": "nginx.service",
        "_HOSTNAME": "web01",
        "_PID": "991",
        "SYSLOG_IDENTIFIER": "nginx",
        "_BOOT_ID": "abc",
        "_MACHINE_ID": "def",
        "_CAP_EFFECTIVE": "0",
        "_SYSTEMD_CGROUP": "/system.slice/nginx.service",
        "_TRANSPORT": "stdout",
        "_UID": "0",
        "_COMM": "nginx",
        "_EXE": "/usr/sbin/nginx",
    }
    entries, layout = _plan([translate(record)])
    row = render_row(entries[0], layout)

    assert len(entries[0].fields) > 15, "the journal really does send this many"
    plain = row.plain
    assert "_SYSTEMD_CGROUP" not in plain
    assert "_MACHINE_ID" not in plain
    assert "_CAP_EFFECTIVE" not in plain
    assert "nginx.service" in plain, "the unit still reaches the source cell"


def test_a_constant_field_earns_no_chip_and_a_varying_one_does() -> None:
    """One rule replaces a per-format decision about `host`."""

    same = [
        json.dumps({"level": "info", "message": "a", "logger": "api", "host": "web01"}),
        json.dumps({"level": "info", "message": "b", "logger": "web", "host": "web01"}),
    ]
    differs = [
        json.dumps({"level": "info", "message": "a", "logger": "api", "host": "web01"}),
        json.dumps({"level": "info", "message": "b", "logger": "web", "host": "web02"}),
    ]

    entries, layout = _plan(same)
    assert "host" not in layout.varying
    assert "host=web01" not in render_row(entries[0], layout).plain

    entries, layout = _plan(differs)
    assert "host" in layout.varying
    assert "host=web01" in render_row(entries[0], layout).plain


def test_a_pid_is_never_a_chip() -> None:
    """It rides in the source cell when it fits, and never becomes a field."""

    entries, layout = _plan(SYSLOG)
    plain = render_row(entries[0], layout).plain

    assert "pid=" not in plain
    assert "sshd[1123]" in plain


# --- what the whole set decides ---------------------------------------------

def test_the_source_column_is_dropped_when_every_line_agrees() -> None:
    """A column of one repeated value is width taken from the message."""

    _, layout = _plan(
        [
            "Aug  7 09:25:01 web01 sshd[1]: one",
            "Aug  7 09:25:02 web01 sshd[2]: two",
        ]
    )
    assert layout.show_source is False


def test_the_source_column_appears_when_they_differ() -> None:
    _, layout = _plan(SYSLOG)
    assert layout.show_source is True


def test_the_date_appears_only_when_the_set_spans_days() -> None:
    _, one_day = _plan(
        ["2026-08-07 09:25:01 - INFO - a", "2026-08-07 23:59:59 - INFO - b"]
    )
    _, two_days = _plan(
        ["2026-08-07 09:25:01 - INFO - a", "2026-08-08 00:00:01 - INFO - b"]
    )

    assert one_day.dated is False
    assert two_days.dated is True
    assert "08-08" in render_row(parse_lines(
        ["2026-08-08 00:00:01 - INFO - b"])[0], two_days).plain


def test_a_second_resolution_set_shows_no_empty_fraction() -> None:
    """A column of `.000` is four cells of nothing."""

    _, syslog = _plan(SYSLOG)
    _, millis = _plan(["2026-08-07 09:25:01.123 - INFO - a"])

    assert syslog.subsecond is False
    assert millis.subsecond is True


def test_the_level_column_is_dropped_when_nothing_declares_one() -> None:
    _, layout = _plan(["Aug  7 09:25:01 web01 sshd[1]: no level token here"])
    assert layout.show_level is False


# --- continuations ----------------------------------------------------------

def test_a_continuation_hangs_off_its_parent() -> None:
    """An inherited stamp is not the continuation's own to state.

    Forty traceback frames each stamped with the parent's time would read as
    forty events at the same instant.
    """

    entries, layout = _plan(
        [
            "2026-08-07 09:25:06 - ERROR - order processing failed",
            "Traceback (most recent call last):",
            '  File "/srv/app/orders.py", line 91, in process',
        ]
    )
    parent, frame = render_row(entries[0], layout), render_row(entries[1], layout)

    assert entries[1].continuation and entries[1].timestamp is not None
    assert "09:25:06" in parent.plain
    assert "09:25:06" not in frame.plain, "the inherited stamp was restated"
    assert "ERROR" not in frame.plain
    assert frame.plain == "Traceback (most recent call last):"


# --- merged views -----------------------------------------------------------

def test_a_merged_row_shows_the_member_and_demotes_the_tag() -> None:
    """One source column, not two — there is not width for both."""

    entries, layout = _plan(SYSLOG, merged=True)
    row = render_row(entries[0], layout, source_label="web01-syslog.log")

    assert layout.merged and layout.show_source
    plain = row.plain
    assert "web01-syslog.log" in plain, "the merged label takes the cell"
    assert "tag=sshd" in plain, "the format's own source moves to a chip"


# --- the empty statement ----------------------------------------------------

def test_a_raw_only_view_says_so() -> None:
    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(["one", "two", "three"]))

    app._render_log()

    assert NOTHING_PARSED_NOTE in [getattr(item, "plain", "") for item in _written(app)]


def test_a_parsed_view_says_nothing() -> None:
    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(SYSLOG))

    app._render_log()

    assert NOTHING_PARSED_NOTE not in [
        getattr(item, "plain", "") for item in _written(app)
    ]


def test_the_note_is_not_shown_with_the_switch_off() -> None:
    app = _make_app()
    app.state = SessionState(auto_scroll=False, pretty_rendering=False)
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(["one", "two"]))

    app._render_log()

    assert NOTHING_PARSED_NOTE not in [
        getattr(item, "plain", "") for item in _written(app)
    ]


# --- the O(new) guarantee ---------------------------------------------------

def test_the_columns_are_planned_once_per_render_not_once_per_row(monkeypatch) -> None:
    calls = []
    real = columns_module.plan_columns

    def counted(entries, **kwargs):
        calls.append(len(entries))
        return real(entries, **kwargs)

    monkeypatch.setattr("clv.app.plan_columns", counted)

    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines([f"Aug  7 09:25:01 web01 app[{i}]: line {i}"
                                      for i in range(200)]))
    app._render_log()

    assert calls == [200], "planned once, over the window the pane draws"


def test_appending_reuses_the_planned_layout(monkeypatch) -> None:
    """The tail path must cost what arrived, not what is on screen."""

    app = _make_app()
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(SYSLOG))
    app._render_log()
    before = app._column_layout

    calls = []
    monkeypatch.setattr("clv.app.plan_columns", lambda *a, **k: calls.append(1))

    app.log_panel.reset_mock()
    app._append_entries(parse_lines(["Aug  7 09:25:09 web02 sshd[9]: a new line"]))

    assert calls == [], "the append path re-planned the columns"
    assert app._column_layout is before, "the layout object was replaced"
    assert len(_written(app)) == 1
    app.log_panel.clear.assert_not_called()


# --- geometry ---------------------------------------------------------------

def _rendered(pane) -> list:
    return [pane.render_line(y) for y in range(pane.size.height)]


def test_a_wide_row_still_fits_eighty_columns(tmp_path: Path) -> None:
    """80 columns is a supported width, and nothing may sit off-screen in it."""

    async def scenario() -> None:
        path = tmp_path / "syslog.log"
        path.write_text("\n".join(SYSLOG * 4) + "\n", encoding="utf-8")
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            await pilot.pause()
            app._set_structured(True)
            await pilot.pause()

            pane = app.log_panel
            for strip in _rendered(pane):
                assert strip.cell_length <= pane.size.width, "a row ran off the pane"

    asyncio.run(scenario())


def test_wrapped_lines_align_to_the_message_column(tmp_path: Path) -> None:
    """The hanging indent is the whole reason a row is not a plain `Text`."""

    long_line = (
        "Aug  7 09:25:04 web01 sshd[1130]: Accepted publickey for deploy from "
        "10.0.0.9 port 41022 ssh2: RSA SHA256:8f3a91c2b4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"
    )

    async def scenario() -> None:
        path = tmp_path / "syslog.log"
        path.write_text(long_line + "\n", encoding="utf-8")
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            await pilot.pause()
            app._set_structured(True)
            await pilot.pause()

            strips = app.log_panel.rows[0].strips
            assert len(strips) > 1, "the line was expected to wrap"

            def leading_blanks(strip) -> int:
                text = "".join(segment.text for segment in strip)
                return len(text) - len(text.lstrip(" "))

            indent = leading_blanks(strips[1])
            assert indent >= 10, "a wrapped line restarted at column zero"
            assert all(leading_blanks(strip) == indent for strip in strips[1:])

    asyncio.run(scenario())


def test_the_message_cell_never_starves() -> None:
    """Cells give way in order rather than squeezing the message to nothing."""

    entries, layout = _plan(SYSLOG, merged=True, clustering=True)
    row = render_row(entries[0], layout, source_label="a-very-long-source-name.log")

    from rich.console import Console

    for width in (200, 160, 130, 100, 80, 60, 40):
        console = Console(width=width, no_color=True)
        with console.capture() as capture:
            console.print(row)
        for line in capture.get().rstrip("\n").split("\n"):
            assert len(line) <= width, f"overflowed at width {width}"


def test_a_pane_too_narrow_for_cells_still_shows_the_message() -> None:
    from rich.console import Console

    row = _row(SYSLOG[0])
    console = Console(width=15, no_color=True)
    with console.capture() as capture:
        console.print(row)

    assert "Failed password" in capture.get()


def test_the_cells_give_way_in_order_as_the_pane_narrows() -> None:
    """Source, then the span, then the date, then the level. Never the message.

    Asserted against `_resolve` directly because this is the contract the
    rendering depends on, and reading it back out of painted characters would
    test the paint instead.
    """

    _, layout = _plan(SYSLOG, merged=True, clustering=True)
    resolve = columns_module._resolve

    seen = []
    for width in (200, 160, 130, 110, 90, 70, 55, 45):
        cells = resolve(layout, width)
        room = width - (cells.marker + cells.time + cells.level + cells.source)
        seen.append((width, cells.source, cells.level, room))
        assert room >= MIN_MESSAGE, f"the message cell starved at width {width}"

    # Monotonic: a narrower pane never gives a cell back.
    sources = [source for _, source, _, _ in seen]
    levels = [level for _, _, level, _ in seen]
    assert sources == sorted(sources, reverse=True)
    assert levels == sorted(levels, reverse=True)
    assert sources[-1] == 0, "the source cell never gave way"
