"""A plugin teaches CLV a format, and it is a first-class one.

Phase 7a of ``PLUGIN_TODO.md``. Before it, "CLV does not know my format" had no
plugin-shaped answer at all: a `FilterStage` can rewrite `fields` after the fact
but cannot make a line parse, cannot claim a `format_name`, and cannot supply
the timestamp the timeline buckets on.

The bar is Requirement 9, and the **Feature parity** block below *is* that
requirement for this phase: an entry a plugin format produced has to be
searchable by field query, bucketed by the timeline, folded by the clusterer,
shown in the detail pane, exportable and watchable — with no code anywhere that
knows a plugin was involved. Those tests pass because the entry is well-formed,
which is the whole claim; if any of them needed a special case the seam would be
offering something less than equal terms.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from types import MappingProxyType

import pytest

from clv.plugins import FormatStack, LogFormat, PluginBudget, PluginRegistry
from clv.services.clustering import cluster_entries
from clv.services.export import write_jsonl
from clv.services.formats import FormatProfile
from clv.services.marks import MarkSet, mark_key
from clv.services.parsing import LogEntry, LogParser, parse_line, parse_lines
from clv.services.query import collect_field_names, parse_query, match_terms, MATCH_HIT
from clv.services.timeline import build_timeline
from clv.services.watch import WatchRule, WatchIndex
from clv.widgets import columns as columns_module
from clv.widgets.columns import plan_columns, render_row
from clv.widgets.detail_pane import DetailPane


# --- a format under test ----------------------------------------------------
#
# `PIPE|` lines: deliberately nothing any built-in matcher looks at, so a line
# that reaches this format is a line that genuinely fell through.

_SAMPLE = "PIPE|2026-08-21T09:25:01|error|web01|shop|upstream refused"


class PipeFormat(LogFormat):
    """``PIPE|<ts>|<level>|<node>|<svc>|<message>``."""

    name = "pipe"
    format_name = "pipe"
    label = "Pipe-delimited"
    field_names = frozenset({"appliance", "svc"})
    columns = FormatProfile(source_keys=("svc",), chips=("appliance",))

    def parse(self, line):
        if not line.startswith("PIPE|"):
            return None
        _, stamp, level, appliance, svc, message = line.split("|", 5)
        return LogEntry(
            raw=line,
            timestamp=datetime.fromisoformat(stamp),
            level=level.upper(),
            message=message,
            format_name="pipe",
            fields=MappingProxyType({"appliance": appliance, "svc": svc}),
        )


def _registry(*plugins, budget: PluginBudget | None = None):
    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version="2.9.0"
        ), [str(error) for error in registry.errors]
    registry.order()
    return registry, registry.format_stack(budget=budget)


@pytest.fixture(autouse=True)
def installed_profiles_are_not_shared_between_tests():
    """`install_profiles` is module state, and a leaked profile is a false pass."""

    yield
    columns_module.install_profiles({}, {})


@pytest.fixture
def pipe_entries():
    _, stack = _registry(PipeFormat())
    return parse_lines(
        [
            _SAMPLE,
            "PIPE|2026-08-21T09:25:04|warn|web02|api|slow upstream",
            "PIPE|2026-08-21T09:25:09|error|web01|shop|upstream refused",
        ],
        formats=stack,
    )


# --- the seam ---------------------------------------------------------------


def test_a_plugin_format_parses_what_the_built_ins_call_raw() -> None:
    """The line before and after, so the phase's premise is stated as a test."""

    assert parse_line(_SAMPLE).format_name == "raw"

    _, stack = _registry(PipeFormat())
    entry = parse_line(_SAMPLE, formats=stack)

    assert entry.format_name == "pipe"
    assert entry.level == "ERROR"
    assert entry.timestamp == datetime(2026, 8, 21, 9, 25, 1)
    assert entry.message == "upstream refused"
    assert dict(entry.fields) == {"appliance": "web01", "svc": "shop"}


