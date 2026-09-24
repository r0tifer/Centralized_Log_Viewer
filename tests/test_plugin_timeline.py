"""A plugin extends the timeline, and the timeline stays foldable.

Phase 11 of ``PLUGIN_TODO.md``. The histogram counted entries and coloured by
worst severity, and both were closed: a log whose interesting quantity is bytes
got a bar of the wrong thing, and a spike had no way to carry its cause.

Three claims are under test here, and they are different in kind.

**Requirement 9, equal terms.** A plugin metric scales the bar, is folded by
``extend``, survives tailing through the app, and is named in the caption — a
caption that knows about a metric in general and about no plugin in particular.
A plugin annotation is drawn, captioned and steppable through the same widget
that draws the built-in bar.

**The fold rule is enforced by the interface, and this is where that is shown
to be true.** ``test_a_metric_survives_a_tail`` builds a grid, tails ten lines
into it, and asserts the folded result is the one a full rebuild would have
produced. That test is the reason ``TimelineMetric`` declares a per-entry value
and nothing else.

**Requirement 10, a build with no plugins pays nothing.** ``tests/test_timeline.py``
is **not modified by this phase at all**, which is the evidence rather than an
assertion about it; what is asserted here is the narrower thing that file cannot
see — that a bucket's ``value`` is ``0.0``, its ``annotations`` empty, and the
caption byte-identical to the string it was before this seam existed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from textual.widgets import Static

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import (
    PluginBudget,
    PluginRegistry,
    TimelineAnnotation,
    TimelineMetric,
)
from clv.services.filtering import TimeWindow
from clv.services.parsing import LogEntry
from clv.services.timeline import (
    MAX_ANNOTATIONS,
    annotation_levels,
    build_timeline,
    describe_bucket,
    describe_metric,
    format_metric,
    install_timeline_plugins,
    installed_annotators,
    installed_metric,
)
from clv.widgets.timeline import BLOCKS

BASE = datetime(2026, 8, 7, 9, 0, 0)


def _entries(
    count: int, *, step: int = 1, level: str = "INFO", start: int = 0, **fields: str
) -> list[LogEntry]:
    return [
        LogEntry(
            raw=f"line {start + index}",
            timestamp=BASE + timedelta(seconds=(start + index) * step),
            level=level,
            fields=dict(fields),
        )
        for index in range(count)
    ]


# --- plugins under test -----------------------------------------------------


class Deploys(TimelineAnnotation):
    """Marks read from a list handed in at construction, never fetched."""

    name = "deploys"

    def __init__(self, *marks: tuple[datetime, str, str | None]) -> None:
        self.marks = list(marks)
        self.calls = 0
        self.windows: list[TimeWindow] = []

    def annotations(self, window):
        self.calls += 1
        self.windows.append(window)
        return list(self.marks)


class Bytes(TimelineMetric):
    """The canonical metric: a field, read, and nothing else."""

    name = "bytes-metric"
    metric_name = "bytes read"
    unit = "B"

    def __init__(self) -> None:
        self.calls = 0

    def value(self, entry: LogEntry):
        self.calls += 1
        raw = entry.fields.get("bytes")
        return None if raw is None else float(raw)


class Ones(TimelineMetric):
    """Measures one per entry, so a metric bar and a count bar coincide."""

    name = "ones"
    metric_name = "ones"

    def value(self, entry: LogEntry):
        return 1.0


def _install(*plugins, budget: PluginBudget | None = None):
    """Load *plugins* into a registry and install what they declared."""

    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
        ), [str(error) for error in registry.errors]
    registry.order()
    stack = registry.timeline_stack(budget=budget)
    install_timeline_plugins(stack.annotators, stack.metric)
    return registry, stack


def _reinstall(registry: PluginRegistry, budget: PluginBudget | None = None):
    """Rebuild the stack, as the app does when the generation moves."""

    stack = registry.timeline_stack(budget=budget)
    install_timeline_plugins(stack.annotators, stack.metric)
    return stack


# --- a build with no timeline plugins ---------------------------------------


def test_with_no_plugins_a_bucket_is_what_it_always_was() -> None:
    """Requirement 10, asserted against literals rather than against a promise."""

    timeline = build_timeline(_entries(10), width=5)

    assert timeline.metric is None
    assert timeline.annotations == ()
    assert all(bucket.value == 0.0 for bucket in timeline.buckets)
    assert describe_metric(timeline) == ""
    assert installed_annotators() == ()
    assert installed_metric() is None


def test_with_no_plugins_the_caption_is_byte_identical() -> None:
    """The string, not its shape. A caption that gained a separator would be a
    caption every operator's eye has to re-learn for no reason."""

    timeline = build_timeline(
        [LogEntry(raw="a", timestamp=BASE, level="ERROR")], width=1
    )

    assert describe_bucket(timeline, 0) == "2026-08-07 09:00:00–09:00:02 · 1 event · ERROR"


