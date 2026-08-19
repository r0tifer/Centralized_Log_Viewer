"""Watch rules (Item 10): matching, coalescing, highlighting, persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic

import pytest
from textual.widgets import Input, Switch

from clv.app import LogViewerApp
from clv.services.config import LogConfig
from clv.services.discovery import DiscoverySettings
from clv.services.parsing import LogParser
from clv.services.refs import RemoteRef, format_ref
from clv.services.watch import (
    ACTION_HIGHLIGHT,
    ACTION_NOTIFY,
    WatchIndex,
    WatchNotifier,
    WatchRule,
    describe_rules,
    notifying,
    toggled,
    validate_pattern,
)
from clv.storage import SessionState, StateStore
from clv.widgets.watch_dialog import WatchRulesDialog

SOURCE = Path("/var/log/example.log")


def parse_lines(*lines: str):
    return LogParser().feed(list(lines))


def _run(scenario) -> None:
    asyncio.run(scenario())


# --- the rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a dict",
        {},
        {"name": "no pattern"},
        {"name": "", "pattern": "x"},
        {"pattern": "orphan"},
        {"name": "blank", "pattern": "   "},
    ],
)
def test_an_unusable_rule_is_dropped_rather_than_raising(raw) -> None:
    assert WatchRule.from_dict(raw) is None


def test_a_rule_with_an_unknown_action_falls_back_to_both() -> None:
    rule = WatchRule.from_dict({"name": "r", "pattern": "oom", "action": "explode"})
    assert rule is not None
    assert rule.action == "both"
    assert rule.highlights and rule.notifies


def test_actions_decide_what_a_rule_does() -> None:
    highlight = WatchRule(name="h", pattern="x", action=ACTION_HIGHLIGHT)
    notify = WatchRule(name="n", pattern="x", action=ACTION_NOTIFY)
    assert highlight.highlights and not highlight.notifies
    assert notify.notifies and not notify.highlights


def test_validate_pattern_reports_where_it_was_typed() -> None:
    assert validate_pattern("") is not None
    assert validate_pattern("oom-killer") is None
    assert validate_pattern("host:web01", {"host"}) is None
    assert validate_pattern("(unclosed") is not None
    assert validate_pattern("status>=", {"status"}) is not None


# --- the index --------------------------------------------------------------


def test_a_rule_matches_text_and_field_terms() -> None:
    entries = parse_lines(
        "Aug  7 09:25:01 web01 kernel[1]: Out of memory: oom-killer",
        "Aug  7 09:25:02 db02 cron[91]: session opened",
    )
    index = WatchIndex([WatchRule(name="oom", pattern="oom-killer")])
    fired = index.evaluate(SOURCE, entries)
    assert [entry.raw for entry, _ in fired] == [entries[0].raw]

    index = WatchIndex([WatchRule(name="db", pattern="host:db02")], {"host"})
    fired = index.evaluate(SOURCE, entries)
    assert [entry.raw for entry, _ in fired] == [entries[1].raw]


def test_a_disabled_rule_is_never_evaluated() -> None:
    entries = parse_lines("boom")
    index = WatchIndex([WatchRule(name="off", pattern="boom", enabled=False)])
    assert index.active is False
    assert index.evaluate(SOURCE, entries) == []
    assert index.watched(SOURCE, entries[0]) is False


def test_a_broken_pattern_matches_nothing_instead_of_raising() -> None:
    """A rule nobody can fix mid-render must not take the render down."""

    entries = parse_lines("anything")
    index = WatchIndex([WatchRule(name="bad", pattern="(unclosed")])
    assert index.evaluate(SOURCE, entries) == []


def test_a_line_is_evaluated_once_however_often_it_is_asked_about() -> None:
    """The guard behind 'rules do not evaluate on re-render'."""

    entries = parse_lines("oom-killer invoked", "quiet line")
    index = WatchIndex([WatchRule(name="oom", pattern="oom")])

    index.evaluate(SOURCE, entries)
    assert index.evaluations == 2

    for _ in range(10):
        index.evaluate(SOURCE, entries)
        for entry in entries:
            index.watched(SOURCE, entry)
    assert index.evaluations == 2, "a re-render must be a lookup, not a match"


def test_identical_lines_are_matched_once_but_counted_every_time() -> None:
    """Fifty identical failures are fifty events, not one."""

    entries = parse_lines(*(["connection refused"] * 50))
    index = WatchIndex([WatchRule(name="refused", pattern="refused")])

    fired = index.evaluate(SOURCE, entries)
    assert len(fired) == 50, "every occurrence is an occurrence"
    assert index.evaluations == 1, "but the question was only asked once"


def test_the_same_line_in_another_source_is_evaluated_separately() -> None:
    entries = parse_lines("oom-killer invoked")
    index = WatchIndex([WatchRule(name="oom", pattern="oom")])
    index.evaluate(SOURCE, entries)
    index.evaluate(Path("/var/log/other.log"), entries)
    assert index.evaluations == 2


def test_changing_the_rules_forgets_every_cached_answer() -> None:
    entries = parse_lines("oom-killer invoked")
    index = WatchIndex([WatchRule(name="oom", pattern="oom")])
    index.evaluate(SOURCE, entries)
    assert index.watched(SOURCE, entries[0])

    index.set_rules([WatchRule(name="other", pattern="nothing here")])
    assert index.watched(SOURCE, entries[0]) is False
    index.evaluate(SOURCE, entries)
    assert index.watched(SOURCE, entries[0]) is False


def test_pruning_drops_answers_for_evicted_lines() -> None:
    entries = parse_lines("oom-killer invoked", "second line")
    index = WatchIndex([WatchRule(name="oom", pattern="oom")])
    index.evaluate(SOURCE, entries)

    index.prune(SOURCE, entries[1:])
    assert index.watched(SOURCE, entries[0]) is False
    assert index.hits(SOURCE, entries[1]) == ()


def test_notifying_filters_out_highlight_only_rules() -> None:
    rules = [
        WatchRule(name="quiet", pattern="x", action=ACTION_HIGHLIGHT),
        WatchRule(name="loud", pattern="y", action=ACTION_NOTIFY),
    ]
    assert notifying(("quiet", "loud"), rules) == ("loud",)


# --- the notifier -----------------------------------------------------------


def test_the_first_match_is_reported_immediately() -> None:
    notifier = WatchNotifier(window=60)
    notifier.record(["oom"])
    assert notifier.due(0.0) == ["Watch 'oom' matched a line."]


def test_matches_inside_the_window_coalesce_into_one_count() -> None:
    notifier = WatchNotifier(window=60)
    notifier.record(["oom"])
    assert notifier.due(0.0)  # the first one goes out

    for _ in range(11):
        notifier.record(["oom"])
    assert notifier.due(30.0) == [], "still inside the window"

    messages = notifier.due(61.0)
    assert messages == ["Watch 'oom' matched 11 lines."]


def test_a_quiet_rule_reports_nothing_when_its_window_closes() -> None:
    notifier = WatchNotifier(window=10)
    notifier.record(["oom"])
    notifier.due(0.0)
    assert notifier.due(100.0) == []


def test_rules_are_rate_limited_independently() -> None:
    notifier = WatchNotifier(window=60)
    notifier.record(["a"])
    assert notifier.due(0.0) == ["Watch 'a' matched a line."]
    notifier.record(["b"])
    assert notifier.due(1.0) == ["Watch 'b' matched a line."]


def test_describe_rules_counts_what_is_live() -> None:
    assert describe_rules([]) == "Watch rules: none"
    rules = [WatchRule(name="a", pattern="x"), WatchRule(name="b", pattern="y", enabled=False)]
    assert describe_rules(rules) == "Watch rules: 1 active of 2"
    assert describe_rules(toggled(rules, "b", True)) == "Watch rules: 2 active of 2"


# --- persistence ------------------------------------------------------------


def test_rules_survive_a_state_store_round_trip(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    rules = (
        WatchRule(name="oom", pattern="oom-killer", action=ACTION_NOTIFY),
        WatchRule(name="5xx", pattern="status>=500", enabled=False),
    )
    store.save(SessionState(watch_rules=rules))
    assert store.load().watch_rules == rules


def test_one_malformed_rule_does_not_cost_the_others(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "watch_rules": [
                    {"name": "good", "pattern": "boom"},
                    {"name": "no pattern"},
                    42,
                    {"name": "also good", "pattern": "bang", "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    names = [rule.name for rule in store.load().watch_rules]
    assert names == ["good", "also good"]


# --- through the app --------------------------------------------------------


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


def test_opening_a_source_highlights_matches_without_notifying(tmp_path: Path) -> None:
    """Lines that were already there are shown, not announced."""

    path = _log(tmp_path, "2026-08-07 09:25:01 ERROR oom-killer invoked", "quiet line")

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            notices: list[str] = []
            app._notify = lambda text, severity="info": notices.append(text)  # type: ignore[assignment]

            app._set_watch_rules([WatchRule(name="oom", pattern="oom-killer")])
            app._select_source(path)
            await pilot.pause()

            watched = [
                app.log_panel.is_row_watched(index)
                for index, _ in app.log_panel.entry_rows()
            ]
            assert watched == [True, False]
            assert not any("matched" in text for text in notices)

    _run(scenario)


def test_a_tailed_line_raises_exactly_one_notification(tmp_path: Path) -> None:
    path = _log(tmp_path, "2026-08-07 09:25:01 INFO started")

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules([WatchRule(name="oom", pattern="oom-killer")])
            app._select_source(path)
            await pilot.pause()

            notices: list[str] = []
            app._notify = lambda text, severity="info": notices.append(text)  # type: ignore[assignment]

            with path.open("a", encoding="utf-8") as handle:
                handle.write("2026-08-07 09:25:02 ERROR oom-killer invoked\n")
            app._poll_tail()
            await pilot.pause()

            assert notices == ["Watch 'oom' matched a line."]
            # And the row that caused it is highlighted.
            assert app.log_panel.is_row_watched(len(app.log_panel.rows) - 1)

            # A second poll with nothing new says nothing more.
            app._poll_tail()
            await pilot.pause()
            assert len(notices) == 1

    _run(scenario)


def test_a_storm_of_matches_produces_one_message(tmp_path: Path) -> None:
    path = _log(tmp_path, "2026-08-07 09:25:01 INFO started")

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules([WatchRule(name="noisy", pattern="line")])
            app._select_source(path)
            await pilot.pause()

            notices: list[str] = []
            app._notify = lambda text, severity="info": notices.append(text)  # type: ignore[assignment]

            with path.open("a", encoding="utf-8") as handle:
                for index in range(50):
                    handle.write(f"2026-08-07 09:25:02 INFO line {index}\n")
            app._poll_tail()
            await pilot.pause()

            # One message, and it is honest about how much it stands for.
            assert notices == ["Watch 'noisy' matched 50 lines."]

            # Nothing is left over to arrive later, either.
            later = monotonic() + app._config.watch_rate_limit + 1.0
            assert app._watch_notifier.due(later) == []

    _run(scenario)


def test_re_rendering_does_not_re_evaluate_the_rules(tmp_path: Path) -> None:
    path = _log(tmp_path, *[f"2026-08-07 09:25:0{i} INFO line {i}" for i in range(5)])

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules([WatchRule(name="all", pattern="line")])
            app._select_source(path)
            await pilot.pause()

            evaluated = app._watch_index.evaluations
            assert evaluated == 5

            for _ in range(5):
                app._render_log()
            await pilot.pause()

            assert app._watch_index.evaluations == evaluated

    _run(scenario)


def test_the_highlight_is_not_the_severity_colour(tmp_path: Path) -> None:
    """A watched INFO line must look watched, not like an error."""

    path = _log(tmp_path, "2026-08-07 09:25:01 INFO nothing alarming here")

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules([WatchRule(name="w", pattern="alarming")])
            app._select_source(path)
            await pilot.pause()

            style = app.log_panel.get_component_rich_style("log-view--watch")
            assert style.bgcolor is not None, "the highlight must be a background"
            assert style.bold
            # Severity colouring is a foreground, so the two cannot be confused.
            assert app.log_panel.is_row_watched(0)

    _run(scenario)


def test_a_chip_disables_its_rule_without_deleting_it(tmp_path: Path) -> None:
    _log(tmp_path, "2026-08-07 09:25:01 INFO started")

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules([WatchRule(name="oom", pattern="oom-killer")])
            await pilot.pause()

            keys = [chip.key for chip in app.chip_bar.query("FilterChip")]
            assert "watch:oom" in keys

            app._dismiss_chip("watch:oom")
            await pilot.pause()

            assert app.state.watch_rules[0].enabled is False
            assert app._watch_index.active is False
            keys = [chip.key for chip in app.chip_bar.query("FilterChip")]
            assert "watch:oom" not in keys

    _run(scenario)


def test_many_rules_collapse_into_one_chip(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._set_watch_rules(
                [WatchRule(name=f"rule {i}", pattern=str(i)) for i in range(5)]
            )
            await pilot.pause()

            keys = [chip.key for chip in app.chip_bar.query("FilterChip")]
            assert keys == ["watch:*"]

            app._dismiss_chip("watch:*")
            await pilot.pause()
            assert not any(rule.enabled for rule in app.state.watch_rules)

    _run(scenario)


def test_the_drawer_switch_turns_the_whole_set_on_and_off(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules([WatchRule(name="oom", pattern="oom")])
            await pilot.pause()

            switch = app.advanced_drawer.query_one("#drawer-watch-rules", Switch)
            assert switch.value is True

            switch.value = False
            await pilot.pause()
            assert not any(rule.enabled for rule in app.state.watch_rules)

            switch.value = True
            await pilot.pause()
            assert all(rule.enabled for rule in app.state.watch_rules)

    _run(scenario)


def test_the_drawer_reports_how_many_rules_are_live(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._set_watch_rules(
                [
                    WatchRule(name="a", pattern="x"),
                    WatchRule(name="b", pattern="y", enabled=False),
                ]
            )
            await pilot.pause()

            status = app.advanced_drawer.query_one("#watch-status")
            assert status.render().plain == "Watch rules: 1 active of 2"

    _run(scenario)


def test_the_bell_is_off_unless_asked_for(tmp_path: Path) -> None:
    async def scenario(bell: bool) -> list[bool]:
        # A file per run: the watch index is content-keyed, so re-using one
        # would leave the second run's "new" line already answered for.
        path = _log(
            tmp_path, "2026-08-07 09:25:01 INFO started", name=f"bell-{bell}.log"
        )
        rung: list[bool] = []
        app = _app(tmp_path, watch_bell=bell)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app.bell = lambda: rung.append(True)  # type: ignore[method-assign]
            app._set_watch_rules([WatchRule(name="oom", pattern="oom-killer")])
            app._select_source(path)
            await pilot.pause()

            with path.open("a", encoding="utf-8") as handle:
                handle.write("2026-08-07 09:25:02 ERROR oom-killer invoked\n")
            app._poll_tail()
            await pilot.pause()
        return rung

    assert asyncio.run(scenario(False)) == []
    assert asyncio.run(scenario(True)) == [True]


def test_the_rules_dialog_adds_a_rule_by_keyboard(tmp_path: Path) -> None:
    async def scenario() -> None:
        results: list[tuple[WatchRule, ...] | None] = []
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(WatchRulesDialog(), callback=results.append)
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            dialog = app.screen
            assert dialog.query_one("#watch-name", Input).has_focus

            for char in "oom":
                await pilot.press(char)
            pattern = dialog.query_one("#watch-pattern", Input)
            pattern.value = "oom-killer"
            pattern.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert not dialog.editing, "saving closes the editor"

            await pilot.press("escape")
            await pilot.pause()

            assert results == [(WatchRule(name="oom", pattern="oom-killer"),)]

    _run(scenario)


def test_the_rules_dialog_refuses_a_pattern_it_cannot_compile(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(WatchRulesDialog())
            await pilot.pause()

            dialog = app.screen
            await pilot.press("a")
            await pilot.pause()
            dialog.query_one("#watch-name", Input).value = "broken"
            pattern = dialog.query_one("#watch-pattern", Input)
            pattern.value = "(unclosed"
            pattern.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert dialog.editing, "a bad pattern keeps the editor open"
            hint = dialog.query_one("#watch-hint")
            assert "-warning" in hint.classes

    _run(scenario)


def test_the_rules_dialog_says_nothing_changed_when_nothing_did(tmp_path: Path) -> None:
    async def scenario() -> None:
        results: list[tuple[WatchRule, ...] | None] = []
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(
                WatchRulesDialog([WatchRule(name="a", pattern="x")]), callback=results.append
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert results == [None]

    _run(scenario)


def test_the_rules_dialog_fits_eighty_columns(tmp_path: Path) -> None:
    async def scenario() -> None:
        rules = [WatchRule(name=f"rule {i}", pattern=f"pattern {i}") for i in range(10)]
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(WatchRulesDialog(rules))
            await pilot.pause()

            dialog = app.screen.query_one("#watch-dialog")
            assert dialog.region.right <= 80
            assert dialog.region.bottom <= 24

            await pilot.press("a")  # editor open is the tallest state
            await pilot.pause()
            for widget_id in ("#watch-list", "#watch-editor", "#dialog-actions"):
                region = app.screen.query_one(widget_id).region
                assert region.width > 0 and region.height > 0, widget_id
                assert region.bottom <= 24, widget_id

    _run(scenario)


# --- remote sources ---------------------------------------------------------
#
# Phase 6's parity sweep. A `WatchRule` binds to no source at all — it is a
# pattern and an action, applied to whatever is open — so nothing source-shaped
# is persisted and there is nothing here for a remote ref to corrupt. What does
# see a source is the hit cache, which keys through `mark_key`; these pin that
# it scopes by machine, and that the rules themselves stay machine-agnostic.


def _watch_entries(*messages: str):
    parser = LogParser()
    return parser.feed(
        [f"2026-08-07 09:25:0{index} - ERROR - {text}" for index, text in enumerate(messages)]
    )


def test_a_rule_fires_on_a_remote_source() -> None:
    index = WatchIndex([WatchRule(name="refused", pattern="connection refused")])
    remote = RemoteRef.build("web01", "/var/log/syslog")

    fired = index.evaluate(remote, _watch_entries("connection refused"))

    assert [names for _, names in fired] == [("refused",)]
    assert index.watched(remote, _watch_entries("connection refused")[0]) is True


def test_the_hit_cache_counts_the_same_line_on_two_machines_twice() -> None:
    """A fleet-wide outage produces one identical line per machine. Folding
    them into one cached answer would report a single event and lose four."""

    index = WatchIndex([WatchRule(name="refused", pattern="connection refused")])
    entries = _watch_entries("connection refused")

    hosts = [RemoteRef.build(name, "/var/log/syslog") for name in ("web01", "web02")]
    for host in hosts:
        index.evaluate(host, entries)

    # Two distinct cache keys, so two evaluations rather than one and a hit.
    assert index.evaluations == 2
    assert all(index.watched(host, entries[0]) for host in hosts)


def test_pruning_one_machine_leaves_the_other_machine_s_hits() -> None:
    index = WatchIndex([WatchRule(name="refused", pattern="connection refused")])
    entries = _watch_entries("connection refused", "connection refused")
    web01 = RemoteRef.build("web01", "/var/log/syslog")
    db02 = RemoteRef.build("db02", "/var/log/syslog")

    index.evaluate(web01, entries)
    index.evaluate(db02, entries)
    index.prune(web01, entries[:1])

    assert index.watched(web01, entries[0]) is True
    assert index.watched(web01, entries[1]) is False
    # `retain` is global by design, so db02's cache goes with it — the point
    # here is that the *keys* are distinct, not that pruning is per host.
    assert index.watched(db02, entries[0]) is False


def test_a_rule_persists_unchanged_while_a_remote_source_is_open(
    tmp_path: Path,
) -> None:
    """The proof that a rule carries nothing source-shaped: the same tuple comes
    back beside a remote `starred` entry, byte for byte."""

    store = StateStore(tmp_path / "state.json")
    rules = (WatchRule(name="refused", pattern="connection refused"),)
    remote = format_ref(RemoteRef.build("web01", "/var/log/syslog"))

    store.save(SessionState(watch_rules=rules, starred=(remote,)))
    restored = store.load()

    assert restored.watch_rules == rules
    assert restored.starred == (remote,)
