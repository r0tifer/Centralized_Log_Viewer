"""The merged multi-source view (Item 13).

The acceptance bar this item set for itself is that **no feature becomes
single-source-only**, so the second half of this file is filtering, navigation,
marks and export exercised against a merge rather than a file. The first half
is the merge itself: order, anchoring, bounded memory, and tailing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from clv.app import MERGED_VIEW, LogViewerApp
from clv.services import SourceManager
from clv.services.session import ORIGIN_FIELD, SourceSession
from clv.storage import SavedView, SessionState, StateStore
from clv.widgets.log_view import GUTTER_WIDTH


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    """Two logs whose lines interleave in time."""

    root = tmp_path / "logs"
    root.mkdir(exist_ok=True)
    alpha = _write(
        root / "alpha.log",
        [
            "2026-08-11 10:00:00 - INFO - alpha one",
            "2026-08-11 10:00:02 - ERROR - alpha two",
        ],
    )
    beta = _write(
        root / "beta.log",
        [
            "2026-08-11 10:00:01 - INFO - beta one",
            "2026-08-11 10:00:03 - WARN - beta two",
        ],
    )
    return alpha, beta


def _messages(session: SourceSession) -> list[str]:
    return [entry.message for entry in session.entries]


# --- the merge --------------------------------------------------------------


def test_two_sources_merge_into_timestamp_order(tmp_path: Path) -> None:
    session = SourceSession(max_lines=100)
    session.open_many(list(_sources(tmp_path)))

    assert _messages(session) == ["alpha one", "beta one", "alpha two", "beta two"]
    assert session.is_merged is True


def test_three_sources_merge_into_timestamp_order(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)
    gamma = _write(
        tmp_path / "logs" / "gamma.log",
        ["2026-08-11 09:59:59 - INFO - gamma first", "2026-08-11 10:00:04 - INFO - gamma last"],
    )

    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta, gamma])

    assert _messages(session) == [
        "gamma first",
        "alpha one",
        "beta one",
        "alpha two",
        "beta two",
        "gamma last",
    ]


def test_every_merged_line_knows_its_source(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    origins = [session.origin_of(entry) for entry in session.entries]

    assert origins == [alpha, beta, alpha, beta]
    assert all(entry.fields[ORIGIN_FIELD] for entry in session.entries)


def test_untimestamped_lines_are_anchored_to_their_own_source_not_dropped(
    tmp_path: Path,
) -> None:
    """The 'never silently lose a line' rule, applied to ordering."""

    root = tmp_path / "logs"
    root.mkdir()
    alpha = _write(
        root / "alpha.log",
        [
            "2026-08-11 10:00:00 - INFO - alpha one",
            "  at com.example.Thing.method(Thing.java:42)",
            "2026-08-11 10:00:02 - INFO - alpha two",
        ],
    )
    beta = _write(root / "beta.log", ["2026-08-11 10:00:01 - INFO - beta one"])

    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    raws = [entry.raw for entry in session.entries]
    # The continuation stays directly after the line it belongs to, and before
    # beta's line, because it inherited alpha's timestamp.
    assert raws[1].strip().startswith("at com.example")
    assert len(raws) == 4


def test_lines_with_no_timestamp_at_all_are_counted_and_kept(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    alpha = _write(root / "alpha.log", ["no timestamp here", "nor here"])
    beta = _write(root / "beta.log", ["2026-08-11 10:00:01 - INFO - beta one"])

    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    assert len(session.entries) == 3
    assert session.anchored == 2
    # Nothing to anchor to, so they sort first, in their own source's order.
    assert [entry.raw for entry in session.entries][:2] == ["no timestamp here", "nor here"]


def test_aware_and_naive_timestamps_merge_rather_than_refuse(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    aware = _write(
        root / "aware.log", ['{"timestamp": "2026-08-11T10:00:01+00:00", "message": "aware"}']
    )
    naive = _write(root / "naive.log", ["2026-08-11 10:00:00 - INFO - naive"])

    session = SourceSession(max_lines=100)
    session.open_many([aware, naive])

    assert [entry.message for entry in session.entries] == ["naive", "aware"]


def test_a_source_whose_own_stamps_jump_around_still_merges_correctly(
    tmp_path: Path,
) -> None:
    """heapq.merge trusts its inputs, so each source is ordered first."""

    root = tmp_path / "logs"
    root.mkdir()
    alpha = _write(
        root / "alpha.log",
        [
            "2026-08-11 10:00:05 - INFO - late first",
            "2026-08-11 10:00:01 - INFO - early second",
        ],
    )
    beta = _write(root / "beta.log", ["2026-08-11 10:00:03 - INFO - beta"])

    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    assert [entry.message for entry in session.entries] == [
        "early second",
        "beta",
        "late first",
    ]


def test_the_buffer_cap_is_per_source(tmp_path: Path) -> None:
    """Total memory is n * max_buffer_lines, and each source keeps its own."""

    root = tmp_path / "logs"
    root.mkdir()
    alpha = _write(root / "alpha.log", [f"2026-08-11 10:00:{i:02d} - INFO - a{i}" for i in range(10)])
    beta = _write(root / "beta.log", [f"2026-08-11 10:00:{i:02d} - INFO - b{i}" for i in range(10)])

    session = SourceSession(max_lines=3)
    session.open_many([alpha, beta])

    assert len(session.entries) == 6
    for buffer in session.buffers:
        assert len(buffer.entries) == 3


def test_the_merge_is_a_view_and_not_a_fourth_copy(tmp_path: Path) -> None:
    session = SourceSession(max_lines=100)
    session.open_many(list(_sources(tmp_path)))

    merged = session.entries
    held = [entry for buffer in session.buffers for entry in buffer.entries]

    assert {id(entry) for entry in merged} == {id(entry) for entry in held}


def test_the_merge_is_cached_between_renders(tmp_path: Path) -> None:
    """A render happens on every keystroke in the query box; a merge must not."""

    session = SourceSession(max_lines=100)
    session.open_many(list(_sources(tmp_path)))

    assert session.entries is session.entries


def test_tailing_a_member_lands_in_the_merged_stream(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    with beta.open("a", encoding="utf-8") as handle:
        handle.write("2026-08-11 10:00:04 - INFO - beta three\n")
    outcomes = session.poll()

    assert len(outcomes) == 1
    assert _messages(session)[-1] == "beta three"
    assert session.lands_at_the_end(outcomes[0].entries) is True


def test_a_line_that_sorts_into_the_middle_is_reported_as_such(tmp_path: Path) -> None:
    """The caller redraws instead of appending, or the row lands in the wrong place."""

    alpha, beta = _sources(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    with alpha.open("a", encoding="utf-8") as handle:
        handle.write("2026-08-11 10:00:01 - INFO - out of order\n")
    outcomes = session.poll()

    assert session.lands_at_the_end(outcomes[0].entries) is False
    # It landed in the middle, which is exactly why an append would be wrong.
    # Two lines share 10:00:01, and the member order breaks the tie.
    assert [entry.message for entry in session.entries] == [
        "alpha one",
        "out of order",
        "beta one",
        "alpha two",
        "beta two",
    ]


def test_polling_asks_each_member_exactly_once(tmp_path: Path) -> None:
    """Poll cost must not scale super-linearly with member count."""

    alpha, beta = _sources(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_many([alpha, beta])

    counts = {"n": 0}
    for buffer in session.buffers:
        inner = buffer.reader.poll

        def counting(inner=inner):
            counts["n"] += 1
            return inner()

        buffer.reader.poll = counting

    session.poll()

    assert counts["n"] == 2


def test_a_member_that_cannot_be_opened_is_reported_and_the_rest_survive(
    tmp_path: Path,
) -> None:
    alpha, beta = _sources(tmp_path)
    session = SourceSession(max_lines=100)

    opened, failed = session.open_many([alpha, tmp_path / "logs" / "gone.log", beta])

    assert len(opened) == 2
    assert [path.name for path, _reason in failed] == ["gone.log"]
    assert len(session.entries) == 4


# --- the app ----------------------------------------------------------------


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


def _merged_app(tmp_path: Path):
    alpha, beta = _sources(tmp_path)

    def build(app: LogViewerApp) -> None:
        app._source_manager = SourceManager([alpha.parent], [])
        app.state = replace_state(app.state, merged=(str(alpha), str(beta)))

    return alpha, beta, build


def replace_state(state: SessionState, **changes) -> SessionState:
    from dataclasses import replace

    return replace(state, **changes)


def test_x_toggles_a_source_into_the_set_and_u_opens_it(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            await app._rescan()
            await pilot.pause()

            app._highlight_source(alpha, select=False)
            await pilot.pause()
            app.action_toggle_merge()
            await pilot.pause()
            app._highlight_source(beta, select=False)
            await pilot.pause()
            app.action_toggle_merge()
            await pilot.pause()

            assert len(app.state.merged) == 2

            app.action_open_merged()
            await pilot.pause()

            assert app._session.is_merged is True
            assert [entry.message for entry in app._entries] == [
                "alpha one",
                "beta one",
                "alpha two",
                "beta two",
            ]

            # Toggling again removes it.
            app.action_toggle_merge()
            await pilot.pause()
            assert len(app.state.merged) == 1

    asyncio.run(scenario())


def test_the_merged_set_is_indicated_in_the_tree(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(merged=(str(alpha),))
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            labels = {
                Path(str(node.data)).name: str(node.label)
                for node in _walk(tree.root)
                if isinstance(node.data, Path) and node.data.is_file()
            }
            assert "⧉" in labels["alpha.log"]
            assert "⧉" not in labels["beta.log"]

    asyncio.run(scenario())


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _open_merged(app: LogViewerApp, alpha: Path, beta: Path) -> None:
    app._source_manager = SourceManager([alpha.parent], [])
    app._update_state(merged=(str(alpha), str(beta)))
    app.action_open_merged()


def test_the_source_column_is_rendered_and_shrinks_at_80_columns(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario(width: int, expected: str) -> None:
        app = LogViewerApp()
        async with app.run_test(size=(width, 30)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            # Past the gutter, which belongs to marks.
            rendered = [line[GUTTER_WIDTH:] for line in app.log_panel.text_lines]
            assert any(line.startswith(expected) for line in rendered), rendered
            # The log text starts after a column of the width for this
            # breakpoint — the column gives way before the log does.
            first = next(line for line in rendered if line.startswith(expected))
            assert first[len(expected) :].lstrip().startswith("2026-08-11")
            # Nothing may run off the edge at 80 columns.
            assert app.log_panel.region.right <= width

    asyncio.run(scenario(150, "alpha.log"))
    # 8 columns at -compact: elided from the left, because rotated members and
    # unit names differ at the end.
    asyncio.run(scenario(80, "…pha.log"))


def test_the_status_line_names_the_set_and_counts_anchored_lines(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    alpha = _write(root / "alpha.log", ["no timestamp"])
    beta = _write(root / "beta.log", ["2026-08-11 10:00:01 - INFO - beta"])

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            status = _status(app)
            assert "alpha.log+beta.log" in status
            assert "2 sources" in status
            assert "1 anchored" in status

    asyncio.run(scenario())


def test_filtering_a_merged_view_keeps_both_sources(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            app._update_state(query="two")
            app._render_log()
            await pilot.pause()

            shown = [entry.message for entry in app._visible_entries(app._entries).entries]
            assert shown == ["alpha two", "beta two"]

            # A field query on the origin works because the lines carry it.
            app._update_state(query="source:beta.log")
            app._render_log()
            await pilot.pause()
            shown = [entry.message for entry in app._visible_entries(app._entries).entries]
            assert shown == ["beta one", "beta two"]

    asyncio.run(scenario())


def test_navigation_steps_through_a_merged_view(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            # No query, severity "all": n steps between WARN and above, which
            # here means one line from each source.
            app.action_next_match()
            await pilot.pause()
            first = app.log_panel.cursor_entry
            app.action_next_match()
            await pilot.pause()
            second = app.log_panel.cursor_entry

            assert {first.message, second.message} == {"alpha two", "beta two"}

    asyncio.run(scenario())


def test_marks_in_a_merged_view_stay_distinct_per_source(tmp_path: Path) -> None:
    """Two identical lines from two logs are two lines, not one."""

    root = tmp_path / "logs"
    root.mkdir()
    same = "2026-08-11 10:00:00 - ERROR - disk full"
    alpha = _write(root / "alpha.log", [same])
    beta = _write(root / "beta.log", [same])

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            app.log_panel.move_cursor(0)
            app.action_toggle_mark()
            await pilot.pause()

            entries = list(app._entries)
            assert app._marks.contains(app._origin(entries[0]), entries[0]) is True
            assert app._marks.contains(app._origin(entries[1]), entries[1]) is False
            assert app._marks.count_for(*app._origins()) == 1
            assert "1 marked" in _status(app)

    asyncio.run(scenario())


def test_marks_survive_a_re_render_of_a_merged_view(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            app.log_panel.move_cursor(1)
            app.action_toggle_mark()
            await pilot.pause()
            marked = app.log_panel.cursor_entry

            app._render_log()
            await pilot.pause()

            assert app._marks.contains(app._origin(marked), marked) is True
            assert app._marks.count_for(*app._origins()) == 1

    asyncio.run(scenario())


def test_export_of_a_merged_view_writes_every_source(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)
    destination = tmp_path / "out.txt"

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            from clv.widgets.export_dialog import ExportRequest

            entries = list(app._visible_entries(app._entries).entries)
            app._run_export(
                ExportRequest(key="builtin:text", path=destination, marked_only=False),
                entries,
            )
            await pilot.pause()

            written = destination.read_text(encoding="utf-8").splitlines()
            assert [line.split(" - ")[-1] for line in written] == [
                "alpha one",
                "beta one",
                "alpha two",
                "beta two",
            ]

    asyncio.run(scenario())


def test_the_export_filename_names_the_set(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            assert app._merged_name() == "alpha.log+beta.log"

    asyncio.run(scenario())


def test_the_detail_pane_shows_a_merged_line_with_its_source(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            app._set_detail_pane(True)
            app.log_panel.move_cursor(1)
            await pilot.pause()

            strips = app.screen._compositor.render_strips()
            painted = "\n".join(
                "".join(segment.text for segment in strip) for strip in strips
            )
            # The origin reached the property list because it is a field, so
            # the detail pane needed no changes at all for a merged view.
            assert "beta.log" in painted

    asyncio.run(scenario())


def test_a_member_deleted_mid_session_is_reported_and_the_rest_keep_going(
    tmp_path: Path,
) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(merged=(str(alpha), str(beta)))
            beta.unlink()
            app.action_open_merged()
            await pilot.pause()

            assert app._session.is_merged is False  # only alpha opened
            assert [entry.message for entry in app._entries] == ["alpha one", "alpha two"]

    asyncio.run(scenario())


# --- persistence ------------------------------------------------------------


def test_the_merged_set_persists_across_a_reload(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path / "cache")
    store.save(SessionState(merged=("/var/log/a.log", "/var/log/b.log")))

    assert store.load().merged == ("/var/log/a.log", "/var/log/b.log")


def test_a_saved_view_captures_and_restores_the_merged_set(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            _open_merged(app, alpha, beta)
            await pilot.pause()

            view = app._capture_view("both")
            assert view.merged == (str(alpha), str(beta))

            # Applying it puts the whole set back, not just one member.
            app._update_state(merged=())
            app._select_source(alpha)
            await pilot.pause()
            app._apply_view(view)
            await pilot.pause()

            assert app._session.is_merged is True
            assert len(app._entries) == 4

    asyncio.run(scenario())


def test_a_view_saved_on_one_log_carries_no_merged_set(tmp_path: Path) -> None:
    alpha, _beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._update_state(merged=(str(alpha),))
            app._select_source(alpha)
            await pilot.pause()

            assert app._capture_view("one").merged == ()

    asyncio.run(scenario())


def test_a_malformed_merged_list_in_a_view_is_dropped_not_fatal() -> None:
    view = SavedView.from_dict({"name": "broken", "merged": ["/a.log", 7, None]})

    assert view is not None
    assert view.merged == ("/a.log",)


# --- the tree, while the set is edited --------------------------------------


def _expanded_folders(tree) -> set[str]:
    """Expanded *folders* only — a group node is not a folder, and the merged
    group legitimately appears and disappears as the set is edited."""

    return {
        str(node.label)
        for node in _walk(tree.root)
        if node.allow_expand
        and node.is_expanded
        and node is not tree.root
        and isinstance(node.data, Path)
    }


def _group_labels(tree) -> list[str]:
    """Labels of the group rows at the top of the tree.

    The merged group carries a marker rather than `None`, because unlike the
    other headings it is selectable — see MERGED_VIEW.
    """

    return [
        str(node.label)
        for node in tree.root.children
        if node.data is None or node.data is MERGED_VIEW
    ]


def _merged_row(tree):
    return next((n for n in tree.root.children if n.data is MERGED_VIEW), None)


def test_toggling_merge_leaves_the_tree_expansion_alone(tmp_path: Path) -> None:
    """Rebuilding the tree collapses every folder the operator had opened."""

    alpha, beta = _sources(tmp_path)
    nested = alpha.parent / "nested"
    nested.mkdir()
    _write(nested / "deep.log", ["2026-08-11 10:00:00 - INFO - deep"])

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            for node in _walk(tree.root):
                if node.allow_expand and node is not tree.root:
                    node.expand()
            await pilot.pause()
            before = _expanded_folders(tree)
            assert before, "fixture should have folders to expand"

            app._highlight_source(alpha, select=False)
            await pilot.pause()
            app.action_toggle_merge()
            await pilot.pause()

            assert _expanded_folders(tree) == before, "adding to the set collapsed the tree"

            app.action_toggle_merge()
            await pilot.pause()

            assert _expanded_folders(tree) == before, "removing from the set collapsed the tree"

    asyncio.run(scenario())


def test_toggling_merge_does_not_re_run_discovery(tmp_path: Path) -> None:
    """Membership says nothing about what is on disk."""

    alpha, _beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            await app._rescan()
            await pilot.pause()

            calls = {"n": 0}
            original = app._rescan

            async def counting():
                calls["n"] += 1
                await original()

            app._rescan = counting
            app._highlight_source(alpha, select=False)
            await pilot.pause()
            app.action_toggle_merge()
            await pilot.pause()

            assert calls["n"] == 0

    asyncio.run(scenario())


def test_the_merged_group_appears_below_starred_and_above_the_roots(
    tmp_path: Path,
) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(starred=(str(beta.resolve()),))
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            assert _merged_row(tree) is None

            app._highlight_source(alpha, select=False)
            await pilot.pause()
            app.action_toggle_merge()
            await pilot.pause()

            children = list(tree.root.children)
            labels = [str(node.label) for node in children]
            merged_at = children.index(_merged_row(tree))
            assert labels.index("⭐ Starred") < merged_at
            # And above the configured root, which is the last thing here.
            assert merged_at < len(labels) - 1

            group = _merged_row(tree)
            assert [Path(str(child.data)).name for child in group.children] == ["alpha.log"]
            # The count is what makes the row read as something to open.
            assert str(group.label) == "⧉ Merged (1 source)"

    asyncio.run(scenario())


def test_the_group_grows_shrinks_and_disappears_with_the_set(tmp_path: Path) -> None:
    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            for path in (beta, alpha):
                app._highlight_source(path, select=False)
                await pilot.pause()
                app.action_toggle_merge()
                await pilot.pause()

            group = _merged_row(tree)
            assert [Path(str(c.data)).name for c in group.children] == [
                "alpha.log",
                "beta.log",
            ]
            assert str(group.label) == "⧉ Merged (2 sources)"

            for path in (alpha, beta):
                app._highlight_source(path, select=False)
                await pilot.pause()
                app.action_toggle_merge()
                await pilot.pause()

            # An empty group is a row that explains nothing.
            assert _merged_row(tree) is None

    asyncio.run(scenario())


def test_the_indicator_tracks_membership_on_the_file_itself(tmp_path: Path) -> None:
    alpha, _beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(starred=(str(alpha.resolve()),))
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)

            def labels_for(path: Path) -> list[str]:
                group = app._merged_group(tree)
                return [
                    str(node.label)
                    for node in _walk(tree.root)
                    if isinstance(node.data, Path)
                    and node.data.resolve() == path.resolve()
                    and node.parent is not group
                ]

            assert all("⧉" not in label for label in labels_for(alpha))

            app._highlight_source(alpha, select=False)
            await pilot.pause()
            app.action_toggle_merge()
            await pilot.pause()

            marked = labels_for(alpha)
            # Both copies: the file under its folder, and the starred entry.
            assert len(marked) == 2
            assert all(label.startswith("⧉") for label in marked)
            # The star still owns the icon slot; the indicator is a prefix.
            assert any("⭐" in label for label in marked)

            app.action_toggle_merge()
            await pilot.pause()
            assert all("⧉" not in label for label in labels_for(alpha))

    asyncio.run(scenario())


def test_the_group_survives_a_rescan(tmp_path: Path) -> None:
    alpha, _beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(merged=(str(alpha),))
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            assert _merged_row(tree) is not None

    asyncio.run(scenario())


def test_a_merged_member_that_vanished_is_still_listed(tmp_path: Path) -> None:
    """It was chosen one keystroke at a time; it should be visible to remove."""

    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(merged=(str(alpha), str(beta)))
            beta.unlink()
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            group = _merged_row(tree)
            assert [Path(str(c.data)).name for c in group.children] == [
                "alpha.log",
                "beta.log",
            ]

    asyncio.run(scenario())


def test_selecting_the_merged_row_opens_the_merged_view(tmp_path: Path) -> None:
    """The only way back into a set with the mouse, and the obvious one after
    a restart — `u` is a keybinding nobody has to know to be able to click."""

    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(merged=(str(alpha), str(beta)))
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            row = _merged_row(tree)
            assert row is not None

            tree.select_node(row)
            await pilot.pause()

            assert app._session.is_merged is True
            assert [entry.message for entry in app._entries] == [
                "alpha one",
                "beta one",
                "alpha two",
                "beta two",
            ]

    asyncio.run(scenario())


def test_a_member_row_still_opens_that_member_on_its_own(tmp_path: Path) -> None:
    """Selecting one member is also a reasonable thing to want."""

    alpha, beta = _sources(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            app._update_state(merged=(str(alpha), str(beta)))
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            member = _merged_row(tree).children[0]
            tree.select_node(member)
            await pilot.pause()

            assert app._session.is_merged is False
            assert [entry.message for entry in app._entries] == ["alpha one", "alpha two"]

    asyncio.run(scenario())


def test_the_merged_set_is_reachable_after_a_restart(tmp_path: Path) -> None:
    """The set is persisted, so a fresh app must offer it without a keystroke."""

    alpha, beta = _sources(tmp_path)
    store = StateStore(root=tmp_path / "cache")

    async def first_run() -> None:
        app = LogViewerApp(store=store)
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            await app._rescan()
            await pilot.pause()
            for path in (alpha, beta):
                app._highlight_source(path, select=False)
                await pilot.pause()
                app.action_toggle_merge()
                await pilot.pause()
            app._persist_state = True
            store.save(app.state)

    async def second_run() -> None:
        app = LogViewerApp(store=store)
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([alpha.parent], [])
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)
            row = _merged_row(tree)
            assert row is not None, "the merged set did not survive the restart"
            assert str(row.label) == "⧉ Merged (2 sources)"

            tree.select_node(row)
            await pilot.pause()
            assert app._session.is_merged is True

    asyncio.run(first_run())
    asyncio.run(second_run())


def test_applying_a_view_moves_the_merged_group_to_its_set(tmp_path: Path) -> None:
    """Two named sets, switched between — the tree must follow the open one.

    A delta cannot express this: a view replaces the whole set at once, so the
    tree is reconciled from state rather than patched per source.
    """

    root = tmp_path / "logs"
    root.mkdir()
    web = [_write(root / f"web{i}.log", [f"2026-08-12 10:00:0{i} - INFO - w{i}"]) for i in (1, 2)]
    db = [_write(root / f"db{i}.log", [f"2026-08-12 10:00:0{i} - INFO - d{i}"]) for i in (1, 2)]

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            from clv.app import LogTree

            tree = app.query_one("#source-tree", LogTree)

            def listed() -> list[str]:
                group = _merged_row(tree)
                return [Path(str(c.data)).name for c in group.children] if group else []

            def marked() -> set[str]:
                return {
                    Path(str(node.data)).name
                    for node in _walk(tree.root)
                    if isinstance(node.data, Path)
                    and node.parent is not _merged_row(tree)
                    and str(node.label).startswith("⧉")
                }

            views = []
            for members, name in ((web, "web tier"), (db, "db tier")):
                app._update_state(merged=tuple(str(m) for m in members))
                app._sync_merged_tree()
                app.action_open_merged()
                await pilot.pause()
                views.append(app._capture_view(name))

            assert [view.name for view in views] == ["web tier", "db tier"]
            assert all(len(view.merged) == 2 for view in views)

            for view, expected in ((views[0], "web"), (views[1], "db"), (views[0], "web")):
                app._apply_view(view)
                await pilot.pause()

                names = {f"{expected}1.log", f"{expected}2.log"}
                assert app._session.is_merged is True
                assert set(listed()) == names, f"group lists {listed()} for '{view.name}'"
                assert marked() == names, f"indicators say {marked()} for '{view.name}'"
                assert str(_merged_row(tree).label) == "⧉ Merged (2 sources)"

    asyncio.run(scenario())
