"""A plugin extends the query grammar, and it extends it on equal terms.

Phase 8 of ``PLUGIN_TODO.md``. Before it the grammar was a closed set — the
operator alternation was a literal in ``_TERM_RE`` and ``FieldTerm.test`` was an
if-chain over it — so a plugin could produce a *field* and say nothing new
*about* one.

Two claims are under test here and they are different in kind.

**Requirement 9, equal terms.** A plugin's operator and a plugin's computed
field work in the query box, in a saved view and in a watch rule, because all
three route through ``parse_query`` and none of them knows a plugin exists.

**Requirement 12, degradation.** This is the first seam whose absence can change
what a *saved* thing means: ``svc~web`` with the operator gone is not an error,
it is a regex over the raw line that happens to parse. So a view or rule records
what its query needs, and a missing plugin makes it unusable rather than
different. The byte-identity assertions below are that requirement — they are
what stops "preserve, disable, explain" degrading into "rewrite helpfully".
"""

from __future__ import annotations

import json
import re
import pytest

from clv.plugins import ComputedField, PluginBudget, PluginRegistry, QueryOperator
from clv.services.filtering import FilterSpec, filter_entries
from clv.services.parsing import parse_line
from clv.services.query import (
    BUILTIN_OPERATORS,
    MATCH_HIT,
    MATCH_MISS,
    MATCH_MISSING_FIELD,
    NORMALISED_FIELD_KEYS,
    QueryError,
    computed_field_names,
    install_query_plugins,
    match_terms,
    parse_query,
    provided_plugins,
    requirements,
    unsatisfied,
)
from clv.services import query as query_module
from clv.services.watch import (
    WatchIndex,
    WatchRule,
    describe_missing,
    rule_requirements,
    validate_pattern,
)
from clv.storage import SavedView, SessionState, StateStore

# The grammar is module state and is reset between tests by an autouse fixture
# in `conftest.py`, so a leftover `~` from one test cannot decide another.


# --- plugins under test -----------------------------------------------------


class RegexMatch(QueryOperator):
    """``key~pattern`` — the stored value matches the regular expression."""

    name = "field-regex"
    token = "~"

    def test(self, stored: str, value: str) -> bool:
        return re.search(value, stored) is not None


class Length(ComputedField):
    """``length`` — how long the raw line is. Nothing in a log line says so."""

    name = "line-length"
    field_name = "length"

    def value(self, entry):
        return str(len(entry.raw))


class Boom(QueryOperator):
    name = "boom"
    token = "%"

    def test(self, stored: str, value: str) -> bool:
        raise RuntimeError("no")


class BoomField(ComputedField):
    name = "boom-field"
    field_name = "boomed"

    def value(self, entry):
        raise RuntimeError("no")


_LINE = "Aug 21 09:25:01 web01 sshd[42]: connection refused"


def _install(*plugins, budget: PluginBudget | None = None):
    """Load *plugins* into a registry and install what they declared."""

    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version="2.9.0"
        ), [str(error) for error in registry.errors]
    registry.order()
    stack = registry.query_stack(budget=budget)
    install_query_plugins(stack.operators, stack.computed)
    return registry, stack


def _known() -> frozenset[str]:
    """What the app hands `parse_query`: the normalised keys plus the plugins'."""

    return NORMALISED_FIELD_KEYS | computed_field_names()


def _entry(line: str = _LINE):
    return parse_line(line)


# --- the operator works -----------------------------------------------------


def test_a_plugin_operator_parses_matches_and_renders_back() -> None:
    _install(RegexMatch())
    parsed = parse_query("host~^web[0-9]+$", _known())

    assert len(parsed.terms) == 1
    term = parsed.terms[0]
    assert (term.key, term.op, term.value) == ("host", "~", "^web[0-9]+$")
    assert parsed.text == ""
    # `render` is what the chip bar and the view summary print. A term nobody
    # can read back is a term an operator cannot correct.
    assert term.render() == "host~^web[0-9]+$"
    assert match_terms(_entry(), parsed.terms) == MATCH_HIT


