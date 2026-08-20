"""The log pane's structured row: fixed cells, then the message.

What the switch is for
----------------------

The parser already recovers a normalised timestamp, a canonical severity and a
bag of named fields from every line, and until this module existed all of it was
thrown away at render time — the pane drew ``entry.raw`` with one severity span
over the whole thing. A dense syslog file was exactly as hard to read with the
structured switch on as with it off.

So a structured row puts that recovered structure in *fixed cells*::

    09:25:01 ERROR sshd     Failed password for root from 10.0.0.5
    09:25:01 INFO  CRON     (root) CMD (/usr/bin/backup.sh)
    09:25:02 INFO  sshd     Accepted publickey for deploy from 10.
                            0.0.9 port 41022 ssh2: RSA SHA256:8f3a
    09:25:04 ERROR nginx    POST /pay HTTP/1.1  status=500

The message starts at the same screen column on every row, which is the whole
point: an operator scans *down* a column instead of reading *across* a line.

The cells replace the prefix, they do not precede it
----------------------------------------------------

The message cell holds ``entry.message`` — the tail the parser did not consume —
not ``entry.raw``. Repeating the timestamp and the tag as text beside the cells
that already state them is how a structured view ends up *wider* than the line
it replaced. Nothing is lost: ``entry.raw`` is untouched on the entry, and it is
what the detail pane shows, what ``y`` copies, and what an export writes.

Widths are fixed, and that is a hard requirement
------------------------------------------------

``_BANDS`` keys the cell widths off the pane's render width alone — never off
the content. ``render_row`` is called from the tail-append path, and a width
that depended on the arriving line would force ``LogView._relayout()``, which is
O(rows), on every line that arrives. ``MERGED_COLUMN_WIDTHS`` in ``clv/app.py``
is the same rule for the same reason.

Anything that *does* need to look at the whole set — is there a level to show,
does the set span more than one day, does a field vary enough to earn a chip —
is decided once by :func:`plan_columns` and frozen into a :class:`ColumnLayout`.
The append path reads that layout and never recomputes it.

Where this lives
----------------

Beside ``severity.py``, and for its reason: this builds a renderable, and a
renderable is not a layout decision. CSS decides geometry and never sees a
line's interior. Nothing here imports Textual, and ``clv/services`` stays free
of Rich.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, NamedTuple, Optional, Sequence

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.text import Text

from ..services.parsing import LogEntry
from .severity import SEVERITY_COLORS

#: Narrowest message cell worth having. Below this the columns are taking room
#: from the thing they exist to make readable, and one of them gives way.
MIN_MESSAGE = 28

#: Below this the pane cannot carry cells at all and a row is its bare message.
MIN_USABLE = 20

#: Longest chip value before it is cut. A trace id is worth showing; a stack
#: trace pasted into a field is not.
MAX_CHIP_VALUE = 24

#: Canonical level -> (short form, long form). The short form is used where the
#: level cell is 4 wide, the long where it is 6. Every long form is <= 5 cells,
#: so ``CRITICAL`` and ``NOTICE`` are the two that have to give.
LEVEL_ABBREV: dict[str, tuple[str, str]] = {
    "CRITICAL": ("CRT", "CRIT"),
    "ERROR": ("ERR", "ERROR"),
    "WARN": ("WRN", "WARN"),
    "NOTICE": ("NOT", "NOTIC"),
    "INFO": ("INF", "INFO"),
    "DEBUG": ("DBG", "DEBUG"),
    "TRACE": ("TRC", "TRACE"),
}

#: Never a chip, whatever a profile says. Both are provenance keys the session
#: layer stamps on (``ORIGIN_FIELD``/``NODE_FIELD``), and answering "which
#: source" is the merged column's job, not a chip's.
NEVER_A_CHIP = frozenset({"source", "node"})

NOTHING_PARSED_NOTE = (
    "Nothing in view matched a known format, so the columns have nothing to "
    "fill. Every line is shown whole — the raw text is unchanged in the detail "
    "pane, in what y copies and in an export."
)

_TIME_STYLE = "#94a3b8"
_SOURCE_STYLE = "#7aa3d1"
_CHIP_STYLE = "dim #94a3b8"
_MARKER_STYLE = "bold #7aa3d1"


@dataclass(frozen=True, slots=True)
class FormatProfile:
    """Which of a format's fields are worth a cell, and which a chip."""

    #: Candidates for the source cell, best answer first.
    source_keys: tuple[str, ...] = ()
    #: Appended to the source cell as ``tag[pid]`` when there is room for it.
    pid_key: str = ""
    #: Always shown when present. For a chip to be pinned it has to carry
    #: something the message cell cannot.
    pinned_chips: tuple[str, ...] = ()
    #: Shown only when the value actually varies across the rendered set.
    chips: tuple[str, ...] = ()