def test_a_line_a_built_in_handles_is_never_offered_to_a_plugin() -> None:
    """Built-ins first: a syslog file costs an installed format nothing."""

    offered: list[str] = []

    class Nosy(PipeFormat):
        name = "nosy"

        def parse(self, line):
            offered.append(line)
            return None

    _, stack = _registry(Nosy())
    entries = parse_lines(
        [
            "Aug 21 09:25:01 web01 sshd[1234]: Failed password for root",
            '{"level": "error", "msg": "boom"}',
            "2026-08-21 09:25:01,123 - WARNING - disk almost full",
            "something nothing recognises",
        ],
        formats=stack,
    )

    assert [entry.format_name for entry in entries] == [
        "syslog", "json", "python-logging", "raw",
    ]
    assert offered == ["something nothing recognises"]


def test_zero_format_plugins_parses_exactly_as_before() -> None:
    """Requirement 10, for this seam: the empty case enters none of it."""

    lines = [_SAMPLE, "Aug 21 09:25:01 web01 sshd[1234]: hi", "   ", "trailing"]

    assert [e.format_name for e in parse_lines(lines)] == [
        e.format_name for e in parse_lines(lines, formats=())
    ]
    assert LogParser().feed(lines) == LogParser(formats=()).feed(lines)


def test_ordering_across_formats_follows_priority() -> None:
    """Phase 5's one rule, not a second one invented here."""

    class Late(PipeFormat):
        name = "late"
        priority = 900
        format_name = "late"

        def parse(self, line):
            entry = super().parse(line)
            return None if entry is None else entry.__class__(
                raw=line, message="late", format_name="late"
            )

    class Early(PipeFormat):
        name = "early"
        priority = 10
        format_name = "early"

        def parse(self, line):
            entry = super().parse(line)
            return None if entry is None else entry.__class__(
                raw=line, message="early", format_name="early"
            )

    # Registered in the wrong order on purpose: `order()` is what settles it.
    _, stack = _registry(Late(), Early())
    assert parse_line(_SAMPLE, formats=stack).format_name == "early"


def test_continuation_after_a_plugin_format_line_inherits_correctly(
    pipe_entries,
) -> None:
    """Carry-forward needs no cooperation from a format, and gets none."""

    _, stack = _registry(PipeFormat())
    entries = parse_lines(
        [_SAMPLE, "    at frame one", "    at frame two"], formats=stack
    )

    trace = entries[1]
    assert trace.continuation is True
    assert trace.timestamp == entries[0].timestamp
    assert trace.level == "ERROR"
    # A stack trace frame has no appliance or service of its own to report.
    assert not trace.fields


# --- feature parity: Requirement 9 ------------------------------------------


def test_field_names_reach_the_query_vocabulary_before_a_line_is_read() -> None:
    """The point of declaring them: an empty buffer still knows the word."""

    registry, _ = _registry(PipeFormat())
    declared = {name for fmt in registry.formats for name in fmt.field_names}

    # `collect_field_names` sees nothing, because nothing has been read.
    assert collect_field_names([]) == frozenset()
    # The vocabulary the app hands `parse_query` is the union, and it is what
    # makes the token a term rather than part of the regex.
    parsed = parse_query("svc:shop refused", declared)
    assert [(t.key, t.op, t.value) for t in parsed.terms] == [("svc", ":", "shop")]
    assert parsed.text == "refused"


def test_a_field_query_matches_a_plugin_formats_entries(pipe_entries) -> None:
    parsed = parse_query("appliance:web01", {"appliance", "svc"})
    hits = [e for e in pipe_entries if match_terms(e, parsed.terms) == MATCH_HIT]

    assert len(hits) == 2
    assert {e.fields["svc"] for e in hits} == {"shop"}


def test_the_timeline_buckets_a_plugin_formats_entries(pipe_entries) -> None:
    timeline = build_timeline(pipe_entries, width=4)

    assert sum(bucket.count for bucket in timeline.buckets) == 3
    assert timeline.undated == 0


