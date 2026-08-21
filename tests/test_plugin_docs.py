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