def test_a_plugin_operator_mixes_with_builtins_and_free_text() -> None:
    _install(RegexMatch())
    parsed = parse_query("host~^web tag:sshd refused|timeout", _known())

    assert [term.op for term in parsed.terms] == ["~", ":"]
    assert parsed.text == "refused|timeout"
    assert match_terms(_entry(), parsed.terms) == MATCH_HIT


def test_a_plugin_operator_can_miss() -> None:
    _install(RegexMatch())
    parsed = parse_query("host~^db", _known())
    assert match_terms(_entry(), parsed.terms) == MATCH_MISS


def test_a_plugin_operator_on_an_absent_field_is_missing_not_a_miss() -> None:
    """The distinction the UI reports differently, and plugins do not get to blur."""

    _install(RegexMatch())
    parsed = parse_query("msgid~anything", _known())
    assert match_terms(_entry(), parsed.terms) == MATCH_MISSING_FIELD


def test_a_plugin_operator_groups_a_quoted_value() -> None:
    """The tokeniser's quote rule is derived from the token set, not hardcoded."""

    _install(RegexMatch())
    parsed = parse_query('host~"web 01"', _known())
    assert parsed.terms[0].value == "web 01"


# --- reserved and malformed tokens -----------------------------------------


@pytest.mark.parametrize("token", sorted(BUILTIN_OPERATORS))
def test_a_plugin_claiming_a_builtin_token_is_rejected(token: str) -> None:
    class Thief(QueryOperator):
        name = "thief"

        def test(self, stored: str, value: str) -> bool:  # pragma: no cover
            return True

    Thief.token = token
    registry = PluginRegistry()
    assert not registry.add(Thief(), origin="/tmp/thief.py", clv_version="2.9.0")
    assert "is one of CLV's own comparisons" in str(registry.errors[0])
    assert registry.operators == []


def test_the_builtin_keeps_working_after_a_plugin_tried_to_claim_it() -> None:
    class Thief(QueryOperator):
        name = "thief"
        token = ":"

        def test(self, stored: str, value: str) -> bool:  # pragma: no cover
            return False

    registry = PluginRegistry()
    registry.add(Thief(), origin="/tmp/thief.py", clv_version="2.9.0")
    registry.order()
    stack = registry.query_stack()
    install_query_plugins(stack.operators, stack.computed)

    parsed = parse_query("tag:sshd", _known())
    assert match_terms(_entry(), parsed.terms) == MATCH_HIT


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("", "declares no token"),
        ("   ", "declares no token"),
        ("~a", "which a field key may also contain"),
        ("-", "which a field key may also contain"),
        ("~.", "which a field key may also contain"),
        ("~ ", "contains whitespace"),
        ("~\t~", "contains whitespace"),
        ('~"', "contains a quote"),
        ("~'", "contains a quote"),
    ],
)
def test_a_malformed_token_is_rejected_with_the_rule_it_broke(
    token: str, expected: str
) -> None:
    class Odd(QueryOperator):
        name = "odd"

        def test(self, stored: str, value: str) -> bool:  # pragma: no cover
            return True

    Odd.token = token
    registry = PluginRegistry()
    assert not registry.add(Odd(), origin="/tmp/odd.py", clv_version="2.9.0")
    assert expected in str(registry.errors[0])


def test_two_plugins_claiming_one_token_collide_once() -> None:
    class Second(QueryOperator):
        name = "second"
        token = "~"

        def test(self, stored: str, value: str) -> bool:  # pragma: no cover
            return True

    registry = PluginRegistry()
    assert registry.add(RegexMatch(), origin="/tmp/a.py", clv_version="2.9.0")
    assert not registry.add(Second(), origin="/tmp/b.py", clv_version="2.9.0")
    assert "already registered by field-regex" in str(registry.errors[0])
    assert len(registry.operators) == 1


# --- longest token first ----------------------------------------------------


def test_registering_a_one_character_token_does_not_break_a_builtin() -> None:
    _install(RegexMatch())
    parsed = parse_query("status>=500 status<=599 status!=404", _known())
    assert [term.op for term in parsed.terms] == [">=", "<=", "!="]


