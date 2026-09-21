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

Foldable metrics
----------------

A bucket counts entries. ``PLUGIN_TODO.md`` Phase 11 lets a
:class:`~clv.plugins.TimelineMetric` say it should measure something else —
bytes, durations, whatever the log carries — and the grid above is what bounds
the shape of that interface.

:meth:`Timeline.extend` adds an arrival to an existing bucket by **arithmetic**.
A sum survives that: adding one more number to a total gives the total a rebuild
would have computed. A median does not, and neither does a percentile, a
distinct count or an average that has forgotten its denominator — folding one
arrival into those needs the members, and the members are exactly what this
module does not keep.

So a metric declares a per-entry value and **CLV does the summing**. There is no
aggregate hook to implement, which makes a non-foldable metric *unexpressible*
rather than broken: the alternative — an ``aggregate()`` a plugin could
implement as ``statistics.median`` — would produce a bar that was right on the
first render and silently wrong on every tailed line after it.

``count`` keeps its meaning throughout. A metric changes what the bar is scaled
by and what the caption leads with, and nothing downstream that reads a bucket's
count has to know one is installed.

Marks on the axis
-----------------

A :class:`~clv.plugins.TimelineAnnotation` supplies ``(moment, label, level)``
triples for the window on screen — deploys, incidents, maintenance windows, the
context that makes a spike mean something. They are resolved to a bucket index
once, when the grid is built, and a moment outside the grid never becomes an
:class:`Annotation`; the providers are asked once per *window* rather than once
per rebuild, because a rebuild is a keystroke in the query box.

Never silently lose a line
--------------------------

An entry with no timestamp cannot be placed on a time axis, so it is counted in
:attr:`Timeline.undated` and reported, exactly as the severity and time filters
report what they hide. A source where *nothing* has a timestamp produces a
timeline with no buckets at all, which the bar renders as an explanation rather
than as an empty rectangle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional, Sequence

from .filtering import TimeWindow, sortable_moment
from .parsing import LogEntry, level_rank

#: How to read an entry's stamp as an absolute instant. Supplied only when the
#: plain timestamp is not one — a merged set spanning machines whose naive
#: stamps mean different instants. The session owns the rule; this module only
#: has to key the histogram by the same thing the pane sorts by.
MomentOf = Callable[[LogEntry], Optional[datetime]]


@dataclass(frozen=True, slots=True)
class AnnotationSpec:
    """One installed provider of marks on the time axis.

    :attr:`fetch` is handed the window the bar is about to draw and answers with
    ``(moment, label, level)`` triples. It already carries its guard and its
    budget, exactly as a :class:`~clv.services.clustering.ShapeSpec` does — this
    module never learns that ``clv.plugins`` exists.
    """

    plugin: str
    fetch: Callable[[TimeWindow], Sequence[tuple[datetime, str, Optional[str]]]]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """What a bucket measures, when it is not measuring entries.

    :attr:`value` is a per-entry number and CLV does the summing. There is
    deliberately no aggregate hook: see *Foldable metrics* above, where the
    shape of this record is the whole argument.
    """

    plugin: str
    metric_name: str
    #: Printed after the scaled number in the caption — ``"B"`` reads as
    #: ``1.4 MB``. Empty is the ordinary case and prints a bare figure.
    unit: str
    value: Callable[[LogEntry], Optional[float]]


@dataclass(frozen=True, slots=True)
class Annotation:
    """One mark, already placed on a grid.

    The bucket is resolved once, when the mark is read, rather than per render:
    it is keyed exactly the way an entry's moment is keyed (see
    :attr:`Timeline.naive`), and a moment outside the grid never becomes an
    ``Annotation`` at all — which is how "not drawn" is arranged, rather than by
    the bar checking.
    """

    moment: datetime
    label: str
    #: Normalised severity, or None. Decides the colour the mark is drawn in.
    level: Optional[str]
    #: Which bucket it lands in. Always within the grid it was resolved against.
    index: int


#: How many marks one grid keeps. A provider answering with a year of deploys
#: for a bar seventy cells wide is not a fault — it cannot know what it will be
#: asked for next — but drawing them all is pointless and captioning them all is
#: worse, so the excess is dropped.
MAX_ANNOTATIONS = 200