def test_the_clusterer_folds_a_plugin_formats_entries(pipe_entries) -> None:
    """The two identical `upstream refused` lines fold; the `slow upstream` does not."""

    stream = cluster_entries(pipe_entries)
    counts = sorted(getattr(row, "count", 1) for row in stream.rows)

    # The two identical `upstream refused` lines fold despite their different
    # timestamps, which is the clusterer's normalisation doing its ordinary job
    # on an entry it has no idea a plugin produced.
    assert counts == [1, 2]
    assert stream.entries == 3


def test_the_detail_pane_shows_a_plugin_formats_label_and_fields(
    pipe_entries,
) -> None:
    columns_module.install_profiles(
        {"pipe": PipeFormat.columns}, {"pipe": PipeFormat.label}
    )
    pane = DetailPane()
    table = pane._property_table(pipe_entries[0])
    rendered = [list(column.cells) for column in table.columns]

    assert "Pipe-delimited" in rendered[1]
    assert "appliance" in rendered[0] and "web01" in rendered[1]
    # Not the bare identifier, which is what a format with no `label` gets.
    assert "pipe" not in rendered[1]


def test_a_plugin_formats_entries_export(pipe_entries) -> None:
    handle = io.StringIO()
    write_jsonl(pipe_entries, handle)
    records = [json.loads(line) for line in handle.getvalue().splitlines()]

    assert [r["format"] for r in records] == ["pipe"] * 3
    assert records[0]["fields"]["svc"] == "shop"


def test_a_watch_rule_fires_on_a_plugin_formats_field(pipe_entries) -> None:
    index = WatchIndex()
    index.set_rules([WatchRule(name="shop errors", pattern="svc:shop")], {"svc"})

    fired = index.evaluate(None, pipe_entries)

    assert [names for _, names in fired] == [("shop errors",), ("shop errors",)]
    assert {entry.fields["svc"] for entry, _ in fired} == {"shop"}


def test_a_plugin_formats_entries_are_bookmarkable(pipe_entries) -> None:
    """Marks key on the entry's own identity, which a plugin entry has like any other."""

    marks = MarkSet()
    for entry in pipe_entries:
        assert not marks.contains(None, entry)
    marks.toggle(None, pipe_entries[0])

    assert marks.contains(None, pipe_entries[0])
    assert not marks.contains(None, pipe_entries[1])
    # Distinct lines get distinct keys: a mark on one is not a mark on all of
    # them, which is what a key derived from `format_name` alone would give.
    assert len({mark_key(None, entry) for entry in pipe_entries}) == 3


# --- the structured row -----------------------------------------------------


def test_a_format_that_declares_both_gets_a_source_cell_and_chips(
    pipe_entries,
) -> None:
    """Through `render_row`, not by reading the profile table back."""

    columns_module.install_profiles({"pipe": PipeFormat.columns}, {})
    layout = plan_columns(pipe_entries, merged=False, clustering=False)
    rows = [render_row(entry, layout).plain for entry in pipe_entries]

    assert "shop" in rows[0] and "api" in rows[1]
    # `appliance` varies across the set, so the varying rule turns the chip on.
    assert "appliance=web01" in rows[0]
    assert "appliance=web02" in rows[1]


def test_a_format_that_declares_neither_still_renders() -> None:
    """The degradation is defined rather than accidental — and it is a downgrade."""

    class Bare(LogFormat):
        name = "bare"
        format_name = "bare"

        def parse(self, line):
            if not line.startswith("BARE "):
                return None
            return LogEntry(
                raw=line,
                timestamp=datetime(2026, 8, 21, 9, 25, 1),
                level="ERROR",
                message=line[5:],
                format_name="bare",
            )

    _, stack = _registry(Bare())
    entries = parse_lines(["BARE something went wrong"], formats=stack)
    columns_module.install_profiles({}, {})
    layout = plan_columns(entries, merged=False, clustering=False)
    row = render_row(entries[0], layout).plain

    # The right timestamp and level, and nothing else: no source cell, no chips.
    assert row == "09:25:01 ERROR something went wrong"
    assert columns_module.format_label("bare") == "bare"


