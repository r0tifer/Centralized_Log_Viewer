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