# --- the metric -------------------------------------------------------------


def test_a_metric_changes_the_values_and_leaves_the_count_alone() -> None:
    _install(Bytes())

    timeline = build_timeline(
        [
            LogEntry(raw="a", timestamp=BASE, fields={"bytes": "100"}),
            LogEntry(raw="b", timestamp=BASE, fields={"bytes": "400"}),
        ],
        width=1,
    )

    assert timeline.buckets[0].value == 500.0
    # The half of this that matters most: `count` never stops meaning entries,
    # so nothing downstream that reads it has to learn a metric exists.
    assert timeline.buckets[0].count == 2
    assert timeline.total == 2
    assert timeline.peak == 2
    assert timeline.peak_value == 500.0


def test_an_entry_the_metric_declines_is_still_counted() -> None:
    """`None` is "not measured by me", not "not an entry"."""

    _install(Bytes())

    timeline = build_timeline(
        [
            LogEntry(raw="a", timestamp=BASE, fields={"bytes": "100"}),
            LogEntry(raw="b", timestamp=BASE),
        ],
        width=1,
    )

    assert timeline.buckets[0].count == 2
    assert timeline.buckets[0].value == 100.0


def test_the_caption_names_the_metric_and_the_plugin() -> None:
    """A metric bar and a count bar are the same glyphs. This is the difference."""

    _install(Bytes())

    timeline = build_timeline(
        [LogEntry(raw="a", timestamp=BASE, level="ERROR", fields={"bytes": "1449984"})],
        width=1,
    )
    caption = describe_bucket(timeline, 0)

    assert "1.4 MB" in caption
    assert "bytes read" in caption
    assert "bytes-metric" in caption
    # And the count is still in it, behind the measurement.
    assert "1 event" in caption
    assert describe_metric(timeline) == "bytes read (bytes-metric)"


def test_the_metric_leads_the_caption() -> None:
    """The bar's heights are drawn from it, so it is the first figure read."""

    _install(Bytes())

    timeline = build_timeline(
        [LogEntry(raw="a", timestamp=BASE, fields={"bytes": "10"})], width=1
    )
    caption = describe_bucket(timeline, 0)

    assert caption.index("bytes read") < caption.index("1 event")


def test_an_undated_entry_contributes_to_neither_and_is_still_reported() -> None:
    """The rule the module has always had, extended to a thing that could break it."""

    metric = Bytes()
    _install(metric)

    timeline = build_timeline(
        [
            LogEntry(raw="a", timestamp=BASE, fields={"bytes": "100"}),
            LogEntry(raw="no stamp", fields={"bytes": "999"}),
        ],
        width=1,
    )

    assert timeline.undated == 1
    assert timeline.total == 1
    assert timeline.buckets[0].value == 100.0
    # Not merely excluded from the sum: never asked. An entry with no place on
    # the axis has no bucket to contribute to, and asking would invite a plugin
    # to believe otherwise.
    assert metric.calls == 1


def test_a_metric_survives_a_tail() -> None:
    """The test the fold constraint exists for.

    Build over a hundred lines, tail ten more into the grid by arithmetic, and
    assert the result is the one a full rebuild would have produced — buckets,
    values and all. A metric that CLV did not sum itself could not pass this.
    """

    _install(Bytes())
    first = _entries(100, step=1, bytes="10")
    # Inside the grid, which is the case `extend` exists for: an arrival past
    # the last bucket is a rebuild by design and says so by returning None.
    tailed = _entries(10, step=1, start=5, bytes="10")

    grid = build_timeline(first, width=40)
    folded = grid.extend(tailed)
    rebuilt = build_timeline(first + tailed, width=40)

    assert folded is not None
    assert folded.buckets == rebuilt.buckets
    assert sum(bucket.value for bucket in folded.buckets) == 1100.0
    assert sum(bucket.count for bucket in folded.buckets) == 110


def test_an_undated_arrival_is_folded_into_undated_and_measured_by_nothing() -> None:
    _install(Bytes())
    grid = build_timeline(_entries(10, bytes="5"), width=5)

    folded = grid.extend([LogEntry(raw="no stamp", fields={"bytes": "999"})])

    assert folded is not None
    assert folded.undated == 1
    assert sum(bucket.value for bucket in folded.buckets) == 50.0


