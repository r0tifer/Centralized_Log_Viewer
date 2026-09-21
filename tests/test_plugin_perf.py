"""What a plugin costs the pane, and what the pane stops paying twice.

Two things land here, and they are two halves of one argument. The staged view
is memoised, so a render stops running every filter stage over every buffered
line twice; and every stage is timed, so one that is slow anyway is named and
taken out of service rather than left to look like CLV being broken.

The benchmarks at the bottom are deliberately loose ceilings, in the shape
``tests/test_clustering.py`` established: the point is to catch an
order-of-magnitude regression, not to measure this machine. A tight budget on a
shared CI box is a flaky test, and a flaky test gets deleted.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import (
    FilterStage,
    LogFormat,
    PluginBudget,
    PluginRegistry,
    QueryOperator,
)
from clv.services.clustering import normalise
from clv.services.filtering import FilterSpec, filter_entries
from clv.services.parsing import LogParser, parse_lines
from clv.services.query import NORMALISED_FIELD_KEYS, install_query_plugins
from clv.services.session import SourceBuffer
from clv.storage import SessionState
from clv.widgets.log_view import LogView


class Counting(FilterStage):
    """Passes everything through, and says how many entries it saw."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def apply(self, entry, context):
        self.calls += 1
        return entry


class Shouty(FilterStage):
    """Rewrites the line, so a cached result can be told from a fresh one."""

    name = "shouty"

    def __init__(self, suffix: str = "!") -> None:
        self.suffix = suffix

    def apply(self, entry, context):
        from dataclasses import replace

        return replace(entry, raw=entry.raw + self.suffix)


def _make_app(lines=("alpha", "beta", "gamma"), **state) -> LogViewerApp:
    app = LogViewerApp()
    app.log_panel = MagicMock(spec=LogView)
    app.log_panel.cursor_entry = None
    app.log_panel.cursor = -1
    app.state = SessionState(auto_scroll=False, **state)
    app._selected_source = Path("/tmp/example.log")
    app._entries = deque(parse_lines(list(lines)))
    return app


def _install(app, *stages):
    for stage in stages:
        app._plugins.add(stage, origin="test", clv_version=__version__)
    return stages[0] if len(stages) == 1 else stages


# --- the cache --------------------------------------------------------------


def test_the_same_state_twice_runs_the_stages_once() -> None:
    """The double call this phase exists to remove."""

    app = _make_app()
    stage = _install(app, Counting())

    app._visible_view()
    app._visible_view()

    assert stage.calls == 3, "three entries, one pass"


def test_a_render_and_the_status_line_share_one_pass() -> None:
    """The two callers that used to run every stage over the whole buffer."""

    app = _make_app()
    stage = _install(app, Counting())

    app._render_log()
    app._visible_view()

    assert stage.calls == 3


def test_a_changed_query_invalidates_the_cache() -> None:
    app = _make_app()
    stage = _install(app, Counting())

    app._visible_view()
    app.state.query = "alpha"
    app._visible_view()

    assert stage.calls == 6


def test_replacing_the_buffer_invalidates_the_cache() -> None:
    """`set_entries` is how the whole suite seeds a pane; it has to count."""

    app = _make_app()
    stage = _install(app, Counting())

    app._visible_view()
    app._entries = deque(parse_lines(["one", "two", "three"]))
    result = app._visible_view()

    assert stage.calls == 6
    assert [entry.raw for entry in result.entries] == ["one", "two", "three"]


def test_a_tailed_line_invalidates_the_cache() -> None:
    """Same length would not have been enough; the revision is what catches it."""

    app = _make_app()
    stage = _install(app, Counting())

    app._visible_view()
    app._session.primary.entries.append(parse_lines(["delta"])[0])
    app._session.primary.revision += 1
    result = app._visible_view()

    assert stage.calls == 7
    assert len(result.entries) == 4


def test_a_buffer_mutated_in_place_still_invalidates_by_length() -> None:
    """The hole `revision` alone leaves, closed by the line count.

    Reaching past the session to extend a buffer's deque is not supported, and
    parts of this project's own suite do it anyway.
    """

    app = _make_app()
    stage = _install(app, Counting())

    app._visible_view()
    app._session.primary.entries.extend(parse_lines(["delta", "epsilon"]))
    result = app._visible_view()

    assert stage.calls == 8
    assert len(result.entries) == 5


def test_disabling_a_plugin_invalidates_the_cache() -> None:
    app = _make_app()
    stage = _install(app, Shouty())

    before = app._visible_view()
    assert [entry.raw for entry in before.entries] == ["alpha!", "beta!", "gamma!"]

    app._plugins.disable(stage, "switched off", record=False)
    after = app._visible_view()

    assert [entry.raw for entry in after.entries] == ["alpha", "beta", "gamma"]


