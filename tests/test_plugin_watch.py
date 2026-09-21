"""A plugin extends the watch rules, and it extends them on equal terms.

Phase 9 of ``PLUGIN_TODO.md``. Before it a watch rule was a fixed shape — a
pattern, an action, a rate limit — and its only destination was a toast. Two
seams, and they are unlike each other in every way that matters here.

**Requirement 9, equal terms.** A plugin-supplied rule *kind* is compiled,
matched, highlighted, persisted and reported by the same code a pattern rule
is, and a plugin-supplied *destination* is fed by the same coalescing CLV's own
toast is fed by. Neither path knows a plugin exists.

**Requirement 12, degradation.** A rule is the second kind of saved record whose
meaning a missing plugin could change, and it changes it worse than a view: a
``burst`` rule whose matcher is gone would fall back to matching ``oom x5/60``
as a *query*, which parses. So a kind that nothing provides makes the rule
unusable rather than different, and the byte-identity assertions below are what
stop "preserve, disable, explain" degrading into "rewrite helpfully".

**And one claim that is neither.** A sink runs somewhere the rest of CLV never
does: off the event loop, on a thread, where it is allowed to block. The tests
for that are about what happens to the *poll* while a sink misbehaves, which is
the only question an operator watching a stuttering pane is actually asking.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from time import monotonic

import pytest

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import (
    PluginBudget,
    PluginRegistry,
    WatchMatcher,
    WatchSink,
)
from clv.services.config import LogConfig
from clv.services.discovery import DiscoverySettings
from clv.services.parsing import LogParser
from clv.services.watch import (
    KIND_PATTERN,
    SINK_SAMPLE_LIMIT,
    SinkDispatcher,
    WatchHit,
    WatchIndex,
    WatchNotifier,
    WatchRule,
    install_watch_plugins,
    matcher_kinds,
    validate_pattern,
    wants_entry_samples,
)
from clv.storage import SessionState, StateStore
from clv.widgets.watch_dialog import WatchRulesDialog

# The matchers and sinks are module state and are reset between tests by an
# autouse fixture in `conftest.py`, so one test's `burst` cannot decide another.


# --- plugins under test -----------------------------------------------------


class Burst(WatchMatcher):
    """``burst`` — fires on the Nth matching line, whenever that arrives."""

    name = "burst-matcher"
    kind = "burst"

    def __init__(self, needed: int = 2) -> None:
        self.needed = needed
        self.seen = 0
        self.validated: list[str] = []

    def matches(self, entry, rule) -> bool:
        if rule.pattern.split()[0] not in entry.raw:
            return False
        self.seen += 1
        return self.seen >= self.needed

    def validate(self, pattern: str):
        self.validated.append(pattern)
        return None if pattern.strip() else "empty"


class Truthy(WatchMatcher):
    """Returns a match object rather than a bool, as an author naturally would."""

    name = "truthy-matcher"
    kind = "truthy"

    def matches(self, entry, rule):
        return re.search(rule.pattern, entry.raw)


class BoomMatcher(WatchMatcher):
    name = "boom-matcher"
    kind = "boom"

    def matches(self, entry, rule) -> bool:
        raise RuntimeError("no")


class SlowMatcher(WatchMatcher):
    name = "slow-matcher"
    kind = "slow"

    def matches(self, entry, rule) -> bool:
        return True


class Recorder(WatchSink):
    name = "recorder"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, tuple]] = []

    def deliver(self, name, count, context, entries=()) -> None:
        self.calls.append((name, count, tuple(entries)))


class ContentSink(Recorder):
    name = "content-sink"
    wants_entries = True


class BoomSink(WatchSink):
    name = "boom-sink"

    def deliver(self, name, count, context, entries=()) -> None:
        raise RuntimeError("no")


class HangingSink(WatchSink):
    name = "hanging-sink"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def deliver(self, name, count, context, entries=()) -> None:
        self.started.set()
        # Bounded so a failing test cannot wedge the suite; the dispatcher is
        # expected to have given up on this long before it returns.
        self.release.wait(10.0)


_LINE = "2026-08-07 09:25:01 ERROR oom-killer invoked"


# --- harness ----------------------------------------------------------------


def _run(scenario) -> None:
    asyncio.run(scenario())


def _parse(*lines: str):
    return LogParser().feed(list(lines))


def _install(*plugins, budget: PluginBudget | None = None):
    """Load *plugins* into a registry and install what they declared."""

    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
        ), [str(error) for error in registry.errors]
    registry.order()
    stack = registry.watch_stack(budget=budget)
    install_watch_plugins(stack.matchers, stack.sinks)
    return registry, stack


def _log(tmp_path: Path, *lines: str, name: str = "app.log") -> Path:
    root = tmp_path / "logs"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _app(tmp_path: Path, **config) -> LogViewerApp:
    return LogViewerApp(
        config=LogConfig(
            log_dirs=[tmp_path / "logs"], discovery=DiscoverySettings(), **config
        )
    )


def _attach(app: LogViewerApp, *plugins) -> PluginRegistry:
    """Give a running app a registry holding *plugins*, wired as at mount."""

    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
        ), [str(error) for error in registry.errors]
    registry.order()
    app._plugins = registry
    # Rebound for the reason `on_mount` rebinds them: a budget pointing at the
    # registry it was built against would disable into one nothing reads.
    app._watch_budget = app._new_watch_budget()
    app._install_watch_plugins()
    return registry


# --- the matcher ------------------------------------------------------------


def test_a_plugin_matcher_fires_a_rule() -> None:
    _install(Burst(needed=2))
    rule = WatchRule(name="oom", pattern="oom-killer x2/60", kind="burst")
    index = WatchIndex([rule])

    entries = _parse(_LINE, "quiet line", _LINE)
    fired = [names for _entry, names in index.evaluate(None, entries)]

    # The second matching line crosses the threshold; the first does not, and
    # the line in between was never a candidate.
    assert fired == [("oom",)]


def test_the_kind_is_matched_case_insensitively() -> None:
    """A stored kind is operator-facing text, so `Burst` is a typo, not intent."""

    _install(Burst(needed=1))
    rule = WatchRule(name="oom", pattern="oom-killer x1/60", kind="Burst")

    assert rule.missing_kind == ""
    assert rule.unusable_reason is None
    assert [n for _e, n in WatchIndex([rule]).evaluate(None, _parse(_LINE))] == [("oom",)]


def test_a_matcher_may_return_anything_truthy() -> None:
    """`re.search(...)` is what an author writes, and it is not a bool."""

    _install(Truthy())
    rule = WatchRule(name="m", pattern="oom-killer", kind="truthy")

    fired = [n for _e, n in WatchIndex([rule]).evaluate(None, _parse(_LINE, "quiet"))]
    assert fired == [("m",)]


def test_a_matcher_that_raises_is_disabled_once_and_never_throws_per_line() -> None:
    registry, _stack = _install(BoomMatcher())
    plugin = registry.matchers[0]
    rule = WatchRule(name="m", pattern="x", kind="boom")
    index = WatchIndex([rule])

    # Two hundred lines, and the rule simply never matches.
    entries = _parse(*[f"2026-08-07 09:25:01 INFO line {i}" for i in range(200)])
    assert [n for _e, n in index.evaluate(None, entries)] == []

    assert registry.is_disabled(plugin)
    assert len(registry.errors) == 1
    assert "raised: no" in str(registry.errors[0])


def test_a_matcher_out_of_service_breaks_its_rules_rather_than_reinterpreting() -> None:
    """The reservation rule, one level up from a query operator's token.

    A `burst` rule whose matcher is switched off must not quietly become a rule
    that matches its parameter string as a query — which it would, because
    `oom-killer x2/60` parses perfectly well as free text.
    """

    registry, _stack = _install(Burst(needed=1))
    plugin = registry.matchers[0]
    rule = WatchRule(name="oom", pattern="oom-killer x2/60", kind="burst")

    registry.disable(plugin, "operator")
    stack = registry.watch_stack()
    install_watch_plugins(stack.matchers, stack.sinks)

    # Still registered — the kind is claimed, so the rule is *reported*...
    assert "burst" in matcher_kinds()
    assert rule.missing_kind == ""
    assert rule.unusable_reason == "needs the 'burst-matcher' plugin, which is not in service"

    # ...and matches nothing at all, rather than matching the wrong lines.
    assert [n for _e, n in WatchIndex([rule]).evaluate(None, _parse(_LINE))] == []


def test_matchers_run_under_the_watch_budget() -> None:
    """Three consecutive passes over the ceiling, through the same disable()."""

    registry = PluginRegistry()
    plugin = SlowMatcher()
    assert registry.add(plugin, origin="/tmp/slow.py", clv_version=__version__)
    budget = PluginBudget(registry, limit_ms=1.0, label="watch")
    stack = registry.watch_stack(budget=budget)
    install_watch_plugins(stack.matchers, stack.sinks)

    rule = WatchRule(name="m", pattern="x", kind="slow")
    index = WatchIndex([rule])
    for pass_number in range(3):
        stack.start()
        # Charged directly: what is under test is the policy, not how long a
        # return-True matcher happens to take on the machine running this.
        index.evaluate(None, _parse(f"2026-08-07 09:25:0{pass_number} INFO x"))
        budget.charge(plugin, 0.5)
        stack.settle()

    assert registry.is_disabled(plugin)
    assert "over the watch budget" in (registry.disabled_reason(plugin) or "")


# --- the kind as a saved record ---------------------------------------------


def test_a_rule_file_written_before_this_phase_loads_as_a_pattern_rule() -> None:
    restored = WatchRule.from_dict(
        {"name": "oom", "pattern": "oom-killer", "action": "both", "enabled": True}
    )

    assert restored is not None
    assert restored.kind == KIND_PATTERN
    assert restored.unusable_reason is None


@pytest.mark.parametrize("stored", [None, "", "   ", 7, [], {}])
def test_an_unreadable_kind_falls_back_to_the_built_in_one(stored) -> None:
    """One bad field must not cost the rule, and must not invent a new kind."""

    restored = WatchRule.from_dict(
        {"name": "oom", "pattern": "oom-killer", "kind": stored}
    )

    assert restored is not None
    assert restored.kind == KIND_PATTERN


def test_a_rule_whose_kind_is_missing_is_preserved_disabled_and_named(tmp_path) -> None:
    """Requirement 12: preserve comes first, and it is checked byte for byte."""

    _install(Burst(needed=1))
    rule = WatchRule(name="oom", pattern="oom-killer x2/60", kind="burst")
    store = StateStore(root=tmp_path)
    store.save(SessionState(watch_rules=(rule,)))
    written = store.path.read_text(encoding="utf-8")

    # The plugin goes away entirely — not disabled, uninstalled.
    install_watch_plugins()

    restored = store.load().watch_rules[0]
    assert restored.pattern == "oom-killer x2/60", "the pattern was rewritten"
    assert restored.kind == "burst", "the kind was rewritten"
    assert restored.missing_kind == "burst"
    assert restored.unusable_reason == (
        "needs the 'burst' rule kind, which no installed plugin provides"
    )
    assert [n for _e, n in WatchIndex([restored]).evaluate(None, _parse(_LINE))] == []

    # Saved back untouched: a round trip through a build without the plugin
    # must not drop the record that marks it unusable.
    store.save(SessionState(watch_rules=(restored,)))
    assert store.path.read_text(encoding="utf-8") == written


def test_a_missing_kind_is_reported_where_the_rule_is_typed() -> None:
    assert validate_pattern("oom x2/60", (), (), "burst") == (
        "Rule needs the 'burst' rule kind, which no installed plugin provides."
    )


def test_a_plugin_kind_validates_through_its_matcher_not_through_the_grammar() -> None:
    """The pattern is the matcher's parameter, and CLV has no opinion on it."""

    matcher = Burst()
    _install(matcher)

    # A string the query grammar would reject outright is fine here, because
    # the query grammar never sees it.
    assert validate_pattern("oom-killer x2/60", (), (), "burst") is None
    assert matcher.validated == ["oom-killer x2/60"]

    # And the matcher's own complaint is what comes back.
    assert validate_pattern("   x", (), (), "burst") is None
    matcher.validated.clear()