def test_a_longer_plugin_token_beats_a_shorter_one() -> None:
    class Fuzzy(QueryOperator):
        name = "fuzzy"
        token = "~="

        def test(self, stored: str, value: str) -> bool:
            return stored.casefold() == value.casefold()

    _install(RegexMatch(), Fuzzy())

    assert parse_query("host~=WEB01", _known()).terms[0].op == "~="
    assert parse_query("host~^web", _known()).terms[0].op == "~"
    assert match_terms(_entry(), parse_query("host~=WEB01", _known()).terms) == MATCH_HIT


def test_a_token_sharing_a_prefix_with_a_builtin_resolves_longest_first() -> None:
    class NotMatch(QueryOperator):
        name = "not-match"
        token = "!~"

        def test(self, stored: str, value: str) -> bool:
            return re.search(value, stored) is None

    _install(RegexMatch(), NotMatch())

    assert parse_query("host!=web01", _known()).terms[0].op == "!="
    assert parse_query("host!~^db", _known()).terms[0].op == "!~"
    assert match_terms(_entry(), parse_query("host!~^db", _known()).terms) == MATCH_HIT


# --- computed fields --------------------------------------------------------


def test_a_computed_field_is_queryable() -> None:
    _install(Length())
    entry = _entry()

    assert "length" in _known()
    assert match_terms(entry, parse_query("length>10", _known()).terms) == MATCH_HIT
    assert match_terms(entry, parse_query("length>9999", _known()).terms) == MATCH_MISS
    assert (
        match_terms(entry, parse_query(f"length={len(entry.raw)}", _known()).terms)
        == MATCH_HIT
    )


def test_a_computed_field_is_offered_before_a_line_has_been_read() -> None:
    """The same promise the normalised keys make, for the same reason."""

    _install(Length())
    assert computed_field_names() == frozenset({"length"})
    assert parse_query("length>10", _known()).terms


def test_a_computed_field_does_not_shadow_a_parsed_field_of_the_same_name() -> None:
    """Per entry, not per key — which is what makes the claim literally true."""

    class FakeHost(ComputedField):
        name = "fake-host"
        field_name = "host"

        def value(self, entry):
            return "computed-not-parsed"

    _install(FakeHost())
    parsed_line = _entry()
    unparsed = _entry("a line with no structure whatsoever")

    # The line said `web01`, so the line wins.
    assert parsed_line.fields["host"] == "web01"
    assert match_terms(parsed_line, parse_query("host=web01", _known()).terms) == MATCH_HIT
    assert (
        match_terms(parsed_line, parse_query("host=computed-not-parsed", _known()).terms)
        == MATCH_MISS
    )
    # A line that says nothing about a host is where the plugin gets a turn.
    assert "host" not in unparsed.fields
    assert (
        match_terms(unparsed, parse_query("host=computed-not-parsed", _known()).terms)
        == MATCH_HIT
    )


def test_a_computed_field_returning_none_is_a_missing_field_not_a_miss() -> None:
    class Sometimes(ComputedField):
        name = "sometimes"
        field_name = "sometimes"

        def value(self, entry):
            return None

    _install(Sometimes())
    assert (
        match_terms(_entry(), parse_query("sometimes:anything", _known()).terms)
        == MATCH_MISSING_FIELD
    )


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("", "declares no field_name"),
        ("  ", "declares no field_name"),
        (" age", "has leading or trailing whitespace"),
        ("9lives", "is not a legal query key"),
        ("has space", "is not a legal query key"),
        ("has:colon", "is not a legal query key"),
    ],
)
def test_a_malformed_field_name_is_rejected(field_name: str, expected: str) -> None:
    class Odd(ComputedField):
        name = "odd"

        def value(self, entry):  # pragma: no cover
            return None

    Odd.field_name = field_name
    registry = PluginRegistry()
    assert not registry.add(Odd(), origin="/tmp/odd.py", clv_version="2.9.0")
    assert expected in str(registry.errors[0])