def test_re_enabling_a_plugin_invalidates_the_cache() -> None:
    app = _make_app()
    stage = _install(app, Shouty())
    app._plugins.disable(stage, "switched off", record=False)
    app._visible_view()

    app._plugins.enable(stage)
    result = app._visible_view()

    assert [entry.raw for entry in result.entries] == ["alpha!", "beta!", "gamma!"]


def test_a_re_read_settings_section_invalidates_the_cache() -> None:
    """A plugin reads through a live mapping, so nothing else would notice."""

    class Configurable(FilterStage):
        name = "configurable"

        def configure(self, settings):
            self.settings = settings

        def apply(self, entry, context):
            from dataclasses import replace

            return replace(entry, raw=entry.raw + self.settings.get("suffix", ""))

    app = _make_app()
    # `add()` calls `configure()` itself, with the view for the section named
    # after the origin -- "test" here, `redact_secrets` for a real user plugin.
    _install(app, Configurable())

    assert [e.raw for e in app._visible_view().entries] == ["alpha", "beta", "gamma"]

    app._plugins.refresh_settings({"test": {"suffix": "-x"}})

    assert [e.raw for e in app._visible_view().entries] == [
        "alpha-x", "beta-x", "gamma-x"
    ]


def test_an_invalid_query_is_not_cached() -> None:
    from clv.services.filtering import QueryError

    app = _make_app()
    _install(app, Counting())
    app.state.query = "["

    for _ in range(2):
        with pytest.raises(QueryError):
            app._visible_view()

    app.state.query = ""
    assert len(app._visible_view().entries) == 3


# --- the generation counter and the shape cache -----------------------------


def test_re_enabling_a_plugin_clears_its_strikes_on_every_budget() -> None:
    """**Re-enable** promises a clean count, and it has to mean every ceiling.

    The re-enable path was written when the app held two budgets and named both
    by hand. Four seam phases have added one each since, none of them here — so
    a query operator, a watch matcher, a shape contributor or a timeline metric
    that had struck once or twice on its own ceiling and was then taken out of
    service for another reason came back still carrying those strikes, and one
    slow pass took it straight out again.

    Driven off the app's own budgets rather than a list written here, so the
    seventh cannot be missed the same way.
    """

    app = LogViewerApp()
    budgets = [value for value in vars(app).values() if isinstance(value, PluginBudget)]
    assert len(budgets) == 6, "a budget was added; `_forget_budgets` needs it too"
    plugin = Counting()

    # Two strikes each: one short of the three that disable.
    for budget in budgets:
        for _ in range(2):
            budget.start()
            budget.charge(plugin, 10.0)
            budget.settle()
    assert not app._plugins.is_disabled(plugin)

    app._forget_budgets(plugin)

    for budget in budgets:
        budget.start()
        budget.charge(plugin, 10.0)
        budget.settle()

    assert not app._plugins.is_disabled(plugin), (
        "a strike survived Re-enable on at least one budget"
    )


def test_a_plugin_change_clears_the_clustering_shape_cache() -> None:
    """The assertion `PLUGIN_TODO.md` Phase 10 rests on.

    `normalise` is an ``lru_cache`` on a module-level function, so there is no
    instance to key on a generation. Phase 10 feeds it plugin-supplied rules,
    and a cache left alone across an enable would fold entries by the old rules
    -- a wrong answer, not a stale one.
    """

    app = _make_app()
    stage = _install(app, Counting())
    normalise("connection refused for 10.0.0.5:5432")
    assert normalise.cache_info().currsize > 0

    app._plugins.disable(stage, "switched off", record=False)
    app._visible_view()

    assert normalise.cache_info().currsize == 0


def test_a_render_with_no_plugin_change_leaves_the_shape_cache_alone() -> None:
    """The guard on the guard: clearing it every render would undo clustering."""

    app = _make_app()
    _install(app, Counting())
    app._visible_view()
    normalise("connection refused for 10.0.0.5:5432")

    app.state.query = "alpha"
    app._visible_view()

    assert normalise.cache_info().currsize > 0


# --- the relative time window ----------------------------------------------


def test_a_relative_window_holds_still_across_one_render() -> None:
    """`datetime.now()` per call made the key never repeat -- and the pane and
    the status line filter against windows microseconds apart."""

    app = _make_app(time_window="15m")
    stage = _install(app, Counting())

    app._visible_view()
    app._visible_view()

    assert stage.calls == 3


def test_a_relative_window_re_anchors_when_the_lines_change() -> None:
    app = _make_app(time_window="15m")
    _install(app, Counting())

    first = app._filter_spec().window
    app._visible_view()
    app._entries = deque(parse_lines(["one"]))
    app._visible_view()

    assert app._filter_spec().window.end > first.end