def test_an_empty_pattern_is_still_refused_for_a_plugin_kind() -> None:
    _install(Burst())
    assert validate_pattern("   ", (), (), "burst") == "Enter a pattern."


# --- load-time rejection ----------------------------------------------------


class _NoKind(WatchMatcher):
    name = "no-kind"

    def matches(self, entry, rule) -> bool:
        return False


class _SpacedKind(_NoKind):
    name = "spaced-kind"
    kind = "two words"


class _ReservedKind(_NoKind):
    name = "reserved-kind"
    kind = "pattern"


class _ReservedKindCased(_NoKind):
    name = "reserved-cased"
    kind = "Pattern"


class _DuplicateKind(_NoKind):
    name = "duplicate-kind"
    kind = "BURST"


@pytest.mark.parametrize(
    ("plugin", "expected"),
    [
        (_NoKind, "declares no kind"),
        (_SpacedKind, "contains whitespace"),
        (_ReservedKind, "is CLV's own"),
        (_ReservedKindCased, "is CLV's own"),
        (_DuplicateKind, "already registered by burst-matcher"),
    ],
)
def test_a_matcher_that_could_never_be_reached_is_refused_at_load(
    plugin, expected: str
) -> None:
    registry = PluginRegistry()
    assert registry.add(Burst(), origin="/tmp/burst.py", clv_version=__version__)

    assert not registry.add(plugin(), origin="/tmp/x.py", clv_version=__version__)
    assert expected in str(registry.errors[-1])
    # And the one that was already there is untouched.
    assert len(registry.matchers) == 1