def test_consumed_keeps_a_spent_key_off_the_row() -> None:
    """The generalisation of `_JSON_CONSUMED`, on a format that is not json."""

    class Spender(LogFormat):
        name = "spender"
        format_name = "spender"
        field_names = frozenset({"svc", "lvl"})
        columns = FormatProfile(
            source_keys=("svc",), chips=("lvl",), consumed=frozenset({"lvl"})
        )

        def parse(self, line):
            if not line.startswith("SP "):
                return None
            svc, lvl = line[3:].split(" ", 1)
            return LogEntry(
                raw=line,
                level="ERROR",
                message="spent",
                format_name="spender",
                fields=MappingProxyType({"svc": svc, "lvl": lvl}),
            )

    _, stack = _registry(Spender())
    entries = parse_lines(["SP shop error", "SP api warn"], formats=stack)
    columns_module.install_profiles({"spender": Spender.columns}, {})
    layout = plan_columns(entries, merged=False, clustering=False)

    assert "lvl=" not in render_row(entries[0], layout).plain


# --- refusing what would corrupt the buffer ---------------------------------


def test_a_format_that_raises_is_disabled_and_parsing_continues() -> None:
    class Boom(LogFormat):
        name = "boom"
        format_name = "boom"

        def parse(self, line):
            raise RuntimeError("kaboom")

    registry, stack = _registry(Boom(), PipeFormat())
    boom = registry.formats[0] if registry.formats[0].name == "boom" else registry.formats[1]
    entries = parse_lines([_SAMPLE, _SAMPLE, _SAMPLE], formats=stack)

    assert registry.is_disabled(boom)
    # One error for three lines, not three.
    assert len(registry.errors) == 1
    assert "raised: kaboom" in str(registry.errors[0])
    # And the format behind it still did its job.
    assert [e.format_name for e in entries] == ["pipe"] * 3


@pytest.mark.parametrize(
    "returns, expected",
    [
        ("nonsense", "returned str, not a LogEntry"),
        (
            LogEntry(raw="x", format_name="syslog"),
            "not the 'liar' it declared",
        ),
        (
            LogEntry(raw="x", format_name="liar", fields=MappingProxyType({"n": 5})),
            "returned a non-string field",
        ),
    ],
)
def test_a_malformed_return_is_refused_by_the_rule_it_broke(returns, expected) -> None:
    """No corrupt entry reaches the buffer, and the message says which rule."""

    class Liar(LogFormat):
        name = "liar"
        format_name = "liar"

        def parse(self, line):
            return returns

    registry, stack = _registry(Liar())
    entries = parse_lines(["one", "two"], formats=stack)

    assert [entry.format_name for entry in entries] == ["raw", "raw"]
    assert len(registry.errors) == 1
    assert expected in str(registry.errors[0])


@pytest.mark.parametrize(
    "attrs, expected",
    [
        ({"format_name": ""}, "declares no format_name"),
        ({"format_name": "json"}, "is a built-in format"),
        ({"format_name": "raw"}, "is a built-in format"),
        ({"format_name": " pipe"}, "leading or trailing whitespace"),
        (
            {"columns": FormatProfile(source_keys=("nope",))},
            "which is not in field_names",
        ),
        ({"field_names": "svc"}, "field_names must be a set of strings"),
        ({"field_names": frozenset({""})}, "non-empty strings"),
        ({"columns": "not a profile"}, "columns must be a FormatProfile"),
    ],
)
def test_a_format_is_refused_at_load_with_the_rule_it_broke(attrs, expected) -> None:
    """At load, because every one of these is silent at runtime."""

    bad = type("Bad", (PipeFormat,), attrs)()
    registry = PluginRegistry()

    assert registry.add(bad, origin="/tmp/bad.py", clv_version="2.9.0") is False
    assert expected in str(registry.errors[0])
    assert registry.formats == []


def test_two_formats_cannot_claim_the_same_name() -> None:
    registry = PluginRegistry()
    assert registry.add(PipeFormat(), origin="/tmp/a.py", clv_version="2.9.0")
    assert not registry.add(PipeFormat(), origin="/tmp/b.py", clv_version="2.9.0")

    assert "already registered by pipe" in str(registry.errors[0])


# --- the read-path budget ---------------------------------------------------


