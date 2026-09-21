r"""A plugin extends the clusterer, and it extends it on equal terms.

Phase 10 of ``PLUGIN_TODO.md``. Before it, both halves of a cluster were closed
sets: the nine normalisation rules were a literal tuple, and the shape two
entries must share was composed by hand from the source, the level and the
normalised message. So a log whose volatile token CLV does not know about — a
mail address, an ANSI colour run — got one cluster per line, which is the
feature failing on exactly the logs that needed it.

Three claims are under test here and they are different in kind.

**Requirement 9, equal terms.** A plugin rule folds a repeat through `c`, the
status line, the expansion and a clustered export, none of which knows a plugin
exists.

**Requirement 10, a build with no plugins pays nothing.** ``shape_of`` returns
the byte-identical string it returned before this seam, asserted against a
literal — and ``tests/test_clustering.py`` is **not modified by this phase at
all**, which is the evidence rather than an assertion about it. The two
invariants that file owns are re-run here with plugins active instead.

**The load checks are the whole of the error handling.** A ``ClusterRule``
declares a pattern and a placeholder and CLV performs the substitution, so
there is no runtime call of the plugin's to fail. Everything that can go wrong
is therefore caught at load, with a message, and the matrix below is that
promise written out.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import Static

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import (
    ClusterRule,
    PluginBudget,
    PluginRegistry,
    ShapeContributor,
)
from clv.services.clustering import (
    Cluster,
    ClusterStream,
    cluster_entries,
    expand,
    install_cluster_plugins,
    installed_contributors,
    installed_rules,
    normalise,
    shape_of,
)
from clv.services.parsing import LogEntry
from clv.services.session import ORIGIN_FIELD

BASE = datetime(2026, 8, 7, 9, 25, 0)


def _entry(
    message: str,
    *,
    level: str = "INFO",
    second: int = 0,
    source: str = "",
    **fields: str,
) -> LogEntry:
    carried = dict(fields)
    if source:
        carried[ORIGIN_FIELD] = source
    return LogEntry(
        raw=f"2026-08-07 09:25:{second:02d} - {level} - {message}",
        timestamp=BASE + timedelta(seconds=second),
        level=level,
        message=message,
        format_name="python-logging",
        fields=carried,
    )


# --- plugins under test -----------------------------------------------------


class Email(ClusterRule):
    """The token none of the nine built-ins matches: not hex, not a path."""

    name = "email"
    pattern = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    placeholder = "<email>"


class Ansi(ClusterRule):
    """An empty placeholder: the token is decoration, not a value.

    Written against what is *left* of an escape once the built-in integer rule
    has been through it — see
    ``test_a_rule_is_handed_the_line_the_builtins_already_normalised``.
    """

    name = "ansi"
    pattern = re.compile(r"\x1b\[[^m]*m")
    placeholder = ""


class ByUnit(ShapeContributor):
    name = "by-unit"

    def contribute(self, entry: LogEntry) -> str:
        return entry.fields.get("unit", "")


class Constant(ShapeContributor):
    name = "constant"

    def contribute(self, entry: LogEntry) -> str:
        return "always-the-same"


class BoomContributor(ShapeContributor):
    name = "boom-contributor"

    def contribute(self, entry: LogEntry) -> str:
        raise RuntimeError("no")


class WrongType(ShapeContributor):
    name = "wrong-type"

    def contribute(self, entry: LogEntry):
        return object()


def _install(*plugins, budget: PluginBudget | None = None):
    """Load *plugins* into a registry and install what they declared."""

    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
        ), [str(error) for error in registry.errors]
    registry.order()
    stack = registry.cluster_stack(budget=budget)
    install_cluster_plugins(stack.rules, stack.contributors)
    return registry, stack


def _reinstall(registry: PluginRegistry, budget: PluginBudget | None = None):
    """Rebuild the stack, as the app does when the generation moves."""

    stack = registry.cluster_stack(budget=budget)
    install_cluster_plugins(stack.rules, stack.contributors)
    return stack


_MAIL = "authentication failed for alice@corp.example"
_OTHER_MAIL = "authentication failed for bob@corp.example"


# --- the rule works ---------------------------------------------------------


def test_a_plugin_rule_normalises_a_token_the_builtins_leave_alone() -> None:
    before = normalise(_MAIL)
    assert "alice@corp.example" in before, "the built-ins leave this alone"

    _install(Email())

    assert normalise(_MAIL) == "authentication failed for <email>"


def test_two_lines_differing_only_in_an_address_cluster_together() -> None:
    _install(Email())

    stream = cluster_entries([_entry(_MAIL), _entry(_OTHER_MAIL, second=1)])

    assert len(stream.rows) == 1
    assert isinstance(stream.rows[0], Cluster)
    assert stream.rows[0].count == 2


def test_without_the_rule_the_same_two_lines_are_two_rows() -> None:
    """The other half of the test above: the seam is doing the work."""

    stream = cluster_entries([_entry(_MAIL), _entry(_OTHER_MAIL, second=1)])

    assert len(stream.rows) == 2


def test_plugin_rules_run_after_the_builtins() -> None:
    """A rule that would eat a built-in placeholder cannot, and here is why.

    The digit-free invariant is what makes appending safe: by the time a plugin
    rule runs, every volatile token CLV knows about is already a placeholder,
    and a plugin pattern matching numbers finds nothing in them to match.
    """

    class EatNumbers(ClusterRule):
        name = "eat-numbers"
        pattern = re.compile(r"\d+")
        placeholder = "<num>"

    _install(EatNumbers())

    shaped = normalise("request 8821 took 12ms from 10.0.0.5")

    # The built-ins got there first and what they wrote survived intact.
    assert shaped == "request <int> took <int>ms from <ip>"
    assert "<num>" not in shaped


def test_a_rule_is_handed_the_line_the_builtins_already_normalised() -> None:
    """The trap this seam's shape creates, pinned so the docs cannot drift.

    Running last is what makes appending safe, and it is also what makes the
    obvious Kubernetes rule never fire: by the time a plugin rule sees
    ``api-7d9f8b6c4-x2n9q`` the built-in hex rule has made it ``api-<hex>-x2n9q``
    and there is nothing left for the pattern to match. Both the worked example
    and ``clv/plugins/AGENTS.md`` teach this; this is what keeps them honest.
    """

    class PodSuffix(ClusterRule):
        name = "pod-suffix"
        pattern = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b")
        placeholder = "<pod>"

    _install(PodSuffix())

    assert normalise("pod api-7d9f8b6c4-x2n9q refused") == "pod api-<hex>-x2n9q refused"


def test_an_empty_placeholder_deletes_the_token() -> None:
    _install(Ansi())

    assert normalise("\x1b[31mdisk full\x1b[0m") == "disk full"


def test_a_coloured_line_clusters_with_a_plain_one() -> None:
    _install(Ansi())

    stream = cluster_entries(
        [_entry("\x1b[31mdisk full\x1b[0m"), _entry("disk full", second=1)]
    )

    assert len(stream.rows) == 1
    assert stream.rows[0].count == 2


def test_rules_run_in_priority_order_whatever_order_they_loaded_in() -> None:
    """Two rules that fight, composed deterministically rather than by luck.

    ``strip`` removes the bracket the ``tag`` rule needs to recognise its
    token, so running first it wins and running second it has nothing left to
    do. The point is not which answer is right — it is that the answer does not
    depend on which file the loader happened to walk first.
    """

    class Strip(ClusterRule):
        name = "strip"
        priority = 50
        pattern = re.compile(r"[\[\]]")
        placeholder = ""

    class Tag(ClusterRule):
        name = "tag"
        priority = 900
        pattern = re.compile(r"\[[a-z]+\]")
        placeholder = "<tag>"

    _install(Tag(), Strip())
    assert normalise("worker [alpha] started") == "worker alpha started"

    # The same two, added the other way round: identical, because the order is
    # a function of `priority` and not of load order.
    _install(Strip(), Tag())
    assert normalise("worker [alpha] started") == "worker alpha started"

    # And with the priorities swapped the composition flips, deterministically.
    Strip.priority, Tag.priority = 900, 50
    try:
        _install(Strip(), Tag())
        assert normalise("worker [alpha] started") == "worker <tag> started"
    finally:
        Strip.priority, Tag.priority = 50, 900


def test_equal_priorities_are_ordered_by_name() -> None:
    class Aardvark(ClusterRule):
        name = "aardvark"
        pattern = re.compile(r"x")
        placeholder = "<a>"

    class Zebra(ClusterRule):
        name = "zebra"
        pattern = re.compile(r"x")
        placeholder = "<z>"

    registry, _ = _install(Zebra(), Aardvark())

    assert [plugin.name for plugin in registry.rules] == ["aardvark", "zebra"]
    # The first to run is the one that gets the token.
    assert normalise("x") == "<a>"


# --- the load checks --------------------------------------------------------


def _reject(plugin) -> str:
    registry = PluginRegistry()
    assert not registry.add(
        plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
    )
    assert len(registry.errors) == 1
    return registry.errors[0].message


def test_a_placeholder_with_a_digit_is_rejected_with_the_reason() -> None:
    class Numbered(ClusterRule):
        name = "numbered"
        pattern = re.compile(r"x+")
        placeholder = "<n1>"

    message = _reject(Numbered())

    assert "contains a digit" in message
    assert "run after CLV's own" in message


def test_a_placeholder_with_a_backslash_is_rejected_as_a_template() -> None:
    """``\\1`` is a group reference, not the two characters the author wrote."""

    class Grouped(ClusterRule):
        name = "grouped"
        pattern = re.compile(r"(x)+")
        placeholder = "<\\1>"

    message = _reject(Grouped())

    # Named before the digit it also contains: the backslash is what was
    # actually written, and "contains a digit" would be a true but useless
    # answer to a plugin that meant to reference a group.
    assert "backslash" in message
    assert "substitution template" in message


@pytest.mark.parametrize(
    ("pattern", "placeholder", "expected"),
    [
        ("", "<x>", "declares no pattern"),
        ("([unclosed", "<x>", "not a usable regular expression"),
        (42, "<x>", "must be a compiled regular expression or a string"),
        (r"\d*", "<x>", "matches the empty string"),
        (r"[a-z]*", "<x>", "matches the empty string"),
        (r"x+", 7, "placeholder must be a string"),
    ],
)
def test_a_malformed_rule_is_rejected_with_the_rule_it_broke(
    pattern, placeholder, expected: str
) -> None:
    class Malformed(ClusterRule):
        name = "malformed"

    Malformed.pattern = pattern
    Malformed.placeholder = placeholder

    assert expected in _reject(Malformed())


def test_a_pattern_matching_the_empty_string_says_what_it_would_do() -> None:
    """The one an author is least likely to see coming, so it says it in full."""

    class Everywhere(ClusterRule):
        name = "everywhere"
        pattern = re.compile(r"\d*")
        placeholder = "<n>"

    assert "every position of every line" in _reject(Everywhere())


def test_a_pattern_may_be_a_string_and_is_compiled_at_load() -> None:
    """Writing `re.compile` around a literal is ceremony, not a contract."""

    class Plain(ClusterRule):
        name = "plain"
        pattern = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
        placeholder = "<email>"

    _install(Plain())

    assert normalise(_MAIL) == "authentication failed for <email>"


def test_a_rejected_rule_costs_only_itself() -> None:
    """One error, and the plugins beside it still load."""

    class Bad(ClusterRule):
        name = "bad"
        pattern = re.compile(r"x+")
        placeholder = "<1>"

    registry = PluginRegistry()
    assert not registry.add(Bad(), origin="/tmp/bad.py", clv_version=__version__)
    assert registry.add(
        Email(), origin="/tmp/email.py", clv_version=__version__
    )
    registry.order()
    _reinstall(registry)

    assert len(registry.errors) == 1
    assert len(installed_rules()) == 1
    assert normalise(_MAIL) == "authentication failed for <email>"


# --- the contributor --------------------------------------------------------


def test_a_contributor_splits_a_cluster_that_would_otherwise_merge() -> None:
    entries = [
        _entry("Started", unit="nginx.service"),
        _entry("Started", second=1, unit="postgres.service"),
    ]

    assert cluster_entries(entries).rows[0].count == 2

    _install(ByUnit())

    assert len(cluster_entries(entries).rows) == 2


def test_a_contributor_returning_a_constant_changes_nothing() -> None:
    """The property that makes adding one safe: it can only ever split."""

    entries = [_entry("Started"), _entry("Started", second=1)]
    before = [type(row) for row in cluster_entries(entries).rows]

    _install(Constant())

    rows = cluster_entries(entries).rows
    assert [type(row) for row in rows] == before
    assert rows[0].count == 2


def test_a_contributor_that_raises_is_disabled_once_and_clustering_continues() -> None:
    registry, _ = _install(BoomContributor())
    plugin = registry.contributors[0]

    entries = [_entry("Started", unit="a"), _entry("Started", second=1, unit="b")]
    stream = cluster_entries(entries)

    assert registry.is_disabled(plugin)
    assert "raised: no" in registry.disabled_reason(plugin)
    # Recorded once however many entries it was asked about.
    assert len(registry.errors) == 1
    # And the shapes are the ones the built-ins produce, so the two lines fold.
    assert len(stream.rows) == 1
    assert stream.rows[0].count == 2


def test_a_contributor_returning_a_non_string_is_taken_out_of_service() -> None:
    registry, _ = _install(WrongType())
    plugin = registry.contributors[0]

    cluster_entries([_entry("Started")])

    assert registry.is_disabled(plugin)
    reason = registry.disabled_reason(plugin)
    assert "contribute() returned object" in reason
    assert "must return a string" in reason


def test_a_disabled_contributor_leaves_the_shape_it_would_have_widened() -> None:
    registry, _ = _install(ByUnit())
    plugin = registry.contributors[0]
    split = _entry("Started", unit="nginx.service")

    widened = shape_of(split)
    registry.disable(plugin, "off", record=False)

    # Without rebuilding the stack: the guard re-checks per call, because a
    # plugin disabled mid-render is disabled immediately.
    assert shape_of(split) != widened
    assert shape_of(split) == _BARE_SHAPE_OF_STARTED


_BARE_SHAPE_OF_STARTED = "\0INFO\0Started"


# --- the cache --------------------------------------------------------------


def test_enabling_a_rule_mid_session_reshapes_what_was_already_cached() -> None:
    """The test that fails if the ``lru_cache`` is left alone.

    Shaped once with no rules, so the answer is memoised; then a rule is
    installed and the same text asked again. A stale cache would serve the
    pre-plugin shape — not a slow answer but a wrong one, and one that looks
    exactly like the rule not being installed.
    """

    assert "alice@corp.example" in normalise(_MAIL)

    _install(Email())

    assert normalise(_MAIL) == "authentication failed for <email>"


def test_disabling_a_rule_restores_the_previous_shapes_exactly() -> None:
    before = normalise(_MAIL)
    registry, _ = _install(Email())
    assert normalise(_MAIL) != before

    registry.disable(registry.rules[0], "off", record=False)
    _reinstall(registry)

    assert normalise(_MAIL) == before
    # An out-of-service rule is not installed at all, unlike an operator token
    # or a watch kind: there is no saved text whose meaning it has to reserve.
    assert installed_rules() == ()


def test_re_enabling_a_rule_puts_it_back(tmp_path: Path) -> None:
    registry, _ = _install(Email())
    plugin = registry.rules[0]

    registry.disable(plugin, "off", record=False)
    _reinstall(registry)
    assert "alice@corp.example" in normalise(_MAIL)

    registry.enable(plugin)
    _reinstall(registry)
    assert normalise(_MAIL) == "authentication failed for <email>"


# --- the invariants, with plugins active ------------------------------------


def test_expanding_a_cluster_gives_back_every_original_line_with_plugins() -> None:
    """``tests/test_clustering.py``'s guarantee, re-run through the seam.

    A copy rather than an edit there: that file is Requirement 10's evidence
    and this phase owed it the right to be literally unmodified.
    """

    _install(Email(), Ansi(), ByUnit())

    entries = [
        _entry(
            f"\x1b[31mauthentication failed for {who}@corp.example\x1b[0m",
            second=index,
            unit="nginx.service",
        )
        for index, who in enumerate(("alice", "bob", "carol"))
    ] + [_entry("disk almost full", level="WARN", second=9)]

    stream = cluster_entries(entries)
    assert len(stream.rows) == 2
    assert stream.rows[0].count == 3

    recovered = expand(stream.rows)
    assert [line.raw for line in recovered] == [line.raw for line in entries]
    assert recovered == entries


def test_incremental_clustering_matches_a_full_recompute_with_plugins() -> None:
    """The other invariant that file owns, for the same reason.

    A contributor that is not a pure function of the entry would break exactly
    this, which is why the interface says so in its own docstring.
    """

    _install(Email(), ByUnit())

    entries = []
    for index in range(20):
        unit = "nginx.service" if index % 2 else "postgres.service"
        entries.append(
            _entry(f"failed for user{index % 10}@corp.example", second=index, unit=unit)
        )

    full = cluster_entries(entries)

    incremental = ClusterStream()
    for entry in entries:
        incremental.add(entry)

    assert [
        (row.shape, row.count) if isinstance(row, Cluster) else row.raw
        for row in incremental.rows
    ] == [
        (row.shape, row.count) if isinstance(row, Cluster) else row.raw
        for row in full.rows
    ]


# --- the budget -------------------------------------------------------------


class SlowRule(ClusterRule):
    name = "slow-rule"
    pattern = re.compile(r"(?:[a-z]+ )+refused")
    placeholder = "<slow>"


class SlowContributor(ShapeContributor):
    name = "slow-contributor"

    def contribute(self, entry: LogEntry) -> str:
        end = __import__("time").perf_counter() + 0.002
        while __import__("time").perf_counter() < end:
            pass
        return ""


def test_a_contributor_over_the_budget_is_disabled_after_three_passes() -> None:
    registry = PluginRegistry()
    plugin = SlowContributor()
    assert registry.add(plugin, origin="/tmp/slow.py", clv_version=__version__)
    registry.order()
    budget = PluginBudget(registry, limit_ms=5.0, label="cluster")
    stack = _reinstall(registry, budget=budget)

    entries = [_entry("Started", second=index) for index in range(3)]
    for _ in range(2):
        stack.start()
        cluster_entries(entries)
        stack.settle()
    assert not registry.is_disabled(plugin), "two strikes is not three"

    stack.start()
    cluster_entries(entries)
    stack.settle()

    assert registry.is_disabled(plugin)
    reason = registry.disabled_reason(plugin)
    assert "over the cluster budget" in reason
    assert "3 consecutive passes" in reason


def test_a_pass_under_the_line_clears_the_count() -> None:
    registry = PluginRegistry()
    plugin = SlowContributor()
    assert registry.add(plugin, origin="/tmp/slow.py", clv_version=__version__)
    registry.order()
    budget = PluginBudget(registry, limit_ms=5.0, label="cluster")
    stack = _reinstall(registry, budget=budget)

    entries = [_entry("Started", second=index) for index in range(3)]
    for _ in range(2):
        stack.start()
        cluster_entries(entries)
        stack.settle()

    # A pass the plugin *runs in* and comes in under the line. It has to be a
    # pass it ran in: `settle` clears a strike for what it was charged for, so
    # a pass a plugin was not asked about says nothing about the plugin.
    stack.start()
    cluster_entries(entries[:1])
    stack.settle()

    stack.start()
    cluster_entries(entries)
    stack.settle()
    assert not registry.is_disabled(plugin)


def test_a_switched_off_budget_measures_nothing() -> None:
    registry = PluginRegistry()
    plugin = SlowContributor()
    assert registry.add(plugin, origin="/tmp/slow.py", clv_version=__version__)
    registry.order()
    budget = PluginBudget(registry, limit_ms=0, label="cluster")
    stack = _reinstall(registry, budget=budget)

    for _ in range(5):
        stack.start()
        cluster_entries([_entry("Started", second=index) for index in range(3)])
        stack.settle()

    assert not registry.is_disabled(plugin)


# --- the zero state ---------------------------------------------------------


def test_with_no_clustering_plugins_the_shape_is_what_it_always_was() -> None:
    """Asserted against a literal, because "unchanged" has to mean this string."""

    entry = _entry("connection refused", level="ERROR", source="web01.log")

    assert shape_of(entry) == "web01.log\0ERROR\0connection refused"
    assert installed_rules() == ()
    assert installed_contributors() == ()


def test_uninstalling_restores_the_shape_exactly() -> None:
    entry = _entry("connection refused", level="ERROR", source="web01.log")
    before = shape_of(entry)

    _install(Email(), ByUnit())
    install_cluster_plugins()

    assert shape_of(entry) == before


def test_a_registry_with_no_clustering_plugins_installs_nothing() -> None:
    registry = PluginRegistry()
    stack = registry.cluster_stack()

    assert stack.rules == () and stack.contributors == ()
    # And the pass is a no-op rather than an error, so the app may bracket
    # every clustering unconditionally.
    stack.start()
    stack.settle()


# --- the worked example -----------------------------------------------------
#
# Seeded into the operator's plugin directory from this module's own source, so
# it cannot rot into something that no longer loads while still being handed out.


def test_the_worked_example_loads_and_works() -> None:
    from clv.examples.cluster_rules import AnsiColour, EmailAddress, SplitByField

    registry, _ = _install(AnsiColour(), EmailAddress(), SplitByField())
    assert [tuple(row.kinds) for row in registry.loaded] == [
        ("cluster rule",),
        ("cluster rule",),
        ("shape",),
    ]

    assert normalise("\x1b[31mauth failed for alice@corp.example\x1b[0m") == (
        "auth failed for <email>"
    )


def test_the_worked_examples_ansi_rule_survives_the_builtin_integer_rule() -> None:
    r"""The example teaches the trap in its own docstring; this is the proof.

    ``\x1b\[[0-9;]*m`` — the pattern anyone would write first — cannot match,
    because CLV's integer rule has already turned ``\x1b[31m`` into
    ``\x1b[<int>m``. The example is written as "everything up to the closing
    ``m``" instead, and that has to keep being true.
    """

    from clv.examples.cluster_rules import AnsiColour

    _install(AnsiColour())

    assert normalise("\x1b[38;5;196mred\x1b[0m") == "red"
    assert normalise("\x1b[mplain\x1b[m") == "plain"


def test_the_worked_examples_contributor_is_inert_until_configured(
    monkeypatch,
) -> None:
    """Consent applied to a seam where the cost of guessing is a useless pane.

    Loaded the way an operator loads it, rather than constructed here, because
    half of what is under test is that the section name the example documents —
    ``[plugin:cluster_rules]`` — is the one that actually reaches it. A test
    that handed the settings over directly would pass with the name wrong.
    """

    entries = [
        _entry("Started", unit="nginx.service"),
        _entry("Started", second=1, unit="postgres.service"),
    ]

    _reinstall(_installed_example(monkeypatch))
    # No `[plugin:cluster_rules]` section: the two fold, as they always did.
    assert cluster_entries(entries).rows[0].count == 2

    _reinstall(
        _installed_example(monkeypatch, settings={"cluster_rules": {"split_by": "unit"}})
    )
    assert len(cluster_entries(entries).rows) == 2


def test_the_worked_example_is_what_gets_seeded() -> None:
    """The file an operator finds is this module, not a copy of it."""

    from clv.services.config import SEEDED_EXAMPLES, ensure_user_plugin_dir

    assert SEEDED_EXAMPLES["cluster_rules.py"] == "clv.examples.cluster_rules"
    seeded = ensure_user_plugin_dir() / "examples" / "cluster_rules.py"
    assert "class EmailAddress(ClusterRule)" in seeded.read_text(encoding="utf-8")


def _installed_example(monkeypatch, settings=None) -> PluginRegistry:
    """Copy the seeded example up a level and load it, as an operator would."""

    from clv.plugins import PLUGIN_PATH_ENV, load_plugins
    from clv.services.config import ensure_user_plugin_dir

    root = ensure_user_plugin_dir()
    (root / "cluster_rules.py").write_text(
        (root / "examples" / "cluster_rules.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(root))
    return load_plugins(enabled=["cluster_rules"], settings=settings)


def test_the_example_loads_the_way_an_operator_installs_it(monkeypatch) -> None:
    """The whole install path, end to end: copy it up, name it, run it.

    Requirement 1 for this seam. ``load_plugins`` is what an operator's copy
    reaches, and it is the one caller of ``add()`` this file does not otherwise
    exercise — a rule that passed ``_cluster_fault`` in a unit test but broke
    the namespace scan would still be a plugin that does not work.
    """

    registry = _installed_example(monkeypatch)

    assert not [str(error) for error in registry.errors]
    assert [plugin.name for plugin in registry.rules] == [
        "ansi-colour",
        "email-address",
    ]
    assert [plugin.name for plugin in registry.contributors] == ["split-by-field"]

    _reinstall(registry)
    assert normalise("auth failed for alice@corp.example") == "auth failed for <email>"


# --- through the app --------------------------------------------------------


def _noisy_log(tmp_path: Path) -> Path:
    """Thirty lines from thirty users, and one that is genuinely different."""

    path = tmp_path / "logins.log"
    lines = [
        f"2026-08-07 09:25:{index % 60:02d} - ERROR - authentication failed "
        f"for user{index:02d}@corp.example"
        for index in range(30)
    ]
    lines.append("2026-08-07 09:26:00 - WARN - disk almost full")
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
    app._cluster_budget = app._new_cluster_budget()
    app._install_cluster_plugins()
    return registry


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


def test_a_plugin_rule_folds_a_repeat_the_builtins_could_not(tmp_path: Path) -> None:
    """Requirement 9: `c` knows nothing about plugins and neither does the row.

    Thirty addressed lines are thirty rows without the rule and one cluster
    with it, and every original line comes back on `Enter`.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_noisy_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            # No rule: each address is its own shape, so nothing folds.
            assert len(app.log_panel.rows) == 31

            _attach(app, Email())
            app._render_log()
            await pilot.pause()

            assert len(app.log_panel.rows) == 2
            assert "30 lines in 1 clusters" in _status(app)

            # And the no-loss guarantee through the pane, not only the service.
            app.log_panel.move_cursor(0)
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.log_panel.rows) == 32

    asyncio.run(scenario())


def test_switching_a_rule_off_mid_session_re_clusters_the_pane(tmp_path: Path) -> None:
    """The generation path end to end, including the cache clear behind it."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(_noisy_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            registry = _attach(app, Email())
            await pilot.press("c")
            await pilot.pause()
            assert len(app.log_panel.rows) == 2

            registry.disable(registry.rules[0], "off", record=False)
            # Nothing else: the app notices the generation on the next render.
            app._render_log()
            await pilot.pause()

            assert len(app.log_panel.rows) == 31

            registry.enable(registry.rules[0])
            app._render_log()
            await pilot.pause()

            assert len(app.log_panel.rows) == 2

    asyncio.run(scenario())