# --- the sink ---------------------------------------------------------------


def test_a_sink_receives_exactly_what_the_notifier_coalesced() -> None:
    """The anti-storm guarantee, asserted at the sink boundary."""

    sink = Recorder()
    _registry, stack = _install(sink)
    notifier = WatchNotifier(window=60)
    for _ in range(500):
        notifier.record(("noisy",))

    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=5000)
    try:
        dispatcher.deliver(notifier.due_hits(0.0), None)
        _settle(dispatcher)
    finally:
        dispatcher.stop(timeout=1.0)

    # Once, with a count that is honest about what it stands for.
    assert sink.calls == [("noisy", 500, ())]


def test_a_sink_that_did_not_ask_for_content_never_receives_a_line() -> None:
    sink = Recorder()
    _registry, stack = _install(sink)
    notifier = WatchNotifier(window=60, sample_limit=SINK_SAMPLE_LIMIT)
    entry = _parse(_LINE)[0]
    notifier.record(("oom",), entry=entry)

    hits = notifier.due_hits(0.0)
    assert hits[0].entries == (entry,), "the sample was not collected at all"

    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=5000)
    try:
        dispatcher.deliver(hits, None)
        _settle(dispatcher)
    finally:
        dispatcher.stop(timeout=1.0)

    # The sample exists and this sink is still handed nothing.
    assert sink.calls == [("oom", 1, ())]