def test_a_format_over_the_read_budget_is_disabled_and_named() -> None:
    """Three consecutive batches, the same policy the render path uses."""

    class Sluggish(PipeFormat):
        name = "sluggish"
        format_name = "sluggish"

        def parse(self, line):
            import time

            time.sleep(0.004)
            return None

    registry = PluginRegistry()
    plugin = Sluggish()
    registry.add(plugin, origin="/tmp/sluggish.py", clv_version="2.9.0")
    budget = PluginBudget(registry, limit_ms=1, label="read")
    parser = LogParser(formats=registry.format_stack(budget=budget))

    for _ in range(3):
        parser.feed(["nothing recognises this"])

    assert registry.is_disabled(plugin)
    reason = registry.disabled_reason(plugin)
    assert "over the read budget" in reason and "3 consecutive passes" in reason


def test_a_format_under_the_read_budget_is_never_disabled() -> None:
    registry = PluginRegistry()
    plugin = PipeFormat()
    registry.add(plugin, origin="/tmp/pipe.py", clv_version="2.9.0")
    budget = PluginBudget(registry, limit_ms=5_000, label="read")
    parser = LogParser(formats=registry.format_stack(budget=budget))

    for _ in range(20):
        parser.feed([_SAMPLE, "unmatched"])

    assert not registry.is_disabled(plugin)
    assert list(registry.errors) == []


def test_a_budget_of_zero_measures_nothing() -> None:
    """The documented escape hatch, and it is off however it is driven."""

    registry = PluginRegistry()
    plugin = PipeFormat()
    registry.add(plugin, origin="/tmp/pipe.py", clv_version="2.9.0")
    budget = PluginBudget(registry, limit_ms=0, label="read")
    stack = registry.format_stack(budget=budget)

    assert budget.active is False
    stack.start()
    stack.parse("unmatched")
    stack.settle()
    assert not registry.is_disabled(plugin)


def test_a_disabled_format_stops_being_called() -> None:
    """Checked per line, so the `P` dialog's toggle takes effect immediately."""

    calls: list[str] = []

    class Counted(PipeFormat):
        name = "counted"
        format_name = "counted"

        def parse(self, line):
            calls.append(line)
            return None

    registry, stack = _registry(Counted())
    stack.parse("one")
    registry.disable(registry.formats[0], "turned off by the operator", record=False)
    stack.parse("two")

    assert calls == ["one"]


# --- the stack itself -------------------------------------------------------


def test_the_stack_is_a_sequence_so_the_empty_case_is_falsy() -> None:
    """What lets `LogParser` treat `()` and a live stack as the same kind of thing."""

    registry = PluginRegistry()
    empty = registry.format_stack()

    assert isinstance(empty, FormatStack)
    assert not empty and len(empty) == 0
    registry.add(PipeFormat(), origin="/tmp/pipe.py", clv_version="2.9.0")
    assert registry.format_stack()


# --- the shipped example ----------------------------------------------------

_NGINX = (
    "2026/08/07 09:25:01 [error] 1234#0: *42 connect() failed (111: Connection "
    "refused) while connecting to upstream, client: 10.0.0.5, server: "
    'shop.example.com, request: "GET /pay HTTP/1.1", upstream: '
    '"http://10.0.0.9:8080/pay"'
)
_NGINX_PLAIN = "2026/08/07 09:24:59 [notice] 1233#0: signal process started"


def test_the_nginx_reference_parses_a_real_error_log() -> None:
    from clv.examples.nginx_error import NginxErrorFormat

    assert parse_line(_NGINX).format_name == "raw"

    _, stack = _registry(NginxErrorFormat())
    entry = parse_line(_NGINX, formats=stack)

    assert entry.format_name == "nginx-error"
    assert entry.level == "ERROR"
    assert entry.timestamp == datetime(2026, 8, 7, 9, 25, 1)
    assert entry.message == (
        "connect() failed (111: Connection refused) while connecting to upstream"
    )
    assert dict(entry.fields) == {
        "pid": "1234",
        "tid": "0",
        "cid": "42",
        "host": "10.0.0.5",
        "server": "shop.example.com",
        "request": "GET /pay HTTP/1.1",
        "upstream": "http://10.0.0.9:8080/pay",
    }


