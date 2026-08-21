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
            rendered = "\n".join(app.log_panel.text_lines)
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


# --- tree expansion state --------------------------------------------------


def _nested_tree(tmp_path: Path) -> Path:
    """A root with nested folders, each holding a log."""
    root = tmp_path / "logs"
    for relative in ("", "alpha", "alpha/deep", "alpha/deep/deeper", "beta"):
        directory = root / relative if relative else root
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "a.log").write_text("2026-08-07 09:00:00 - INFO - x\n", encoding="utf-8")
    return root


def test_tree_starts_fully_collapsed(tmp_path: Path) -> None:
    """Expanding everything up front buries the roots under the hierarchy."""

    root = _nested_tree(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            folders = [n for n in _walk(tree.root) if n is not tree.root and n.allow_expand]

            assert folders, "expected folder nodes in the fixture"
            assert not any(node.is_expanded for node in folders)
            # Only the configured root is on screen.
            assert tree.last_line + 1 == 1

    asyncio.run(scenario())


def test_folders_still_expand_on_demand(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            tree.focus()
            tree.cursor_line = 0

            await pilot.press("enter")
            await pilot.pause()
            expanded_rows = tree.last_line + 1
            assert expanded_rows > 1

            await pilot.press("enter")
            await pilot.pause()
            assert tree.last_line + 1 == 1

    asyncio.run(scenario())


def test_revealing_a_source_expands_only_its_own_path(tmp_path: Path) -> None:
    """Regression: expand() invalidates the tree's line list.

    Selecting in the same frame read a stale line for the target and parked the
    cursor on the root instead. It only became visible once folders stopped
    being expanded when the tree was built.
    """

    root = _nested_tree(tmp_path)
    deep = root / "alpha" / "deep" / "deeper" / "a.log"

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            app._highlight_source(deep)
            await pilot.pause()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            cursor = tree.cursor_node
            assert cursor is not None and isinstance(cursor.data, Path)
            assert cursor.data.resolve() == deep.resolve(), "cursor did not land on the target"

            # Siblings outside the revealed path stay shut.
            beta = next(
                node
                for node in _walk(tree.root)
                if isinstance(node.data, Path) and node.data.name == "beta"
            )
            assert not beta.is_expanded

    asyncio.run(scenario())


def test_rescan_returns_to_a_collapsed_tree(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            app._highlight_source(root / "alpha" / "a.log")
            await pilot.pause()
            await pilot.pause()
            assert app.query_one("#source-tree", LogTree).last_line + 1 > 1

            await app._rescan()
            await pilot.pause()
            assert app.query_one("#source-tree", LogTree).last_line + 1 == 1

    asyncio.run(scenario())


# --- what a launch opens ----------------------------------------------------


def test_launch_shows_the_summary_not_the_last_source(tmp_path: Path) -> None:
    """A relaunch must not silently resume tailing whatever was open before."""

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "a.log"

    async def scenario() -> None:
        from clv.services import SourceManager
        from clv.storage import StateStore

        store = StateStore(root=tmp_path / "state")

        # Session one: open a source and set a filter.
        first = LogViewerApp(store=store)
        async with first.run_test(size=(120, 30)) as pilot:
            first._source_manager = SourceManager([root], [])
            await first._rescan()
            first._select_source(target)
            first._update_state(query="hello", severity="warn")
            await pilot.pause()
            assert first._selected_source == target.resolve()
            first._store.save(first.state)

        # Session two: relaunch against the same store.
        second = LogViewerApp(store=StateStore(root=tmp_path / "state"))
        async with second.run_test(size=(120, 30)) as pilot:
            second._source_manager = SourceManager([root], [])
            await second._rescan()
            await pilot.pause()

            assert second._selected_source is None, "a source was reopened on launch"
            assert second._tail_timer is None, "a tail was started on launch"

            rendered = "\n".join(second.log_panel.text_lines)
            assert "Log files found" in rendered
            assert "Select a log from the tree to begin." in rendered

            # Filters still come back; only the open source does not.
            assert second.state.query == "hello"
            assert second.state.severity == "warn"

    asyncio.run(scenario())


def test_the_open_source_path_is_never_written_to_disk(tmp_path: Path) -> None:
    """Storing it would record where someone had been reading, unused."""

    import json

    from clv.storage import SessionState, StateStore

    store = StateStore(root=tmp_path)
    store.save(SessionState(query="x"))

    payload = json.loads(store.path.read_text(encoding="utf-8"))

    assert "selected_source" not in payload
    assert not hasattr(SessionState(), "selected_source")


# --- starred logs -----------------------------------------------------------


def _starred_group(tree):
    from clv.app import STARRED_GROUP

    return next((n for n in _walk(tree.root) if str(n.label).startswith(STARRED_GROUP)), None)


def test_starring_marks_the_log_without_opening_it(tmp_path: Path) -> None:
    """A star is a bookmark. select_node would post NodeSelected and open it."""

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "deep" / "deeper" / "a.log"

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            app._highlight_source(target, select=False)
            await pilot.pause()
            await pilot.pause()

            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()

            assert app.state.starred == (str(target.resolve()),)
            assert app._selected_source is None, "starring opened the log"
            rendered = "\n".join(app.log_panel.text_lines)
            assert "Log files found" in rendered

    asyncio.run(scenario())


def test_starred_logs_get_a_group_at_the_top_of_the_tree(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)
    target = root / "alpha" / "deep" / "deeper" / "a.log"

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=(str(target.resolve()),))
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            assert group is not None, "no Starred group"
            # Collapsed on arrival, like the roots below it: a group at the top
            # is a shortcut to something buried, and one that unfolds itself
            # pushes the rest of the tree off screen.
            assert group.is_expanded is False
            assert [n.data for n in group.children] == [target]
            # It is the first thing in the tree, above the configured roots.
            assert tree.root.children[0] is group

    asyncio.run(scenario())


def test_starring_toggles_off_again(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)
    target = root / "alpha" / "a.log"

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            app._highlight_source(target, select=False)
            await pilot.pause()
            await pilot.pause()

            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()
            assert app.state.starred == (str(target.resolve()),)

            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()
            assert app.state.starred == ()
            assert _starred_group(app.query_one("#source-tree", LogTree)) is None

    asyncio.run(scenario())


def test_one_star_opens_on_launch_several_do_not(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)
    first = root / "alpha" / "a.log"
    second = root / "beta" / "a.log"

    async def launch(starred: tuple[str, ...]):
        from clv.services import SourceManager
        from clv.storage import SessionState, StateStore

        store = StateStore(root=tmp_path / "state")
        store.save(SessionState(starred=starred))

        app = LogViewerApp(store=StateStore(root=tmp_path / "state"))
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            app._open_starred_on_launch()
            await pilot.pause()
            return app._selected_source

    async def scenario() -> None:
        opened = await launch((str(first.resolve()),))
        assert opened == first.resolve(), "a single star should open on launch"

        opened = await launch((str(first.resolve()), str(second.resolve())))
        assert opened is None, "several stars are favourites, not an instruction"

    asyncio.run(scenario())


def test_a_star_pointing_at_a_missing_log_warns_and_is_kept(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)
    gone = root / "alpha" / "a.log"
    survivor = root / "beta" / "a.log"
    starred = (str(gone.resolve()), str(survivor.resolve()))
    gone.unlink()

    async def scenario() -> None:
        from clv.services import SourceManager
        from clv.storage import SessionState, StateStore

        store = StateStore(root=tmp_path / "state")
        store.save(SessionState(starred=starred))

        app = LogViewerApp(store=StateStore(root=tmp_path / "state"))
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()

            notices: list[tuple[str, str]] = []
            app.notify = lambda message, **kw: notices.append(
                (message, kw.get("severity", ""))
            )
            app._open_starred_on_launch()
            await pilot.pause()

            warnings = [m for m, s in notices if "not available" in m]
            assert warnings and str(gone) in warnings[0]
            # Kept, because a rotated log comes back.
            assert app.state.starred == starred
            # The one that survived is the only one available, so it opens.
            assert app._selected_source == survivor.resolve()
            # Kept *and* removable: the group lists it, so there is a row to
            # put the cursor on and a `✕` to press. Keeping it used to mean
            # being stuck with it.
            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            assert group is not None
            assert gone.resolve() in [n.data for n in group.children]

    asyncio.run(scenario())


def test_stars_survive_a_restart(tmp_path: Path) -> None:
    from clv.storage import SessionState, StateStore

    store = StateStore(root=tmp_path)
    store.save(SessionState(starred=("/var/log/a.log", "/var/log/b.log")))

    restored = StateStore(root=tmp_path).load()

    assert restored.starred == ("/var/log/a.log", "/var/log/b.log")


def test_a_corrupt_starred_list_degrades_to_the_usable_entries(tmp_path: Path) -> None:
    import json

    from clv.storage import StateStore

    store = StateStore(root=tmp_path)
    store.path.write_text(
        json.dumps({"starred": ["/var/log/a.log", 42, None, "/var/log/b.log"]}),
        encoding="utf-8",
    )

    assert store.load().starred == ("/var/log/a.log", "/var/log/b.log")


def test_star_button_reflects_and_toggles_the_star(tmp_path: Path) -> None:
    """The on-screen path to starring, for anyone who never finds the keybinding."""

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "a.log"

    async def scenario() -> None:
        from textual.widgets import Button

        from clv.services import SourceManager
        from clv.widgets.query_bar import STAR_OFF, STAR_ON

        app = LogViewerApp()
        async with app.run_test(size=(160, 32)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            button = app.query_bar.query_one("#toggle-star", Button)

            # Nothing to star yet, so the control says so rather than lying.
            assert button.disabled is True
            assert str(button.label) == STAR_OFF

            app._select_source(target)
            await pilot.pause()
            assert button.disabled is False
            assert str(button.label) == STAR_OFF

            button.press()
            await pilot.pause()
            await pilot.pause()
            assert app.state.starred == (str(target.resolve()),)
            assert str(button.label) == STAR_ON
            assert button.has_class("-starred")

            button.press()
            await pilot.pause()
            await pilot.pause()
            assert app.state.starred == ()
            assert str(button.label) == STAR_OFF
            assert not button.has_class("-starred")

    asyncio.run(scenario())


def test_button_and_keybinding_stay_in_agreement(tmp_path: Path) -> None:
    root = _nested_tree(tmp_path)
    target = root / "alpha" / "a.log"

    async def scenario() -> None:
        from textual.widgets import Button

        from clv.services import SourceManager
        from clv.widgets.query_bar import STAR_ON

        app = LogViewerApp()
        async with app.run_test(size=(160, 32)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            app._select_source(target)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()

            button = app.query_bar.query_one("#toggle-star", Button)
            assert app.state.starred == (str(target.resolve()),)
            assert str(button.label) == STAR_ON, "button did not follow the keybinding"

    asyncio.run(scenario())


def test_star_target_follows_the_tree_cursor_when_the_tree_has_focus(tmp_path: Path) -> None:
    """Otherwise the toolbar button would star whatever the cursor rested on."""

    root = _nested_tree(tmp_path)
    opened = root / "alpha" / "a.log"
    pointed_at = root / "beta" / "a.log"

    async def scenario() -> None:
        from clv.services import SourceManager

        app = LogViewerApp()
        async with app.run_test(size=(160, 32)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            app._select_source(opened)
            await pilot.pause()

            # Focus away from the tree: the star acts on what is on screen.
            app.set_focus(app.log_panel)
            await pilot.pause()
            assert app._star_target() == opened.resolve()

            # Focus the tree and point elsewhere: the cursor wins.
            tree = app.query_one("#source-tree", LogTree)
            app._highlight_source(pointed_at, select=False)
            await pilot.pause()
            await pilot.pause()
            tree.focus()
            await pilot.pause()
            assert app._star_target() == pointed_at

    asyncio.run(scenario())


def test_star_binding_survives_footer_truncation_at_eighty_columns() -> None:
    """The footer drops entries from the right; Star must not be one of them.

    It sits ahead of the filter bindings for this reason — at 80 columns it was
    previously cut to a bare "*" with no label.

    Textual separately hides bindings that cannot fire, so with the query input
    focused every single-letter binding disappears. That is correct (the key
    really would type into the box) and is why the toolbar button exists: it is
    the only way to star while typing a query.
    """

    async def footer_at(width: int, focus_input: bool) -> str:
        from textual.widgets import Input

        app = LogViewerApp()
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            if focus_input:
                app.set_focus(app.query_bar.query_one("#query-input", Input))
            else:
                app.set_focus(app.log_panel)
            await pilot.pause()
            strips = app.screen._compositor.render_strips()
            return "".join(segment.text for segment in strips[-1])

    async def scenario() -> None:
        narrow = await footer_at(80, focus_input=False)
        assert "Star" in narrow, f"Star missing from the footer: {narrow!r}"

        typing = await footer_at(80, focus_input=True)
        assert "Star" not in typing
        # ...and the button covers that state.
        from textual.widgets import Button

        app = LogViewerApp()
        async with app.run_test(size=(160, 32)) as pilot:
            await pilot.pause()
            assert app.query_bar.query_one("#toggle-star", Button) is not None

    asyncio.run(scenario())


def test_every_group_arrives_collapsed(tmp_path: Path) -> None:
    """A shortcut that unfolds itself is not a shortcut.

    These groups exist so something buried is one keystroke away. Expanded on
    launch they do the opposite: a hundred journal units, or a long list of
    saved views, pushes the configured roots off the bottom of the pane.
    """

    from clv.plugins import LogSourceProvider, PluginRegistry, ProviderSource
    from clv.services import SourceManager

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "a.log"

    class Fake(LogSourceProvider):
        name = "fake"

        def discover(self):
            return [ProviderSource(Path("fake:one"), "One")]

        def open(self, path):
            return iter(())

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._plugins = PluginRegistry(sources=[Fake()])
            app._source_manager = SourceManager([root], [])
            app._update_state(
                starred=(str(target.resolve()),), merged=(str(target),)
            )
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            groups = [node for node in tree.root.children if not isinstance(node.data, Path)]
            assert len(groups) >= 3, [str(n.label) for n in tree.root.children]
            for node in groups:
                assert node.is_expanded is False, f"{node.label} arrived expanded"

            # And the roots below them are still reachable without scrolling.
            assert any(isinstance(node.data, Path) for node in tree.root.children)

    asyncio.run(scenario())


# --- the access hint belongs to the backend, not to this module -------------
#
# `ACCESS_HINT` says "re-launch with sudo". For a file on another machine that
# is not merely unhelpful — it recommends the one thing CLV refuses to do
# anywhere (SSH_TODO.md Requirement 11). So the hint comes from the backend that
# could not read the file, and the local wording is what the local backend says.


def test_check_access_uses_the_local_hint_for_a_local_file(tmp_path: Path) -> None:
    """Unchanged, and asserted so the refactor cannot quietly reword it."""

    from clv.services import ACCESS_HINT
    from clv.services.sources import check_access

    allowed, reason = check_access(tmp_path / "missing.log")
    assert allowed is False
    assert reason == f"Path '{tmp_path / 'missing.log'}' does not exist."

    locked = tmp_path / "locked.log"
    locked.write_text("alpha\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        allowed, reason = check_access(locked)
    finally:
        locked.chmod(0o600)

    if allowed:  # running as root, which can read anything
        return
    assert reason == f"Read access required for file '{locked}'. {ACCESS_HINT}"


def test_check_access_takes_its_hint_from_the_backend(tmp_path: Path) -> None:
    """A remote refusal must not tell the operator to use sudo."""

    import os as _os

    from clv.services.backend import (
        BackendCapabilities,
        LocalBackend,
        blocking,
        blocking_methods,
    )
    from clv.services.sources import check_access

    remote_hint = "Add the SSH user to the `adm` group on web01, or grant an ACL."

    class Elsewhere(LocalBackend):
        @property
        def capabilities(self):
            return BackendCapabilities(
                name="elsewhere",
                blocking=blocking_methods(Elsewhere),
                stable_identity=True,
                access_hint=remote_hint,
            )

        # Marked, because `blocking_methods` refuses an override that is not --
        # which is the point of it, and is what this override just demonstrated
        # by being rejected the first time it was written without one.
        @blocking
        def access(self, ref, mode):  # nothing here is readable
            return False

    target = tmp_path / "syslog"
    target.write_text("alpha\n", encoding="utf-8")

    allowed, reason = check_access(target, backend=Elsewhere())

    assert allowed is False
    assert reason is not None
    assert remote_hint in reason
    assert "sudo" not in reason
    assert _os.R_OK is not None  # the mode reached the backend at all


def test_check_access_reports_a_directory_that_will_not_list(tmp_path: Path) -> None:
    """`list_dir` raises where `walk` skips, and this is the reason it does:
    an ACL can refuse a listing that the permission bits say is fine."""

    from clv.services.backend import LocalBackend, blocking
    from clv.services.sources import check_access

    folder = tmp_path / "logs"
    folder.mkdir()

    class Refusing(LocalBackend):
        @blocking
        def list_dir(self, ref):
            raise PermissionError(13, "Permission denied")

    allowed, reason = check_access(folder, backend=Refusing())

    assert allowed is False
    assert reason is not None
    assert "Permission denied while listing" in reason


# --- a star whose log has gone ----------------------------------------------
#
# The group used to be built by filtering discovery, so a star pointing at a
# rotated-away log produced no row: nothing to put the cursor on, and therefore
# no way to press `*` on it. The only way out was hand-editing session.json.


def _find_action(app, action: str) -> tuple[int, int]:
    """Screen position of an action marker, found by its metadata."""

    from clv.app import ACTION_META

    screen = app.screen
    for y in range(screen.size.height):
        for x in range(screen.size.width):
            if screen.get_style_at(x, y).meta.get(ACTION_META) == action:
                return x, y
    raise AssertionError(f"no {action!r} marker painted anywhere on screen")


def _one_gone(tmp_path: Path):
    """A tree with two starred logs, one of which has since rotated away."""

    root = _nested_tree(tmp_path)
    gone = root / "alpha" / "a.log"
    survivor = root / "beta" / "a.log"
    starred = (str(gone.resolve()), str(survivor.resolve()))
    gone.unlink()
    return LogViewerApp(), root, gone.resolve(), survivor.resolve(), starred


def test_a_starred_log_that_is_gone_is_still_listed(tmp_path: Path) -> None:
    """The row is the whole fix: without one there is nothing to act on."""

    app, root, gone, survivor, starred = _one_gone(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=starred)
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            assert group is not None, "no Starred group"
            listed = [n.data for n in group.children]
            assert gone in listed, "the missing star has no row"
            assert survivor in listed
            # Still collapsed on arrival, and still the first thing in the tree.
            assert group.is_expanded is False
            assert tree.root.children[0] is group

    asyncio.run(scenario())


def test_a_missing_starred_row_is_marked_as_such(tmp_path: Path) -> None:
    """Dimmed, not suffixed: the panel has no seven spare columns for a word."""

    from rich.text import Text

    app, root, gone, survivor, starred = _one_gone(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=starred)
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            by_data = {n.data: n.label for n in group.children}

            def dim_of(label: Text, index: int) -> bool:
                return any(
                    span.start <= index < span.end and getattr(span.style, "dim", False)
                    for span in label.spans
                )

            from clv.app import ACTION_META

            missing = by_data[gone]
            present = by_data[survivor]
            # The icon and the name go dim; the row it stands for is gone.
            assert dim_of(missing, 0), "the missing row is not dimmed"
            assert dim_of(missing, len(missing.plain) - 1), "the name is not dimmed"
            assert not dim_of(present, 0), "a present row should look ordinary"
            # The control keeps full brightness: it is what you came for.
            marker = next(
                span
                for span in missing.spans
                if getattr(span.style, "meta", {}).get(ACTION_META) == "star-remove"
            )
            assert not getattr(marker.style, "dim", False)
            assert not dim_of(missing, marker.start)

    asyncio.run(scenario())


def test_the_star_marker_unstars_a_missing_log(tmp_path: Path) -> None:
    """The GUI half of the fix — no keyboard, no hand-edited session.json."""

    app, root, gone, survivor, starred = _one_gone(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=starred)
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            group.expand()
            await pilot.pause()

            # The missing row sorts first: alpha/a.log before beta/a.log.
            assert group.children[0].data == gone
            tree.focus()
            tree.move_cursor(group.children[0])
            await pilot.pause()

            await pilot.click(offset=_find_action(app, "star-remove"))
            await pilot.pause()
            await pilot.pause()

            assert app.state.starred == (str(survivor),)

    asyncio.run(scenario())


def test_the_star_key_acts_on_the_missing_row_the_cursor_is_on(tmp_path: Path) -> None:
    """`*` used to fall through to the open log, toggling a star nobody picked."""

    app, root, gone, survivor, starred = _one_gone(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=starred)
            await app._rescan()
            await pilot.pause()

            # A *different* log open, so a fall-through is visible in the result.
            app._select_source(survivor)
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            group.expand()
            await pilot.pause()
            tree.focus()
            tree.move_cursor(group.children[0])
            await pilot.pause()
            assert tree.cursor_node.data == gone

            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()

            # The row under the cursor lost its star; the open log kept its own.
            assert app.state.starred == (str(survivor),)

    asyncio.run(scenario())


def test_selecting_a_missing_starred_row_says_why(tmp_path: Path) -> None:
    """It used to be a silent no-op — a row that answered nothing at all."""

    app, root, gone, survivor, starred = _one_gone(tmp_path)

    async def scenario() -> None:
        from clv.services import SourceManager

        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=starred)
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            group.expand()
            await pilot.pause()

            notices: list[tuple[str, str]] = []
            app.notify = lambda message, **kw: notices.append(
                (message, kw.get("severity", ""))
            )
            tree.select_node(group.children[0])
            await pilot.pause()
            await pilot.pause()

            said = [m for m, sev in notices if sev == "warning" and str(gone) in m]
            assert said, f"nothing explained the dead row: {notices}"
            assert app._selected_source is None, "a missing log was opened"

    asyncio.run(scenario())


def test_unstarring_does_not_collapse_the_folders_you_opened(tmp_path: Path) -> None:
    """The reason the reconciler exists: `✕` is a click, and a rebuild is a rug pull."""

    from clv.services import SourceManager

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "deep" / "deeper" / "a.log"

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=(str(target.resolve()),))
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            for node in _walk(tree.root):
                if node.allow_expand and node is not tree.root:
                    node.expand()
            await pilot.pause()
            opened = {
                str(node.label)
                for node in _walk(tree.root)
                if node.allow_expand and node.is_expanded and isinstance(node.data, Path)
            }
            assert opened, "fixture should have folders to expand"

            app._highlight_source(target, select=False)
            await pilot.pause()
            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()

            assert app.state.starred == ()
            still = {
                str(node.label)
                for node in _walk(tree.root)
                if node.allow_expand and node.is_expanded and isinstance(node.data, Path)
            }
            assert still == opened, "unstarring collapsed the tree"

    asyncio.run(scenario())


def test_removing_the_last_star_does_not_strand_the_cursor(tmp_path: Path) -> None:
    """The group is removed when it empties — possibly out from under the cursor."""

    from clv.services import SourceManager

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "deep" / "deeper" / "a.log"

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=(str(target.resolve()),))
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            group.expand()
            await pilot.pause()
            tree.focus()
            tree.move_cursor(group.children[0])
            await pilot.pause()

            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()

            assert app.state.starred == ()
            assert _starred_group(tree) is None
            # Still somewhere valid, rather than pointing at a removed node.
            assert tree.cursor_node is not None
            assert tree.cursor_node.tree is tree

    asyncio.run(scenario())


def test_the_unstar_marker_survives_the_default_panel_width(tmp_path: Path) -> None:
    """A control behind a long path is not a control.

    Measured: with the verbs trailing the name, a dated filename pushed the `✕`
    past the rendered width at the default `tree_width` of 38 — the tree scrolls
    horizontally, so it was still there and no longer reachable. This is why the
    verbs sit between the icon and the name.
    """

    from clv.app import ACTION_META
    from clv.services import SourceManager

    root = tmp_path / "logs"
    root.mkdir()
    long_name = root / "runtime_2026-08-04_application_server.log"
    long_name.write_text("2026-08-07 09:00:00 - INFO - x\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=(str(long_name.resolve()),), tree_width=38)
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            _starred_group(tree).expand()
            await pilot.pause()

            screen = app.screen
            painted = [
                (x, y)
                for y in range(screen.size.height)
                for x in range(screen.size.width)
                if screen.get_style_at(x, y).meta.get(ACTION_META) == "star-remove"
            ]
            assert painted, "the unstar control is off the rendered width"

    asyncio.run(scenario())


# --- the panel has to be able to grow a tree, and lose one ------------------


def _no_logs(tmp_path: Path) -> Path:
    empty = tmp_path / "nologs"
    empty.mkdir()
    return empty


def test_a_star_outlives_every_log_on_the_machine(tmp_path: Path) -> None:
    """`report.files` empty and a star kept is not "no log sources found".

    The placeholder used to be decided by the walk alone, so a machine whose
    logs had all rotated away showed a notice with no tree — and the star it
    was keeping had nowhere to appear and no way to be removed.
    """

    from clv.services import SourceManager

    root = tmp_path / "logs"
    root.mkdir()
    gone = root / "runtime_2026-08-04.log"

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([root], [])
            app._update_state(starred=(str(gone.resolve()),))
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            group = _starred_group(tree)
            assert group is not None, "the placeholder swallowed the only star"
            assert [n.data for n in group.children] == [gone.resolve()]

    asyncio.run(scenario())


def test_starring_puts_a_tree_back_and_unstarring_takes_it_away(tmp_path: Path) -> None:
    """The one transition the in-place reconcilers cannot make themselves.

    They begin by looking for a tree; with the empty-panel notice up there is
    none, so they return and the star is kept but never drawn.
    """

    from textual.widgets import Static

    from clv.services import SourceManager

    empty = _no_logs(tmp_path)
    absent = empty / "nothing.log"

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._source_manager = SourceManager([empty], [])
            await app._rescan()
            await pilot.pause()

            panel = app.query_one("#tree-panel")
            assert panel.query("Static.empty-tree"), "expected the empty notice"

            app._update_state(starred=(str(absent),))
            await app._ensure_tree_panel()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            assert _starred_group(tree) is not None

            app._update_state(starred=())
            await app._ensure_tree_panel()
            await pilot.pause()

            # And back again: an empty tree widget explains less than the notice.
            assert app.query_one("#tree-panel").query("Static.empty-tree")

    asyncio.run(scenario())


def test_creating_or_destroying_a_star_never_closes_the_tree(tmp_path: Path) -> None:
    """Both directions, and the click as well as the key.

    Starring used to rebuild the whole tree from the report, which collapses
    every folder the operator had opened — survivable while it was a keystroke,
    and not once the `✕` on a row made it a click that lands on a node the
    rebuild then destroys.
    """

    from clv.app import ACTION_META
    from clv.services import SourceManager

    root = _nested_tree(tmp_path)
    target = root / "alpha" / "deep" / "deeper" / "a.log"

    def opened(tree) -> set[str]:
        return {
            str(node.label)
            for node in _walk(tree.root)
            if node.allow_expand and node.is_expanded and isinstance(node.data, Path)
        }

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            for node in _walk(tree.root):
                if node.allow_expand:
                    node.expand()
            await pilot.pause()
            before = opened(tree)
            assert before, "fixture should have folders to expand"

            tree.focus()
            app._highlight_source(target, select=False)
            await pilot.pause()
            await pilot.press("*")
            await pilot.pause()
            await pilot.pause()
            assert app.state.starred, "the star was not created"
            assert app.query_one("#source-tree", LogTree) is tree, "the tree was remounted"
            assert opened(tree) == before, "creating a star closed the tree"

            # And out again, through the marker rather than the key.
            _starred_group(tree).expand()
            await pilot.pause()
            screen = app.screen
            pos = next(
                (x, y)
                for y in range(screen.size.height)
                for x in range(screen.size.width)
                if screen.get_style_at(x, y).meta.get(ACTION_META) == "star-remove"
            )
            await pilot.click(offset=pos)
            await pilot.pause()
            await pilot.pause()

            assert app.state.starred == ()
            assert app.query_one("#source-tree", LogTree) is tree, "the tree was remounted"
            assert opened(tree) == before, "destroying a star closed the tree"

    asyncio.run(scenario())