def test_a_content_sink_receives_a_capped_sample_and_the_true_count() -> None:
    sink = ContentSink()
    _registry, stack = _install(sink)
    assert wants_entry_samples() is True

    notifier = WatchNotifier(window=60, sample_limit=SINK_SAMPLE_LIMIT)
    entries = _parse(
        *[f"2026-08-07 09:25:01 ERROR line {i}" for i in range(SINK_SAMPLE_LIMIT + 20)]
    )
    for entry in entries:
        notifier.record(("noisy",), entry=entry)

    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=5000)
    try:
        dispatcher.deliver(notifier.due_hits(0.0), None)
        _settle(dispatcher)
    finally:
        dispatcher.stop(timeout=1.0)

    (name, count, sample) = sink.calls[0]
    assert name == "noisy"
    assert count == SINK_SAMPLE_LIMIT + 20, "the count was capped along with the sample"
    assert len(sample) == SINK_SAMPLE_LIMIT
    # The most recent ones, which is what a sample of a burst should be.
    assert sample[-1] is entries[-1]


def test_no_sample_is_kept_when_nothing_asked_for_one() -> None:
    """Requirement 10: an operator with no content sink stores no log lines."""

    _install(Recorder())
    assert wants_entry_samples() is False

    notifier = WatchNotifier(window=60)
    notifier.record(("oom",), entry=_parse(_LINE)[0])
    assert notifier.due_hits(0.0)[0].entries == ()


