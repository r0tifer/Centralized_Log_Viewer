"""Saved views (Item 9): capture, apply, persist, rename, delete."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static, Switch

from clv.app import STARRED_GROUP, VIEWS_GROUP, LogTree, LogViewerApp
from clv.services.config import LogConfig
from clv.services.discovery import DiscoverySettings
from clv.storage import SavedView, SessionState, StateStore
from clv.widgets.view_dialogs import SaveViewDialog, ViewPickerDialog, ViewRequest


def _run(scenario) -> None:
    asyncio.run(scenario())


def _logs(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "logs"
    root.mkdir(exist_ok=True)
    first = root / "alpha.log"
    second = root / "beta.log"
    first.write_text(
        "2026-08-07 09:25:01 ERROR disk full on alpha\n"
        "2026-08-07 09:25:02 INFO all good on alpha\n",
        encoding="utf-8",
    )
    second.write_text("2026-08-07 09:25:03 WARN beta is unhappy\n", encoding="utf-8")
    return first, second


def _app(tmp_path: Path) -> LogViewerApp:
    config = LogConfig(log_dirs=[tmp_path / "logs"], discovery=DiscoverySettings())
    return LogViewerApp(config=config)


# --- the record -------------------------------------------------------------


def test_a_view_captures_every_filter_field() -> None:
    view = SavedView(
        name="5xx on web01",
        query="status>=500",
        severity="error",
        time_window="15m",
        case_sensitive=True,
        use_regex=False,
        invert_match=True,
        include_globs="*.log",
        source="/var/log/nginx/access.log",
    )
    restored = SavedView.from_dict(json.loads(json.dumps(view.__dict__)))
    assert restored == view


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a dict",
        {},
        {"name": ""},
        {"name": "   "},
        {"query": "orphaned"},
        {"name": 7},
    ],
)
def test_an_unusable_record_is_dropped_rather_than_raising(raw) -> None:
    assert SavedView.from_dict(raw) is None


def test_a_mistyped_field_is_ignored_and_the_rest_of_the_view_survives() -> None:
    view = SavedView.from_dict({"name": "keep me", "query": "boom", "use_regex": "yes"})
    assert view is not None
    assert view.query == "boom"
    assert view.use_regex is True  # the default, not the string


def test_the_summary_says_what_the_view_filters_to() -> None:
    view = SavedView(name="x", query="oom", severity="error", time_window="15m")
    summary = view.summary()
    assert "oom" in summary and "error" in summary and "15m" in summary
    assert SavedView(name="empty").summary() == "no filters"


# --- persistence ------------------------------------------------------------


def test_views_survive_a_state_store_round_trip(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    views = (
        SavedView(name="errors", query="ERROR", severity="error"),
        SavedView(name="last hour", time_window="1h", source="/var/log/syslog"),
    )
    store.save(SessionState(views=views))
    assert store.load().views == views


def test_one_malformed_view_in_the_file_does_not_cost_the_others(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    payload = {
        "views": [
            {"name": "good", "query": "ERROR"},
            {"nope": True},
            "not even a dict",
            {"name": "also good", "severity": "warn"},
        ]
    }
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    state = store.load()
    assert [view.name for view in state.views] == ["good", "also good"]


def test_a_views_key_that_is_not_a_list_is_ignored(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    store.path.write_text(json.dumps({"views": "nonsense", "query": "kept"}), encoding="utf-8")

    state = store.load()
    assert state.views == ()
    assert state.query == "kept"


def test_saved_views_hold_no_log_content(tmp_path: Path) -> None:
    """The privacy guard: filters and a path, never a line or a result."""

    store = StateStore(root=tmp_path)
    store.save(
        SessionState(
            views=(SavedView(name="v", query="disk full", source="/var/log/alpha.log"),)
        )
    )
    written = store.path.read_text(encoding="utf-8")
    assert "disk full" in written  # the operator's own query, not log text
    assert "matched" not in written and "entries" not in written
    record = json.loads(written)["views"][0]
    assert set(record) == set(SavedView(name="v").__dict__)


# --- through the app --------------------------------------------------------


def test_saving_captures_the_open_source_and_the_active_filters(tmp_path: Path) -> None:
    first, _ = _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._select_source(first)
            app._update_state(query="disk", severity="error")
            await pilot.pause()

            view = app._capture_view("disk errors")
            assert view.query == "disk"
            assert view.severity == "error"
            assert Path(view.source) == first

    _run(scenario)


def test_applying_a_view_restores_every_field_in_one_render(tmp_path: Path) -> None:
    first, second = _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._select_source(second)
            await pilot.pause()

            view = SavedView(
                name="alpha errors",
                query="disk",
                severity="error",
                time_window="all",
                case_sensitive=True,
                invert_match=False,
                source=str(first),
            )

            renders = 0
            original = app._render_log

            def counting(**kwargs):
                nonlocal renders
                renders += 1
                return original(**kwargs)

            app._render_log = counting  # type: ignore[method-assign]
            app._apply_view(view)
            await pilot.pause()

            assert renders == 1, "a view must apply atomically, not field by field"
            assert app.state.query == "disk"
            assert app.state.severity == "error"
            assert app.advanced_drawer.settings.case_sensitive is True
            assert app.query_bar.get_query_value() == "disk"
            assert app._selected_source == first.resolve()
            # The drawer's switch follows the setting, not just the value.
            assert app.advanced_drawer.query_one("#case-sensitive", Switch).value is True

    _run(scenario)


def test_a_view_naming_a_vanished_source_still_applies_its_filters(tmp_path: Path) -> None:
    first, _ = _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._select_source(first)
            await pilot.pause()

            notices: list[str] = []
            app._notify = lambda text, severity="info": notices.append(text)  # type: ignore[assignment]

            app._apply_view(
                SavedView(name="gone", query="disk", source=str(tmp_path / "logs" / "nope.log"))
            )
            await pilot.pause()

            assert app.state.query == "disk"
            assert any("no longer there" in text for text in notices)

    _run(scenario)


def test_the_tree_lists_views_above_starred_above_the_roots(tmp_path: Path) -> None:
    first, _ = _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._update_state(starred=(str(first.resolve()),))
            await app._store_views([SavedView(name="alpha errors", query="disk")])
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            labels = [str(node.label) for node in tree.root.children]
            assert labels[0] == VIEWS_GROUP
            assert labels[1] == STARRED_GROUP
            assert len(labels) > 2

    _run(scenario)


def test_selecting_a_view_in_the_tree_applies_it(tmp_path: Path) -> None:
    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await app._store_views([SavedView(name="only errors", severity="error")])
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            view_node = tree.root.children[0].children[0]
            tree.select_node(view_node)
            await pilot.pause()

            assert app.state.severity == "error"

    _run(scenario)


def test_save_and_pick_by_keyboard_only(tmp_path: Path) -> None:
    first, _ = _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app._select_source(first)
            app._update_state(query="disk")
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("V")
            await pilot.pause()
            assert isinstance(app.screen, SaveViewDialog)

            for char in "keep":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            assert [view.name for view in app.state.views] == ["keep"]

            # And back out of the picker, applying it.
            app._update_state(query="")
            app.set_focus(app.log_panel)
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, ViewPickerDialog)

            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            assert app.state.query == "disk"

    _run(scenario)


def test_rename_and_delete_from_the_picker(tmp_path: Path) -> None:
    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await app._store_views(
                [SavedView(name="one", query="a"), SavedView(name="two", query="b")]
            )
            await pilot.pause()

            assert await app._handle_view_request(ViewRequest("rename", "one", "uno"))
            assert [view.name for view in app.state.views] == ["two", "uno"]

            assert await app._handle_view_request(ViewRequest("delete", "two"))
            assert [view.name for view in app.state.views] == ["uno"]

    _run(scenario)


def test_the_picker_arms_a_delete_before_doing_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        views = [SavedView(name="one"), SavedView(name="two")]
        results: list[ViewRequest | None] = []

        class _Host(LogViewerApp):
            pass

        app = _Host(config=LogConfig(log_dirs=[], discovery=DiscoverySettings()))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(ViewPickerDialog(views), callback=results.append)
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ViewPickerDialog), "one press must not delete"
            hint = app.screen.query_one("#view-hint", Static)
            assert "again" in hint.render().plain

            await pilot.press("d")
            await pilot.pause()
            assert results == [ViewRequest("delete", "one")]

    _run(scenario)


def test_renaming_in_the_picker_types_a_name_rather_than_commands(tmp_path: Path) -> None:
    """`d` inside the rename box is a letter, not a delete."""

    async def scenario() -> None:
        results: list[ViewRequest | None] = []
        app = LogViewerApp(config=LogConfig(log_dirs=[], discovery=DiscoverySettings()))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(ViewPickerDialog([SavedView(name="one")]), callback=results.append)
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()
            rename = app.screen.query_one("#rename-input", Input)
            assert rename.has_focus

            rename.value = ""
            for char in "odd":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()

            assert results == [ViewRequest("rename", "one", "odd")]

    _run(scenario)


def test_the_picker_fits_eighty_columns(tmp_path: Path) -> None:
    async def scenario() -> None:
        views = [
            SavedView(name=f"view {index}", query="something quite long to look at")
            for index in range(12)
        ]
        app = LogViewerApp(config=LogConfig(log_dirs=[], discovery=DiscoverySettings()))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(ViewPickerDialog(views))
            await pilot.pause()

            dialog = app.screen.query_one("#view-picker-dialog")
            assert dialog.region.right <= 80
            assert dialog.region.bottom <= 24
            for widget_id in (
                "#view-list",
                "#view-hint",
                "#dialog-actions",
                # Three buttons now. The row is right-aligned, so a button that
                # did not fit would run off the left edge rather than the right.
                "#delete-view",
            ):
                region = app.screen.query_one(widget_id).region
                assert region.width > 0 and region.height > 0, widget_id
                assert region.bottom <= 24, widget_id
                assert region.x >= 0, widget_id

    _run(scenario)


# --- the verbs on a view's row ----------------------------------------------
#
# Rename and delete were keyboard-only, and the tree row applied on click and
# offered nothing else. The merged row had answered the same question already.


def _find_action(app, action: str) -> tuple[int, int]:
    """Screen position of an action marker, found by its metadata."""

    from clv.app import ACTION_META

    screen = app.screen
    for y in range(screen.size.height):
        for x in range(screen.size.width):
            if screen.get_style_at(x, y).meta.get(ACTION_META) == action:
                return x, y
    raise AssertionError(f"no {action!r} marker painted anywhere on screen")


async def _with_one_view(app, pilot, name: str = "alpha errors"):
    """Save one view and expand the group so its row is on screen."""

    await pilot.pause()
    await app._store_views([SavedView(name=name, query="disk")])
    await pilot.pause()
    tree = app.query_one("#source-tree", LogTree)
    tree.root.children[0].expand()
    await pilot.pause()
    return tree


def test_a_view_row_offers_rename_and_delete(tmp_path: Path) -> None:
    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await _with_one_view(app, pilot)

            rename = _find_action(app, "view-rename")
            delete = _find_action(app, "view-delete")
            # Two distinct cells on one row.
            assert rename != delete
            assert rename[1] == delete[1]

    _run(scenario)


def test_the_rename_marker_opens_the_picker_on_that_view(tmp_path: Path) -> None:
    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await _with_one_view(app, pilot)

            await pilot.click(offset=_find_action(app, "view-rename"))
            await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, ViewPickerDialog)
            rename = app.screen.query_one("#rename-input", Input)
            assert rename.has_class("-active"), "the picker did not open on a rename"
            assert rename.value == "alpha errors"

    _run(scenario)


def test_the_delete_marker_arms_rather_than_deleting(tmp_path: Path) -> None:
    """A view is named work and there is no undo anywhere in CLV."""

    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await _with_one_view(app, pilot)

            await pilot.click(offset=_find_action(app, "view-delete"))
            await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, ViewPickerDialog)
            hint = app.screen.query_one("#view-hint", Static)
            assert "again" in hint.render().plain
            assert [view.name for view in app.state.views] == ["alpha errors"]

    _run(scenario)


def test_clicking_a_marker_does_not_apply_the_view(tmp_path: Path) -> None:
    """`prevent_default` is what stops the row selecting as well as acting."""

    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await _with_one_view(app, pilot)
            assert app.state.query == ""

            await pilot.click(offset=_find_action(app, "view-rename"))
            await pilot.pause()
            await pilot.pause()

            assert app.state.query == "", "the marker also applied the view"

    _run(scenario)


def test_the_delete_button_matches_the_d_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        views = [SavedView(name="one", query="a"), SavedView(name="two", query="b")]
        app = LogViewerApp(config=LogConfig(log_dirs=[], discovery=DiscoverySettings()))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            result: list = []
            app.push_screen(ViewPickerDialog(views), callback=result.append)
            await pilot.pause()

            await pilot.click("#delete-view")
            await pilot.pause()
            # Once arms and nothing more — the same contract the key has.
            assert not result
            assert isinstance(app.screen, ViewPickerDialog)

            # Textual ignores a click on a button still showing its press
            # animation, so wait it out rather than testing the debounce.
            await asyncio.sleep(Button("x").active_effect_duration + 0.05)
            await pilot.click("#delete-view")
            await pilot.pause()
            assert result == [ViewRequest("delete", "one")]

    _run(scenario)


def test_a_view_named_with_brackets_is_not_markup(tmp_path: Path) -> None:
    """A `str` label goes through Text.from_markup; the name is not ours to read."""

    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            tree = await _with_one_view(app, pilot, name="[bold]prod")

            row = tree.root.children[0].children[0]
            assert "[bold]prod" in row.label.plain

    _run(scenario)


def test_the_detail_pane_does_not_toggle_while_the_picker_is_open(tmp_path: Path) -> None:
    """`d` is toggle_detail globally, and the help now advertises the scoped one."""

    async def scenario() -> None:
        views = [SavedView(name="one", query="a")]
        app = LogViewerApp(config=LogConfig(log_dirs=[], discovery=DiscoverySettings()))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            before = app.state.detail_pane
            app.push_screen(ViewPickerDialog(views))
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()

            assert app.state.detail_pane is before, "the modal let `d` through"

    _run(scenario)


def test_creating_the_first_view_puts_a_tree_back(tmp_path: Path) -> None:
    """With no logs found the panel is a notice, and the reconcilers need a tree.

    They begin by looking for one; finding none they return, so the view was
    saved and stayed invisible.
    """

    async def scenario() -> None:
        app = LogViewerApp(config=LogConfig(log_dirs=[], discovery=DiscoverySettings()))
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            panel = app.query_one("#tree-panel")
            assert panel.query("Static.empty-tree"), "expected the empty notice"

            await app._store_views([SavedView(name="only one", query="disk")])
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            assert [str(node.label) for node in tree.root.children] == [VIEWS_GROUP]

            # And back to the notice when the last one goes.
            await app._store_views([])
            await pilot.pause()
            assert app.query_one("#tree-panel").query("Static.empty-tree")

    _run(scenario)


def test_creating_or_destroying_a_view_never_closes_the_tree(tmp_path: Path) -> None:
    """Saving a view used to rebuild the tree, collapsing every open folder."""

    root = tmp_path / "logs"
    for relative in ("", "alpha", "alpha/deep"):
        folder = root / relative if relative else root
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "a.log").write_text("2026-08-07 09:00:00 ERROR x\n", encoding="utf-8")

    def walk(node):
        for child in node.children:
            yield child
            yield from walk(child)

    def opened(tree) -> set[str]:
        return {
            str(node.label)
            for node in walk(tree.root)
            if node.allow_expand and node.is_expanded and isinstance(node.data, Path)
        }

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            tree = app.query_one("#source-tree", LogTree)
            for node in walk(tree.root):
                if node.allow_expand:
                    node.expand()
            await pilot.pause()
            before = opened(tree)
            assert before, "fixture should have folders to expand"

            await app._store_views([SavedView(name="one", query="d")])
            await pilot.pause()
            assert app.query_one("#source-tree", LogTree) is tree, "the tree was remounted"
            assert opened(tree) == before, "creating a view closed the tree"

            await app._store_views(
                [SavedView(name="one", query="d"), SavedView(name="two", query="x")]
            )
            await pilot.pause()
            assert opened(tree) == before, "a second view closed the tree"

            await app._store_views([SavedView(name="one", query="d")])
            await pilot.pause()
            assert opened(tree) == before, "destroying a view closed the tree"

            await app._store_views([])
            await pilot.pause()
            assert app.query_one("#source-tree", LogTree) is tree, "the tree was remounted"
            assert opened(tree) == before, "destroying the last view closed the tree"

    _run(scenario)


def test_deleting_the_only_view_from_its_row_closes_the_picker(tmp_path: Path) -> None:
    """The marker reopens the picker, and with nothing left it must not stay up."""

    from clv.app import ACTION_META

    _logs(tmp_path)

    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.run_test(size=(120, 34)) as pilot:
            tree = await _with_one_view(app, pilot, name="only one")

            await pilot.click(offset=_find_action(app, "view-delete"))
            await pilot.pause()
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            assert app.state.views == ()
            assert not isinstance(app.screen, ViewPickerDialog), "the picker stayed up"
            assert app._views_group(app.query_one("#source-tree", LogTree)) is None

    _run(scenario)