DEFAULT_PROFILE = FormatProfile()

#: Keyed on ``LogEntry.format_name``, the same key ``detail_pane.FORMAT_LABELS``
#: uses — a new format needs an entry in both.
FORMAT_PROFILES: dict[str, FormatProfile] = {
    # Which program spoke is the scanning axis for a syslog file, so `tag` takes
    # the cell. `host` is one value in a single-file view and earns nothing;
    # across a fleet merge it varies, and the `varying` rule turns it on with no
    # special case here.
    "syslog": FormatProfile(source_keys=("tag",), pid_key="pid", chips=("host",)),
    # RFC 5424's APP-NAME is filed under `tag` by the parser precisely so one
    # rule reaches both dialects. `msgid` is usually NILVALUE and already
    # dropped by the parser, so it only appears where a writer set it.
    "syslog-5424": FormatProfile(
        source_keys=("tag",), pid_key="pid", chips=("host", "msgid")
    ),
    # **The correctness case.** `_parse_structured` sets `message` to the
    # request line alone, so an access-log row that showed only its message
    # would drop the 500 an operator is hunting for. `status` is pinned for
    # that reason and no other. The cell holds the *client*, which is the
    # question an access log is usually asked.
    "access-log": FormatProfile(
        source_keys=("host",), pinned_chips=("status",), chips=("user", "size")
    ),
    # **An allowlist, never a sweep.** The journald source copies every journal
    # key into its payload, so a journal line arrives here carrying
    # `_SYSTEMD_CGROUP`, `_MACHINE_ID`, `_CAP_EFFECTIVE` and thirty more. Any
    # rule of the form "show the leftover fields" renders an unreadable row.
    # `unit` sorts before `tag` because `_SYSTEMD_UNIT` is the more specific
    # answer and `SYSLOG_IDENTIFIER` is the unit-less fallback.
    "json": FormatProfile(
        source_keys=("unit", "logger", "service", "component", "tag", "name"),
        pid_key="pid",
        chips=(
            "host",
            "status",
            "code",
            "error",
            "exception",
            "method",
            "path",
            "duration_ms",
            "latency_ms",
            "request_id",
            "trace_id",
        ),
    ),
    # python-logging, iso-level, iso and raw recover no fields at all -- the
    # detail pane says as much in NO_FIELD_REASONS -- so they take
    # DEFAULT_PROFILE and contribute no source value. A file of nothing but
    # these makes `plan_columns` drop the source cell on its own.
}

#: Keys `_parse_json` already consumed into timestamp/level/message. Showing one
#: as a chip would repeat the cell beside it.
_JSON_CONSUMED = frozenset(
    {
        "timestamp", "@timestamp", "time", "ts", "asctime", "eventTime", "date",
        "level", "levelname", "severity", "lvl", "loglevel", "log_level", "priority",
        "message", "msg", "event", "text", "log",
    }
)


class _Widths(NamedTuple):
    """What one pane-width band can afford.

    The time cell is not a width here but a set of permissions, because its size
    depends on what the *set* turned out to carry — see :func:`_resolve`. The
    rest are widths, each including its trailing space.
    """

    date: bool  # room for a `MM-DD ` prefix at all
    subsecond: bool  # room for `.mmm`
    span: int  # room to state a cluster's whole span; 0 = show the first stamp
    level: int
    source: int
    chips: int


#: Fixed, never measured from content -- see the module docstring.
#:
#: Keyed on the **pane's** width, not the terminal's: with the source tree open
#: at its default 38, a 110-column terminal leaves the pane 68, and a band table
#: that read the terminal would hand this pane the layout of a much wider one.
#:
#: No band withholds a cell it has room for. Starvation is the runtime clamp's
#: job in `_resolve`, and giving that job to the table as well is how a 68-wide
#: pane ends up with no source column and 46 columns of unused message room.
_BANDS: tuple[tuple[int, _Widths], ...] = (
    (80, _Widths(date=True, subsecond=False, span=0, level=4, source=9, chips=1)),
    (110, _Widths(date=True, subsecond=False, span=0, level=6, source=13, chips=2)),
    (1 << 30, _Widths(date=True, subsecond=True, span=18, level=6, source=19, chips=3)),
)

#: `HH:MM:SS`, `.mmm`, `MM-DD `.
_CLOCK_WIDTH = 8
_SUBSECOND_WIDTH = 4
_DATE_WIDTH = 6


