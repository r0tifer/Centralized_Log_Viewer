from __future__ import annotations

from pathlib import Path

import asyncio
from unittest.mock import AsyncMock, MagicMock

from clv.app import LogTree, LogViewerApp
from clv.services import SourceAddition, SourceManager, persist_log_sources


def test_source_manager_adds_directory(tmp_path: Path) -> None:
    sample_dir = tmp_path / "logs"
    sample_dir.mkdir()

    manager = SourceManager([], [])
    result = manager.add(str(sample_dir))

    assert result.success is True
    assert sample_dir.resolve() in manager.directories
    assert sample_dir.resolve() in manager.added_paths
    severities = [message.severity for message in result.messages]
    assert "info" in severities


def test_source_manager_rejects_duplicates(tmp_path: Path) -> None:
    sample_dir = tmp_path / "logs"
    sample_dir.mkdir()

    manager = SourceManager([sample_dir], [])
    duplicate = manager.add(str(sample_dir))

    assert duplicate.success is False
    assert duplicate.messages
    assert duplicate.messages[0].severity == "warning"


def test_source_manager_warns_for_non_log_file(tmp_path: Path) -> None:
    sample_dir = tmp_path / "logs"
    sample_dir.mkdir()
    sample_file = sample_dir / "output.txt"
    sample_file.write_text("test", encoding="utf-8")

    manager = SourceManager([], [])
    result = manager.add(str(sample_file))

    assert result.success is True
    severities = [message.severity for message in result.messages]
    assert "warning" in severities
    assert "info" in severities
    assert sample_file.resolve() in manager.files


def test_persist_log_sources_creates_file(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.conf"
    entries = [Path("/var/log/app.log"), Path("/var/log/custom")]

    persist_log_sources(config_path, entries)

    data = config_path.read_text(encoding="utf-8").splitlines()
    assert "[log_viewer]" in data
    assert any(line.startswith("log_dirs = ") for line in data)
    log_line = next(line for line in data if line.startswith("log_dirs = "))
    assert "/var/log/app.log" in log_line
    assert "/var/log/custom" in log_line


def test_persist_log_sources_merges_existing_values(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.conf"
    config_path.write_text(
        "[log_viewer]\nlog_dirs = /var/log\nrefresh_hz = 2\n",
        encoding="utf-8",
    )

    persist_log_sources(config_path, [Path("/var/log"), Path("/opt/service.log")])

    contents = config_path.read_text(encoding="utf-8")
    assert "log_dirs = /var/log, /opt/service.log" in contents


def test_added_source_appears_in_tree(tmp_path: Path) -> None:
    sample_dir = tmp_path / "logs"
    sample_dir.mkdir()
    (sample_dir / "service.log").write_text("line", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test() as pilot:  # noqa: F841 - pilot kept for context management
            app._source_manager = SourceManager([], [])
            await app._populate_tree()

            addition = app._source_manager.add(str(sample_dir))
            assert addition.success is True

            await app._populate_tree()
            app._highlight_source(sample_dir)

            tree_panel = app.query_one("#tree-panel")
            directory_tree = tree_panel.query_one(LogTree)
            assert directory_tree.root.data == sample_dir.resolve()
            focused = directory_tree.cursor_node
            assert focused is not None
            assert isinstance(focused.data, Path)
            assert focused.data.resolve() == sample_dir.resolve()

    asyncio.run(scenario())


def test_show_message_uses_colored_toasts() -> None:

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test() as pilot:  # noqa: F841 - pilot kept for context management
            mock_notify = MagicMock()
            app.notify = mock_notify

            def panel_contains(substring: str) -> bool:
                for strip in app.log_panel.lines:
                    plain = getattr(strip, "plain", None)
                    if plain is None:
                        plain = str(strip)
                    if substring in plain:
                        return True
                return False

            app.log_panel.clear()
            app._show_message("All good", "info")
            await pilot.pause()
            info_kwargs = mock_notify.call_args_list[-1].kwargs
            assert info_kwargs["severity"] == "information"
            assert info_kwargs["title"] == ""
            assert info_kwargs["markup"] is False
            assert panel_contains("SUCCESS: All good")

            app.log_panel.clear()
            app._show_message("Heads up", "warning")
            await pilot.pause()
            warning_kwargs = mock_notify.call_args_list[-1].kwargs
            assert warning_kwargs["severity"] == "warning"
            assert panel_contains("WARNING: Heads up")

    asyncio.run(scenario())


def test_prompt_add_source_cancel_shows_notification(monkeypatch) -> None:

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test() as pilot:  # noqa: F841 - pilot kept for context management
            app._source_manager = SourceManager([], [])
            monkeypatch.setattr(app, "push_screen", AsyncMock(return_value=None))

            notifications: list[tuple[str, str]] = []

            def record(message: str, *, severity: str, **_: object) -> None:
                notifications.append((message, severity))

            app.notify = MagicMock(side_effect=record)

            await app._prompt_add_source()
            await pilot.pause()

            assert notifications
            message, severity = notifications[-1]
            assert "canceled" in message.lower()
            assert severity == "information"

    asyncio.run(scenario())


def test_prompt_add_source_failure_without_messages_shows_fallback(monkeypatch) -> None:

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test() as pilot:  # noqa: F841 - pilot kept for context management
            app._source_manager = SourceManager([], [])
            monkeypatch.setattr(app, "push_screen", AsyncMock(return_value="/tmp/missing"))

            addition = SourceAddition(success=False, path=Path("/tmp/missing"), messages=[])
            app._source_manager.add = MagicMock(return_value=addition)

            notifications: list[tuple[str, str]] = []

            def record(message: str, *, severity: str, **_: object) -> None:
                notifications.append((message, severity))

            app.notify = MagicMock(side_effect=record)

            await app._prompt_add_source()
            await pilot.pause()

            assert notifications
            message, severity = notifications[-1]
            assert "unable to add log source" in message.lower()
            assert severity == "error"

    asyncio.run(scenario())