def test_a_metric_installed_after_a_grid_was_built_does_not_change_it() -> None:
    """`metric` rides on the grid, exactly as `moment_of` does.

    Otherwise an arrival tailed after a metric was switched on would be summed
    under one rule into buckets summed under another, and the bar would be a
    blend of two questions with nothing to say so.
    """

    grid = build_timeline(_entries(10, bytes="5"), width=5)
    _install(Bytes())

    folded = grid.extend(_entries(1, start=5, bytes="5"))

    assert folded is not None
    assert folded.metric is None
    assert all(bucket.value == 0.0 for bucket in folded.buckets)


# --- one metric at a time ---------------------------------------------------


def test_the_higher_priority_metric_wins_and_the_conflict_is_reported() -> None:
    winner, loser = Bytes(), Ones()
    winner.priority = 10
    loser.priority = 90

    registry, stack = _install(winner, loser)

    assert stack.metric is not None
    assert stack.metric.plugin == "bytes-metric"
    notes = [error for error in registry.errors if error.category == "conflict"]
    assert len(notes) == 1
    assert notes[0].origin == "ones"
    assert "bytes-metric has priority" in notes[0].message


def test_losing_the_tie_break_is_not_a_fault() -> None:
    """The loser is loaded, healthy, and one switch away from being the one.

    Marking it `failed` would conflate a conflict with a defect — and a module
    shipping a losing metric beside a working annotation would be reported as
    broken when nothing about it is.
    """

    winner, loser = Bytes(), Ones()
    winner.priority = 10
    loser.priority = 90

    registry, _ = _install(winner, loser)
    rows = {row.name: row for row in registry.status()}

    assert rows["ones.py"].state == "loaded"
    assert "bytes-metric has priority" in rows["ones.py"].detail
    assert registry.is_disabled(loser) is False


def test_switching_the_winner_off_promotes_the_runner_up() -> None:
    """The election is re-run per stack rebuild, so the note has to go with it."""

    winner, loser = Bytes(), Ones()
    winner.priority = 10
    loser.priority = 90
    registry, stack = _install(winner, loser)
    assert stack.metric.plugin == "bytes-metric"

    registry.disable(winner, "off", record=False)
    stack = _reinstall(registry)

    assert stack.metric is not None
    assert stack.metric.plugin == "ones"
    # And the stale note is gone: a plugin whose marks are on the bar must not
    # still be described as not in use.
    assert [error for error in registry.errors if error.category == "conflict"] == []


def test_a_metric_with_no_name_is_refused_at_load() -> None:
    """A caption that cannot name the metric cannot say what the bar shows."""

    class Nameless(TimelineMetric):
        name = "nameless"

        def value(self, entry):
            return 1.0

    registry = PluginRegistry()

    assert not registry.add(
        Nameless(), origin="/tmp/nameless.py", clv_version=__version__
    )
    assert "metric_name" in str(registry.errors[0])


# --- the metric's guard -----------------------------------------------------


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        ("12", "must return a number"),
        (True, "must return a number"),
        (float("inf"), "finite"),
        (float("nan"), "finite"),
    ],
)
def test_a_metric_returning_something_unusable_is_taken_out_of_service(
    returned, expected: str
) -> None:
    """Each one makes the *scale* meaningless rather than one bucket wrong."""

    class Bad(TimelineMetric):
        name = "bad-metric"
        metric_name = "bad"

        def value(self, entry):
            return returned

    plugin = Bad()
    registry, _ = _install(plugin)

    timeline = build_timeline(_entries(3), width=2)

    assert registry.is_disabled(plugin)
    assert expected in registry.disabled_reason(plugin)
    assert all(bucket.value == 0.0 for bucket in timeline.buckets)
    # And the bar is still a bar: counts are untouched by a metric that failed.
    assert timeline.total == 3


def test_a_metric_that_raises_is_disabled_and_the_bar_keeps_counting() -> None:
    class Boom(TimelineMetric):
        name = "boom-metric"
        metric_name = "boom"

        def value(self, entry):
            raise RuntimeError("no")

    plugin = Boom()
    registry, _ = _install(plugin)

    timeline = build_timeline(_entries(4), width=2)

    assert registry.is_disabled(plugin)
    assert "raised: no" in registry.disabled_reason(plugin)
    assert timeline.total == 4