#: Installed annotation providers, in ``plugin_sort_key`` order. Empty on every
#: build with no timeline plugins, which is what keeps this seam free:
#: :func:`build_timeline` skips the fetch entirely rather than caching a miss.
_ANNOTATORS: tuple[AnnotationSpec, ...] = ()

#: The one installed metric, or None. *One*, not a list: two plugins measuring a
#: bucket differently is a conflict the operator resolves, and ``clv.plugins``
#: has already picked by priority and reported that it did.
_METRIC: Optional[MetricSpec] = None

#: ``(window, triples)`` for the last window asked about, or None.
#:
#: One entry, and that is the whole cache. A provider is asked once per
#: *window*, not once per rebuild — and a rebuild happens on every keystroke in
#: the query box, which is what this is for. A grid with a different span is a
#: different question and refetches; nothing accumulates, because the previous
#: answer is exactly as stale as the window it was for.
_ANNOTATION_CACHE: Optional[
    tuple[
        tuple[Optional[datetime], Optional[datetime]],
        tuple[tuple[datetime, str, Optional[str]], ...],
    ]
] = None


def install_timeline_plugins(
    annotators: Sequence[AnnotationSpec] = (),
    metric: Optional[MetricSpec] = None,
) -> None:
    """Register what the enabled timeline plugins declared. Replaces, not merges.

    Called once at mount and again whenever the plugin registry's generation
    moves, so a provider switched off in the ``P`` dialog stops marking the bar
    and a re-enabled one starts again. Both arguments default to empty, which is
    how a test — and ``on_unmount`` — puts the module back.

    **This clears the annotation cache, and that is not the caller's job.** The
    triples held there came from a provider that may have just been switched
    off, or predate one that has just been switched on; serving them would draw
    a mark for a plugin that is not running, which is a wrong answer rather than
    a stale one. Owning the invalidation here means there is no way to install a
    provider set and forget — the argument
    :func:`clv.services.clustering.install_cluster_plugins` makes for the shape
    cache, one seam along.
    """

    global _ANNOTATORS, _METRIC, _ANNOTATION_CACHE

    _ANNOTATORS = tuple(annotators)
    _METRIC = metric
    _ANNOTATION_CACHE = None


def installed_annotators() -> tuple[AnnotationSpec, ...]:
    """Every installed annotation provider, in the order they are asked."""

    return _ANNOTATORS


