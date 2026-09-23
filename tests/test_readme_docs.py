"""The user-facing promises about remote sources, pinned so they cannot lapse.

`tests/test_plugin_docs.py` does this for the plugin contract; this is the same
idea for `README.md`, and it exists because of what a README *is*: the only
document most operators read, and the one that governs what they believe CLV
does with their credentials and their machines.

Three kinds of statement are pinned here, and each is pinned for its own reason.

**The security positions** — no password, no `sudo`, no disabling host key
verification. These are enforced in code and asserted by `test_ssh_source.py`,
so the risk is not that they stop being true; it is that they stop being *said*.
An operator who cannot find the answer to "where do I put the password" in the
documentation reasonably concludes there is a way and they have missed it.

**The section itself.** README carried a paragraph saying the remote reference
material was not written yet. Deleting that paragraph without writing the
material is a one-line edit that looks like progress, and this is what fails.

**`sshfs`.** Naming the alternative costs nothing and buys trust, and it is the
first thing a tidy-up drops, because it reads like an advertisement for someone
else's software. It is not: for a user who already mounts their servers it is
the better answer, and saying so is what makes the rest of the section credible.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

pytestmark = pytest.mark.skipif(
    not README.exists(), reason="running from an installed package"
)


def _read() -> str:
    return README.read_text(encoding="utf-8")


def test_the_remote_section_exists() -> None:
    assert "### Remote sources over SSH" in _read()


def test_the_deferral_paragraph_is_gone() -> None:
    """It said the section below "lands with the release". The release landed."""

    text = _read()
    assert "not documented here yet" not in text
    assert "that section lands with the release" not in text


def test_the_settings_table_documents_the_master_switch() -> None:
    """An option nobody can find is an option that does not exist."""

    assert "| `enable_ssh` |" in _read()


def test_the_host_section_schema_is_documented() -> None:
    """Every `[ssh:<name>]` option, including the five the dialog cannot show."""

    text = _read()
    assert "### Remote host sections" in text
    for option in (
        "`host`",
        "`user`",
        "`port`",
        "`identity_file`",
        "`log_dirs`",
        "`enabled`",
        "`include_globs`",
        "`max_files`",
        "`max_buffer_lines`",
        "`correct_clock_skew`",
    ):
        assert option in text, f"README does not document {option}"


def test_the_no_password_position_is_stated() -> None:
    """Requirement 9, said out loud where an operator will look for it."""

    text = _read().lower()
    assert "there is no password option" in text
    assert "ssh-agent" in text


def test_the_no_sudo_position_is_stated_with_the_alternative() -> None:
    """Refusing without naming the fix is how a refusal reads as a limitation."""

    text = _read()
    assert "no `sudo` option" in text
    assert "adm" in text and "systemd-journal" in text


def test_host_key_verification_is_documented_as_never_disabled() -> None:
    text = _read()
    assert "Host key verification is never disabled" in text
    assert "StrictHostKeyChecking" in text


def test_sshfs_is_named_as_an_alternative() -> None:
    """With its trade-off, not as a footnote."""

    text = _read()
    assert "sshfs" in text
    assert "per-file round trip" in text


def test_what_degrades_on_a_non_gnu_remote_is_documented() -> None:
    """Requirement 5 is only kept if the degradation is written down."""

    text = _read()
    for profile in ("`gnu`", "`busybox`", "`bsd`", "`posix`"):
        assert profile in text, f"README does not name the {profile} profile"


def test_every_internal_link_resolves() -> None:
    """A `](#anchor)` that names no heading is a dead link in the shipped docs.

    Cheap to check and easy to break: the remote section is reached by link from
    two other places, so renaming its heading without following the references
    would leave the settings table pointing at nothing.
    """

    import re

    text = _read()
    headings = set()
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match is None:
            continue
        slug = re.sub(r"[`*_]", "", match.group(1).strip().lower())
        slug = re.sub(r"[^\w\s-]", "", slug)
        headings.add(re.sub(r"\s+", "-", slug))

    links = set(re.findall(r"\]\(#([^)]+)\)", text))

    assert links, "the anchors this guards have gone"
    assert links <= headings, f"README links to missing headings: {links - headings}"


# --- installing a plugin ----------------------------------------------------


def _plugin_chapter() -> str:
    """The `## Plugins` chapter, from its heading to the next chapter.

    Sliced on the chapter heading rather than on `## Installing a plugin`, which
    is what these tests used before the chapter existed. That string is still
    present -- as `### Installing a plugin` -- so the old slice kept passing
    after the promotion, silently, while selecting a region that started
    mid-chapter. A test that passes for a reason that is no longer true is worse
    than one that fails.
    """

    text = _read()
    assert "\n## Plugins\n" in text, "README.md lost its `## Plugins` chapter"
    return text.split("\n## Plugins\n", 1)[1].split("\n## ", 1)[0]


def test_the_plugin_chapter_exists() -> None:
    """One chapter, with the sections an operator needs inside it.

    Phase 16 promoted two separate `##` sections into one. The sub-headings keep
    their exact text, because a slug is built from the text and
    `#running-a-plugin-where-it-can-be-stopped` is linked twice from elsewhere
    in this file -- `test_every_internal_link_resolves` is what would catch a
    rename, and it should not be the only thing standing between an operator and
    a dead link.
    """

    chapter = _plugin_chapter()
    for heading in (
        "### Installing a plugin",
        "### Installing a packaged plugin",
        "### A worked example, already on your machine",
        "### Managing what is installed",
        "### Running a plugin where it can be stopped",
        "### Writing a plugin",
    ):
        assert heading in chapter, f"the Plugins chapter lost {heading!r}"


def test_readme_says_where_a_plugin_is_installed() -> None:
    """The whole point of Phase 3 is that this path is findable.

    An operator on a frozen build has no `pip`, no source tree, and no reason
    to guess at `~/.config/clv/plugins/`. If the README does not name it, the
    install path exists and nobody can use it.
    """

    text = _read()

    assert "~/.config/clv/plugins/" in text
    assert "### Installing a plugin" in _plugin_chapter()


def test_readme_says_a_dropped_file_does_not_run() -> None:
    """The rule most likely to be lost in a tidy-up, and the one that matters.

    An operator who believes copying a file in is enough will conclude the
    feature is broken; one who believes it runs immediately will treat the
    directory as more dangerous than it is. Both need the sentence.
    """

    section = _plugin_chapter()

    assert "plugins = redact_secrets" in section
    assert "does not run it" in section.lower() or "not run" in section.lower()


def test_readme_calls_a_plugin_trusted_code_where_it_says_to_install_one() -> None:
    """Said at the point of the decision, not only in a document nobody opened.

    `tests/test_plugin_docs.py` pins the trust model in `clv/plugins/AGENTS.md`;
    this pins the two sentences of it that have to survive in the file an
    operator actually reads before copying a stranger's file into their home
    directory.
    """

    section = _plugin_chapter()

    assert "trusted code" in section
    assert "your privileges" in section
    assert "AGENTS.md" in section


def test_readme_documents_the_plugins_setting() -> None:
    """The settings table is the third copy of this key, and the one that drifts."""

    text = _read()

    assert "| `plugins` |" in text


def test_readme_says_the_operator_set_is_extensible() -> None:
    """Field queries is where someone reads what they can type."""

    text = _read()
    section = text.split("### Field queries", 1)[1].split("### Navigating", 1)[0]
    assert "The operator set is extensible" in section
    assert "field_regex" in section


def test_readme_says_a_saved_view_records_what_its_query_needs() -> None:
    """The degradation rule, where the operator meets its consequence."""

    text = _read()
    section = text.split("### Field queries", 1)[1].split("### Navigating", 1)[0]
    assert "records which plugins its query needs" in section
    assert "refused rather than applied" in section
    # And says nothing was rewritten, which is the half people assume otherwise.
    assert "nothing was rewritten" in section


# --- the suite count the documentation quotes --------------------------------

AGENTS = REPO_ROOT / "AGENTS.md"

#: ``2454 passed, 1 skipped, 11 deselected`` wherever a document claims it.
_COUNT = re.compile(
    r"(?P<passed>\d+) passed, (?P<skipped>\d+) skipped, (?P<deselected>\d+) deselected"
)


def _quoted_counts(path: Path) -> list[re.Match[str]]:
    return list(_COUNT.finditer(path.read_text(encoding="utf-8")))


def test_the_documented_suite_counts_agree_with_each_other() -> None:
    """Two documents quote the figure and both are edited by hand.

    `README.md` prints it in the development section and `AGENTS.md` prints it in
    its testing section. Nothing connected them, so a phase that refreshed one
    left the other stating a number from several phases earlier -- which is how
    `README.md` came to claim 290 tests against a suite of 717.
    """

    quoted = {
        path.name: [match.group(0) for match in _quoted_counts(path)]
        for path in (README, AGENTS)
        if path.exists()
    }
    for name, matches in quoted.items():
        assert matches, f"{name} quotes no suite count at all"

    distinct = {figure for matches in quoted.values() for figure in matches}
    assert len(distinct) == 1, f"the documents disagree about the suite: {quoted}"


@pytest.mark.skipif(not AGENTS.exists(), reason="running from an installed package")
def test_the_documented_suite_count_is_what_the_suite_collects() -> None:
    """The check `PLUGIN_TODO.md` deferred to Phase 16, finally written.

    Collection rather than a run: it costs a second or two instead of ten
    minutes, and it catches the failure that actually happens -- a phase adding
    tests and not refreshing the number. It cannot catch a test that newly skips
    or newly fails, and it does not pretend to: what is asserted is
    ``passed + skipped == selected``, which is true of any green run.

    `-p no:cacheprovider` so collecting does not write a cache into the tree,
    and `--collect-only` so nothing recurses.
    """

    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    tally = re.search(
        r"(?P<selected>\d+)/(?P<total>\d+) tests collected"
        r"(?: \((?P<deselected>\d+) deselected\))?",
        done.stdout,
    )
    if tally is None:  # pragma: no cover - a collection error, not a count drift
        pytest.fail(
            "could not read a collection tally from pytest:\n"
            + done.stdout[-2000:]
            + done.stderr[-2000:]
        )

    selected = int(tally.group("selected"))
    deselected = int(tally.group("deselected") or 0)

    claimed = _quoted_counts(README)[0]
    documented = int(claimed.group("passed")) + int(claimed.group("skipped"))

    assert documented == selected, (
        f"the documentation says {claimed.group(0)!r}, which is {documented} "
        f"tests; the suite collects {selected}. Refresh README.md and AGENTS.md."
    )
    assert int(claimed.group("deselected")) == deselected, (
        f"the documentation says {claimed.group('deselected')} deselected; "
        f"the suite deselects {deselected}."
    )