# --- annotations ------------------------------------------------------------


def test_an_annotation_lands_in_the_bucket_its_moment_falls_in() -> None:
    moment = BASE + timedelta(seconds=30)
    _install(Deploys((moment, "deploy api v4.2", "notice")))

    timeline = build_timeline(_entries(60), width=6)

    assert len(timeline.annotations) == 1
    mark = timeline.annotations[0]
    bucket = timeline.buckets[mark.index]
    assert bucket.start <= moment < bucket.end
    assert mark.label == "deploy api v4.2"
    assert mark.level == "NOTICE"


def test_an_annotation_outside_the_window_is_not_drawn() -> None:
    """Returning it is harmless; keeping it would not be."""

    _install(
        Deploys(
            (BASE - timedelta(hours=1), "yesterday", None),
            (BASE + timedelta(seconds=10), "during", None),
            (BASE + timedelta(hours=1), "tomorrow", None),
        )
    )

    timeline = build_timeline(_entries(60), width=6)

    assert [mark.label for mark in timeline.annotations] == ["during"]


def test_the_annotation_label_reaches_the_caption() -> None:
    _install(Deploys((BASE, "deploy api v4.2", "notice")))

    timeline = build_timeline(_entries(10), width=5)

    assert "deploy api v4.2" in describe_bucket(timeline, 0)


def test_several_marks_in_one_bucket_caption_as_one_plus_a_count() -> None:
    """A bucket is one cell wide and the caption is one row."""

    _install(
        Deploys(
            (BASE, "first", None),
            (BASE, "second", None),
            (BASE, "third", None),
        )
    )

    timeline = build_timeline(_entries(2), width=1)
    caption = describe_bucket(timeline, 0)

    assert "first (+2 more)" in caption
    assert "second" not in caption


def test_the_worst_level_colours_a_bucket_with_several_marks() -> None:
    """The rule the buckets themselves already follow, applied to their marks."""

    _install(
        Deploys(
            (BASE, "maintenance", None),
            (BASE, "incident", "error"),
            (BASE, "deploy", "notice"),
        )
    )

    timeline = build_timeline(_entries(2), width=1)

    assert annotation_levels(timeline) == {0: "ERROR"}


def test_annotations_are_carried_through_a_tail_rather_than_refetched() -> None:
    """The grid is the one they were placed on, so every index is still right."""

    provider = Deploys((BASE + timedelta(seconds=2), "deploy", None))
    _install(provider)
    grid = build_timeline(_entries(10), width=5)
    assert provider.calls == 1

    folded = grid.extend(_entries(1, start=5))

    assert folded is not None
    assert folded.annotations == grid.annotations
    assert provider.calls == 1


def test_a_provider_is_asked_once_per_window_not_once_per_rebuild() -> None:
    """A rebuild is a keystroke in the query box; a window is not."""

    provider = Deploys((BASE, "deploy", None))
    _install(provider)
    entries = _entries(30)

    build_timeline(entries, width=10)
    build_timeline(entries, width=10)
    build_timeline(entries, width=10)

    assert provider.calls == 1


def test_a_different_window_is_a_different_question() -> None:
    provider = Deploys((BASE, "deploy", None))
    _install(provider)

    build_timeline(_entries(30), width=10)
    build_timeline(_entries(300), width=10)

    assert provider.calls == 2


def test_installing_again_invalidates_the_cache() -> None:
    """What a generation bump reaches this module as.

    A cached mark is a mark from a provider that may have just been switched
    off — a wrong answer rather than a stale one, which is why the install owns
    the clear rather than the caller.
    """

    provider = Deploys((BASE, "deploy", None))
    registry, _ = _install(provider)
    entries = _entries(30)
    build_timeline(entries, width=10)
    assert provider.calls == 1

    _reinstall(registry)
    build_timeline(entries, width=10)

    assert provider.calls == 2


def test_a_provider_switched_off_stops_marking_the_bar() -> None:
    provider = Deploys((BASE, "deploy", None))
    registry, _ = _install(provider)
    assert build_timeline(_entries(30), width=10).annotations

    registry.disable(provider, "off", record=False)
    _reinstall(registry)

    assert build_timeline(_entries(30), width=10).annotations == ()


def test_the_provider_is_asked_for_the_window_the_bar_is_showing() -> None:
    provider = Deploys()
    _install(provider)

    timeline = build_timeline(_entries(60), width=6)

    window = provider.windows[0]
    assert window.start == timeline.buckets[0].start
    assert window.end == timeline.buckets[-1].end