def test_a_relative_window_re_anchors_when_the_operator_changes_it() -> None:
    """Otherwise "the last 15 minutes" would mean 15 minutes before the last
    line arrived, which on an idle log is not what was asked for."""

    app = _make_app(time_window="15m")
    _install(app, Counting())
    app._visible_view()
    first = app._filter_spec().window.end

    app.state.time_window = "6h"
    app._visible_view()

    assert app._filter_spec().window.end > first


# --- correctness under the cache -------------------------------------------


def test_the_cached_result_matches_an_uncached_one_across_every_change() -> None:
    """A cache that is fast and wrong is worse than the double call."""

    from clv.plugins import FilterContext
    from clv.services.filtering import filter_entries

    app = _make_app()
    _install(app, Shouty())

    def uncached():
        spec = app._filter_spec()
        context = FilterContext(spec=spec, source=app._selected_source)
        staged = app._plugins.apply_filters(list(app._entries), context)
        return [entry.raw for entry in filter_entries(staged, spec).entries]

    def tail():
        app._session.primary.entries.extend(parse_lines(["delta", "alpha 2"]))
        app._session.primary.revision += 1

    def rotate():
        buffer = app._session.primary
        buffer.entries.clear()
        buffer.entries.extend(parse_lines(["rotated one", "rotated two"]))
        buffer.revision += 1

    def merge():
        second = SourceBuffer(Path("/tmp/second.log"), max_lines=100, tag_origin=True)
        second.entries.extend(parse_lines(["2026-08-07 09:25:04 - INFO - second"]))
        app._session._buffers.append(second)

    steps = [
        lambda: None,
        lambda: setattr(app.state, "query", "a"),          # a filter change
        tail,                                              # a tail append
        lambda: setattr(app.state, "query", ""),
        rotate,                                            # a rotation
        lambda: app._session.set_entries(deque(parse_lines(["one", "two"]))),
        merge,                                             # a second member
    ]
    for index, step in enumerate(steps):
        step()
        assert [e.raw for e in app._visible_view().entries] == uncached(), (
            f"step {index} disagreed with an uncached pass"
        )


# --- benchmarks -------------------------------------------------------------


@pytest.mark.parametrize("count", [5_000])
def test_the_cache_removes_the_second_pass_over_the_buffer(count) -> None:
    app = _make_app(lines=[f"line {index}" for index in range(count)])
    stage = _install(app, Shouty())

    start = time.perf_counter()
    app._visible_view()
    cold = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(20):
        app._visible_view()
    warm = (time.perf_counter() - start) / 20

    assert warm < cold / 10, (
        f"a repeat view of {count} entries took {warm * 1000:.1f} ms "
        f"against {cold * 1000:.1f} ms cold"
    )


def test_timing_a_no_op_stage_is_a_small_share_of_the_pass() -> None:
    """What the per-call clock reads actually cost, measured rather than claimed."""

    from clv.plugins import FilterContext, PluginBudget
    from clv.services.filtering import FilterSpec

    app = _make_app(lines=[f"line {index}" for index in range(5_000)])
    _install(app, Counting())
    context = FilterContext(spec=FilterSpec(), source=None)
    entries = list(app._entries)

    start = time.perf_counter()
    app._plugins.apply_filters(entries, context)
    untimed = time.perf_counter() - start

    budget = PluginBudget(app._plugins, limit_ms=60_000, label="bench")
    start = time.perf_counter()
    app._plugins.apply_filters(entries, context, budget=budget)
    timed = time.perf_counter() - start

    # Two clock reads for the whole pass, not two per call: the ceiling is a
    # loose multiple of a no-op stage's own cost, which is about as unfavourable
    # a comparison as the measurement can be given.
    assert timed < untimed * 1.5 + 0.001, (
        f"timing 5000 entries cost {timed * 1000:.2f} ms "
        f"against {untimed * 1000:.2f} ms untimed"
    )


def test_a_build_with_no_plugins_pays_nothing_for_any_of_this() -> None:
    """Requirement 10, at the top of the stack rather than in the loader."""

    app = _make_app(lines=[f"line {index}" for index in range(5_000)])

    start = time.perf_counter()
    for _ in range(20):
        app._visible_view()
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"20 plugin-free views took {elapsed:.3f}s"
    assert app._plugins.total == 0


# --- the read path ----------------------------------------------------------
#
# Phase 7a's budget. A `LogFormat.parse` runs once per *line read* rather than
# once per render, so the question here is different from the one above: not
# "does the guard cost less than the work" over a whole buffer, but "does a
# format that is offered a line it does not want get out of the way cheaply".


_UNMATCHED = [f"nothing recognises line {index}" for index in range(5_000)]


class Declining(LogFormat):
    """A format that wants none of these lines, which is the common case."""

    name = "declining"
    format_name = "declining"

    def parse(self, line):
        return None