def installed_metric() -> Optional[MetricSpec]:
    """The metric the bar is scaled by, or None when it is counting entries."""

    return _METRIC


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
    #: What the installed :class:`MetricSpec` measured over this bucket's
    #: entries, summed. ``0.0`` with no metric installed, which is every build
    #: without a timeline plugin — and is why :attr:`count` keeps its meaning: a
    #: metric changes what the *bar* is scaled by, never what a bucket says
    #: about how many lines landed in it.
    value: float = 0.0


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
    #: Marks on the axis, already placed on this grid, in bucket order. Empty on
    #: every build with no annotation provider installed — which is why the bar
    #: and the caption can read it unconditionally.
    #:
    #: Part of the comparison, unlike :attr:`moment_of` and :attr:`metric`: two
    #: grids carrying different marks are not the same histogram, because the
    #: marks are drawn.
    annotations: tuple[Annotation, ...] = ()
    #: How to read an entry's stamp as an instant, when the plain timestamp is
    #: not one — a merge spanning machines in different zones. ``None`` means
    #: ``entry.timestamp``, which is every local case and the default.
    #:
    #: Carried on the grid for the reason ``naive`` is: ``extend`` has to key an
    #: arrival exactly the way the build keyed the entries around it, and a
    #: caller that had to remember to pass it twice would eventually not.
    #: ``compare=False`` because two grids are equal when their buckets are —
    #: a function object is machinery, not part of the histogram.
    moment_of: Optional[MomentOf] = field(default=None, compare=False, repr=False)
    #: What the buckets' :attr:`~Bucket.value` measures, or None when nothing is
    #: measured and the bar is scaled by counts.
    #:
    #: Carried on the grid for exactly the reason :attr:`moment_of` is: ``extend``
    #: has to fold an arrival with the same metric the build summed the entries
    #: around it with, and a caller obliged to pass it twice would eventually
    #: not. ``compare=False`` on the same argument too — a spec is machinery, and
    #: what it produced is in the buckets, which *are* compared.
    metric: Optional[MetricSpec] = field(default=None, compare=False, repr=False)

    def moment(self, entry: LogEntry) -> Optional[datetime]:
        """*entry*'s place on the axis — the mapping, or its own stamp."""

        if self.moment_of is None:
            return entry.timestamp
        return self.moment_of(entry)

    @property
    def total(self) -> int:
        """Entries placed on the axis. ``undated`` is deliberately not in it."""

        return sum(bucket.count for bucket in self.buckets)

    @property
    def peak(self) -> int:
        """The busiest bucket's count — what a bar scales its heights against."""

        return max((bucket.count for bucket in self.buckets), default=0)

    @property
    def peak_value(self) -> float:
        """The largest bucket value — what a *metric* bar scales against.

        Beside :attr:`peak` rather than replacing it, because the two answer
        different questions and the bar picks by whether a metric is installed.
        """

        return max((bucket.value for bucket in self.buckets), default=0.0)

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
        values = [bucket.value for bucket in self.buckets]
        metric = self.metric
        undated = self.undated
        touched = False

        for entry in entries:
            moment = self.moment(entry)
            if moment is None:
                # Neither counted nor measured. An entry with no timestamp has
                # no place on the axis, and a metric does not get to invent one:
                # it is reported in `undated` and that is the whole of it.
                undated += 1
                touched = True
                continue
            index = self.index_of(moment)
            if not (0 <= index < len(counts)):
                return None
            counts[index] += 1
            if metric is not None:
                # The fold, and the reason the interface is a per-entry number.
                # Adding an arrival to a sum is the same arithmetic a rebuild
                # would do; adding one to a median is not, which is why there is
                # no way to declare one.
                measured = metric.value(entry)
                if measured is not None:
                    values[index] += measured
            if level_rank(entry.level) > level_rank(levels[index]):
                levels[index] = entry.level
            touched = True

        if not touched:
            return self
        buckets = tuple(
            Bucket(bucket.start, bucket.end, count, level, value)
            for bucket, count, level, value in zip(self.buckets, counts, levels, values)
        )
        return Timeline(
            buckets=buckets,
            origin=self.origin,
            step=self.step,
            undated=undated,
            naive=self.naive,
            # Carried, not refetched: the grid is the one the marks were placed
            # on, so every index is still the bucket it was. Refetching here
            # would ask a provider twice a second for a window that has not
            # moved.
            annotations=self.annotations,
            moment_of=self.moment_of,
            metric=self.metric,
        )


#: Nothing at all: no source open, or a source whose every line the filters
#: hid. Distinct from "buckets are empty", which means lines exist and none of
#: them carry a timestamp.
EMPTY = Timeline(buckets=(), origin=datetime.min, step=timedelta(seconds=1), undated=0)


