"""How an operator upgrading to a new build learns that settings were added.

The problem this covers is not correctness. Every option is optional-with-a-
default, so an old settings file keeps working indefinitely — verified below
against a file that predates half the schema. The problem is that it stops
*learning*: `ensure_user_settings_file` returns early when the file exists, so
the prose documenting a new option only ever reaches a first-run user, and the
shipped reference sits unread beside the binary.

The launch path's answers stay read-only: CLV does not rewrite the operator's
settings file while starting up, and the notice below is asserted not to. The
third answer, `clv --upgrade-config`, does rewrite it — but only when asked, and
only after copying the previous file aside. Both halves are covered here.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from clv.app import LogViewerApp, main
from clv.services.config import (
    CONFIG_SECTION,
    CURRENT_CONFIG_VERSION,
    DEFAULT_SETTINGS_TEMPLATE,
    config_version_of,
    default_config_text,
    load_config,
    template_config_version,
    undocumented_settings,
    user_config_path,
)
from clv.services.config_upgrade import upgrade_user_settings
from clv.services.settings_file import SettingsDocument
from clv.storage import SessionState


def _run(scenario) -> None:
    asyncio.run(scenario())


ANCIENT = """\
[log_viewer]
# The operator's own comment, from a much older build.
log_dirs = /var/log
max_buffer_lines = 500
refresh_hz = 4
"""


# --- an old file keeps working ----------------------------------------------


def test_a_settings_file_predating_the_schema_still_loads_cleanly(tmp_path: Path) -> None:
    """No setting is *required*, which is why no migration is required either."""

    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    parsed = load_config(config)

    assert parsed.issues == ()
    assert parsed.enable_ssh is False
    assert parsed.hosts == ()
    assert parsed.max_buffer_lines == 500


# --- the reference, one command away ----------------------------------------


def test_print_default_config_writes_the_shipped_file_and_exits(capsys) -> None:
    assert main(["--print-default-config"]) == 0

    printed = capsys.readouterr().out

    assert printed == default_config_text()
    assert printed.startswith("[log_viewer]")
    # The documentation is the point: a stripped file would print and teach
    # nothing, which is the failure this flag exists to fix.
    assert "# [ssh:web01]" in printed
    assert "enable_ssh" in printed


def test_the_reference_is_the_shipped_file_and_falls_back_to_the_builtin(
    monkeypatch, tmp_path: Path
) -> None:
    """Same precedence `ensure_user_settings_file` uses, so what this prints is
    what a first run would actually have written."""

    absent = tmp_path / "nowhere" / "settings.conf"
    monkeypatch.setattr("clv.services.config.bundled_config_path", lambda: absent)

    assert default_config_text() == DEFAULT_SETTINGS_TEMPLATE


def test_version_is_printed_so_a_stale_build_is_identifiable(capsys) -> None:
    """"The feature is missing" and "the build is old" look identical from the
    UI, and one of them is fixed by upgrading."""

    from clv import __version__

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


# --- the delta ---------------------------------------------------------------


def test_the_delta_names_what_the_file_does_not_carry(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    missing = undocumented_settings(config)

    assert "enable_ssh" in missing
    assert "enable_journald" in missing
    assert "log_dirs" not in missing, "the file carries this one"
    assert "refresh_hz" not in missing


def test_a_file_carrying_everything_reports_nothing(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(default_config_text(), encoding="utf-8")

    assert undocumented_settings(config) == ()


def test_host_sections_are_not_reported_as_missing_settings(tmp_path: Path) -> None:
    """A `[ssh:<name>]` section is a machine the operator named, not a setting
    they are missing. Listing the shipped example host as an absence would be
    nonsense, and it would never stop being reported."""

    config = tmp_path / "settings.conf"
    config.write_text(
        default_config_text() + "\n[ssh:web01]\nlog_dirs = /var/log\n",
        encoding="utf-8",
    )

    assert undocumented_settings(config) == ()


def test_a_missing_or_unreadable_file_reports_nothing(tmp_path: Path) -> None:
    assert undocumented_settings(None) == ()
    assert undocumented_settings(tmp_path / "absent.conf") == ()


# --- the notice --------------------------------------------------------------


def _panel_text(app: LogViewerApp) -> str:
    """Everything the log pane is currently showing, as plain text.

    `LogView` stores rows rather than lines, and a summary row is a `Text` the
    app handed it — so this reads what was written rather than what was painted,
    which keeps the assertion about content and not about wrapping.
    """

    return "\n".join(str(row.renderable) for row in app.log_panel._rows)


def test_the_notice_appears_once_after_an_upgrade(tmp_path: Path) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp(config=load_config(config))
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._settings_path = config
            app.state = replace(app.state, last_seen_version="2.0.0")
            app._upgrade_notice_shown = False

            app._show_discovery_summary(app._report)
            await pilot.pause()
            first = _panel_text(app)

            assert "settings your settings file does not carry" in first
            assert "clv --print-default-config" in first
            assert "already using its default" in first, (
                "an operator must not read this as something being broken"
            )

            # A rescan redraws the summary; the notice does not come back.
            app._show_discovery_summary(app._report)
            await pilot.pause()
            assert "does not carry" not in _panel_text(app)

    _run(scenario)


def test_a_first_run_is_not_greeted_with_a_migration_notice(tmp_path: Path) -> None:
    """There is nothing to have upgraded *from*, and the file a first run writes
    is a copy of the reference anyway."""

    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp(config=load_config(config))
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._settings_path = config
            app.state = replace(app.state, last_seen_version="")
            app._upgrade_notice_shown = False

            app._show_discovery_summary(app._report)
            await pilot.pause()

            assert "does not carry" not in _panel_text(app)
            # But the version is recorded, so the *next* upgrade does report.
            assert app.state.last_seen_version != ""

    _run(scenario)


def test_the_same_version_says_nothing(tmp_path: Path) -> None:
    from clv import __version__

    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp(config=load_config(config))
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._settings_path = config
            app.state = replace(app.state, last_seen_version=__version__)
            app._upgrade_notice_shown = False

            app._show_discovery_summary(app._report)
            await pilot.pause()

            assert "does not carry" not in _panel_text(app)

    _run(scenario)


def test_the_notice_never_writes_to_the_settings_file(tmp_path: Path) -> None:
    """The whole position in one assertion: CLV reports a difference and does
    not act on it. The settings file is the operator's, and the launch path is
    the worst place in the program to put a write."""

    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")
    before = config.read_text(encoding="utf-8")
    stamp = config.stat().st_mtime_ns

    async def scenario() -> None:
        app = LogViewerApp(config=load_config(config))
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._settings_path = config
            app.state = replace(app.state, last_seen_version="2.0.0")
            app._upgrade_notice_shown = False
            app._show_discovery_summary(app._report)
            await pilot.pause()

    _run(scenario)

    assert config.read_text(encoding="utf-8") == before
    assert config.stat().st_mtime_ns == stamp, "the file was not even rewritten"


def test_the_version_survives_a_state_round_trip() -> None:
    state = SessionState(last_seen_version="2.7.4")

    assert "last_seen_version" in SessionState.PERSISTED_FIELDS
    assert SessionState.from_dict(state.to_dict()).last_seen_version == "2.7.4"


# --- the version marker ------------------------------------------------------


def test_a_file_with_no_marker_reads_as_version_zero(tmp_path: Path) -> None:
    """Every settings file written before this feature existed is version 0.

    That is the whole reason the marker's absence has to be a number rather than
    an error: there is no other signal that a file predates the schema.
    """
    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    assert config_version_of(config) == 0


def test_a_missing_or_unparsable_marker_reads_as_version_zero(tmp_path: Path) -> None:
    """A hand-edited marker must not raise on a path that runs inside an installer."""
    assert config_version_of(tmp_path / "nothing-here.conf") == 0
    assert config_version_of(None) == 0

    garbage = tmp_path / "settings.conf"
    garbage.write_text(
        "[log_viewer]\nlog_dirs = /var/log\nconfig_version = tuesday\n", encoding="utf-8"
    )
    assert config_version_of(garbage) == 0


def test_the_shipped_template_is_stamped_with_the_current_version() -> None:
    """If these drift, an install writes a file it then thinks is out of date."""
    assert template_config_version() == CURRENT_CONFIG_VERSION

    document = SettingsDocument(default_config_text().splitlines())
    assert document.get(CONFIG_SECTION, "config_version") == str(CURRENT_CONFIG_VERSION)


def test_the_marker_is_not_reported_as_a_missing_setting(tmp_path: Path) -> None:
    """It is plumbing, not something an operator would ever want to tune."""
    config = tmp_path / "settings.conf"
    config.write_text(ANCIENT, encoding="utf-8")

    assert "config_version" not in undocumented_settings(config)


# --- the merge ---------------------------------------------------------------


def test_no_settings_file_is_nothing_to_upgrade(tmp_path: Path) -> None:
    """First run creates one from this same template; there is no gap to close."""
    target = tmp_path / "settings.conf"

    result = upgrade_user_settings(target)

    assert result.status == "absent"
    assert not target.exists(), "the upgrade path must not double as a creator"


def test_a_current_file_is_not_even_touched(tmp_path: Path) -> None:
    """Idempotence, asserted on the mtime rather than the bytes.

    A rewrite that happens to produce identical content is still a rewrite: it
    takes a backup, and it moves the file under anything watching it.
    """
    target = tmp_path / "settings.conf"
    target.write_text(default_config_text(), encoding="utf-8")
    stamp = target.stat().st_mtime_ns

    result = upgrade_user_settings(target)

    assert result.status == "current"
    assert result.from_version == result.to_version == CURRENT_CONFIG_VERSION
    assert target.stat().st_mtime_ns == stamp
    assert list(tmp_path.glob("*.bak-*")) == []


def test_an_ancient_file_gains_the_whole_current_schema(tmp_path: Path) -> None:
    """The point of the feature: the file stops being out of date, and the values
    the operator set are still the values in effect."""
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT, encoding="utf-8")
    before = load_config(target)

    result = upgrade_user_settings(target)

    assert result.status == "upgraded"
    assert (result.from_version, result.to_version) == (0, CURRENT_CONFIG_VERSION)

    merged = target.read_text(encoding="utf-8")
    assert undocumented_settings(target) == (), "still missing options after an upgrade"
    assert config_version_of(target) == CURRENT_CONFIG_VERSION

    after = load_config(target)
    assert after.log_dirs == before.log_dirs
    assert after.max_buffer_lines == before.max_buffer_lines == 500
    assert after.refresh_hz == before.refresh_hz == 4

    # And the prose arrived with it -- that is what the operator was missing.
    assert merged.count("#") > 50, "the merged file did not pick up the documentation"


def test_an_upgrade_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT, encoding="utf-8")

    assert upgrade_user_settings(target).status == "upgraded"
    after = target.read_text(encoding="utf-8")
    stamp = target.stat().st_mtime_ns

    assert upgrade_user_settings(target).status == "current"
    assert target.read_text(encoding="utf-8") == after
    assert target.stat().st_mtime_ns == stamp


def test_options_this_version_does_not_document_are_carried_not_dropped(
    tmp_path: Path,
) -> None:
    """Silently deleting a key the operator wrote is not a migration, it is a loss.

    The line comes across verbatim rather than re-rendered, which is what keeps a
    refused key producing its warning instead of being quietly normalised away.
    """
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT + "legacy_thing   =   7\n", encoding="utf-8")

    result = upgrade_user_settings(target)

    assert result.carried == ("legacy_thing",)
    merged = target.read_text(encoding="utf-8")
    assert "Carried over from your previous settings file" in merged
    assert "legacy_thing   =   7" in merged


def test_host_sections_survive_the_merge_byte_for_byte(tmp_path: Path) -> None:
    """Including a host CLV cannot parse, and a key it refuses.

    A bad port means the section never reaches `LogConfig.hosts`, so anything
    that regenerated hosts from parsed state would delete it. The operator wrote
    it; a typo is theirs to fix, not ours to erase. The refused `password` is the
    same argument from the other side: it has to come through unchanged or it
    stops producing the error that tells them why it is being ignored.
    """
    hosts = (
        # Hosts are only parsed at all when the switch is on, so without this
        # the parity assertion below would be vacuous -- both sides empty.
        "enable_ssh = true\n"
        "\n[ssh:web01]\n"
        "host = 10.0.0.5\n"
        "user = deploy\n"
        "log_dirs = /var/log\n"
        "# a note about this box\n"
        "\n[ssh:broken]\n"
        "host = 10.0.0.6\n"
        "port = 99999\n"
        "password = hunter2\n"
    )
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT + hosts, encoding="utf-8")
    before = load_config(target)
    assert [entry.name for entry in before.hosts] == ["web01"], "fixture is inert"
    refusals = [issue.message for issue in before.issues if "password" in issue.message]
    assert refusals, "fixture does not exercise a refused key"

    result = upgrade_user_settings(target)

    assert result.status == "upgraded"
    assert result.hosts == ("web01", "broken")

    merged = target.read_text(encoding="utf-8")
    for line in hosts.strip().splitlines():
        assert line in merged, f"lost {line!r}"

    after = load_config(target)
    assert [entry.name for entry in after.hosts] == ["web01"]
    assert [
        issue.message for issue in after.issues if "password" in issue.message
    ] == refusals, "the refused key stopped being reported"


def test_the_previous_file_is_saved_alongside_it(tmp_path: Path) -> None:
    """The operator's own comments do not survive the merge, so the backup is
    the recovery path and is not optional in practice."""
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT, encoding="utf-8")

    result = upgrade_user_settings(target)

    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == ANCIENT
    assert result.backup_path.name.startswith("settings.conf.bak-")
    # The comment the operator wrote is gone from the live file but not lost.
    assert "The operator's own comment" not in target.read_text(encoding="utf-8")


def test_two_upgrades_in_one_second_do_not_overwrite_the_first_backup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT, encoding="utf-8")
    first = upgrade_user_settings(target).backup_path

    target.write_text(ANCIENT + "legacy_thing = 7\n", encoding="utf-8")
    second = upgrade_user_settings(target).backup_path

    assert first != second
    assert first is not None and second is not None
    assert first.exists() and second.exists()


def test_backup_can_be_declined(tmp_path: Path) -> None:
    target = tmp_path / "settings.conf"
    target.write_text(ANCIENT, encoding="utf-8")

    result = upgrade_user_settings(target, backup=False)

    assert result.status == "upgraded"
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak-*")) == []


def test_a_merge_naming_no_sources_is_refused(tmp_path, monkeypatch) -> None:
    """The same guard first run applies to the template. An empty log_dirs is a
    viewer with nothing in it, which is worse than an out-of-date file."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "settings.conf").write_text(
        "[log_viewer]\nlog_dirs =\nconfig_version = 1\n", encoding="utf-8"
    )
    target = tmp_path / "user.conf"
    target.write_text("[log_viewer]\nlog_dirs =\n", encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    result = upgrade_user_settings(target)

    assert result.status == "failed"
    assert "no log sources" in result.error
    assert target.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("*.bak-*")) == []