def test_two_plugins_claiming_one_field_name_collide() -> None:
    class Second(ComputedField):
        name = "second"
        field_name = "length"

        def value(self, entry):  # pragma: no cover
            return None

    registry = PluginRegistry()
    assert registry.add(Length(), origin="/tmp/a.py", clv_version="2.9.0")
    assert not registry.add(Second(), origin="/tmp/b.py", clv_version="2.9.0")
    assert "already registered by line-length" in str(registry.errors[0])


def test_two_plugins_claiming_one_field_name_in_different_cases_collide() -> None:
    """`Age` and `age` are one field to any query that could ask for either.

    Compared exactly, the second would load and then silently replace the first
    in the installed registry — a plugin doing nothing, with no diagnosis, from
    the outside having loaded fine.
    """

    class Shouty(ComputedField):
        name = "shouty"
        field_name = "LENGTH"

        def value(self, entry):  # pragma: no cover
            return None

    registry = PluginRegistry()
    assert registry.add(Length(), origin="/tmp/a.py", clv_version="2.9.0")
    assert not registry.add(Shouty(), origin="/tmp/b.py", clv_version="2.9.0")
    assert "already registered by line-length" in str(registry.errors[0])


def test_a_computed_field_may_take_a_normalised_key_name() -> None:
    """Not rejected, because it cannot shadow: the parsed field resolves first."""

    class Host(ComputedField):
        name = "host-guess"
        field_name = "host"

        def value(self, entry):  # pragma: no cover
            return None

    registry = PluginRegistry()
    assert registry.add(Host(), origin="/tmp/h.py", clv_version="2.9.0")


# --- failure and the budget -------------------------------------------------


def test_an_operator_that_raises_is_disabled_once_and_then_reports_itself() -> None:
    registry, _ = _install(Boom())
    entries = [_entry() for _ in range(20)]
    terms = parse_query("host%anything", _known()).terms

    for entry in entries:
        assert match_terms(entry, terms) == MATCH_MISS

    assert registry.is_disabled(registry.operators[0])
    assert len(list(registry.errors)) == 1
    assert "raised: no" in str(list(registry.errors)[0])

    # Re-installed on the next generation change, the token stays reserved and
    # the term says why it cannot be answered.
    stack = registry.query_stack()
    install_query_plugins(stack.operators, stack.computed)
    with pytest.raises(QueryError) as excinfo:
        parse_query("host%anything", _known())
    assert "'boom' plugin, which is not in service" in str(excinfo.value)


def test_a_computed_field_that_raises_is_disabled_and_reports_itself() -> None:
    registry, _ = _install(BoomField())
    terms = parse_query("boomed:x", _known()).terms
    assert match_terms(_entry(), terms) == MATCH_MISSING_FIELD
    assert registry.is_disabled(registry.computed[0])

    stack = registry.query_stack()
    install_query_plugins(stack.operators, stack.computed)
    with pytest.raises(QueryError) as excinfo:
        parse_query("boomed:x", _known())
    assert "'boom-field' plugin, which is not in service" in str(excinfo.value)


def test_a_computed_field_returning_a_non_string_is_taken_out_of_service() -> None:
    """Values are compared as strings; a number would quietly never match."""

    class Numeric(ComputedField):
        name = "numeric"
        field_name = "count"

        def value(self, entry):
            return 42

    registry, _ = _install(Numeric())
    assert match_terms(_entry(), parse_query("count=42", _known()).terms) == (
        MATCH_MISSING_FIELD
    )
    assert registry.is_disabled(registry.computed[0])
    assert "must return a string or None" in str(list(registry.errors)[0])


