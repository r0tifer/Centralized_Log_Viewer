"""The severity timeline: one row of bars, and a caption under it.

A histogram of the filtered set, coloured by the worst severity in each bucket.
It is a **control**, not decoration: the cursor keys move a selection along it
and `Enter` narrows the time window to whatever bucket is selected, which turns
"when did this start" into pointing at a spike.

Two rows, not one
-----------------

Item 14 asked for one row. The bar is one row — volume is carried by the height
of the block glyph, so it survives a terminal with no colour at all — and the
row beneath it is a caption naming the selected bucket. Without the caption the
selection is invisible, which makes a control nobody can aim; the status bar,
the other candidate, is already full at 80 columns. The caption doubles as the
place a source with no timestamps explains itself, in the same voice
``describe_empty_result`` uses.

What a plugin can change here
-----------------------------

Two things, and neither of them is the layout (``PLUGIN_TODO.md`` Phase 11).

A :class:`~clv.plugins.TimelineMetric` changes what the heights *mean* — bytes
rather than lines — so the bar scales against
:attr:`~clv.services.timeline.Timeline.peak_value` instead of the count, and the
caption leads with the metric and names the plugin. It has to: the glyphs are
identical either way, and the caption is the only thing that says which question
the bar is answering.

A :class:`~clv.plugins.TimelineAnnotation` marks a bucket. The mark is drawn by
**styling the glyph that is already there** — underlined, in the mark's own
severity colour — rather than by replacing it or by adding a row: replacing it
would lose the volume exactly where something interesting happened, and a third
row would change this widget's height for every build that has a plugin
installed. Underline rather than colour alone, for the reason the block glyphs
carry the volume: it has to read on a monochrome terminal.

The widget renders what it is handed and decides nothing. Bucketing lives in
:mod:`clv.services.timeline`, which is UI-free and testable without a screen.
"""

from __future__ import annotations

import math
from typing import Optional

from rich.console import RenderableType
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from ..services.filtering import TimeWindow
from ..services.timeline import (
    Timeline,
    annotation_levels,
    describe_bucket,
    describe_metric,
    describe_undated,
)
from ..services.timeline import EMPTY as EMPTY_TIMELINE
from .severity import SEVERITY_COLORS, UNKNOWN_COLOR

#: Eight heights of block, so a bar carries its volume without colour. Index 0
#: is reserved for an empty bucket, which is drawn as a low tick rather than a
#: blank so the axis stays visible where nothing happened.
BLOCKS: tuple[str, ...] = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")

#: Drawn where a bucket has no events at all.
EMPTY_GLYPH = "·"