def test_at_most_max_annotations_are_kept() -> None:
    _install(
        Deploys(
            *(
                (BASE + timedelta(seconds=index % 50), f"mark {index}", None)
                for index in range(MAX_ANNOTATIONS + 50)
            )
        )
    )

    timeline = build_timeline(_entries(60), width=6)

    assert len(timeline.annotations) == MAX_ANNOTATIONS


# --- the annotation guard ---------------------------------------------------


def test_a_provider_that_raises_is_disabled_and_the_bar_still_draws() -> None:
    class Boom(TimelineAnnotation):
        name = "boom-marks"

        def annotations(self, window):
            raise RuntimeError("no")

    plugin = Boom()
    registry, _ = _install(plugin)

    timeline = build_timeline(_entries(10), width=5)

    assert registry.is_disabled(plugin)
    assert "raised: no" in registry.disabled_reason(plugin)
    assert timeline.annotations == ()
    assert timeline.total == 10


def test_a_generator_that_raises_part_way_through_is_caught() -> None:
    """The documented shape is a generator, and a generator raises while walked."""

    class HalfWay(TimelineAnnotation):
        name = "half-way"

        def annotations(self, window):
            yield (BASE, "first", None)
            raise RuntimeError("mid-walk")

    plugin = HalfWay()
    registry, _ = _install(plugin)

    timeline = build_timeline(_entries(10), width=5)

    assert registry.is_disabled(plugin)
    assert timeline.annotations == ()


@pytest.mark.parametrize(
    ("produced", "expected"),
    [
        (["not a tuple"], "must be a (moment, label, level) tuple"),
        ([(BASE, "label")], "must be a (moment, label, level) tuple"),
        ([("not a moment", "label", None)], "is not (datetime, str, level)"),
        ([(BASE, 42, None)], "is not (datetime, str, level)"),
    ],
)
def test_a_provider_answering_in_the_wrong_shape_goes_out_of_service(
    produced, expected: str
) -> None:
    """All or nothing per call, and the docstring says why: a provider that
    cannot say what shape its marks are is one whose good marks cannot be
    trusted to mean what their labels say."""

    class Wrong(TimelineAnnotation):
        name = "wrong-shape"

        def annotations(self, window):
            return produced

    plugin = Wrong()
    registry, _ = _install(plugin)

    timeline = build_timeline(_entries(10), width=5)

    assert registry.is_disabled(plugin)
    assert expected in registry.disabled_reason(plugin)
    assert timeline.annotations == ()


def test_a_good_mark_beside_a_bad_one_is_dropped_with_it() -> None:
    class Mixed(TimelineAnnotation):
        name = "mixed"

        def annotations(self, window):
            return [(BASE, "good", None), "rubbish"]

    registry, _ = _install(Mixed())

    assert build_timeline(_entries(10), width=5).annotations == ()


def test_an_unrecognised_level_becomes_none_rather_than_an_invented_severity() -> None:
    _install(Deploys((BASE, "deploy", "not-a-level")))

    timeline = build_timeline(_entries(10), width=5)

    assert timeline.annotations[0].level is None


# --- keying a mark the way the entries were keyed ---------------------------


def test_an_aware_mark_lands_on_a_naive_grid() -> None:
    """The offset comes off, exactly as it does for an entry on a naive grid."""

    aware = (BASE + timedelta(seconds=5)).replace(tzinfo=timezone.utc)
    _install(Deploys((aware, "deploy", None)))

    timeline = build_timeline(_entries(60), width=6)

    assert timeline.naive is True
    assert len(timeline.annotations) == 1
    assert timeline.annotations[0].index == timeline.index_of(BASE + timedelta(seconds=5))


def test_a_naive_mark_lands_on_an_aware_grid() -> None:
    """Read in the grid's own zone rather than refused — compare, do not refuse.

    A provider parsing local timestamps out of a config file is the likely real
    case, and a grid that happened to be aware would otherwise raise on it.
    """

    aware_entries = [
        LogEntry(raw=f"line {index}", timestamp=(BASE + timedelta(seconds=index)).replace(tzinfo=timezone.utc))
        for index in range(60)
    ]
    _install(Deploys((BASE + timedelta(seconds=5), "deploy", None)))

    timeline = build_timeline(aware_entries, width=6)

    assert timeline.naive is False
    assert len(timeline.annotations) == 1


# --- the budget -------------------------------------------------------------