def test_the_nginx_reference_handles_a_line_with_no_connection() -> None:
    """A startup line has no `*cid` and no details; requiring them loses it."""

    from clv.examples.nginx_error import NginxErrorFormat

    _, stack = _registry(NginxErrorFormat())
    entry = parse_line(_NGINX_PLAIN, formats=stack)

    assert entry.format_name == "nginx-error"
    assert entry.level == "NOTICE"
    assert entry.message == "signal process started"
    assert dict(entry.fields) == {"pid": "1233", "tid": "0"}


def test_the_nginx_reference_files_the_client_under_host() -> None:
    """Normalised onto CLV's vocabulary, so one query reaches three formats."""

    from clv.examples.nginx_error import NginxErrorFormat

    _, stack = _registry(NginxErrorFormat())
    entry = parse_line(_NGINX, formats=stack)
    clf = parse_line(
        '10.0.0.5 - - [07/Aug/2026:09:25:01 +0000] "GET /pay HTTP/1.1" 500 12'
    )

    parsed = parse_query("host:10.0.0.5", {"host"})
    assert match_terms(entry, parsed.terms) == MATCH_HIT
    assert match_terms(clf, parsed.terms) == MATCH_HIT


def test_the_nginx_reference_declares_a_valid_profile() -> None:
    """The load-time check, against the example CLV itself ships."""

    from clv.examples.nginx_error import NginxErrorFormat

    registry = PluginRegistry()
    assert registry.add(
        NginxErrorFormat(), origin="/tmp/nginx_error.py", clv_version="2.9.0"
    ), [str(error) for error in registry.errors]


# --- through the real app ---------------------------------------------------
#
# The wiring, not the seam: `on_mount` has to install the stack, the profiles
# and the field vocabulary in an order where each of them has something to work
# with. Every test above reaches the pieces directly and would pass with the
# app wired wrong.


def test_the_app_wires_a_format_into_the_read_path_and_the_vocabulary(
    tmp_path, monkeypatch
) -> None:
    """One test, six claims, because they only mean anything together."""

    import asyncio

    from clv import __version__
    from clv.app import LogViewerApp

    log = tmp_path / "pipe.log"
    log.write_text(_SAMPLE + "\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            # After mount, so this stands in for a format the loader found: the
            # generation bump is what re-runs the install.
            app._plugins.add(
                PipeFormat(), origin="/tmp/pipe.py", clv_version=__version__
            )
            app._plugins.order()
            app._sync_plugin_generation()
            await pilot.pause()

            # 1. The session hands the stack to every buffer it opens.
            assert len(app._session.formats) == 1
            # 2. The vocabulary knows the words before a line has been read.
            assert {"svc", "appliance"} <= app._known_fields
            # 3. The profile and the label are installed.
            assert columns_module.profile_for("pipe").source_keys == ("svc",)
            assert columns_module.format_label("pipe") == "Pipe-delimited"

            app._select_source(log)
            await pilot.pause()

            entries = list(app._entries)
            # 4. The line the built-ins call raw is parsed by the plugin.
            assert [entry.format_name for entry in entries] == ["pipe"]
            # 5. The structured row carries the declared source cell.
            layout = plan_columns(entries, merged=False, clustering=False)
            assert "shop" in render_row(entries[0], layout).plain

            # 6. Switching it off stops it claiming the next lines read.
            app._plugins.disable(app._plugins.formats[0], "off", record=False)
            app._sync_plugin_generation()
            await pilot.pause()

            assert columns_module.profile_for("pipe").source_keys == ()
            assert app._plugin_fields == frozenset()
            # ...and `svc` is *still* a known field, which is right rather than
            # a leak: the lines carrying it are in the buffer and a query for it
            # still has to answer for them. What the disable removed is the
            # promise that the word means something before a line has been read.
            assert "svc" in app._known_fields
            assert "svc" in collect_field_names(app._entries)

    asyncio.run(scenario())
