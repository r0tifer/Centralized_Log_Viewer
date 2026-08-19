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