@dataclass(frozen=True, slots=True)
class ColumnLayout:
    """What the whole rendered set needs, decided once and then frozen.

    Everything here is a property of the *set*, not of a line, which is why the
    append path reuses it untouched: a tailed line that introduces a new host,
    or that crosses midnight, does not move the columns under an operator who is
    reading them. It is folded in at the next full render.
    """

    stamped: bool = False
    dated: bool = False
    #: True when any stamp in the set carries a fraction of a second. Syslog
    #: resolves to the second, and a column of `.000` is four cells of nothing.
    subsecond: bool = False
    #: A cluster row states its span in the time cell, and a span is twice the
    #: width of a stamp. Reserved for the whole set rather than per row, because
    #: a time cell that changed width between neighbours is not a column.
    spanned: bool = False
    show_level: bool = False
    show_source: bool = False
    merged: bool = False
    cluster_width: int = 0
    varying: frozenset[str] = frozenset()
    all_raw: bool = False


EMPTY_LAYOUT = ColumnLayout()


class _Cells(NamedTuple):
    marker: int
    time: int
    level: int
    source: int
    chips: int
    time_format: str
    subsecond: bool


def _band_for(width: int) -> _Widths:
    for limit, widths in _BANDS:
        if width <= limit:
            return widths
    return _BANDS[-1][1]  # pragma: no cover - the last band is unbounded


def _resolve(layout: ColumnLayout, width: int) -> _Cells:
    """Cell widths for this pane width, after the give-way order.

    In order: the source cell, then a cluster's span, then the date, then the
    fraction of a second, then the level. Cheapest answer first — dropping the
    span still leaves a stamp, and dropping the date still leaves a clock — so
    each step costs the reader less than the one after it.

    The message cell is what the reader came for, so it is never what gives
    way, and it stays at or above :data:`MIN_MESSAGE` for as long as any other
    cell can still be dropped.
    """

    band = _band_for(width)
    marker = layout.cluster_width
    dated = band.date and layout.dated
    subsecond = band.subsecond and layout.subsecond
    spanned = bool(band.span) and layout.spanned
    level = band.level if layout.show_level else 0
    source = band.source if layout.show_source else 0

    def clock() -> int:
        if not layout.stamped:
            return 0
        stamp = _CLOCK_WIDTH + (_SUBSECOND_WIDTH if subsecond else 0)
        stamp += _DATE_WIDTH if dated else 0
        return max(stamp + 1, band.span if spanned else 0)

    time = clock()
    while width - (marker + time + level + source) < MIN_MESSAGE:
        if source:
            source = 0
        elif spanned:
            spanned = False
        elif dated:
            dated = False
        elif subsecond:
            subsecond = False
        elif level:
            level = 0
        else:
            break
        time = clock()

    time_format = ""
    if time:
        time_format = ("%m-%d " if dated else "") + "%H:%M:%S"
        if subsecond:
            time_format += ".%f"
    return _Cells(marker, time, level, source, band.chips, time_format, subsecond)


def _cell(value: str, width: int, style: str = "", *, keep: str = "head") -> Text:
    """One fixed-width cell, always padded, truncated at the end that matters.

    ``keep="tail"`` drops the front, which is what a merged view's source label
    wants: rotated members (`app.log`, `app.log.2.gz`) differ at the end, so the
    end is the part that identifies them. A program name is the opposite —
    `systemd-journald` and `systemd-logind` are told apart by their front — so
    everything else keeps the head.
    """

    if width <= 0:
        return Text("")
    room = width - 1
    if len(value) > room:
        if room <= 1:
            value = "…"
        elif keep == "tail":
            value = "…" + value[-(room - 1):]
        else:
            value = value[: room - 1] + "…"
    return Text(f"{value:<{room}} ", style=style)


def _source_pair(
    fields: Mapping[str, str], profile: FormatProfile
) -> tuple[str, str]:
    for key in profile.source_keys:
        value = fields.get(key)
        if value:
            return key, value
    return "", ""


def _chips_for(
    entry: LogEntry, layout: ColumnLayout, profile: FormatProfile
) -> tuple[tuple[str, str], ...]:
    fields = entry.fields
    if not fields:
        return ()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def offer(key: str, value: str) -> None:
        if not value or key in seen or key in NEVER_A_CHIP:
            return
        seen.add(key)
        if len(value) > MAX_CHIP_VALUE:
            value = value[: MAX_CHIP_VALUE - 1] + "…"
        out.append((key, value))

    if layout.merged:
        # The merged cell holds which *source* a line came from, so the format's
        # own source is demoted rather than dropped -- it moves to the end of
        # the line, where a per-row detail belongs.
        offer(*_source_pair(fields, profile))
    else:
        seen.update(profile.source_keys)
    if entry.format_name == "json":
        seen.update(_JSON_CONSUMED)
    if profile.pid_key:
        seen.add(profile.pid_key)

    for key in profile.pinned_chips:
        offer(key, fields.get(key, ""))
    for key in profile.chips:
        if key in layout.varying:
            offer(key, fields.get(key, ""))
    return tuple(out)


