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
        async with app.run_test(size=(150, 40)) as pilot:  # noqa: F841
            app._source_manager = SourceManager([], [])
            await app._rescan()

            addition = app._source_manager.add(str(sample_dir))
            assert addition.success is True

            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            discovered = {
                str(node.data)
                for node in _walk(tree.root)
                if isinstance(node.data, Path)
            }
            assert str((sample_dir / "service.log").resolve()) in discovered

            # Highlighting the added file moves the cursor to it.
            app._highlight_source(sample_dir / "service.log")
            await pilot.pause()
            cursor = tree.cursor_node
            assert cursor is not None
            assert isinstance(cursor.data, Path)
            assert cursor.data.resolve() == (sample_dir / "service.log").resolve()

    asyncio.run(scenario())


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_messages_are_toasts_and_do_not_pollute_the_log_pane() -> None:
    """App messages used to be written into the log, so copy mode copied them."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:  # noqa: F841
            mock_notify = MagicMock()
            app.notify = mock_notify

            app.log_panel.clear()
            app._notify("All good", "info")
            await pilot.pause()

            kwargs = mock_notify.call_args_list[-1].kwargs
            assert kwargs["severity"] == "information"
            assert kwargs["markup"] is False

            app._notify("Heads up", "warning")
            await pilot.pause()
            assert mock_notify.call_args_list[-1].kwargs["severity"] == "warning"

            app._notify("Bad", "error")
            await pilot.pause()
            assert mock_notify.call_args_list[-1].kwargs["severity"] == "error"

            # Nothing landed in the log pane.
            rendered = "\n".join(
                getattr(strip, "plain", str(strip)) for strip in app.log_panel.lines
            )
            for text in ("All good", "Heads up", "Bad"):
                assert text not in rendered

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