def test_a_slow_metric_strikes_out_and_is_disabled() -> None:
    """The sixth budget, same policy as the other five."""

    class Slow(TimelineMetric):
        name = "slow-metric"
        metric_name = "slow"

        def value(self, entry):
            return 1.0

    plugin = Slow()
    registry = PluginRegistry()
    assert registry.add(plugin, origin="/tmp/slow.py", clv_version=__version__)
    registry.order()
    budget = PluginBudget(registry, limit_ms=0.0001, label="timeline")
    stack = registry.timeline_stack(budget=budget)
    install_timeline_plugins(stack.annotators, stack.metric)

    for _ in range(3):
        stack.start()
        build_timeline(_entries(200), width=20)
        stack.settle()

    assert registry.is_disabled(plugin)
    assert "over the timeline budget" in registry.disabled_reason(plugin)


def test_a_provider_is_charged_only_on_the_pass_it_actually_fetched_in() -> None:
    """A cached answer costs nothing, so it strikes at nothing."""

    provider = Deploys((BASE, "deploy", None))
    registry = PluginRegistry()
    assert registry.add(provider, origin="/tmp/deploys.py", clv_version=__version__)
    registry.order()
    budget = PluginBudget(registry, limit_ms=1000.0, label="timeline")
    stack = registry.timeline_stack(budget=budget)
    install_timeline_plugins(stack.annotators, stack.metric)
    entries = _entries(30)

    for _ in range(5):
        stack.start()
        build_timeline(entries, width=10)
        stack.settle()

    assert provider.calls == 1
    assert not registry.is_disabled(provider)


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (0.0, "", "0"),
        (61.0, "", "61"),
        (1449984.0, "B", "1.4 MB"),
        (2500.0, "", "2.5k"),
        (1_500_000_000.0, "B", "1.5 GB"),
        (0.75, "s", "0.75 s"),
        (12345.0, "B", "12.3 kB"),
    ],
)
def test_a_metric_total_is_short_enough_for_a_caption(
    value: float, unit: str, expected: str
) -> None:
    assert format_metric(value, unit) == expected


# --- the worked example -----------------------------------------------------
#
# Seeded into the operator's plugin directory from this module's own source, so
# it cannot rot into something that no longer loads while still being handed out.


def test_the_worked_example_loads_and_works() -> None:
    from clv.examples.timeline_marks import BytesPerBucket, DeployMarks

    registry, stack = _install(BytesPerBucket(), DeployMarks())

    assert sorted(tuple(row.kinds) for row in registry.loaded) == [
        ("metric",),
        ("timeline",),
    ]
    assert stack.metric is not None
    assert stack.metric.metric_name == "bytes"

    timeline = build_timeline([LogEntry(raw="12345678", timestamp=BASE)], width=1)

    # No size_field configured, so it measures the line: eight bytes of raw.
    assert timeline.buckets[0].value == 8.0
    assert "8 B bytes" in describe_bucket(timeline, 0)


def test_the_worked_examples_provider_is_inert_until_configured(monkeypatch) -> None:
    """Consent, applied to a seam whose cost of guessing is a bar of noise.

    Loaded the way an operator loads it rather than constructed here, because
    half of what is under test is that the section name the example documents —
    ``[plugin:timeline_marks]`` — is the one that actually reaches it. A test
    that handed the settings over directly would pass with the name wrong.
    """

    _reinstall(_installed_example(monkeypatch))
    assert build_timeline(_entries(60), width=6).annotations == ()

    _reinstall(
        _installed_example(
            monkeypatch,
            settings={
                "timeline_marks": {
                    "deploys": "2026-08-07 09:00:30",
                    "deploy_labels": "api v4.2",
                }
            },
        )
    )
    marks = build_timeline(_entries(60), width=6).annotations

    assert [mark.label for mark in marks] == ["api v4.2"]
    assert marks[0].level == "NOTICE"


def test_the_worked_examples_metric_reads_the_field_it_is_pointed_at(
    monkeypatch,
) -> None:
    _reinstall(
        _installed_example(
            monkeypatch, settings={"timeline_marks": {"size_field": "bytes"}}
        )
    )

    timeline = build_timeline(
        [
            LogEntry(raw="a", timestamp=BASE, fields={"bytes": "600"}),
            # No size on this line: counted, measured by nothing, and not a
            # reason to take the plugin out of service.
            LogEntry(raw="b", timestamp=BASE),
            LogEntry(raw="c", timestamp=BASE, fields={"bytes": "not a number"}),
        ],
        width=1,
    )

    assert timeline.buckets[0].count == 3
    assert timeline.buckets[0].value == 600.0