def test_an_operator_over_the_budget_is_disabled_after_three_passes() -> None:
    class Slow(QueryOperator):
        name = "slow"
        token = "~"

        def test(self, stored: str, value: str) -> bool:
            deadline = __import__("time").perf_counter() + 0.004
            while __import__("time").perf_counter() < deadline:
                pass
            return True

    registry = PluginRegistry()
    registry.add(Slow(), origin="/tmp/slow.py", clv_version="2.9.0")
    registry.order()
    budget = PluginBudget(registry, limit_ms=1.0, label="query")
    stack = registry.query_stack(budget=budget)
    install_query_plugins(stack.operators, stack.computed)

    terms = parse_query("host~x", _known()).terms
    entry = _entry()
    for _ in range(3):
        stack.start()
        match_terms(entry, terms)
        stack.settle()

    assert registry.is_disabled(registry.operators[0])
    assert "over the query budget" in registry.disabled_reason(registry.operators[0])


def test_a_disabled_operator_matches_nothing_rather_than_something_else() -> None:
    """The window between a mid-render disable and the next install."""

    registry, _ = _install(RegexMatch())
    terms = parse_query("host~^web", _known()).terms
    assert match_terms(_entry(), terms) == MATCH_HIT

    registry.disable(registry.operators[0], "by hand", origin="field-regex")
    assert match_terms(_entry(), terms) == MATCH_MISS


# --- filtering end to end ---------------------------------------------------


def test_a_plugin_operator_filters_through_filter_entries() -> None:
    """No code in `filtering` knows a plugin was involved. Requirement 9."""

    _install(RegexMatch())
    entries = [
        _entry(),
        _entry("Aug 21 09:25:02 db07 postgres[9]: slow query"),
        _entry("Aug 21 09:25:03 web02 nginx[7]: 200 ok"),
    ]
    spec = FilterSpec(query="host~^web", known_fields=_known())
    result = filter_entries(entries, spec)

    assert [entry.fields["host"] for entry in result.entries] == ["web01", "web02"]
    assert result.stats.hidden_by_query == 1


def test_a_computed_field_filters_and_is_counted_when_absent() -> None:
    class Never(ComputedField):
        name = "never"
        field_name = "never"

        def value(self, entry):
            return None

    _install(Never())
    spec = FilterSpec(query="never:x", known_fields=_known())
    result = filter_entries([_entry(), _entry()], spec)

    assert result.entries == []
    # The counter `describe_empty_result` explains, not the generic one.
    assert result.stats.hidden_missing_field == 2
    assert result.stats.hidden_by_query == 0


# --- degradation: saved views -----------------------------------------------


def test_a_saved_view_records_what_its_query_needs() -> None:
    _install(RegexMatch(), Length())
    assert requirements("host~^web length>10 tag:sshd", _known()) == (
        "field-regex",
        "line-length",
    )
    # A query touching no plugin records nothing, so nothing to degrade.
    assert requirements("tag:sshd status>=500", _known()) == ()


def test_a_saved_view_reloads_and_works_while_the_plugin_is_present(
    tmp_path,
) -> None:
    _install(RegexMatch())
    view = SavedView(
        name="web errors",
        query="host~^web refused",
        requires=requirements("host~^web refused", _known()),
    )
    store = StateStore(root=tmp_path)
    store.save(SessionState(views=(view,)))

    restored = store.load().views[0]
    assert restored == view
    assert unsatisfied(restored.requires) == ()

    spec = FilterSpec(query=restored.query, known_fields=_known())
    assert len(filter_entries([_entry()], spec).entries) == 1


def test_a_saved_view_is_kept_byte_intact_when_its_plugin_is_gone(tmp_path) -> None:
    """Requirement 12: preserve, disable, explain — and preserve comes first."""

    _install(RegexMatch())
    query = 'host~^web[0-9]+ "disk full"'
    view = SavedView(name="web errors", query=query, requires=requirements(query, _known()))
    store = StateStore(root=tmp_path)
    store.save(SessionState(views=(view,)))
    written = store.path.read_text(encoding="utf-8")

    # The plugin goes away entirely — not disabled, uninstalled.
    install_query_plugins()

    restored = store.load().views[0]
    assert restored.query == query, "the query string was rewritten"
    assert restored.requires == ("field-regex",)
    assert unsatisfied(restored.requires) == ("field-regex",)

    # Saved back untouched: a round trip through a build without the plugin
    # must not quietly drop the record that marks it unusable.
    store.save(SessionState(views=(restored,)))
    assert store.path.read_text(encoding="utf-8") == written