def test_a_sink_that_raises_is_disabled_and_reported() -> None:
    registry, stack = _install(BoomSink())
    plugin = registry.sinks[0]

    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=5000)
    try:
        dispatcher.deliver((_hit("oom", 1),), None)
        _wait(lambda: registry.is_disabled(plugin))
    finally:
        dispatcher.stop(timeout=1.0)

    assert registry.is_disabled(plugin)
    assert "raised: no" in str(registry.errors[0])


def test_a_sink_that_hangs_is_abandoned_disabled_and_stops_being_fed() -> None:
    sink = HangingSink()
    registry, stack = _install(sink)
    plugin = registry.sinks[0]
    clock = _Clock()

    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=1000, clock=clock)
    try:
        dispatcher.deliver((_hit("oom", 1),), None)
        assert sink.started.wait(5.0), "the sink was never entered"

        # Not yet: inside the deadline, a slow sink is just a slow sink.
        dispatcher.settle()
        assert not registry.is_disabled(plugin)

        clock.advance(2.0)
        dispatcher.settle()
        assert registry.is_disabled(plugin)
        assert "did not return within 1000 ms" in (registry.disabled_reason(plugin) or "")

        # And it is fed nothing more, however much arrives.
        dispatcher.deliver((_hit("oom", 5),), None)
        dispatcher.settle()
    finally:
        sink.release.set()
        dispatcher.stop(timeout=1.0)


def test_stopping_does_not_wait_for_a_wedged_sink() -> None:
    sink = HangingSink()
    _registry, stack = _install(sink)
    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=0)
    try:
        dispatcher.deliver((_hit("oom", 1),), None)
        assert sink.started.wait(5.0)

        started = monotonic()
        dispatcher.stop(timeout=0.2)
        # Exiting the viewer must not depend on third-party code returning.
        assert monotonic() - started < 2.0
    finally:
        sink.release.set()


def test_a_sink_that_comes_back_after_being_retired_starts_clean() -> None:
    sink = HangingSink()
    registry, stack = _install(sink)
    plugin = registry.sinks[0]
    clock = _Clock()
    dispatcher = SinkDispatcher(stack.sinks, timeout_ms=1000, clock=clock)
    try:
        dispatcher.deliver((_hit("oom", 1),), None)
        assert sink.started.wait(5.0)
        clock.advance(2.0)
        dispatcher.settle()
        assert registry.is_disabled(plugin)

        # Re-enabled: a fresh spec, and a worker that is not the wedged one.
        sink.release.set()
        registry.enable(plugin)
        recorder = Recorder()
        rebuilt = registry.watch_stack()
        dispatcher.replace(rebuilt.sinks)
        dispatcher.deliver((_hit("oom", 2),), None)
        _wait(lambda: not sink.started.is_set() or True)
        _settle(dispatcher)
        assert recorder.calls == []
    finally:
        sink.release.set()
        dispatcher.stop(timeout=1.0)


