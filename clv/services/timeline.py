"""Event volume over time, bucketed to fit a bar of a given width.

Event Viewer's "Summary of Administrative Events", except live and one
keystroke from the logs. The question it answers is *when did this start*,
which the pane itself can only answer by scrolling.

UI-free, like every other service: this module turns entries into counts and
knows nothing about block glyphs, colours or where the bar is drawn. ``width``
is the number of buckets the caller has room for, which is what lets the
arithmetic be tested without a screen.

The grid
--------

Buckets are a fixed grid — an ``origin`` and a ``step`` — rather than a list of
ranges computed from the data each time. That is what makes :meth:`Timeline.extend`
possible: a line that arrives on the next poll finds its bucket by arithmetic
instead of a rebuild, so tailing costs what arrived rather than what is
buffered. When an arrival falls outside the grid ``extend`` says so and the
caller rebuilds; with a bar 70-odd cells wide over a buffered hour, that is once
a bucket rather than twice a second.

Never silently lose a line
--------------------------

An entry with no timestamp cannot be placed on a time axis, so it is counted in
:attr:`Timeline.undated` and reported, exactly as the severity and time filters
report what they hide. A source where *nothing* has a timestamp produces a
timeline with no buckets at all, which the bar renders as an explanation rather
than as an empty rectangle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from .filtering import TimeWindow, sortable_moment
from .parsing import LogEntry, level_rank


@dataclass(frozen=True, slots=True)
class Bucket:
    """One column of the histogram."""

    start: datetime
    #: Exclusive: the next bucket starts here.
    end: datetime
    count: int
    #: Highest severity among the entries in this bucket, None when none of
    #: them declared one.
    level: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Timeline:
    """A fixed grid of buckets over the entries it was built from."""

    buckets: tuple[Bucket, ...]
    #: Grid origin — the start of ``buckets[0]``. Kept explicitly so ``extend``
    #: can index into the grid without consulting the buckets themselves.
    origin: datetime
    step: timedelta
    #: Entries with no timestamp. Counted, never dropped.
    undated: int = 0
    #: Whether the grid dropped UTC offsets because the source mixed aware and
    #: naive stamps. Carried so ``extend`` keys arrivals the same way.
    naive: bool = True

    @property
    def total(self) -> int:
        """Entries placed on the axis. ``undated`` is deliberately not in it."""

        return sum(bucket.count for bucket in self.buckets)

    @property
    def peak(self) -> int:
        """The busiest bucket's count — what a bar scales its heights against."""

        return max((bucket.count for bucket in self.buckets), default=0)

    def window_for(self, index: int) -> Optional[TimeWindow]:
        """The time window bucket *index* covers, for filtering down to it.

        The end bound is the bucket's own end, so the boundary instant belongs
        to both neighbours. That costs a duplicated line at an exact edge and
        saves explaining a window that ends a microsecond before it looks like
        it does.
        """

        if not (0 <= index < len(self.buckets)):
            return None
        bucket = self.buckets[index]
        return TimeWindow(start=bucket.start, end=bucket.end)

    def index_of(self, moment: datetime) -> int:
        """Which bucket *moment* falls in. May be outside the grid."""

        return int((sortable_moment(moment, naive=self.naive) - self.origin) // self.step)

    def extend(self, entries: Iterable[LogEntry]) -> Optional["Timeline"]:
        """Fold newly tailed *entries* into this grid.

        Returns the updated timeline, or ``None`` when an entry lands outside
        the grid and only a rebuild would be honest. Cost is proportional to
        what arrived plus the width of the bar, never to what is buffered.
        """

        if not self.buckets:
            # Nothing to fold into: the source had no timestamps when this was
            # built, and one that has arrived changes the whole picture.
            return None

        counts = [bucket.count for bucket in self.buckets]
        levels = [bucket.level for bucket in self.buckets]
        undated = self.undated
        touched = False

        for entry in entries:
            if entry.timestamp is None:
                undated += 1
                touched = True
                continue
            index = self.index_of(entry.timestamp)
            if not (0 <= index < len(counts)):
                return None
            counts[index] += 1
            if level_rank(entry.level) > level_rank(levels[index]):
                levels[index] = entry.level
            touched = True

        if not touched:
            return self
        buckets = tuple(
            Bucket(bucket.start, bucket.end, count, level)
            for bucket, count, level in zip(self.buckets, counts, levels)
        )
        return Timeline(
            buckets=buckets,
            origin=self.origin,
            step=self.step,
            undated=undated,
            naive=self.naive,
        )


#: Nothing at all: no source open, or a source whose every line the filters
#: hid. Distinct from "buckets are empty", which means lines exist and none of
#: them carry a timestamp.
EMPTY = Timeline(buckets=(), origin=datetime.min, step=timedelta(seconds=1), undated=0)


def build_timeline(entries: Sequence[LogEntry], *, width: int) -> Timeline:
    """Bucket *entries* into at most *width* columns.

    *entries* are the ones the operator can see — filtered, not the raw buffer
    — so the histogram answers "when did the thing I am looking at happen"
    rather than "when did anything happen".
    """

    width = max(1, width)
    stamped: list[tuple[datetime, Optional[str]]] = []
    undated = 0
    for entry in entries:
        if entry.timestamp is None:
            undated += 1
            continue
        stamped.append((entry.timestamp, entry.level))

    if not stamped:
        # Lines, but no time axis to put them on. The bar says so; an empty
        # rectangle would look like a quiet source rather than an unanswerable
        # question.
        return Timeline(
            buckets=(),
            origin=EMPTY.origin,
            step=EMPTY.step,
            undated=undated,
        )

    # The set rule, decided once, exactly as the k-way merge decides it: if any
    # member is naive the offsets come off all of them.
    naive = any(moment.tzinfo is None for moment, _ in stamped)
    stamped = [(sortable_moment(moment, naive=naive), level) for moment, level in stamped]

    first = min(moment for moment, _ in stamped)
    last = max(moment for moment, _ in stamped)
    # Whole seconds, so a bucket edge is something a caption can print and a
    # human can type back into the custom range dialog.
    origin = first.replace(microsecond=0)
    step = _step_for(last - origin, width)

    count = int((last - origin) // step) + 1
    counts = [0] * count
    levels: list[Optional[str]] = [None] * count
    for moment, level in stamped:
        index = int((moment - origin) // step)
        counts[index] += 1
        if level_rank(level) > level_rank(levels[index]):
            levels[index] = level

    buckets = tuple(
        Bucket(origin + step * index, origin + step * (index + 1), counts[index], levels[index])
        for index in range(count)
    )
    return Timeline(buckets=buckets, origin=origin, step=step, undated=undated, naive=naive)


def _step_for(span: timedelta, width: int) -> timedelta:
    """Bucket duration: the smallest whole second that fits *span* in *width*.

    Whole seconds rather than the exact quotient because the edges are shown to
    the operator and used as filter bounds. The loop is the inclusive-ends
    correction — a span of exactly ``width`` seconds needs ``width + 1`` one-second
    buckets to cover both ends — and it terminates because each pass strictly
    increases the divisor.
    """

    seconds = max(1, int(span.total_seconds()))
    step = max(1, -(-seconds // width))
    while seconds // step + 1 > width:
        step += 1
    return timedelta(seconds=step)


def describe_bucket(timeline: Timeline, index: int) -> str:
    """One line naming what a bucket covers, for the caption under the bar."""

    if not (0 <= index < len(timeline.buckets)):
        return ""
    bucket = timeline.buckets[index]
    start = bucket.start
    end = bucket.end
    if start.date() == end.date():
        span = f"{start:%Y-%m-%d %H:%M:%S}–{end:%H:%M:%S}"
    else:
        span = f"{start:%Y-%m-%d %H:%M:%S}–{end:%Y-%m-%d %H:%M:%S}"
    if bucket.count == 0:
        return f"{span} · no events"
    plural = "event" if bucket.count == 1 else "events"
    level = bucket.level or "no level"
    return f"{span} · {bucket.count} {plural} · {level}"


def describe_undated(timeline: Timeline) -> str:
    """Explain a bar that cannot be drawn, in `describe_empty_result`'s voice."""

    if timeline.buckets:
        return ""
    if timeline.undated:
        return (
            f"No timeline — {timeline.undated} line(s) have no detected timestamp "
            f"(this source's format carries no date)."
        )
    return "No timeline — nothing to plot."


__all__ = [
    "EMPTY",
    "Bucket",
    "Timeline",
    "build_timeline",
    "describe_bucket",
    "describe_undated",
]