def test_a_missing_plugin_is_named_rather_than_counted() -> None:
    assert describe_missing(("field-regex",)) == (
        "needs the 'field-regex' plugin, which is not installed"
    )
    assert describe_missing(("a", "b")) == (
        "needs the 'a' and 'b' plugins, which are not installed"
    )


def test_a_switched_off_plugin_does_not_make_a_view_unusable() -> None:
    """Two absences, reported differently. Collapsing them would be a worse lie.

    An uninstalled plugin leaves nothing in the grammar, so the query would mean
    something else — the view is unusable. A plugin merely switched off still
    owns its token, so the query still means what it meant and says so through
    the validation line.
    """

    registry, _ = _install(RegexMatch())
    requires = requirements("host~^web", _known())

    registry.disable(registry.operators[0], "by hand", origin="field-regex")
    stack = registry.query_stack()
    install_query_plugins(stack.operators, stack.computed)

    assert unsatisfied(requires) == ()
    assert "field-regex" in provided_plugins()
    with pytest.raises(QueryError):
        parse_query("host~^web", _known())


# --- degradation: watch rules -----------------------------------------------


def _hits(rule: WatchRule, entry) -> tuple[str, ...]:
    index = WatchIndex((rule,), _known())
    index.evaluate(None, [entry])
    return index.hits(None, entry)


def test_a_watch_rule_works_with_a_plugin_operator() -> None:
    _install(RegexMatch())
    pattern = "host~^web"
    rule = WatchRule(
        name="web", pattern=pattern, requires=rule_requirements(pattern, _known())
    )
    assert rule.requires == ("field-regex",)
    assert validate_pattern(pattern, _known(), rule.requires) is None
    assert _hits(rule, _entry()) == ("web",)


def test_a_watch_rule_never_matches_when_its_plugin_is_gone() -> None:
    """Without `requires` this rule would compile as a regex and highlight lines."""

    _install(RegexMatch())
    pattern = "host~^web"
    rule = WatchRule(
        name="web", pattern=pattern, requires=rule_requirements(pattern, _known())
    )
    install_query_plugins()

    assert rule.missing_plugins == ("field-regex",)
    assert _hits(rule, _entry()) == ()
    # And it is reported where an operator would look for it.
    problem = validate_pattern(pattern, _known(), rule.requires)
    assert problem == "Rule needs the 'field-regex' plugin, which is not installed."


def test_a_watch_rule_survives_a_round_trip_byte_intact(tmp_path) -> None:
    _install(RegexMatch())
    pattern = "host~^web[0-9]+"
    rule = WatchRule(
        name="web", pattern=pattern, requires=rule_requirements(pattern, _known())
    )
    store = StateStore(root=tmp_path)
    store.save(SessionState(watch_rules=(rule,)))
    written = store.path.read_text(encoding="utf-8")

    install_query_plugins()
    restored = store.load().watch_rules[0]
    assert restored == rule
    assert restored.pattern == pattern

    store.save(SessionState(watch_rules=(restored,)))
    assert store.path.read_text(encoding="utf-8") == written


def test_a_rule_whose_plugin_returns_starts_matching_again() -> None:
    _install(RegexMatch())
    pattern = "host~^web"
    rule = WatchRule(
        name="web", pattern=pattern, requires=rule_requirements(pattern, _known())
    )
    install_query_plugins()
    assert _hits(rule, _entry()) == ()

    _install(RegexMatch())
    assert _hits(rule, _entry()) == ("web",)


# --- old and new state files ------------------------------------------------


