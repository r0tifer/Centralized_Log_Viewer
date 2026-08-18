"""How an operator upgrading to a new build learns that settings were added.

The problem this covers is not correctness. Every option is optional-with-a-
default, so an old settings file keeps working indefinitely — verified below
against a file that predates half the schema. The problem is that it stops
*learning*: `ensure_user_settings_file` returns early when the file exists, so
the prose documenting a new option only ever reaches a first-run user, and the
shipped reference sits unread beside the binary.

Both answers here are read-only. CLV does not rewrite the operator's settings
file, and it especially does not do it on the launch path.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from clv.app import LogViewerApp, main
from clv.services.config import (
    DEFAULT_SETTINGS_TEMPLATE,
    default_config_text,
    load_config,
    undocumented_settings,
)
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