def test_the_worked_example_is_what_gets_seeded() -> None:
    """The file an operator finds is this module, not a copy of it."""

    from clv.services.config import SEEDED_EXAMPLES, ensure_user_plugin_dir

    assert SEEDED_EXAMPLES["timeline_marks.py"] == "clv.examples.timeline_marks"
    seeded = ensure_user_plugin_dir() / "examples" / "timeline_marks.py"

    assert "class DeployMarks(TimelineAnnotation)" in seeded.read_text(encoding="utf-8")


def _installed_example(monkeypatch, settings=None) -> PluginRegistry:
    """Copy the seeded example up a level and load it, as an operator would."""

    from clv.plugins import PLUGIN_PATH_ENV, load_plugins
    from clv.services.config import ensure_user_plugin_dir

    root = ensure_user_plugin_dir()
    (root / "timeline_marks.py").write_text(
        (root / "examples" / "timeline_marks.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(root))
    return load_plugins(enabled=["timeline_marks"], settings=settings)


def test_the_example_loads_the_way_an_operator_installs_it(monkeypatch) -> None:
    """The whole install path, end to end: copy it up, name it, run it.

    Requirement 1 for this seam. ``load_plugins`` is what an operator's copy
    reaches, and it is the one caller of ``add()`` this file does not otherwise
    exercise — a metric that passed ``_metric_fault`` in a unit test but broke
    the namespace scan would still be a plugin that does not work.
    """

    registry = _installed_example(monkeypatch)

    assert not [str(error) for error in registry.errors]
    assert [plugin.name for plugin in registry.annotators] == ["deploy-marks"]
    assert [plugin.name for plugin in registry.metrics] == ["bytes-metric"]


# --- the bar ----------------------------------------------------------------


def _log_file(tmp_path: Path, count: int = 120, step: int = 5) -> Path:
    path = tmp_path / "app.log"
    lines = []
    for index in range(count):
        stamp = (BASE + timedelta(seconds=index * step)).strftime("%Y-%m-%d %H:%M:%S")
        level = "ERROR" if index % 30 == 0 else "INFO"
        lines.append(f"{stamp} - {level} - request {index} handled")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _attach(app: LogViewerApp, *plugins) -> PluginRegistry:
    """Give a running app a registry holding *plugins*, wired as at mount."""

    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
        ), [str(error) for error in registry.errors]
    registry.order()
    app._plugins = registry
    app._timeline_budget = app._new_timeline_budget()
    app._install_timeline_plugins()
    return registry


async def _open(pilot, app: LogViewerApp, tmp_path: Path, **kwargs) -> None:
    await pilot.pause()
    app._select_source(_log_file(tmp_path, **kwargs), announce=False)
    app.set_focus(app.log_panel)
    await pilot.pause()


def _bar_segments(app: LogViewerApp):
    return list(app.timeline_bar._bar_strip(app.timeline_bar.size.width))


def test_a_marked_bucket_is_underlined_and_keeps_its_glyph(tmp_path: Path) -> None:
    """Requirement 11: the mark is a style, so the layout cannot change.

    And the volume survives it — replacing the glyph would lose the height
    exactly where something interesting happened.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            _attach(app, Deploys((BASE + timedelta(seconds=150), "deploy", "error")))
            await pilot.press("b")
            await pilot.pause()

            marked = annotation_levels(app._timeline)
            assert marked, "the provider's mark should be on this grid"
            index = next(iter(marked))
            segments = _bar_segments(app)

            assert segments[index].style.underline is True
            assert segments[index].text in BLOCKS + ("·",)
            # Its neighbours are untouched, so this is a mark and not a mode.
            assert not any(
                segment.style.underline
                for position, segment in enumerate(segments)
                if position != index and position < len(app._timeline.buckets)
            )

    asyncio.run(scenario())


def test_shift_arrows_step_between_marks(tmp_path: Path) -> None:
    """Plain arrows still mean one bucket; shift means the next thing marked."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            _attach(
                app,
                Deploys(
                    (BASE + timedelta(seconds=30), "first", None),
                    (BASE + timedelta(seconds=450), "second", None),
                ),
            )
            await pilot.press("b")
            await pilot.pause()

            marked = sorted(annotation_levels(app._timeline))
            assert len(marked) == 2, marked

            await pilot.press("home")
            await pilot.pause()
            assert app.timeline_bar.selected == 0

            # Strictly forward, so the expected stop is the first mark past the
            # selection rather than the first mark — which are the same bucket
            # unless a mark happens to land on bucket zero.
            ahead = [index for index in marked if index > 0]
            await pilot.press("shift+right")
            await pilot.pause()
            assert app.timeline_bar.selected == ahead[0]

            if len(ahead) > 1:
                await pilot.press("shift+right")
                await pilot.pause()
            assert app.timeline_bar.selected == marked[-1]

            # No wrap, like the bucket keys beside it.
            await pilot.press("shift+right")
            await pilot.pause()
            assert app.timeline_bar.selected == marked[-1]

            await pilot.press("shift+left")
            await pilot.pause()
            assert app.timeline_bar.selected == marked[-2]

    asyncio.run(scenario())