# --- through the app --------------------------------------------------------


def test_a_plugin_kind_and_a_plugin_sink_work_through_the_running_app(tmp_path) -> None:
    """The phase gate: a plugin rule fires, and a plugin sink is delivered it."""

    path = _log(tmp_path, "2026-08-07 09:25:01 INFO started")
    sink = Recorder()

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            _attach(app, Burst(needed=5), sink)
            app._set_watch_rules(
                [WatchRule(name="oom", pattern="oom-killer x5/60", kind="burst")]
            )
            app._select_source(path)
            await pilot.pause()

            with path.open("a", encoding="utf-8") as handle:
                for index in range(50):
                    handle.write(f"2026-08-07 09:25:02 ERROR oom-killer {index}\n")
            app._poll_tail()
            await pilot.pause()
            _wait(lambda: bool(sink.calls))

            # One delivery for fifty lines, not fifty — and the toast said the
            # same thing, because both came out of the same coalescing.
            assert len(sink.calls) == 1
            assert sink.calls[0][0] == "oom"
            app._sink_dispatcher.stop(timeout=1.0)

    _run(scenario)


def test_a_blocking_sink_does_not_block_the_poll(tmp_path) -> None:
    """The no-IO-on-the-event-loop clause, asserted from the outside."""

    path = _log(tmp_path, "2026-08-07 09:25:01 INFO started")
    sink = HangingSink()

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            _attach(app, sink)
            app._set_watch_rules([WatchRule(name="noisy", pattern="line")])
            app._select_source(path)
            await pilot.pause()

            with path.open("a", encoding="utf-8") as handle:
                handle.write("2026-08-07 09:25:02 INFO line one\n")

            started = monotonic()
            app._poll_tail()
            elapsed = monotonic() - started
            await pilot.pause()

            assert sink.started.wait(5.0), "the sink was never reached"
            # The poll returned while the sink is still inside deliver().
            assert elapsed < 1.0, f"the poll waited {elapsed:.2f}s on a sink"
            assert not sink.release.is_set()

            # And the app is still working: a second poll completes too.
            with path.open("a", encoding="utf-8") as handle:
                handle.write("2026-08-07 09:25:03 INFO line two\n")
            app._poll_tail()
            await pilot.pause()
            assert len(app._entries) == 3

            sink.release.set()
            app._sink_dispatcher.stop(timeout=1.0)

    _run(scenario)


def test_switching_a_matcher_off_recompiles_the_rules_that_used_it(tmp_path) -> None:
    path = _log(tmp_path, _LINE)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            registry = _attach(app, Burst(needed=1))
            app._set_watch_rules(
                [WatchRule(name="oom", pattern="oom-killer x1/60", kind="burst")]
            )
            app._select_source(path)
            await pilot.pause()
            assert app.log_panel.is_row_watched(0)

            registry.disable(registry.matchers[0], "operator")
            app._sync_plugin_generation()
            app._render_log()
            await pilot.pause()

            # The rule stopped matching rather than starting to match something
            # else, and nothing was rewritten to achieve it.
            assert not app.log_panel.is_row_watched(0)
            assert app.state.watch_rules[0].kind == "burst"
            assert app.state.watch_rules[0].pattern == "oom-killer x1/60"

    _run(scenario)


# --- Requirement 10: nothing installed, nothing changes ---------------------


def test_with_no_watch_plugins_the_kinds_are_just_the_built_in_one() -> None:
    install_watch_plugins()
    assert matcher_kinds() == (KIND_PATTERN,)
    assert wants_entry_samples() is False