def test_a_state_file_written_before_this_phase_loads(tmp_path) -> None:
    payload = {
        "views": [{"name": "old", "query": "tag:sshd", "severity": "all"}],
        "watch_rules": [{"name": "old", "pattern": "refused", "action": "both"}],
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    state = StateStore(root=tmp_path).load()
    assert state.views[0].requires == ()
    assert state.watch_rules[0].requires == ()
    assert state.views[0].query == "tag:sshd"


def test_a_state_file_written_after_this_phase_loads_without_the_plugin(
    tmp_path,
) -> None:
    payload = {
        "views": [{"name": "new", "query": "host~^web", "requires": ["field-regex"]}],
        "watch_rules": [
            {"name": "new", "pattern": "host~^web", "requires": ["field-regex"]}
        ],
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    state = StateStore(root=tmp_path).load()
    assert state.views[0].requires == ("field-regex",)
    assert state.watch_rules[0].requires == ("field-regex",)
    assert unsatisfied(state.views[0].requires) == ("field-regex",)


def test_a_hand_edited_requires_drops_the_bad_element_not_the_list(tmp_path) -> None:
    payload = {
        "views": [{"name": "v", "query": "x", "requires": ["good", 7, None]}],
        "watch_rules": [{"name": "r", "pattern": "x", "requires": ["good", 7]}],
    }
    (tmp_path / "session.json").write_text(json.dumps(payload), encoding="utf-8")

    state = StateStore(root=tmp_path).load()
    assert state.views[0].requires == ("good",)
    assert state.watch_rules[0].requires == ("good",)


def test_requires_that_is_not_a_list_degrades_to_empty(tmp_path) -> None:
    payload = {"watch_rules": [{"name": "r", "pattern": "x", "requires": "nope"}]}
    (tmp_path / "session.json").write_text(json.dumps(payload), encoding="utf-8")
    assert StateStore(root=tmp_path).load().watch_rules[0].requires == ()


# --- zero query plugins costs nothing --------------------------------------


def test_with_no_query_plugins_the_grammar_is_what_it_always_was() -> None:
    """Requirement 10, asserted on the built artefacts themselves."""

    install_query_plugins()
    assert query_module._TERM_RE.pattern == (
        r"^(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P<op>>=|<=|!=|>|<|=|:)(?P<value>.*)$"
    )
    assert set(query_module._QUOTE_AFTER) == set(":=<>!")
    assert query_module._HAS_OPERATOR.search("no operators here") is None
    assert query_module._HAS_OPERATOR.search("tag:sshd") is not None


def test_uninstalling_restores_the_grammar_exactly() -> None:
    before = query_module._TERM_RE.pattern
    _install(RegexMatch(), Length())
    assert query_module._TERM_RE.pattern != before
    install_query_plugins()
    assert query_module._TERM_RE.pattern == before


def test_with_no_query_plugins_nothing_is_required_and_nothing_is_missing() -> None:
    install_query_plugins()
    assert provided_plugins() == frozenset()
    assert requirements("host~^web tag:sshd", _known()) == ()
    assert unsatisfied(()) == ()
    assert computed_field_names() == frozenset()


def test_a_plain_regex_is_still_passed_through_untouched_with_plugins_loaded() -> None:
    """The compatibility property the whole module rests on, under a plugin."""

    _install(RegexMatch(), Length())
    for text in ("timeout|refused", "don't  panic", '"disk full"', "10:30:00"):
        assert parse_query(text, _known()).text == text
        assert parse_query(text, _known()).terms == ()


# --- the shipped worked example --------------------------------------------
#
# `clv/examples/field_regex.py` is written into every operator's plugin
# directory on first run. It is exercised here rather than only read, so it
# cannot rot into something that no longer loads while still being handed out.


def test_the_worked_example_loads_and_works() -> None:
    from clv.examples.field_regex import EntryAge, NotRegexMatch, RegexMatch

    registry, _ = _install(RegexMatch(), NotRegexMatch(), EntryAge())
    assert [tuple(row.kinds) for row in registry.loaded] == [
        ("operator",),
        ("operator",),
        ("computed field",),
    ]

    entry = _entry()
    assert match_terms(entry, parse_query("host~^web[0-9]+$", _known()).terms) == MATCH_HIT
    assert match_terms(entry, parse_query("host!~^db", _known()).terms) == MATCH_HIT
    assert match_terms(entry, parse_query("host!~^web", _known()).terms) == MATCH_MISS


def test_the_worked_example_is_smart_case_like_everything_else_in_clv() -> None:
    from clv.examples.field_regex import RegexMatch

    _install(RegexMatch())
    entry = _entry()
    # Lower case matches either; a capital means you meant it.
    assert match_terms(entry, parse_query("host~WEB", _known()).terms) == MATCH_MISS
    assert match_terms(entry, parse_query("host~web", _known()).terms) == MATCH_HIT
    assert match_terms(entry, parse_query("tag~SSHD", _known()).terms) == MATCH_MISS


def test_the_worked_example_survives_a_half_typed_pattern() -> None:
    """A malformed regex is the operator mid-typing, not a fault in the plugin."""

    from clv.examples.field_regex import RegexMatch

    registry, _ = _install(RegexMatch())
    assert match_terms(_entry(), parse_query("host~(", _known()).terms) == MATCH_MISS
    # Crucially it did *not* raise, so the operator still has their plugin.
    assert not registry.is_disabled(registry.operators[0])
    assert list(registry.errors) == []


def test_the_worked_example_computes_an_age() -> None:
    from clv.examples.field_regex import EntryAge

    _install(EntryAge())
    recent = parse_line("2026-08-21T09:25:01 ERROR something")
    assert recent.timestamp is not None

    value = EntryAge().value(recent)
    assert value is not None and value.lstrip("-").isdigit()
    # An entry with no timestamp has no age, and says so rather than guessing.
    assert EntryAge().value(parse_line("a line with no date at all")) is None


def test_the_worked_example_handles_an_aware_timestamp() -> None:
    """Logs mix aware and naive stamps constantly; neither may raise."""

    from clv.examples.field_regex import EntryAge

    aware = parse_line('{"time": "2026-08-21T09:25:01+02:00", "msg": "hi"}')
    assert aware.timestamp is not None and aware.timestamp.tzinfo is not None
    assert EntryAge().value(aware) is not None


# --- invariants two constants have to keep between them ---------------------


def test_the_key_charset_and_the_key_pattern_agree() -> None:
    """`KEY_CHARS` is what a plugin token may not use, and it is a second copy.

    The rule it enforces is "a token may not look like part of a key", and the
    authority on what a key looks like is `_KEY_PATTERN`. Two hand-written
    spellings of one fact: if a character were added to the pattern and not to
    the charset, a plugin could register it as a token and recreate exactly the
    ambiguity the rule exists to refuse — with no error anywhere.
    """

    for char in query_module.KEY_CHARS:
        assert query_module._KEY_RE.fullmatch(f"a{char}"), (
            f"{char!r} is in KEY_CHARS but a key may not contain it"
        )
    # And nothing outside it is accepted, over the printable ASCII a token might
    # plausibly be drawn from.
    for code in range(33, 127):
        char = chr(code)
        accepted = query_module._KEY_RE.fullmatch(f"a{char}") is not None
        assert accepted == (char in query_module.KEY_CHARS), (
            f"{char!r}: key pattern says {accepted}, KEY_CHARS says "
            f"{char in query_module.KEY_CHARS}"
        )


@pytest.mark.parametrize("token", sorted(BUILTIN_OPERATORS))
def test_every_builtin_token_is_answered_by_compare(token: str) -> None:
    """`compare` dispatches the built-ins by hand and falls through to plugins.

    A token added to `BUILTIN_OPERATORS` without a branch in `compare` would
    fall through, find no installed operator, and return False for every entry —
    a comparison that silently matches nothing rather than one that fails.
    """

    install_query_plugins()
    term = parse_query(f"host{token}web01", _known()).terms[0]
    assert term.op == token
    # Three probes that straddle the term's value, so every ordering token has
    # one it answers True to. The fall-through returns False for all three, so
    # a single True proves a branch ran.
    answers = {term.compare(probe) for probe in ("aaa", "web01", "zzz")}
    assert True in answers, f"{token!r} matched nothing at all — no branch handled it"