class TimelineBar(Widget, can_focus=True):
    """A one-row severity histogram with a keyboard-selectable bucket."""

    DEFAULT_CSS = """
    TimelineBar {
        display: none;
        height: 2;
        width: 1fr;
        background: $surface;
        color: $text-muted;
    }

    TimelineBar.-visible { display: block; }

    /* Focus is worth showing: the cursor keys mean something different while
       this has it, and an operator who cannot tell where focus is will move
       the log cursor by accident. */
    TimelineBar:focus { background: $surface 15%; }
    """

    # Widget-scoped, exactly like LogView's cursor keys and for the same
    # reason: these only mean anything while the bar has focus, and binding
    # arrows on the app would fight the tree, the log pane and every input.
    # The action names are deliberately distinct from LogView's so the merged
    # help overlay lists them as their own entries.
    BINDINGS = [
        Binding("left", "bucket_left", "Previous bucket", show=False),
        Binding("right", "bucket_right", "Next bucket", show=False),
        Binding("home", "bucket_home", "First bucket", show=False),
        Binding("end", "bucket_end", "Last bucket", show=False),
        # Shift, rather than teaching the arrows to skip: a bar with annotations
        # installed must still walk bucket by bucket, which is what the bar was
        # built to do, and a key that means something different depending on
        # what is installed is the one thing a plugin seam may not do to a
        # binding that already exists. No-ops with nothing marked, exactly as
        # Enter does with no buckets.
        Binding("shift+left", "annotation_left", "Previous annotation", show=False),
        Binding("shift+right", "annotation_right", "Next annotation", show=False),
        Binding("enter", "apply_bucket", "Filter to the selected bucket", show=False),
    ]

    class WidthChanged(Message):
        """The bar has room for a different number of buckets than it holds.

        The bucket count *is* the width, so a resize is not a re-layout of the
        same histogram — it is a different one. The widget cannot rebuild it
        (bucketing is a service, and the entries are the app's), so it says so
        and the app rebuilds. Also the path that corrects the very first
        render: the bar is hidden until `b`, so it has no width to bucket
        against until the moment it appears.
        """

        def __init__(self, bar: "TimelineBar", width: int) -> None:
            super().__init__()
            self.bar = bar
            self.width = width

        @property
        def control(self) -> "TimelineBar":
            return self.bar

    class BucketSelected(Message):
        """A bucket was chosen — narrow the time window to it."""

        def __init__(self, bar: "TimelineBar", index: int, window: TimeWindow, label: str) -> None:
            super().__init__()
            self.bar = bar
            self.index = index
            self.window = window
            self.label = label

        @property
        def control(self) -> "TimelineBar":
            return self.bar

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._timeline: Timeline = EMPTY_TIMELINE
        self._selected = -1

    # --- content ------------------------------------------------------------

    @property
    def timeline(self) -> Timeline:
        return self._timeline

    @property
    def selected(self) -> int:
        """Index of the selected bucket, or -1 when none is."""

        return self._selected

    @property
    def width_in_buckets(self) -> int:
        """How many buckets the bar has room for, which is what it asks for."""

        return max(1, self.size.width or self.container_size.width or 1)

    def set_timeline(self, timeline: Timeline) -> None:
        """Show *timeline*, keeping the selection where the grid allows.

        A rebuild happens on every filter change, so resetting the selection
        each time would make the bar unusable exactly when it is wanted. The
        selection is kept by index — the grid is the same shape for the same
        width — and clamped when the new one is shorter.
        """

        self._timeline = timeline
        if not timeline.buckets:
            self._selected = -1
        elif self._selected >= len(timeline.buckets):
            self._selected = len(timeline.buckets) - 1
        self.refresh()

    def clear(self) -> None:
        self._timeline = EMPTY_TIMELINE
        self._selected = -1
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width
        if not width or width == len(self._timeline.buckets):
            return
        self.post_message(self.WidthChanged(self, width))

    # --- rendering ----------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y == 0:
            return self._bar_strip(width)
        return self._caption_strip(width)

    def _bar_strip(self, width: int) -> Strip:
        buckets = self._timeline.buckets
        if not buckets:
            # No axis to draw. The caption carries the explanation; leaving the
            # bar row blank is what keeps an unanswerable question from looking
            # like a quiet source.
            return Strip.blank(width, self.rich_style)

        metric = self._timeline.metric
        # What the heights are drawn from. `peak` and `peak_value` answer
        # different questions and the installed metric is what decides which one
        # the bar is asking.
        peak = (
            max(1.0, self._timeline.peak_value)
            if metric
            else float(max(1, self._timeline.peak))
        )
        marks = annotation_levels(self._timeline)
        segments: list[Segment] = []
        for index, bucket in enumerate(buckets[:width]):
            if bucket.count == 0:
                # Keyed on the count even under a metric: `·` means nothing
                # happened here, and a bucket holding a hundred lines that
                # measured zero bytes is not that.
                glyph = EMPTY_GLYPH
            else:
                measure = float(bucket.value if metric else bucket.count)
                # ceil, so a bucket with a single event is never invisible --
                # including one whose metric summed to zero, which is still a
                # bucket with lines in it. Float arithmetic throughout now that
                # a metric can be fractional; the `min` below already absorbs
                # the rounding that puts a full bucket one step over the top.
                height = max(1, math.ceil(measure * len(BLOCKS) / peak))
                glyph = BLOCKS[min(height, len(BLOCKS)) - 1]
            marked = index in marks
            color = SEVERITY_COLORS.get(
                (marks[index] if marked else bucket.level) or "", UNKNOWN_COLOR
            )
            style = Style(
                color=color,
                reverse=index == self._selected,
                underline=marked or None,
            )
            segments.append(Segment(glyph, style))
        strip = Strip(segments)
        return strip.adjust_cell_length(width, self.rich_style)

    def _caption_strip(self, width: int) -> Strip:
        caption = self._caption()
        if len(caption) > width:
            # Truncated here rather than by the layout: this row must never
            # widen the widget, and a caption that wrapped would change the
            # bar's height under the log pane.
            caption = caption[: max(0, width - 1)] + "…"
        return Strip([Segment(caption, self.rich_style)]).adjust_cell_length(
            width, self.rich_style
        )

    def _caption(self) -> str:
        if not self._timeline.buckets:
            return describe_undated(self._timeline)
        undated = self._timeline.undated
        skipped = f"  ({undated} with no timestamp)" if undated else ""
        if self._selected < 0:
            span = self._span_label()
            # The metric is named before the hint, and on the unselected caption
            # as well as the selected one: an operator who never presses an
            # arrow key still has to be told the heights are not lines.
            metric = describe_metric(self._timeline)
            measuring = f" · {metric}" if metric else ""
            return (
                f"{span}{measuring} · ←/→ to select a bucket, "
                f"Enter to filter to it{skipped}"
            )
        return f"{describe_bucket(self._timeline, self._selected)}{skipped}"

    def _span_label(self) -> str:
        buckets = self._timeline.buckets
        first = buckets[0].start
        last = buckets[-1].end
        if first.date() == last.date():
            return f"{first:%Y-%m-%d %H:%M:%S}–{last:%H:%M:%S}"
        return f"{first:%Y-%m-%d %H:%M}–{last:%Y-%m-%d %H:%M}"

    def render(self) -> RenderableType:  # pragma: no cover - render_line is used
        return Text("")

    # --- selection ----------------------------------------------------------

    def select(self, index: int) -> bool:
        """Put the selection on bucket *index*. Returns True when it moved."""

        buckets = self._timeline.buckets
        if not buckets:
            return False
        index = max(0, min(index, len(buckets) - 1))
        if index == self._selected:
            return False
        self._selected = index
        self.refresh()
        return True

    def action_bucket_left(self) -> None:
        self.select(len(self._timeline.buckets) - 1 if self._selected < 0 else self._selected - 1)

    def action_bucket_right(self) -> None:
        self.select(0 if self._selected < 0 else self._selected + 1)

    def action_bucket_home(self) -> None:
        self.select(0)

    def action_bucket_end(self) -> None:
        self.select(len(self._timeline.buckets) - 1)

    def action_annotation_left(self) -> None:
        self._step_annotation(-1)

    def action_annotation_right(self) -> None:
        self._step_annotation(1)

    def _step_annotation(self, direction: int) -> None:
        """Move to the nearest marked bucket in *direction*, if there is one.

        No wrapping, like the bucket keys beside it: `select` clamps, and an
        operator who has walked to the last deploy of the window should not find
        the next press back at the first one.
        """

        marked = sorted({mark.index for mark in self._timeline.annotations})
        if not marked:
            return
        if self._selected < 0:
            # From nowhere, the ends: the same rule `bucket_left` follows, so
            # shift+← from an unselected bar lands on the last mark rather than
            # on nothing.
            self.select(marked[-1] if direction < 0 else marked[0])
            return
        candidates = [
            index
            for index in marked
            if (index < self._selected if direction < 0 else index > self._selected)
        ]
        if not candidates:
            return
        self.select(candidates[-1] if direction < 0 else candidates[0])

    def action_apply_bucket(self) -> None:
        self._emit(self._selected)

    def _emit(self, index: int) -> None:
        window = self._timeline.window_for(index)
        if window is None:
            return
        self.post_message(
            self.BucketSelected(self, index, window, describe_bucket(self._timeline, index))
        )

    # --- mouse --------------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        """One bucket per cell, so the x offset *is* the index.

        No segment metadata needed, unlike ``LogView``: this widget has no
        gutter, no scroll offset and no wrapped rows, so the mapping cannot
        drift.
        """

        if event.y != 0 or not self._timeline.buckets:
            return
        index: Optional[int] = event.x
        if index is None or not (0 <= index < len(self._timeline.buckets)):
            return
        event.stop()
        self.focus()
        self.select(index)
        self._emit(index)


__all__ = ["BLOCKS", "EMPTY_GLYPH", "TimelineBar"]