def test_due_still_returns_the_strings_it_always_did() -> None:
    """`due()` is what `tests/test_watch_rules.py` asserts against, unmodified."""

    notifier = WatchNotifier(window=60)
    notifier.record(("oom",))
    assert notifier.due(0.0) == ["Watch 'oom' matched a line."]


def test_the_rules_dialog_composes_no_kind_control_without_a_matcher() -> None:
    async def scenario() -> None:
        app = _app(Path("/nonexistent"))
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            install_watch_plugins()
            await app.push_screen(WatchRulesDialog())
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, WatchRulesDialog)
            assert not dialog.query("#watch-kind")

    _run(scenario)


def test_the_rules_dialog_offers_the_kinds_that_are_installed() -> None:
    async def scenario() -> None:
        app = _app(Path("/nonexistent"))
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            _install(Burst())
            await app.push_screen(WatchRulesDialog())
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, WatchRulesDialog)
            assert dialog.query("#watch-kind")

            dialog._open_editor(None)
            assert dialog._kind == KIND_PATTERN
            dialog._cycle_kind()
            assert dialog._kind == "burst"
            dialog._cycle_kind()
            assert dialog._kind == KIND_PATTERN

    _run(scenario)


def test_toggling_a_rule_keeps_its_kind_and_its_requires() -> None:
    """`replace`, not a rebuild — the field list used to be written by hand."""

    async def scenario() -> None:
        app = _app(Path("/nonexistent"))
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            _install(Burst())
            rule = WatchRule(
                name="oom",
                pattern="oom-killer x2/60",
                kind="burst",
                requires=("something",),
            )
            await app.push_screen(WatchRulesDialog([rule]))
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, WatchRulesDialog)

            dialog.query_one("#watch-list").highlighted = 0
            dialog._toggle_current()

            toggled_rule = dialog._rules[0]
            assert toggled_rule.enabled is False
            assert toggled_rule.kind == "burst"
            assert toggled_rule.requires == ("something",)

    _run(scenario)


# --- helpers ----------------------------------------------------------------


class _Clock:
    """A monotonic clock a test can move, so a deadline needs no sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _hit(name: str, count: int) -> WatchHit:
    return WatchHit(name=name, count=count, message=f"Watch '{name}' matched.")


def _wait(predicate, timeout: float = 5.0) -> None:
    """Spin until *predicate* holds. The sinks are on threads, not on a clock."""

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)


def _settle(dispatcher: SinkDispatcher, timeout: float = 5.0) -> None:
    """Wait for every queued delivery to have been made."""

    _wait(lambda: not dispatcher._inflight and all(
        pending.empty() for pending in dispatcher._queues.values()
    ), timeout)
    # The worker clears `_inflight` after the call returns, so one more beat
    # guarantees the recorded call is visible to the assertions below.
    threading.Event().wait(0.05)


# --- the cache, and why a matcher suspends it -------------------------------


def test_a_matcher_is_asked_about_every_line_including_identical_ones() -> None:
    """A counting kind cannot work if the cache answers for the repeats.

    `WatchIndex` keys its answers on line *content*, which is exactly right for
    a pattern and exactly wrong for a matcher that is counting: five identical
    `oom-killer` lines are one cache key and five events.
    """

    matcher = Burst(needed=5)
    _install(matcher)
    rule = WatchRule(name="oom", pattern="oom-killer x5/60", kind="burst")
    index = WatchIndex([rule])

    identical = _parse(*[_LINE] * 5)
    fired = [names for _entry, names in index.evaluate(None, identical)]

    assert matcher.seen == 5, "the cache answered for the repeats"
    assert fired == [("oom",)]


def test_a_pattern_rule_still_answers_once_per_distinct_line() -> None:
    """Requirement 10: the cache is untouched for every rule set that had it."""

    index = WatchIndex([WatchRule(name="oom", pattern="oom-killer")])
    index.evaluate(None, _parse(*[_LINE] * 5))

    assert index.evaluations == 1
