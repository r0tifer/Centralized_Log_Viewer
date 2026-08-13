"""The log pane, with a keyboard cursor over parsed entries.

Replaces the `RichLog` the viewer used to render into. `RichLog` is a write-only
stream of renderables: there is no mapping from a screen line back to the
`LogEntry` that produced it, and no way to restyle a line once written. That is
fine for a tail and useless for everything an operator wants to *do* with a
line — inspect it, mark it, copy it, jump to it.

The technique is the same one `RichLog` uses, and the reason is the same: render
each row to `Strip`s once, then serve them from :meth:`render_line`. What is
added is a row model that survives rendering.

Cheap append is the whole point
-------------------------------

Tailing calls :meth:`write_entry` for the handful of lines that just arrived, so
appending must cost what arrived and not what is buffered. A row's strips are
built once and its lines are pushed onto a flat ``line -> row`` map, both O(new).
The one O(total) operation is :meth:`_relayout`, which runs on a width change
and when the row cap is exceeded — and the cap is enforced by dropping a *batch*
of rows rather than one per append, so the rebuild is amortised across
``max_rows // 10`` writes rather than paid every time.

Rows are entry-indexed, never line-indexed. One entry can occupy several screen
lines: raw lines wrap, and in structured mode an entry renders as a whole
bordered panel. A cursor counting screen rows would land in the middle of one
event and call it another.

Three kinds of row
------------------

`write_entry` adds a row backed by a `LogEntry`; the cursor can land on it.
`write` adds a bare renderable — the discovery summary, "No log entries", an
invalid-query message, the `describe_empty_result` explanation — which is
skipped by the cursor because there is nothing there to inspect.

`write_cluster` adds a row backed by a *group* of entries (Item 15). It carries
its cluster **and** that cluster's first entry, so it is selectable and every
consumer that reads `row.entry` — the detail pane, marks, watch highlights,
`n`/`N` — keeps working without learning what a cluster is. `Enter` is the one
thing that differs: on a cluster row it posts `ClusterToggled` rather than
`EntrySelected`, because expanding the group is what an operator means there.

The gutter
----------

Every row reserves two leading cells. They are blank until a line is marked
(bookmarks), and reserving them from the start means a mark cannot change a
row's width and reflow the pane underneath the operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from rich.console import RenderableType
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.cache import LRUCache
from textual.geometry import Region, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from ..services.parsing import LogEntry

#: Cells reserved to the left of every row for the mark indicator.
GUTTER_WIDTH = 2

#: Marked-line indicator. A glyph rather than a colour alone, so the mark is
#: legible on a monochrome terminal and to an operator who cannot distinguish
#: the accent colour.
MARK_GLYPH = "●"


@dataclass
class _Row:
    """One renderable and, when it came from a log line, the entry behind it."""

    renderable: RenderableType
    entry: Optional[LogEntry] = None
    #: The group this row stands for, when it is a collapsed or expanded
    #: cluster. Held opaquely: this widget stores it and hands it back on
    #: `Enter`, and knows nothing about how one is formed.
    cluster: Optional[object] = None
    marked: bool = False
    #: Matched an enabled watch rule. Styling only — a watched row is an
    #: ordinary row that is easier to find.
    watched: bool = False
    strips: list[Strip] = field(default_factory=list)
    #: First line of this row within the flat line map. Recomputed by _relayout.
    start_line: int = 0

    @property
    def selectable(self) -> bool:
        return self.entry is not None


class LogView(ScrollView, can_focus=True):
    """Scrollable log output with a selectable line cursor."""

    COMPONENT_CLASSES = {
        "log-view--cursor",
        "log-view--mark",
        "log-view--watch",
    }

    DEFAULT_CSS = """
    LogView {
        background: $surface;
        color: $foreground;
        overflow-y: scroll;
        scrollbar-gutter: stable;
    }

    LogView > .log-view--cursor {
        background: #3d4f6a;
    }

    LogView:focus > .log-view--cursor {
        background: #4a6288;
    }

    LogView > .log-view--mark {
        color: #facc15;
        text-style: bold;
    }

    /* A watched line has to be distinguishable from a *severe* one, and
       severity is carried in the foreground colour — so this is a background,
       plus bold for a terminal showing no colour at all. Deliberately a
       different hue from the cursor, which is a position rather than a
       property of the line. */
    LogView > .log-view--watch {
        background: #4c1d95;
        text-style: bold;
    }
    """

    # Widget-scoped rather than app-scoped: these only make sense while the log
    # pane has focus, and binding arrows on the app would fight the source tree
    # and every text input. `h`/`j`/`k`/`l` are deliberately left alone so
    # vim-style pane navigation stays available later.
    BINDINGS = [
        Binding("up", "cursor_up", "Previous line", show=False),
        Binding("down", "cursor_down", "Next line", show=False),
        Binding("pageup", "cursor_page_up", "Page up", show=False),
        Binding("pagedown", "cursor_page_down", "Page down", show=False),
        Binding("home", "cursor_home", "First line", show=False),
        Binding("end", "cursor_end", "Last line, resume follow", show=False),
        Binding("enter", "select_cursor", "Open detail for the line", show=False),
    ]

    class CursorMoved(Message):
        """The cursor landed on a different row."""

        def __init__(self, log_view: "LogView", index: int, entry: LogEntry | None, at_end: bool) -> None:
            super().__init__()
            self.log_view = log_view
            self.index = index
            self.entry = entry
            #: True when the cursor is on the last selectable row, which is the
            #: only position from which following new lines is not a fight.
            self.at_end = at_end

        @property
        def control(self) -> "LogView":
            return self.log_view

    class EntrySelected(Message):
        """Enter (or a double click) on a row backed by an entry."""

        def __init__(self, log_view: "LogView", index: int, entry: LogEntry) -> None:
            super().__init__()
            self.log_view = log_view
            self.index = index
            self.entry = entry

        @property
        def control(self) -> "LogView":
            return self.log_view

    class ClusterToggled(Message):
        """Enter on a cluster row — expand it in place, or collapse it again.

        The widget does not do it itself: which clusters are open is app state
        that has to survive a re-render, and the rows are rebuilt from the
        filtered set rather than edited in the pane.
        """

        def __init__(self, log_view: "LogView", index: int, cluster: object) -> None:
            super().__init__()
            self.log_view = log_view
            self.index = index
            self.cluster = cluster

        @property
        def control(self) -> "LogView":
            return self.log_view

    def __init__(
        self,
        *,
        max_rows: int | None = None,
        wrap: bool = True,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.max_rows = max_rows
        self.wrap = wrap
        #: Follow new lines. Same attribute name RichLog exposed, so the app's
        #: single owner (`_set_auto_scroll`) is unchanged.
        self.auto_scroll = True
        self._rows: list[_Row] = []
        #: line index -> (row index, line within that row).
        self._line_map: list[tuple[int, int]] = []
        #: Width the strips were built for; 0 until the first resize.
        self._layout_width = 0
        self._widest = 0
        self._cursor = -1
        self._strip_cache: LRUCache[tuple[int, int, int, int, bool, bool, Style], Strip] = (
            LRUCache(1024)
        )

    # --- content ------------------------------------------------------------

    def write(self, renderable: RenderableType) -> "LogView":
        """Add a row the cursor will skip: a summary, or an empty-pane message."""

        return self._add_row(_Row(renderable))

    def write_entry(self, renderable: RenderableType, entry: LogEntry) -> "LogView":
        """Add a selectable row.

        The renderable comes first so it stays ``args[0]``: the app owns
        severity colouring and structured rendering, and this widget only stores
        what it is handed.
        """

        return self._add_row(_Row(renderable, entry))

    def write_cluster(
        self, renderable: RenderableType, cluster: object, entry: LogEntry
    ) -> "LogView":
        """Add a row standing for a group of entries.

        *entry* is the cluster's representative and is stored as the row's
        entry, so the detail pane, the mark gutter and `n`/`N` all keep working
        on a clustered pane without a branch of their own.
        """

        return self._add_row(_Row(renderable, entry, cluster))

    def update_row(
        self, index: int, renderable: RenderableType, cluster: object | None = None
    ) -> None:
        """Replace what a row draws, in place.

        For a cluster whose count rose on this poll: the row is already on
        screen and only its text changed. Re-strips that one row, and pays for
        a relayout only when the new renderable is a different *height* — which
        it is not, for a count going from ×9 to ×10.

        *cluster* is how a plain entry row becomes a cluster row when its second
        member arrives: the row was written before there was a group to attach.
        """

        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        row.renderable = renderable
        if cluster is not None:
            row.cluster = cluster
        if not self._layout_width:
            return
        previous = len(row.strips)
        row.strips = self._render_strips(row, index)
        self._strip_cache.clear()
        if len(row.strips) != previous:
            self._relayout()
            return
        self._refresh_row(index)

    def clear(self) -> "LogView":
        self._rows.clear()
        self._line_map.clear()
        self._strip_cache.clear()
        self._widest = 0
        self._cursor = -1
        self.virtual_size = Size(0, 0)
        self.refresh()
        return self

    def _add_row(self, row: _Row) -> "LogView":
        self._rows.append(row)
        if not self._layout_width:
            # Width is unknown until the first resize. The row is kept; the
            # strips are built for every row at once by _relayout.
            return self
        row.strips = self._render_strips(row, len(self._rows) - 1)
        self._index_row(len(self._rows) - 1, row)
        # _trim relayouts when it fires, which resizes and refreshes already.
        if not self._trim():
            self._sync_virtual_size()
        if self.auto_scroll:
            self.scroll_end(animate=False, immediate=False, x_axis=False)
        return self

    def _index_row(self, index: int, row: _Row) -> None:
        row.start_line = len(self._line_map)
        self._line_map.extend((index, offset) for offset in range(len(row.strips)))
        if row.strips:
            self._widest = max(self._widest, max(strip.cell_length for strip in row.strips))

    def _sync_virtual_size(self) -> None:
        self.virtual_size = Size(self._widest, len(self._line_map))

    def _trim(self) -> bool:
        """Drop rows past the cap, in batches. Returns True when it relayouted.

        Dropping one row per append would rebuild the line map on every tailed
        line, which is exactly the O(buffer)-per-poll cost this widget exists to
        avoid. Dropping a tenth of the cap at a time amortises the rebuild.
        """

        if self.max_rows is None or len(self._rows) <= self.max_rows:
            return False
        batch = max(1, self.max_rows // 10)
        drop = min(max(len(self._rows) - self.max_rows, batch), len(self._rows))
        del self._rows[:drop]
        if self._cursor >= 0:
            self._cursor = self._cursor - drop
            if self._cursor < 0:
                self._cursor = self._first_selectable()
        self._relayout()
        return True

    # --- layout -------------------------------------------------------------

    def on_resize(self, _event: events.Resize) -> None:
        width = self.scrollable_content_region.width
        if width and width != self._layout_width:
            self._layout_width = width
            self._relayout()

    def notify_style_update(self) -> None:
        super().notify_style_update()
        self._strip_cache.clear()

    def _relayout(self) -> None:
        """Re-render every row. O(rows), so only on a width change or a trim."""

        self._line_map.clear()
        self._strip_cache.clear()
        self._widest = 0
        if not self._layout_width:
            return
        for index, row in enumerate(self._rows):
            row.strips = self._render_strips(row, index)
            self._index_row(index, row)
        self._sync_virtual_size()
        self.refresh()

    def _render_strips(self, row: _Row, index: int) -> list[Strip]:
        width = max(1, self._layout_width - GUTTER_WIDTH)
        console = self.app.console
        options = console.options.update_width(width)
        if isinstance(row.renderable, Text) and not self.wrap:
            options = options.update(overflow="ignore", no_wrap=True)
        lines = list(Segment.split_lines(console.render(row.renderable, options)))
        if not lines:
            lines = [[]]
        strips = [strip.adjust_cell_length(width) for strip in Strip.from_lines(lines)]
        gutter = self._gutter_strips(row, len(strips))
        # The row index travels in the segment metadata, the way OptionList and
        # DataTable carry theirs, so a click resolves to a row without the
        # widget having to reason about borders, padding and scrollbar gutters.
        return [
            (left + right).apply_meta({"row": index})
            for left, right in zip(gutter, strips)
        ]

    def _gutter_strips(self, row: _Row, height: int) -> list[Strip]:
        # No style, deliberately: a strip is built once and reused, so baking
        # today's background into it means the gutter keeps the colours of
        # whatever theme was active when the row was written. Left unstyled,
        # it picks up the widget's background in `render_line` along with
        # everything else, and follows a theme change with the rest of the pane.
        blank = Strip.blank(GUTTER_WIDTH)
        if not row.marked:
            return [blank] * height
        # Only the first line carries the glyph: a wrapped line is the same
        # event, and repeating the marker would read as several marks.
        style = self.get_component_rich_style("log-view--mark")
        head = Strip([Segment(f"{MARK_GLYPH} ", style)], GUTTER_WIDTH)
        return [head] + [blank] * (height - 1)

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        width = self.scrollable_content_region.width
        index = scroll_y + y
        if index < 0 or index >= len(self._line_map):
            return Strip.blank(width, self.rich_style)

        row_index, offset = self._line_map[index]
        row = self._rows[row_index]
        is_cursor = row_index == self._cursor
        # The widget style is part of the key, not just something the cache is
        # cleared on: a cached strip carries the background it was painted
        # with, so switching theme has to miss rather than hand back a row
        # coloured for the theme before it. `notify_style_update` still clears,
        # but correctness must not depend on the order that arrives in.
        base = self.rich_style
        key = (row_index, offset, scroll_x, width, is_cursor, row.watched, base)
        cached = self._strip_cache.get(key)
        if cached is not None:
            return cached

        strip = row.strips[offset]
        strip = strip.crop_extend(scroll_x, scroll_x + width, self.rich_style)
        # Both are *base* styles — per-segment severity colours still win on
        # top, so neither erases the line's own meaning. Watch first, cursor
        # second: where you are looking beats what you asked to be shown.
        if row.watched:
            strip = strip.apply_style(self.get_component_rich_style("log-view--watch"))
        if is_cursor:
            strip = strip.apply_style(self.get_component_rich_style("log-view--cursor"))
        # The widget's own background, applied last and therefore *underneath*
        # everything above — `apply_style` layers its argument beneath the
        # styles already on a segment, so severity colours, the cursor and a
        # watch highlight all still win.
        #
        # Without this the pane never paints its own background at all. Rich
        # renders a Text with no background of its own, so those cells came out
        # unstyled and the terminal's default showed through: invisible on a
        # dark terminal, which is why it survived this long, and a white log
        # pane on a light one no matter which Textual theme was selected.
        strip = strip.apply_style(base)
        self._strip_cache[key] = strip
        return strip

    # --- the row model ------------------------------------------------------

    @property
    def rows(self) -> list[_Row]:
        return self._rows

    @property
    def entries(self) -> list[LogEntry]:
        """Selectable entries, in display order."""

        return [row.entry for row in self._rows if row.entry is not None]

    def entry_rows(self) -> list[tuple[int, LogEntry]]:
        """Selectable rows as ``(row index, entry)``, in display order.

        Row indexes rather than a bare list because callers that pick an entry
        — jump to the next match, jump to a timestamp — have to hand an index
        back to :meth:`move_cursor`, and message rows make the two differ.
        """

        return [
            (index, row.entry) for index, row in enumerate(self._rows) if row.entry is not None
        ]

    @property
    def text_lines(self) -> list[str]:
        """Rendered content as plain text, one item per screen line.

        Exists for tests that need to assert what actually reached the pane;
        nothing in the app reads it.
        """

        return [
            self._rows[row_index].strips[offset].text for row_index, offset in self._line_map
        ]

    def _selectable_indexes(self) -> Iterator[int]:
        return (index for index, row in enumerate(self._rows) if row.selectable)

    def _first_selectable(self) -> int:
        for index in self._selectable_indexes():
            return index
        return -1

    def _last_selectable(self) -> int:
        last = -1
        for index in self._selectable_indexes():
            last = index
        return last

    def _step(self, start: int, direction: int) -> int:
        index = start + direction
        while 0 <= index < len(self._rows):
            if self._rows[index].selectable:
                return index
            index += direction
        return -1

    # --- cursor -------------------------------------------------------------

    @property
    def cursor(self) -> int:
        """Index into the row model, or -1 when nothing is selected."""

        return self._cursor

    @property
    def cursor_entry(self) -> LogEntry | None:
        if 0 <= self._cursor < len(self._rows):
            return self._rows[self._cursor].entry
        return None

    @property
    def cursor_cluster(self) -> object | None:
        """The group under the cursor, when the cursor is on a cluster row."""

        if 0 <= self._cursor < len(self._rows):
            return self._rows[self._cursor].cluster
        return None

    @property
    def cursor_at_end(self) -> bool:
        """True when nothing is selected, or the last entry is."""

        last = self._last_selectable()
        return self._cursor < 0 or self._cursor == last

    def move_cursor(self, index: int, *, scroll: bool = True, notify: bool = True) -> bool:
        """Put the cursor on row *index*. Returns True when it moved."""

        if index < 0 or index >= len(self._rows) or not self._rows[index].selectable:
            return False
        if index == self._cursor:
            if scroll:
                self._scroll_to_cursor()
            return False
        previous = self._cursor
        self._cursor = index
        self._refresh_row(previous)
        self._refresh_row(index)
        if scroll:
            self._scroll_to_cursor()
        if notify:
            self.post_message(
                self.CursorMoved(self, index, self._rows[index].entry, self.cursor_at_end)
            )
        return True

    def move_cursor_to_entry(self, entry: LogEntry, *, near: int = 0, notify: bool = False) -> bool:
        """Restore the cursor onto *entry* after a re-render.

        Identical raw lines are indistinguishable, so the search starts from the
        cursor's previous ordinal and works outwards: after a filter change the
        nearest copy is the one the operator was looking at.
        """

        candidates = [index for index, row in enumerate(self._rows) if row.entry == entry]
        if not candidates:
            return False
        best = min(candidates, key=lambda index: abs(index - near))
        return self.move_cursor(best, notify=notify)

    def clamp_cursor(self, ordinal: int, *, notify: bool = False) -> bool:
        """Park the cursor on the selectable row nearest *ordinal*.

        Used when the previously selected line did not survive a filter change:
        the contract is "nearest surviving line", not "back to the top".
        """

        selectable = list(self._selectable_indexes())
        if not selectable:
            return False
        best = min(selectable, key=lambda index: abs(index - ordinal))
        return self.move_cursor(best, notify=notify)

    def _refresh_row(self, index: int) -> None:
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        if not row.strips:
            return
        self.refresh_lines(row.start_line, len(row.strips))

    def _scroll_to_cursor(self) -> None:
        if not (0 <= self._cursor < len(self._rows)):
            return
        row = self._rows[self._cursor]
        if not row.strips or not self.is_mounted:
            return
        self.scroll_to_region(
            Region(0, row.start_line, self.scrollable_content_region.width, len(row.strips)),
            animate=False,
            force=True,
            immediate=True,
        )

    def set_row_marked(self, index: int, marked: bool) -> None:
        """Flip a row's gutter indicator.

        Re-strips that row only. The renderable and the width are unchanged, so
        the row's height cannot change and the line map stays valid.
        """

        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        if row.marked == marked:
            return
        row.marked = marked
        if self._layout_width and row.strips:
            row.strips = self._render_strips(row, index)
            self._strip_cache.clear()
            self._refresh_row(index)

    def set_row_watched(self, index: int, watched: bool) -> None:
        """Flip a row's watch highlight.

        Cheaper than :meth:`set_row_marked`: the highlight is applied at paint
        time from the row flag, so nothing has to be re-stripped — only the
        cached strips for this row are dropped and the lines repainted.
        """

        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        if row.watched == watched:
            return
        row.watched = watched
        self._strip_cache.clear()
        self._refresh_row(index)

    def row_holds(self, index: int, cluster: object) -> bool:
        """Whether row *index* is still the row standing for *cluster*.

        Row indexes are not stable: :meth:`_trim` drops a batch off the front
        when the cap is exceeded and everything below it moves up. A caller
        holding an index from an earlier render has to ask before writing
        through it, or it rewrites whatever line is there now.
        """

        return 0 <= index < len(self._rows) and self._rows[index].cluster is cluster

    def is_row_watched(self, index: int) -> bool:
        return 0 <= index < len(self._rows) and self._rows[index].watched

    # --- actions ------------------------------------------------------------

    def action_cursor_down(self) -> None:
        target = self._step(self._cursor, 1) if self._cursor >= 0 else self._first_selectable()
        if target >= 0:
            self.move_cursor(target)

    def action_cursor_up(self) -> None:
        target = self._step(self._cursor, -1) if self._cursor >= 0 else self._last_selectable()
        if target >= 0:
            self.move_cursor(target)

    def action_cursor_page_down(self) -> None:
        self._page(1)

    def action_cursor_page_up(self) -> None:
        self._page(-1)

    def _page(self, direction: int) -> None:
        """Move a viewport's worth of *lines*, then land on the row there.

        Measured in lines rather than rows so a page is what the operator sees
        moving past, even when a structured entry occupies a dozen lines.
        """

        if self._cursor < 0:
            target = self._first_selectable() if direction > 0 else self._last_selectable()
            if target >= 0:
                self.move_cursor(target)
            return
        height = max(1, self.scrollable_content_region.height)
        line = self._rows[self._cursor].start_line + direction * height
        line = max(0, min(line, len(self._line_map) - 1))
        if not self._line_map:
            return
        row_index = self._line_map[line][0]
        if not self._rows[row_index].selectable:
            row_index = self._step(row_index, direction)
            if row_index < 0:
                row_index = self._step(self._line_map[line][0], -direction)
        if row_index >= 0:
            self.move_cursor(row_index)
        elif direction > 0:
            self.move_cursor(self._last_selectable())
        else:
            self.move_cursor(self._first_selectable())

    def action_cursor_home(self) -> None:
        target = self._first_selectable()
        if target >= 0:
            self.move_cursor(target)

    def action_cursor_end(self) -> None:
        target = self._last_selectable()
        if target >= 0:
            self.move_cursor(target)
        else:
            self.scroll_end(animate=False)

    def action_select_cursor(self) -> None:
        if not (0 <= self._cursor < len(self._rows)):
            return
        row = self._rows[self._cursor]
        if row.cluster is not None:
            # A cluster row expands; a normal row opens detail. One key, two
            # meanings, decided by what the cursor is on rather than by a mode.
            self.post_message(self.ClusterToggled(self, self._cursor, row.cluster))
            return
        if row.entry is not None:
            self.post_message(self.EntrySelected(self, self._cursor, row.entry))

    # --- mouse --------------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        row_index = event.style.meta.get("row")
        if row_index is None or not (0 <= row_index < len(self._rows)):
            return
        if self._rows[row_index].selectable:
            event.stop()
            self.focus()
            self.move_cursor(row_index)


__all__ = ["GUTTER_WIDTH", "MARK_GLYPH", "LogView"]
