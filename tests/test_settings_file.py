"""The settings file is the operator's, and an edit may only move what it names.

Every assertion here is on the **full text** of the file rather than on what
``configparser`` makes of it. A parse-level assertion passes just as happily when
every comment in the file has been thrown away, and the comments are two thirds
of the shipped ``settings.conf``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clv.services import persist_log_sources, persist_setting
from clv.services.settings_file import SettingsDocument, spans_of


REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the section index ------------------------------------------------------


def test_a_section_ends_before_the_next_ones_banner() -> None:
    """A comment block introducing the next section is not part of this one.

    This is the whole basis of the removal rule: everything below is safe only
    because ``end`` stops short of text that visibly belongs to what follows.
    """

    lines = [
        "[log_viewer]",
        "refresh_hz = 2",
        "",
        "# -------------------",
        "# Remote sources",
        "# -------------------",
        "[ssh:web01]",
        "host = web01.internal",
    ]

    spans = {span.name: span for span in spans_of(lines)}

    assert spans["log_viewer"].end == 2, "the banner belongs to what follows it"
    assert spans["ssh:web01"].end == 8


def test_an_empty_section_has_an_empty_body() -> None:
    spans = {span.name: span for span in spans_of(["[log_viewer]", "", "[ssh:a]"])}

    assert spans["log_viewer"].start == spans["log_viewer"].end == 1


# --- the two hazards that motivated the module ------------------------------


def test_a_global_option_lands_in_log_viewer_not_in_the_last_host(tmp_path: Path) -> None:
    """H10 — the bug the advanced drawer's SSH switch would have shipped with.

    Appending at end of file put ``enable_ssh`` inside ``[ssh:web01]``, where
    ``_parse_host`` ignores it and the refused-key table does not name it — so
    there was not even a warning. The switch reported success and was off again
    on the next launch.
    """

    config = tmp_path / "settings.conf"
    config.write_text(
        "[log_viewer]\nrefresh_hz = 2\n\n[ssh:web01]\nhost = web01.internal\n",
        encoding="utf-8",
    )

    persist_setting(config, "enable_ssh", "true")

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\n"
        "refresh_hz = 2\n"
        "enable_ssh = true\n"
        "\n"
        "[ssh:web01]\n"
        "host = web01.internal\n"
    )


def test_log_dirs_never_rewrites_a_remote_hosts_directories(tmp_path: Path) -> None:
    """H3 — ``log_dirs`` is a key in both schemas, and ``Ctrl+S`` writes it.

    A ``[log_viewer]`` relying on the default plus any host section meant the
    first ``log_dirs`` in the file belonged to the *host*, and saving a local
    source replaced that machine's roots with paths from this one.
    """

    config = tmp_path / "settings.conf"
    config.write_text(
        "[log_viewer]\nrefresh_hz = 2\n\n[ssh:web01]\nlog_dirs = /var/log, /srv/app\n",
        encoding="utf-8",
    )

    persist_log_sources(config, [Path("/opt/local.log")])

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\n"
        "refresh_hz = 2\n"
        "log_dirs = /opt/local.log\n"
        "\n"
        "[ssh:web01]\n"
        "log_dirs = /var/log, /srv/app\n"
    )


# --- preservation -----------------------------------------------------------


def test_editing_one_key_moves_exactly_one_line(tmp_path: Path) -> None:
    original = (
        "# CLV settings\n"
        "\n"
        "[log_viewer]\n"
        "# How often the tail redraws, in hertz.\n"
        "refresh_hz = 2\n"
        "\n"
        "# Remote sources over SSH.\n"
        "enable_ssh = false\n"
        "\n"
        "[ssh:web01]\n"
        "# Reached through the bastion; see ~/.ssh/config.\n"
        "host = web01.internal\n"
    )
    config = tmp_path / "settings.conf"
    config.write_text(original, encoding="utf-8")

    persist_setting(config, "enable_ssh", "true")

    assert config.read_text(encoding="utf-8") == original.replace(
        "enable_ssh = false", "enable_ssh = true"
    )


def test_a_key_the_writer_does_not_own_survives_an_edit(tmp_path: Path) -> None:
    """H15 — including a refused one, which must keep producing its warning.

    ``config.py`` reports ``password`` as unsupported on every load. Silently
    eating the line during an unrelated edit would leave an operator concluding
    CLV had accepted and stored it, which is the one impression this project may
    never give.
    """

    config = tmp_path / "settings.conf"
    config.write_text(
        "[ssh:web01]\n"
        "host = web01.internal\n"
        "password = hunter2\n"
        "max_files = 2000\n",
        encoding="utf-8",
    )

    document = SettingsDocument.load(config)
    document.set("ssh:web01", "host", "10.0.0.9")
    document.save(config)

    assert config.read_text(encoding="utf-8") == (
        "[ssh:web01]\n"
        "host = 10.0.0.9\n"
        "password = hunter2\n"
        "max_files = 2000\n"
    )


def test_an_indented_option_keeps_its_indentation(tmp_path: Path) -> None:
    """Step 0's slice bug: ``leading`` was computed against a stripped line.

    ``line[: len(line) - len(line.strip())]`` counts trailing whitespace as
    leading, so a trailing space on the ``log_dirs`` line made the slice eat real
    characters and the file came back with ``loglog_dirs = ...``.
    """

    config = tmp_path / "settings.conf"
    config.write_text("[log_viewer]\n    log_dirs = /var/log   \n", encoding="utf-8")

    persist_log_sources(config, [Path("/opt/extra.log")])

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\n    log_dirs = /var/log, /opt/extra.log\n"
    )


# --- whole sections ---------------------------------------------------------


def test_removing_a_section_never_touches_a_line_above_its_header(tmp_path: Path) -> None:
    """H4 — against the shipped file, because that is where the trap is.

    ``settings.conf`` ends *inside* a commented-out ``# [ssh:db02]`` example with
    no blank line after it. Append one real section and a "the comment block
    above a header belongs to it" rule would delete an example the operator never
    wrote and cannot recover.
    """

    shipped = (REPO_ROOT / "settings.conf").read_text(encoding="utf-8")
    config = tmp_path / "settings.conf"
    config.write_text(shipped, encoding="utf-8")

    document = SettingsDocument.load(config)
    document.add_section("ssh:web01", [("host", "web01.internal"), ("log_dirs", "/var/log")])
    document.save(config)

    assert config.read_text(encoding="utf-8") == (
        shipped + "\n[ssh:web01]\nhost = web01.internal\nlog_dirs = /var/log\n"
    )

    document = SettingsDocument.load(config)
    assert document.remove_section("ssh:web01") is True
    document.save(config)

    assert config.read_text(encoding="utf-8") == shipped
    assert "# [ssh:db02]" in config.read_text(encoding="utf-8")


def test_removing_a_section_takes_its_own_comments_with_it(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(
        "[log_viewer]\n"
        "refresh_hz = 2\n"
        "\n"
        "# --- Hosts ---\n"
        "[ssh:web01]\n"
        "# The app servers.\n"
        "host = web01.internal\n"
        "\n"
        "[ssh:db02]\n"
        "host = 10.0.0.12\n",
        encoding="utf-8",
    )

    document = SettingsDocument.load(config)
    document.remove_section("ssh:web01")
    document.save(config)

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\n"
        "refresh_hz = 2\n"
        "\n"
        "# --- Hosts ---\n"
        "\n"
        "[ssh:db02]\n"
        "host = 10.0.0.12\n"
    )


def test_a_duplicated_section_is_removed_in_full(tmp_path: Path) -> None:
    """One host to ``configparser``, so half a removal resurrects it."""

    config = tmp_path / "settings.conf"
    config.write_text(
        "[ssh:web01]\nhost = a\n\n[ssh:db02]\nhost = b\n\n[ssh:web01]\nhost = c\n",
        encoding="utf-8",
    )

    document = SettingsDocument.load(config)
    document.remove_section("ssh:web01")
    document.save(config)

    # The blank that separated the two survives as a leading one. Cosmetic,
    # and preferable to a rule that consumes blank lines around a removal and
    # so has to decide which section a separator belonged to.
    assert config.read_text(encoding="utf-8") == "\n[ssh:db02]\nhost = b\n"


def test_a_new_section_opens_after_exactly_one_blank_line(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text("[log_viewer]\nrefresh_hz = 2\n", encoding="utf-8")

    document = SettingsDocument.load(config)
    document.add_section("ssh:web01", [("host", "web01.internal")])
    document.save(config)

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\nrefresh_hz = 2\n\n[ssh:web01]\nhost = web01.internal\n"
    )


def test_setting_an_option_in_an_absent_section_creates_the_section(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text("[log_viewer]\nrefresh_hz = 2\n", encoding="utf-8")

    document = SettingsDocument.load(config)
    document.set("ssh:web01", "host", "web01.internal")
    document.save(config)

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\nrefresh_hz = 2\n\n[ssh:web01]\nhost = web01.internal\n"
    )


def test_removing_an_option_leaves_the_rest_of_the_body(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(
        "[ssh:web01]\nhost = a\n# keep me\nuser = ops\nport = 2222\n", encoding="utf-8"
    )

    document = SettingsDocument.load(config)
    assert document.remove_option("ssh:web01", "user") is True
    assert document.remove_option("ssh:web01", "identity_file") is False
    document.save(config)

    assert config.read_text(encoding="utf-8") == (
        "[ssh:web01]\nhost = a\n# keep me\nport = 2222\n"
    )


def test_reading_an_option_is_scoped_to_its_section(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(
        "[log_viewer]\nlog_dirs = /opt/local\n\n[ssh:web01]\nlog_dirs = /var/log\n",
        encoding="utf-8",
    )

    document = SettingsDocument.load(config)

    assert document.get("log_viewer", "log_dirs") == "/opt/local"
    assert document.get("ssh:web01", "log_dirs") == "/var/log"
    assert document.get("ssh:web01", "user") is None
    assert document.get("ssh:nope", "user") is None
    assert document.sections() == ["log_viewer", "ssh:web01"]


# --- durability -------------------------------------------------------------


def test_a_failed_write_leaves_the_original_intact(tmp_path: Path, monkeypatch) -> None:
    """A truncated write here costs every host and every comment at once."""

    original = "[log_viewer]\nrefresh_hz = 2\n"
    config = tmp_path / "settings.conf"
    config.write_text(original, encoding="utf-8")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("clv.services.settings_file.os.replace", explode)

    document = SettingsDocument.load(config)
    document.set("log_viewer", "refresh_hz", "8")
    with pytest.raises(OSError):
        document.save(config)

    assert config.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*clv-tmp")) == [], "the temp file is cleaned up"


def test_a_missing_file_is_seeded_with_the_global_section(tmp_path: Path) -> None:
    config = tmp_path / "nested" / "settings.conf"

    persist_setting(config, "enable_ssh", "true")

    assert config.read_text(encoding="utf-8") == "[log_viewer]\nenable_ssh = true\n"


def test_an_empty_value_is_written_without_trailing_whitespace() -> None:
    """`include_globs =` is how the shipped template writes "no globs", and this
    file is one operators read and diff."""
    document = SettingsDocument(["[log_viewer]", "include_globs = *.log"])

    document.set("log_viewer", "include_globs", "")
    document.set("log_viewer", "exclude_globs", "")

    assert "include_globs =" in document.lines
    assert "exclude_globs =" in document.lines
    assert not any(line.endswith(" ") for line in document.lines)