def test_zero_format_plugins_costs_what_the_parser_cost_before() -> None:
    """Requirement 10 for the read path: `()` is falsy and nothing below runs."""

    start = time.perf_counter()
    LogParser().feed(_UNMATCHED)
    bare = time.perf_counter() - start

    start = time.perf_counter()
    LogParser(formats=()).feed(_UNMATCHED)
    empty = time.perf_counter() - start

    assert empty < bare * 1.5 + 0.005, (
        f"an empty format stack cost {empty * 1000:.2f} ms "
        f"against {bare * 1000:.2f} ms with no parameter at all"
    )


def test_timing_a_format_costs_a_fraction_of_offering_it_the_line() -> None:
    """One clock read per call, not two, and the batch is the pass.

    The render path's finding was that two reads *per call* cost four times what
    a no-op stage costs. This path cannot avoid per-call measurement — a batch
    is a thousand lines and one verdict — so it halves the reads instead, by
    reusing each call's end stamp as the next one's start.
    """

    registry = PluginRegistry()
    registry.add(Declining(), origin="/tmp/declining.py", clv_version="2.9.0")

    start = time.perf_counter()
    LogParser(formats=registry.format_stack()).feed(_UNMATCHED)
    untimed = time.perf_counter() - start

    budget = PluginBudget(registry, limit_ms=60_000, label="bench")
    start = time.perf_counter()
    LogParser(formats=registry.format_stack(budget=budget)).feed(_UNMATCHED)
    timed = time.perf_counter() - start

    assert timed < untimed * 2.0 + 0.005, (
        f"timing 5000 offered lines cost {timed * 1000:.2f} ms "
        f"against {untimed * 1000:.2f} ms untimed"
    )


# --- the query path ---------------------------------------------------------
#
# Phase 8's operators and computed fields are per entry, on the same trigger as
# a filter stage: a keystroke in the query box, over the whole buffer. So they
# owe the same two things the other two paths owe — nothing when nothing is
# installed, and a guard that costs less than the work it guards.

_QUERY_LINES = [
    f"Aug 21 09:25:{index % 60:02d} web{index % 8:02d} sshd[{index}]: connection refused"
    for index in range(5000)
]


class _Matches(QueryOperator):
    name = "bench-operator"
    token = "~"

    def test(self, stored: str, value: str) -> bool:
        return stored.startswith(value)


def test_zero_query_plugins_costs_what_filtering_cost_before() -> None:
    """Requirement 10, on the hot path: an empty registry is two dict tests."""

    entries = parse_lines(_QUERY_LINES)
    install_query_plugins()
    spec = FilterSpec(query="host:web0", known_fields=NORMALISED_FIELD_KEYS)

    # Warm whatever the first pass pays for, then measure two identical passes.
    filter_entries(entries, spec)
    start = time.perf_counter()
    filter_entries(entries, spec)
    bare = time.perf_counter() - start

    registry = PluginRegistry()
    registry.add(_Matches(), origin="/tmp/bench.py", clv_version="2.9.0")
    registry.order()
    stack = registry.query_stack()
    install_query_plugins(stack.operators, stack.computed)
    try:
        start = time.perf_counter()
        filter_entries(entries, spec)
        loaded = time.perf_counter() - start
    finally:
        install_query_plugins()

    # The query does not use the plugin's token, so an installed-but-unused
    # operator must not be charged to a query that never asks for it.
    assert loaded < bare * 1.5 + 0.005, (
        f"a query using no plugin token cost {loaded * 1000:.2f} ms with one "
        f"installed against {bare * 1000:.2f} ms with none"
    )


def test_guarding_an_operator_costs_a_fraction_of_calling_it() -> None:
    """The wrapper is a disabled-check, a try and two clock reads per call."""

    entries = parse_lines(_QUERY_LINES)
    registry = PluginRegistry()
    registry.add(_Matches(), origin="/tmp/bench.py", clv_version="2.9.0")
    registry.order()
    spec = FilterSpec(query="host~web0", known_fields=NORMALISED_FIELD_KEYS)

    try:
        stack = registry.query_stack()
        install_query_plugins(stack.operators, stack.computed)
        filter_entries(entries, spec)
        start = time.perf_counter()
        filter_entries(entries, spec)
        untimed = time.perf_counter() - start

        budget = PluginBudget(registry, limit_ms=60_000, label="bench")
        stack = registry.query_stack(budget=budget)
        install_query_plugins(stack.operators, stack.computed)
        start = time.perf_counter()
        stack.start()
        filter_entries(entries, spec)
        stack.settle()
        timed = time.perf_counter() - start
    finally:
        install_query_plugins()

    assert timed < untimed * 3.0 + 0.005, (
        f"timing 5000 comparisons cost {timed * 1000:.2f} ms "
        f"against {untimed * 1000:.2f} ms untimed"
    )
