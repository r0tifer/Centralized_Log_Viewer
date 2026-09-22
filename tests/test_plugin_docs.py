"""The plugin doctrine, pinned so it cannot be un-said by accident.

``clv/plugins/AGENTS.md`` used to claim plugins "are sandboxed through defined
interfaces". They are not, and never were: ``import`` executes arbitrary code at
CLV's full privilege before any interface check runs. That sentence is gone, and
these tests exist so it cannot come back — during a later edit, and in particular
during the isolation work, which is when it would be most tempting. An isolation
host buys failure containment, not safety, and a document that blurs the two is
worse than one that promises nothing.

The reversal records are pinned for the same reason. A decision here is rewritten
with its reversal rather than deleted, so that the argument survives; a later edit
that tidies the record away would destroy exactly what it exists to preserve.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_AGENTS = REPO_ROOT / "clv" / "plugins" / "AGENTS.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_sandbox_claim_cannot_come_back() -> None:
    """The word does not appear in the plugin contract, in any casing.

    Not "is not claimed" — does not appear. A softened form ("lightly
    sandboxed", "sandbox-like") would be the same lie with a hedge, and the
    honest alternatives never need the word at all.
    """

    text = _read(PLUGIN_AGENTS)
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if "sandbox" in line.lower()
    ]
    assert not offenders, (
        "clv/plugins/AGENTS.md must not use the word 'sandbox' — plugins run at "
        "CLV's full privilege in CLV's process. Found:\n  " + "\n  ".join(offenders)
    )


def test_the_trust_model_answers_what_a_plugin_can_do() -> None:
    """The three headings a reader needs, and the claim each one carries."""

    text = _read(PLUGIN_AGENTS)
    for heading in (
        "## Trust model",
        "### What a plugin can do",
        "### What isolation does and does not do",
        "### Reviewing a third-party plugin",
    ):
        assert heading in text, f"clv/plugins/AGENTS.md lost its {heading!r} section"

    # The load-bearing sentence. Isolation may be added to this section later;
    # it may not be added in a way that turns containment into safety.
    assert "trusted code" in text.lower()
    assert "failure containment, not safety" in text.lower()


def test_the_author_conventions_state_that_they_are_not_enforced() -> None:
    """Three rules CLV asks for and cannot check, each labelled as such."""

    text = _read(PLUGIN_AGENTS)
    assert "## Conventions for plugin authors" in text
    assert "## Security and Safety" not in text, (
        "the heading promised enforcement CLV does not perform"
    )
    conventions = text.split("## Conventions for plugin authors", 1)[1].split("\n## ", 1)[0]
    assert conventions.lower().count("not enforced") >= 3


@pytest.mark.parametrize(
    ("relative_path", "reversed_claim"),
    [
        # Phase 10 gives clustering a plugin rule interface; the objection to an
        # operator-facing rules DSL stands and the docstring has to say both.
        ("clv/services/clustering.py", "rules DSL"),
        # Phase 8 adds query operators and computed fields; OR, parentheses and
        # precedence stay out, and the docstring has to say how narrow that is.
        ("clv/services/query.py", "query DSL"),
    ],
)
def test_a_reversed_non_goal_still_carries_its_record(
    relative_path: str, reversed_claim: str
) -> None:
    """The reversal is recorded at the point of use, not only in a plan file.

    A reader of the module must be able to see that the non-goal above them was
    reversed, when, and where the argument lives — otherwise the docstring reads
    as a rule the code no longer follows.
    """

    text = _read(REPO_ROOT / relative_path)
    assert reversed_claim in text
    assert "PLUGIN_TODO.md" in text, (
        f"{relative_path} states a reversed non-goal without pointing at the "
        "decision that reversed it"
    )
    assert "Reversed 2026-08-14" in text


def test_the_starring_reversal_carries_its_record() -> None:
    """Phase 9 narrowed what a `ProviderSource` is excluded from. Say so.

    The old text is the tempting thing to delete, because it now reads as
    describing behaviour the code does not have. Deleting it is what would
    destroy the argument: two thirds of that exclusion are still in force and
    are now enforced *by name* rather than by a provider source happening not to
    be a ref, and a reader who cannot see which two will assume all three or
    none.
    """

    text = _read(PLUGIN_AGENTS)

    assert "Reversed 2026-08-19" in text
    assert "SSH_TODO.md" in text
    # The part that survived has to be named, or the record says nothing useful.
    assert "glob filtering and rotated-set grouping" in text


def test_the_plugin_contract_does_not_claim_a_provider_source_cannot_be_starred() -> None:
    """The specific sentence that stopped being true.

    `isinstance(data, Path)` was the spelling of the exclusion for years; it
    became `refs.is_source_ref` when remote refs landed, and a journal ref is
    now inside that union. A contract still describing the old test would send a
    plugin author looking for a guarantee that is gone.
    """

    text = _read(PLUGIN_AGENTS)

    assert "starring, glob filtering and rotated-set grouping all test" not in text


# --- the published API, documented where an author will look ----------------


def test_the_api_surface_section_exists() -> None:
    """A published contract nobody can find is not published.

    `clv/api.py` is the promise; this section is where an author learns the
    promise exists, what it covers, and — the part that only documentation can
    carry — what it deliberately does not.
    """

    text = _read(PLUGIN_AGENTS)
    for heading in (
        "## API surface and stability",
        "### What is published",
        "### The deprecation policy",
        "### The wire form",
    ):
        assert heading in text, f"clv/plugins/AGENTS.md lost its {heading!r} section"


def test_the_deprecation_policy_states_what_is_not_covered() -> None:
    """The load-bearing half.

    "A published name is removed only on an API major" is the easy sentence and
    the one nobody misreads. The sentence that does the work is the other one:
    an import from `clv.services.*` has no promise behind it, and an author who
    is not told that will assume the whole package is the API.
    """

    text = _read(PLUGIN_AGENTS)
    policy = text.split("### The deprecation policy", 1)[1].split("\n### ", 1)[0]

    assert "removed only on an API major" in policy
    assert "DeprecationWarning" in policy
    assert "internal and may move without notice" in policy
    assert "clv.services" in policy


def test_the_two_versions_are_documented_as_two() -> None:
    """`requires_api` is the recommendation, and the docs have to say so.

    The whole value of a separately versioned API is lost if authors keep
    pinning `requires_clv` out of habit, and habit is what they will follow
    unless the document tells them which one they mean.
    """

    text = _read(PLUGIN_AGENTS)
    assert "requires_api" in text
    assert "PLUGIN_API_VERSION" in text
    assert "the one to reach for" in text or "the one to\ndeclare" in text


def test_every_example_imports_from_the_published_module() -> None:
    """`clv.plugins` still works; an example that uses it teaches the wrong path.

    A worked example is what an author copies, so an example importing the
    loader's own namespace hands out an import the deprecation policy explicitly
    does not cover.
    """

    text = _read(PLUGIN_AGENTS)
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip().startswith("from clv.plugins import")
    ]
    assert not offenders, (
        "an example imports from clv.plugins instead of clv.api:\n  "
        + "\n  ".join(offenders)
    )


def test_the_wire_form_says_why_pickle_is_not_the_answer() -> None:
    """The reason is the documentation.

    "Use `entry_to_wire`" invites the obvious question and the obvious
    workaround. `pickle` does not fail on unlucky entries; it fails on all of
    them, and an author who knows that will not go looking for a way around it.
    """

    section = _read(PLUGIN_AGENTS).split("### The wire form", 1)[1]
    assert "pickle" in section
    assert "mappingproxy" in section
    assert "WIRE_VERSION" in section


def test_an_exporter_is_told_it_can_ask_for_a_destination() -> None:
    """The attribute is useless undocumented — nobody guesses at a class attribute."""

    text = _read(PLUGIN_AGENTS)
    assert "wants_path" in text
    assert "suggested_extension" in text
    # And the compatibility rule, which is the part an existing author needs.
    assert "only** when `wants_path` is set" in text


def test_the_readme_points_authors_at_the_published_module() -> None:
    """README is the only document most people read, this one included."""

    readme = REPO_ROOT / "README.md"
    if not readme.exists():  # pragma: no cover - installed package
        pytest.skip("running from an installed package")
    text = _read(readme)
    assert "from clv.api import FilterStage" in text
    assert "from clv.plugins import FilterStage" not in text


def test_the_log_format_seam_is_documented_like_the_others() -> None:
    """A seam with no chapter is a seam only someone who read the source can use.

    Phase 7a's `LogFormat` is the first Stage C interface, and the four things
    below are the ones an author cannot infer from the type: that built-ins go
    first, that a `format_name` is four registrations rather than one, what
    `parse()` is allowed to return, and that it is charged against a *different*
    budget from a filter stage because it runs per line read.
    """

    text = _read(PLUGIN_AGENTS)

    assert "### 3. LogFormat" in text
    assert "Built-ins first, plugins second" in text
    assert "four registrations" in text
    assert "plugin_read_budget_ms" in text
    assert "`LogFormat.parse`" in text
    # The honest limit, stated beside the feature rather than left to be found.
    assert "does not re-parse what is already in the" in text


# --- Phase 8: the query seam and its degradation rule -----------------------


def test_the_clustering_interfaces_are_documented_where_the_others_are() -> None:
    """Both halves of the seam, in the numbered list, like every other kind."""

    text = _read(PLUGIN_AGENTS)

    assert "### 8. ClusterRule" in text
    assert "### 9. ShapeContributor" in text


def test_the_placeholder_invariant_is_documented_as_a_rule_not_a_habit() -> None:
    """The constraint that is obvious in CLV's source and invisible to an author.

    A digit in a placeholder is eaten by a later rule, a backslash is a group
    reference, and a pattern matching the empty string rewrites every position
    of every line. All three are refused at load, and an author who cannot find
    that here meets it as a plugin that silently did not load.
    """

    text = _read(PLUGIN_AGENTS)

    assert "digit-free" in text
    assert "backslash" in text
    assert "empty string" in text


def test_the_normalised_line_trap_is_written_down() -> None:
    """A plugin rule sees what CLV's own rules left, and the docs must say so.

    It is the one thing about this seam an author cannot discover by reading
    the interface: their pattern is handed a line that has already been through
    nine substitutions, so the obvious rule for a pod name matches nothing and
    reports nothing.
    """

    text = _read(PLUGIN_AGENTS)

    assert "already normalised" in text
    assert "api-<hex>-x2n9q" in text


def test_the_clustering_reversal_is_on_the_record_in_the_contract_too() -> None:
    """The module docstring carries it; so does the Non-Goals list.

    The query DSL reversal is recorded in both places, and clustering's was in
    only one — which left a reader of the contract's own Non-Goals with a rule
    the code no longer follows.
    """

    text = _read(PLUGIN_AGENTS)
    reversed_section = text.split("### Reversed", 1)[1]

    assert "No rules DSL for clustering" in reversed_section
    assert "settings.conf" in reversed_section


# --- Phase 11: the timeline seam and the fold rule --------------------------


def test_the_timeline_interfaces_are_documented_where_the_others_are() -> None:
    """Both halves, in the numbered list, like every other kind."""

    text = _read(PLUGIN_AGENTS)

    assert "### 10. TimelineAnnotation" in text
    assert "### 11. TimelineMetric" in text
    # Exporter was 10 and is now 12. Asserted because the renumbering is the
    # easy thing to get half-right, and a list with two number 10s reads as a
    # document nobody checked.
    assert "### 12. Exporter" in text


def test_the_fold_rule_is_documented_as_the_reason_for_the_interface() -> None:
    """The one thing about this seam an author cannot infer from the type.

    `value() -> float` looks like an oversight — where is the aggregate hook? —
    until you know that `extend` folds an arrival by arithmetic. An author who
    is not told will go looking for the missing method, and an author who is
    will understand why a median is not a feature request.
    """

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 11. TimelineMetric", 1)[1].split("\n### ", 1)[0]

    assert "foldable" in section
    assert "median" in section
    assert "CLV does the summing" in section
    # And the honest statement of what that costs, in the same breath.
    assert "unexpressible" in section


def test_the_one_metric_rule_is_written_down_with_what_it_is_not() -> None:
    """A conflict resolved by priority is not a fault, and the docs must say so.

    An operator reading "conflict" in the `P` dialog will assume something is
    broken unless the contract says otherwise — and the loser is a healthy
    plugin one switch away from being the one that runs.
    """

    text = _read(PLUGIN_AGENTS)
    section = text.split("#### One metric at a time", 1)[1].split("\n#### ", 1)[0]

    assert "priority" in section
    assert "not a fault" in section


def test_an_annotation_provider_is_told_it_runs_on_the_event_loop() -> None:
    """The constraint that decides how the plugin is written.

    A provider is the obvious place to put an HTTP call — deploys come from an
    API — and the seam cannot stop one. What it can do is say, where the author
    is looking, that the call is on the render path and that the budget will
    take a plugin that blocks out of service.
    """

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 10. TimelineAnnotation", 1)[1].split("\n### ", 1)[0]

    assert "may not do I/O" in section
    assert "setup()" in section
    assert "once per window" in section


def test_the_timeline_budget_is_documented_as_the_sixth() -> None:
    """That the timeline budget is counted, not what the running total is.

    This asserted "Six budgets, one policy" until Phase 12 added a seventh, and
    the total moved out to `test_the_panel_budget_is_documented_as_the_seventh`
    — one test owns the count and changes when a budget arrives, rather than
    every budget's test changing for every other budget.
    """

    text = _read(PLUGIN_AGENTS)
    budget = text.split("### The budget", 1)[1].split("\n### ", 1)[0]

    assert "get a sixth" in budget
    assert "TimelineMetric" in budget


def test_the_readme_says_what_a_metric_bar_is_showing() -> None:
    """The caption rule is a user-facing promise, not an implementation note."""

    readme = REPO_ROOT / "README.md"
    if not readme.exists():  # pragma: no cover - installed package
        pytest.skip("running from an installed package")
    text = _read(readme)
    section = text.split("### The severity timeline", 1)[1].split("\n### ", 1)[0]

    assert "TimelineMetric" in section
    # Wrapped in the source, so matched on the half that cannot move.
    assert "plugin supplying it" in section
    assert "foldable" in section


def test_the_query_interfaces_are_documented_where_the_others_are() -> None:
    """In the numbered list, because that is where an author looks for a signature."""

    text = _read(PLUGIN_AGENTS)
    for heading in ("### 4. QueryOperator", "### 5. ComputedField"):
        assert heading in text, f"clv/plugins/AGENTS.md lost its {heading!r} section"


def test_the_reserved_tokens_are_documented_as_a_rule_not_a_list() -> None:
    """An author should be able to check, not discover it from a load error."""

    text = _read(PLUGIN_AGENTS)
    assert "BUILTIN_OPERATORS" in text
    assert "Longest token wins" in text
    # The ambiguity rule is the one nobody guesses, so it is written out.
    assert "could not be told from a key called" in text


def test_the_resolution_order_for_a_computed_field_is_stated() -> None:
    text = _read(PLUGIN_AGENTS)
    assert "Parsed fields resolve first, per entry." in text
    assert "it can never change what a line said" in text


def test_the_two_absences_are_documented_separately() -> None:
    """Collapsing them is the mistake this section exists to prevent.

    An uninstalled plugin makes a saved thing *unusable*; a switched-off one
    keeps its token reserved so the query still means what it meant. A document
    that said only "the plugin is missing" would leave an operator unable to
    tell why one case marked their views broken and the other did not.
    """

    text = _read(PLUGIN_AGENTS)
    assert "## Saved views, watch rules and a missing plugin" in text
    assert "not installed" in text
    assert "switched off" in text
    assert "is not in service" in text
    assert "Nothing is ever rewritten." in text


def test_the_query_dsl_reversal_is_on_the_record() -> None:
    """Rewritten with its reversal rather than deleted — the rule from TODO.md."""

    text = _read(PLUGIN_AGENTS)
    assert '- **"No query DSL."** *Reversed' in text
    # And the reversal stays narrow, in the same sentence that offers it.
    reversal = text.split('- **"No query DSL."**', 1)[1].split("- **", 1)[0]
    for word in ("OR", "parentheses", "precedence"):
        assert word in reversal, f"the reversal must still decline {word}"
    assert "vocabulary" in reversal and "structure" in reversal


# --- commands and controls (Phase 12) ----------------------------------------


def test_the_command_seam_is_documented_where_the_others_are() -> None:
    """The thirteenth interface, in the numbered list with the twelve before it.

    Not an appendix. An author reading down the interfaces reaches `Command`
    where they reach everything else, which is the only arrangement in which
    "a plugin extends a core feature on equal terms" is true of the document as
    well as of the code.
    """

    text = _read(PLUGIN_AGENTS)
    assert "### 13. Command" in text
    section = text.split("### 13. Command", 1)[1].split("\n## ", 1)[0]
    for word in ("command_name", "title", "CommandContext", "Panel", "Control"):
        assert word in section, f"the Command section never mentions {word}"


def test_the_published_table_lists_exactly_what_is_published() -> None:
    """Checked against `clv.api`, not against a second list written by hand.

    Four interfaces — `ClusterRule`, `ShapeContributor`, `TimelineAnnotation`
    and `TimelineMetric` — were published by Phases 10 and 11 and never added to
    this table, and nothing noticed for two phases. A table that restates
    `__all__` in prose will go stale; one that is *compared* to it cannot.
    """

    from clv import api

    text = _read(PLUGIN_AGENTS)
    table = text.split("### What is published", 1)[1].split("###", 1)[0]
    missing = [name for name in api.__all__ if f"`{name}`" not in table]
    # The severity constants are listed as a range (`LEVEL_TRACE` … `LEVEL_CRITICAL`)
    # rather than one by one, which is the right call for a table a human reads.
    missing = [
        name
        for name in missing
        if not (name.startswith("LEVEL_") and name not in ("LEVEL_ORDER",))
    ]
    assert missing == [], f"published but undocumented: {missing}"


def test_a_command_author_is_told_it_runs_on_the_event_loop() -> None:
    """The one failure mode this seam has and the others do not.

    A `FilterStage` that is slow is disabled by a budget. A `Command` that hangs
    hangs CLV, because a stopwatch cannot interrupt a call the process is inside
    — so the only honest answer is to say so, and to name what will fix it.
    """

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 13. Command", 1)[1].split("\n## ", 1)[0]
    assert "#### You run on the event loop, synchronously" in section
    assert "freezes the pane" in section
    assert "no budget that can save you" in section
    assert "Phase 13" in section


def test_the_refusal_of_show_true_is_documented_with_its_reason() -> None:
    """Requirement 11, in the document as well as in the loader.

    A refusal an author cannot find the reason for reads as a bug in CLV.
    """

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 13. Command", 1)[1].split("\n## ", 1)[0]
    assert "refused with a reason" in section
    assert "80-column floor" in section
    assert "invocable by name from `C`" in section


def test_the_no_css_rule_is_stated_as_a_rule_with_its_reason() -> None:
    """The concession that keeps every breakpoint test unconditional."""

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 13. Command", 1)[1].split("\n## ", 1)[0]
    assert "**Plugins ship no CSS.**" in section
    assert "breakpoint test" in section


def test_the_queued_context_is_explained_rather_than_just_described() -> None:
    """Why `notify` queues is the whole argument for the shape of the type.

    Handing over a bound method is the obvious implementation and it defeats the
    rule it appears to honour. A document that only listed the four methods
    would leave the next author to re-derive that, or not.
    """

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 13. Command", 1)[1].split("\n## ", 1)[0]
    assert "__self__" in section and "__closure__" in section
    assert "queues" in section or "queue" in section


def test_the_panel_budget_is_documented_as_the_seventh() -> None:
    """Six became seven, and the sentence that counted them has to keep up."""

    text = _read(PLUGIN_AGENTS)
    assert "**Seven budgets, one policy.**" in text
    budget = text.split("### The budget", 1)[1].split("###", 1)[0]
    assert "seventh" in budget
    assert "identical in all seven" in budget


def test_the_control_vocabulary_is_listed_for_an_author_to_check() -> None:
    """`CONTROL_KINDS` published, and the kinds written out where they are used."""

    text = _read(PLUGIN_AGENTS)
    section = text.split("### 13. Command", 1)[1].split("\n## ", 1)[0]
    for kind in ("label", "static", "switch", "input", "select", "button"):
        assert f"`{kind}`" in section, f"the vocabulary never documents {kind}"
    assert "MAX_PANEL_CONTROLS" in section