# --- the command line --------------------------------------------------------


def test_upgrade_config_reports_what_it_did(capsys) -> None:
    """The flag operates on the real user config path, so this also covers the
    XDG resolution the installer depends on."""
    target = user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ANCIENT, encoding="utf-8")

    assert main(["--upgrade-config"]) == 0

    out = capsys.readouterr().out
    assert "Upgraded" in out and "Previous file saved as" in out
    assert config_version_of(target) == CURRENT_CONFIG_VERSION

    # Second run: same command, nothing to do, still exit 0.
    assert main(["--upgrade-config"]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_upgrade_config_on_a_machine_with_no_settings_file(capsys) -> None:
    target = user_config_path()
    if target.exists():
        target.unlink()

    assert main(["--upgrade-config"]) == 0
    assert "will create one on first run" in capsys.readouterr().out


def test_a_duplicated_log_viewer_section_is_refused(tmp_path: Path) -> None:
    """Refusing beats merging half the file.

    `configparser`'s non-strict retry folds two `[log_viewer]` headers into one
    section, so options under the second are live -- but `SettingsDocument`
    edits the first, so a merge would drop them and report success. The file is
    already broken enough for CLV to warn about it; quietly deleting half of it
    is not the fix.
    """
    target = tmp_path / "settings.conf"
    target.write_text(
        ANCIENT + "\n[log_viewer]\nmax_files = 4000\n", encoding="utf-8"
    )
    before = target.read_text(encoding="utf-8")

    result = upgrade_user_settings(target)

    assert result.status == "failed"
    assert "combine them into one" in result.error
    assert target.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("*.bak-*")) == [], "refusal must not leave a backup"