class ColumnarLine:
    """One structured row. Wraps under its own message cell.

    The wrap happens in ``__rich_console__`` rather than at construction, and
    that is deliberate: ``LogView._relayout()`` re-renders a row from the stored
    renderable when the pane changes width and never asks the app to rebuild it,
    so a row with newlines baked in at yesterday's width would show stale
    wrapping after every resize.
    """

    __slots__ = (
        "_layout", "_marker", "_timestamp", "_time_override", "_level",
        "_source", "_pid", "_message", "_chips", "_style", "_continuation",
    )

    def __init__(
        self,
        *,
        layout: ColumnLayout,
        marker: str,
        timestamp: Optional[datetime],
        time_override: str,
        level: Optional[str],
        source: str,
        pid: str,
        message: str,
        chips: tuple[tuple[str, str], ...],
        style: str,
        continuation: bool,
    ) -> None:
        self._layout = layout
        self._marker = marker
        self._timestamp = timestamp
        self._time_override = time_override
        self._level = level
        self._source = source
        self._pid = pid
        self._message = message
        self._chips = chips
        self._style = style
        self._continuation = continuation

    # --- content ------------------------------------------------------------

    def _time_value(self, cells: _Cells) -> str:
        if self._continuation:
            # An inherited stamp rendered as its own would make a forty-frame
            # traceback read as forty events at the same instant.
            return ""
        if self._time_override:
            # A cluster's span is the better answer when it fits — "147 of
            # these, over four seconds" is a different event from "147, over an
            # hour". When it does not fit, the first stamp is shown instead: a
            # truncated span is not a smaller true answer, it is a wrong one.
            if len(self._time_override) <= cells.time - 1 or self._timestamp is None:
                return self._time_override
        if self._timestamp is None or not cells.time_format:
            return ""
        stamp = self._timestamp.strftime(cells.time_format)
        return stamp[:-3] if cells.subsecond else stamp

    def _level_value(self, cells: _Cells) -> str:
        if self._continuation or not self._level:
            return ""
        short, long = LEVEL_ABBREV.get(self._level, (self._level[:3], self._level[:5]))
        return short if cells.level <= 4 else long

    def _prefix(self, cells: _Cells) -> Text:
        prefix = Text()
        if cells.marker:
            prefix.append_text(_cell(self._marker, cells.marker, _MARKER_STYLE))
        if cells.time:
            prefix.append_text(_cell(self._time_value(cells), cells.time, _TIME_STYLE))
        if cells.level:
            value = self._level_value(cells)
            style = SEVERITY_COLORS.get(self._level or "", "") if value else ""
            prefix.append_text(_cell(value, cells.level, f"bold {style}" if style else ""))
        if cells.source:
            prefix.append_text(
                _cell(
                    self._source_value(cells.source),
                    cells.source,
                    _SOURCE_STYLE,
                    keep="tail" if self._layout.merged else "head",
                )
            )
        return prefix

    def _source_value(self, width: int) -> str:
        """The source cell, with the PID only when this width can hold both.

        A PID is never a chip and never a cell of its own: it identifies a
        process, and the scanning axis is which program. So it rides along here
        when there is room and lives in the detail pane when there is not —
        decided against the *rendered* width, because deciding it earlier is how
        `sshd[1123]` ends up truncated to `…d[1123]` in a nine-wide cell.
        """

        if self._continuation:
            return ""
        if self._pid:
            combined = f"{self._source}[{self._pid}]"
            if len(combined) <= width - 1:
                return combined
        return self._source

    def _body(self, chip_limit: int) -> Text:
        style = self._style
        if self._continuation and style:
            style = f"dim {style}"
        elif self._continuation:
            style = "dim"
        body = Text(self._message, style=style, no_wrap=False)
        for key, value in self._chips[:chip_limit]:
            body.append("  ")
            body.append(f"{key}={value}", style=_CHIP_STYLE)
        return body

    @property
    def plain(self) -> str:
        """Every token on the row, single-spaced.

        Not the rendered geometry — the padding depends on a width this does not
        know. It exists so a test can ask *what is on this line* the same way it
        could when a row was a plain ``Text``.
        """

        cells = _resolve(self._layout, 200)
        parts = [
            self._marker,
            self._time_value(cells),
            self._level_value(cells),
            self._source_value(200),
            self._body(len(self._chips)).plain,
        ]
        return " ".join(part for part in parts if part)

    # --- rendering ----------------------------------------------------------

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(1, options.max_width)
        if width < MIN_USABLE:
            yield self._body(0)
            return

        cells = _resolve(self._layout, width)
        prefix = self._prefix(cells)
        pad = prefix.cell_len
        if self._continuation:
            # Two further columns, so a stack trace visibly hangs off the entry
            # it belongs to rather than starting a fresh-looking one.
            prefix.append("  ")
            pad += 2

        body = self._body(cells.chips)
        lines = body.wrap(console, max(8, width - pad), overflow="fold")

        # Segments rather than a `Text` per line. The prefix is rendered once
        # and replayed, which is worth doing here because this runs for every
        # row of every full render: assembling a fresh `Text` per screen line
        # made Rich render two objects where one would do, and a 500-row redraw
        # is the common case, not the worst one.
        head = list(prefix.render(console))
        blank = [Segment(" " * pad)] if pad else []
        newline = Segment.line()
        for index, line in enumerate(lines or [Text()]):
            yield from (head if index == 0 else blank)
            yield from line.render(console)
            yield newline