def build_timeline(
    entries: Sequence[LogEntry],
    *,
    width: int,
    moment_of: Optional[MomentOf] = None,
) -> Timeline:
    """Bucket *entries* into at most *width* columns.

    *entries* are the ones the operator can see — filtered, not the raw buffer
    — so the histogram answers "when did the thing I am looking at happen"
    rather than "when did anything happen".

    *moment_of* is how to read an entry's stamp as an instant. Omitted — every
    local case — it is ``entry.timestamp`` and nothing below changes. A merged
    set spanning zones supplies the session's own rule, so the bar and the pane
    beneath it agree about the order of the same lines. Two copies of that
    decision would be two places for them to disagree, which is the whole
    argument for :data:`clv.services.session._sortable` being an alias.
    """

    width = max(1, width)
    metric = _METRIC
    stamped: list[tuple[datetime, Optional[str], float]] = []
    undated = 0
    for entry in entries:
        moment = entry.timestamp if moment_of is None else moment_of(entry)
        if moment is None:
            # Undated entries are counted in `undated` and measured by nothing.
            # The metric is not even asked: an entry with no place on the axis
            # has no bucket to contribute to, and asking would invite a plugin
            # to believe otherwise.
            undated += 1
            continue
        measured = 0.0
        if metric is not None:
            value = metric.value(entry)
            if value is not None:
                measured = value
        stamped.append((moment, entry.level, measured))

    if not stamped:
        # Lines, but no time axis to put them on. The bar says so; an empty
        # rectangle would look like a quiet source rather than an unanswerable
        # question.
        return Timeline(
            buckets=(),
            origin=EMPTY.origin,
            step=EMPTY.step,
            undated=undated,
            moment_of=moment_of,
            # Named even with no grid to draw it on, so the caption explaining
            # the absence is still able to say what it would have measured.
            metric=metric,
        )

    # The set rule, decided once, exactly as the k-way merge decides it: if any
    # member is naive the offsets come off all of them.
    naive = any(moment.tzinfo is None for moment, _, _ in stamped)
    stamped = [
        (sortable_moment(moment, naive=naive), level, measured)
        for moment, level, measured in stamped
    ]

    first = min(moment for moment, _, _ in stamped)
    last = max(moment for moment, _, _ in stamped)
    # Whole seconds, so a bucket edge is something a caption can print and a
    # human can type back into the custom range dialog.
    origin = first.replace(microsecond=0)
    step = _step_for(last - origin, width)

    count = int((last - origin) // step) + 1
    counts = [0] * count
    values = [0.0] * count
    levels: list[Optional[str]] = [None] * count
    for moment, level, measured in stamped:
        index = int((moment - origin) // step)
        counts[index] += 1
        values[index] += measured
        if level_rank(level) > level_rank(levels[index]):
            levels[index] = level

    buckets = tuple(
        Bucket(
            origin + step * index,
            origin + step * (index + 1),
            counts[index],
            levels[index],
            values[index],
        )
        for index in range(count)
    )
    return Timeline(
        buckets=buckets,
        origin=origin,
        step=step,
        undated=undated,
        naive=naive,
        annotations=_place_annotations(
            _fetch_annotations(TimeWindow(start=origin, end=origin + step * count)),
            origin=origin,
            step=step,
            count=count,
            naive=naive,
        ),
        moment_of=moment_of,
        metric=metric,
    )


def _fetch_annotations(
    window: TimeWindow,
) -> tuple[tuple[datetime, str, Optional[str]], ...]:
    """Every provider's marks for *window*, asked once per window.

    The cache is checked and filled here rather than by the caller, because the
    caller is a render path that runs on every keystroke and the providers are
    third-party code that was told it may do no I/O. Asking them per rebuild
    would be asking them per character typed.
    """

    global _ANNOTATION_CACHE

    if not _ANNOTATORS:
        # Not even a cache entry: a build with no providers must cost what it
        # cost before they existed.
        return ()

    key = (window.start, window.end)
    cached = _ANNOTATION_CACHE
    if cached is not None and cached[0] == key:
        return cached[1]

    marks: list[tuple[datetime, str, Optional[str]]] = []
    for spec in _ANNOTATORS:
        marks.extend(spec.fetch(window))
    result = tuple(marks)
    _ANNOTATION_CACHE = (key, result)
    return result


def _place_annotations(
    marks: Sequence[tuple[datetime, str, Optional[str]]],
    *,
    origin: datetime,
    step: timedelta,
    count: int,
    naive: bool,
) -> tuple[Annotation, ...]:
    """Put *marks* on the grid, dropping the ones that miss it.

    Keyed exactly the way the entries around them were keyed, which is the whole
    of why this is not a subtraction: a grid built from aware stamps and a
    provider answering in naive local time would otherwise raise on the first
    mark. The rule is :func:`clv.services.filtering.align_moments`' rule wearing
    different clothes — compare rather than refuse — applied in the only
    direction left once the grid exists: an offset comes off a mark on a naive
    grid, and a bare mark on an aware grid is read in the grid's own zone.

    The cap is applied to what lands *on the grid*, in the order the providers
    answered, so a provider returning a decade of deploys costs a bounded walk
    rather than a bounded render.
    """

    if not marks:
        return ()

    placed: list[Annotation] = []
    for moment, label, level in marks:
        keyed = sortable_moment(moment, naive=naive)
        if keyed.tzinfo is None and origin.tzinfo is not None:
            keyed = keyed.replace(tzinfo=origin.tzinfo)
        index = int((keyed - origin) // step)
        if not (0 <= index < count):
            continue
        placed.append(Annotation(moment=moment, label=label, level=level, index=index))
        if len(placed) >= MAX_ANNOTATIONS:
            break
    placed.sort(key=lambda mark: mark.index)
    return tuple(placed)


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
    """One line naming what a bucket covers, for the caption under the bar.

    With a metric installed the measurement comes **first**, because the bar's
    heights are drawn from it: a caption whose leading figure is not the one the
    glyphs mean misleads at a glance. The count stays behind it in every case,
    because ``count`` never stopped meaning entries.
    """

    if not (0 <= index < len(timeline.buckets)):
        return ""
    bucket = timeline.buckets[index]
    start = bucket.start
    end = bucket.end
    if start.date() == end.date():
        span = f"{start:%Y-%m-%d %H:%M:%S}–{end:%H:%M:%S}"
    else:
        span = f"{start:%Y-%m-%d %H:%M:%S}–{end:%Y-%m-%d %H:%M:%S}"

    parts = [span]
    metric = timeline.metric
    if metric is not None:
        parts.append(
            f"{format_metric(bucket.value, metric.unit)} "
            f"{metric.metric_name} ({metric.plugin})"
        )
    if bucket.count == 0:
        parts.append("no events")
    else:
        plural = "event" if bucket.count == 1 else "events"
        parts.append(f"{bucket.count} {plural}")
        parts.append(bucket.level or "no level")
    labels = [mark.label for mark in timeline.annotations if mark.index == index]
    if labels:
        # One label, and a count for the rest. A bucket is one cell wide and the
        # caption is one row; five deploys in ten seconds is a real thing for a
        # provider to report and not a thing to print.
        extra = f" (+{len(labels) - 1} more)" if len(labels) > 1 else ""
        parts.append(f"{labels[0]}{extra}")
    return " · ".join(parts)


def describe_metric(timeline: Timeline) -> str:
    """The metric and the plugin behind it, or ``""`` when counting entries.

    Required, not decorative: a bar whose heights mean bytes looks exactly like
    a bar whose heights mean lines, and the only thing standing between the two
    is this string.
    """

    metric = timeline.metric
    if metric is None:
        return ""
    return f"{metric.metric_name} ({metric.plugin})"


def format_metric(value: float, unit: str = "") -> str:
    """A metric total, short enough to sit in a caption at 80 columns.

    Scaled by thousands rather than by 1024: the prefixes are printed against
    the plugin's own unit, so ``unit = "B"`` reads as ``1.4 MB`` and means what
    a disk vendor means by it. A plugin that wants binary prefixes says so in
    its unit and does its own division.
    """

    magnitude = abs(value)
    for threshold, prefix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if magnitude >= threshold:
            scaled = value / threshold
            return f"{scaled:.1f} {prefix}{unit}" if unit else f"{scaled:.1f}{prefix}"
    if magnitude and magnitude < 1:
        # Two decimals rather than none, so a small metric is not every bucket
        # reading zero.
        return f"{value:.2f} {unit}" if unit else f"{value:.2f}"
    return f"{value:,.0f} {unit}" if unit else f"{value:,.0f}"


def annotation_levels(timeline: Timeline) -> dict[int, Optional[str]]:
    """``bucket index -> worst level marked there``, for whoever draws it.

    Worst rather than first, on the rule the buckets themselves already follow:
    an incident and a maintenance window in one bucket are drawn in the
    incident's colour. Here rather than in the widget so the arithmetic stays
    testable without a screen, like everything else in this module.
    """

    levels: dict[int, Optional[str]] = {}
    for mark in timeline.annotations:
        if mark.index in levels and level_rank(mark.level) <= level_rank(levels[mark.index]):
            continue
        levels[mark.index] = mark.level
    return levels


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
    "MAX_ANNOTATIONS",
    "Annotation",
    "AnnotationSpec",
    "Bucket",
    "MetricSpec",
    "MomentOf",
    "Timeline",
    "annotation_levels",
    "build_timeline",
    "describe_bucket",
    "describe_metric",
    "describe_undated",
    "format_metric",
    "install_timeline_plugins",
    "installed_annotators",
    "installed_metric",
]