def test_shift_arrows_do_nothing_with_no_marks(tmp_path: Path) -> None:
    """A key documented in the overlay that no-ops is not a key that misbehaves."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.press("home")
            await pilot.pause()

            await pilot.press("shift+right")
            await pilot.pause()

            assert app.timeline_bar.selected == 0

    asyncio.run(scenario())


def test_a_metric_survives_tailing_through_the_app(tmp_path: Path) -> None:
    """The gate, end to end: the `extend` path with a plugin metric on it."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            _attach(app, Ones())
            await pilot.press("b")
            await pilot.pause()

            before = sum(bucket.value for bucket in app._timeline.buckets)
            assert before == app._timeline.total

            app._extend_timeline(
                [LogEntry(raw="tailed", timestamp=BASE + timedelta(seconds=10))]
            )

            after = sum(bucket.value for bucket in app._timeline.buckets)
            assert after == before + 1
            assert after == app._timeline.total

    asyncio.run(scenario())


def test_the_bar_is_scaled_by_the_metric(tmp_path: Path) -> None:
    """One tall bucket by volume, a different one by bytes."""

    class Fixed(TimelineMetric):
        name = "fixed-metric"
        metric_name = "weight"

        def value(self, entry):
            # Every line in the first half of the window weighs nothing; every
            # line after it weighs a thousand. The count is flat, so the two
            # bars cannot coincide by accident.
            assert entry.timestamp is not None
            return 1000.0 if entry.timestamp >= BASE + timedelta(seconds=300) else 0.0

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.pause()
            counted = [segment.text for segment in _bar_segments(app)]

            _attach(app, Fixed())
            app._render_log()
            await pilot.pause()
            measured = [segment.text for segment in _bar_segments(app)]

            assert counted != measured
            # The empty glyph still means "no events", not "measured zero".
            buckets = app._timeline.buckets
            quiet = next(
                index
                for index, bucket in enumerate(buckets)
                if bucket.count and bucket.value == 0.0
            )
            assert measured[quiet] in BLOCKS

    asyncio.run(scenario())


def test_the_caption_says_what_the_bar_is_showing(tmp_path: Path) -> None:
    """Both captions: the one under a selection and the one before there is one."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            _attach(app, Ones())
            await pilot.press("b")
            await pilot.pause()

            assert "ones (ones)" in app.timeline_bar._caption()

            await pilot.press("home")
            await pilot.pause()

            assert "ones (ones)" in app.timeline_bar._caption()

    asyncio.run(scenario())


def test_switching_a_metric_off_mid_session_puts_the_counts_back(tmp_path: Path) -> None:
    """The generation path end to end, for both halves of the seam."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            metric = Ones()
            registry = _attach(app, metric)
            await pilot.press("b")
            await pilot.pause()
            assert app._timeline.metric is not None

            registry.disable(metric, "off", record=False)
            # Nothing else: the app notices the generation on the next render.
            app._render_log()
            await pilot.pause()

            assert app._timeline.metric is None
            assert all(bucket.value == 0.0 for bucket in app._timeline.buckets)
            assert "ones" not in app.timeline_bar._caption()

    asyncio.run(scenario())


def test_the_help_overlay_lists_the_annotation_keys(tmp_path: Path) -> None:
    """Bound on the widget, so the overlay has to be handed them explicitly.

    Read off the rendered widgets rather than off the compositor, which is what
    this asserted before help grew pages. A paint assertion now answers "is
    this row inside the current scroll offset of the current page", and these
    rows sit in the middle of Navigation — so it would fail for a reason that
    has nothing to do with whether the binding reached help. The sibling suite
    in `tests/test_help_overlay.py` reads the same way.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            rendered = "\n".join(
                str(static.content) for static in overlay.query(Static).results()
            )

            assert "annotation" in rendered.lower()

    asyncio.run(scenario())