def plan_columns(
    entries: Sequence[LogEntry], *, merged: bool, clustering: bool
) -> ColumnLayout:
    """Decide the shape of the columns for one rendered set.

    Called once per full render, over the same window the pane is about to draw,
    and then frozen — see :class:`ColumnLayout`. One pass, and every distinct-
    value count stops at two, so this is O(entries) with a small constant.
    """

    stamped = False
    dated = False
    subsecond = False
    show_level = False
    all_raw = bool(entries)
    first_date = None
    sources: set[str] = set()
    distinct: dict[str, set[str]] = {}

    for entry in entries:
        if entry.format_name != "raw":
            all_raw = False
        stamp = entry.timestamp
        if stamp is not None:
            stamped = True
            if stamp.microsecond:
                subsecond = True
            if not dated:
                day = stamp.date()
                if first_date is None:
                    first_date = day
                elif day != first_date:
                    dated = True
        if entry.level:
            show_level = True

        profile = FORMAT_PROFILES.get(entry.format_name, DEFAULT_PROFILE)
        if not entry.fields:
            continue
        if len(sources) < 2:
            _, value = _source_pair(entry.fields, profile)
            if value:
                sources.add(value)
        for key in profile.chips:
            value = entry.fields.get(key)
            if not value:
                continue
            seen = distinct.setdefault(key, set())
            if len(seen) < 2:
                seen.add(value)

    return ColumnLayout(
        stamped=stamped,
        dated=dated,
        subsecond=subsecond,
        show_level=show_level,
        show_source=merged or len(sources) >= 2,
        merged=merged,
        cluster_width=8 if clustering else 0,
        spanned=clustering,
        varying=frozenset(key for key, seen in distinct.items() if len(seen) >= 2),
        all_raw=all_raw,
    )


def render_row(
    entry: LogEntry,
    layout: ColumnLayout,
    *,
    source_label: str = "",
    marker: str = "",
    time_override: str = "",
) -> ColumnarLine:
    """Build the structured row for *entry* under *layout*.

    *source_label* is the merged view's answer to "which source" and wins the
    source cell when there is one. *marker* and *time_override* are how a
    cluster row states its count and its span.
    """

    profile = FORMAT_PROFILES.get(entry.format_name, DEFAULT_PROFILE)
    pid = ""
    if layout.merged:
        source = source_label
    else:
        _, source = _source_pair(entry.fields, profile)
        if profile.pid_key:
            pid = entry.fields.get(profile.pid_key, "")
    return ColumnarLine(
        layout=layout,
        marker=marker,
        timestamp=entry.timestamp,
        time_override=time_override,
        level=entry.level,
        source=source,
        pid=pid,
        # `message` and not `raw`: the cells replace the prefix. A raw line has
        # message == raw, so it costs nothing there.
        message=entry.message or entry.raw,
        chips=_chips_for(entry, layout, profile),
        style=SEVERITY_COLORS.get(entry.level or "", ""),
        continuation=entry.continuation,
    )


__all__ = [
    "ColumnLayout",
    "ColumnarLine",
    "DEFAULT_PROFILE",
    "EMPTY_LAYOUT",
    "FORMAT_PROFILES",
    "FormatProfile",
    "LEVEL_ABBREV",
    "MIN_MESSAGE",
    "NEVER_A_CHIP",
    "NOTHING_PARSED_NOTE",
    "plan_columns",
    "render_row",
]
