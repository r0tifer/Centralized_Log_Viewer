"""Repeat clustering: the shape rules, the lookback, and the no-loss guarantee.

The guarantee is the point of the feature and the thing most easily broken by a
later optimisation: collapsing is a *display transform*, so every line behind a
cluster must still be reachable, markable and exportable. Several tests here
exist only to keep that true.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Group
from textual.widgets import Button, Checkbox, Input, OptionList, Static, Switch

from clv.app import LogViewerApp
from clv.services.clustering import (
    DEFAULT_LOOKBACK,
    Cluster,
    ClusterStream,
    cluster_entries,
    describe,
    expand,
    normalise,
    shape_of,
    summarise,
)
from clv.services.parsing import LogEntry, parse_lines
from clv.services.refs import RemoteRef
from clv.services.session import tag_origins
from clv.services.session import ORIGIN_FIELD
from clv.storage import SessionState, StateStore
from clv.widgets.export_dialog import ExportDialog

BASE = datetime(2026, 8, 7, 9, 25, 0)


def _entry(message: str, *, level: str = "INFO", second: int = 0, source: str = "") -> LogEntry:
    fields = {ORIGIN_FIELD: source} if source else {}
    return LogEntry(
        raw=f"2026-08-07 09:25:{second:02d} - {level} - {message}",
        timestamp=BASE + timedelta(seconds=second),
        level=level,
        message=message,
        format_name="python-logging",
        fields=fields,
    )


# --- the normalisation rules ------------------------------------------------


def test_each_rule_replaces_what_it_owns() -> None:
    cases = [
        ('said "hello world" twice', 'said <str> twice'),
        ("started at 2026-08-07 09:25:01 sharp", "started at <ts> sharp"),
        ("at 09:25:01 sharp", "at <ts> sharp"),
        ("job 3f2504e0-4f89-11d3-9a0c-0305e82c3301 done", "job <uuid> done"),
        ("peer fe80::1ff:fe23:4567:890a closed", "peer <ipv6> closed"),
        ("from 10.0.0.5 refused", "from <ip> refused"),
        ("from 10.0.0.5:5432 refused", "from <ip> refused"),
        ("addr 0xdeadbeef unmapped", "addr <hex> unmapped"),
        ("digest a1b2c3d4e5f6 mismatch", "digest <hex> mismatch"),
        ("reading /var/log/app.log now", "reading <path> now"),
        ("took 1.25s", "took <float>s"),
        ("took 125ms", "took <int>ms"),
        ("retry 7 of 9", "retry <int> of <int>"),
    ]

    for text, expected in cases:
        assert normalise(text) == expected, text


def test_the_rules_combine_without_eating_each_other() -> None:
    """Each rule runs on what the last left, and placeholders carry no digits."""

    text = (
        'GET /orders/8821 from 10.0.0.5:5432 at 2026-08-07 09:25:01 '
        'took 1.25s id=3f2504e0-4f89-11d3-9a0c-0305e82c3301 "retry 3"'
    )

    assert normalise(text) == (
        "GET <path> from <ip> at <ts> took <float>s id=<uuid> <str>"
    )


def test_a_word_with_digits_in_it_keeps_them() -> None:
    """`sha256` is a name, not a number. Only a *leading* boundary counts."""

    assert normalise("sha256 checksum for utf8 payload") == "sha256 checksum for utf8 payload"


def test_whitespace_is_collapsed_so_alignment_does_not_split_a_cluster() -> None:
    assert normalise("disk    almost   full") == "disk almost full"


# --- what clusters, and what does not ---------------------------------------


def test_two_lines_differing_only_in_a_request_id_cluster() -> None:
    entries = [
        _entry("handled request 8821 in 12ms", second=1),
        _entry("handled request 8822 in 47ms", second=2),
    ]

    stream = cluster_entries(entries)

    assert len(stream.rows) == 1
    assert isinstance(stream.rows[0], Cluster)
    assert stream.rows[0].count == 2


def test_two_genuinely_different_lines_do_not_cluster() -> None:
    entries = [
        _entry("handled request 8821", second=1),
        _entry("disk almost full", second=2),
    ]

    stream = cluster_entries(entries)

    assert len(stream.rows) == 2
    assert stream.clustered == 0


def test_the_same_text_at_two_severities_stays_two_clusters() -> None:
    """A WARN and an ERROR that read alike are not the same event."""

    entries = [
        _entry("timeout talking to 10.0.0.1", level="WARN", second=1),
        _entry("timeout talking to 10.0.0.2", level="ERROR", second=2),
        _entry("timeout talking to 10.0.0.3", level="WARN", second=3),
    ]

    stream = cluster_entries(entries)

    assert len(stream.rows) == 2
    assert shape_of(entries[0]) != shape_of(entries[1])


def test_a_cluster_never_spans_two_sources() -> None:
    """In a merged view the source column has to keep meaning something."""

    entries = [
        _entry("connection refused for 10.0.0.1", second=1, source="/logs/alpha.log"),
        _entry("connection refused for 10.0.0.2", second=2, source="/logs/beta.log"),
        _entry("connection refused for 10.0.0.3", second=3, source="/logs/alpha.log"),
    ]

    stream = cluster_entries(entries)

    assert len(stream.rows) == 2
    alpha = stream.rows[0]
    assert isinstance(alpha, Cluster)
    assert alpha.count == 2
    assert {entry.fields[ORIGIN_FIELD] for entry in alpha.entries} == {"/logs/alpha.log"}


def test_a_cluster_reports_its_count_span_and_worst_level() -> None:
    entries = [_entry(f"request {index} failed", level="ERROR", second=index) for index in range(5)]

    cluster = cluster_entries(entries).rows[0]

    assert isinstance(cluster, Cluster)
    assert cluster.count == 5
    assert cluster.first == BASE
    assert cluster.last == BASE + timedelta(seconds=4)
    assert cluster.level == "ERROR"
    assert cluster.representative is entries[0]


def test_a_cluster_of_lines_with_no_timestamp_has_no_span() -> None:
    entries = [LogEntry(raw=f"plain {index}", message=f"plain {index}") for index in range(3)]

    cluster = cluster_entries(entries).rows[0]

    assert isinstance(cluster, Cluster)
    assert cluster.first is None and cluster.last is None
    assert cluster.count == 3


# --- the lookback bound -----------------------------------------------------


def test_entries_beyond_the_lookback_do_not_cluster_together() -> None:
    """A cluster must not silently span a session and swallow an old event."""

    entries = [_entry("connection refused for 10.0.0.1", second=0)]
    # These cluster among themselves — `unrelated <int>` is one shape — which is
    # why the assertions below name the cluster they are about.
    entries += [_entry(f"unrelated {index}", second=index + 1) for index in range(10)]
    entries += [_entry("connection refused for 10.0.0.2", second=20)]

    def refused(stream) -> int:
        return sum(
            row.count
            for row in stream.rows
            if isinstance(row, Cluster) and "refused" in row.representative.raw
        )

    assert refused(cluster_entries(entries, lookback=20)) == 2
    far = cluster_entries(entries, lookback=5)
    assert refused(far) == 0
    assert far.entries == len(entries)


def test_the_lookback_is_measured_from_the_last_member_not_the_first() -> None:
    """A steady drip keeps one cluster alive; that is what a repeat *is*."""

    entries = []
    for index in range(30):
        entries.append(_entry(f"connection refused for 10.0.0.{index}", second=index))
        entries.append(_entry(f"unrelated {index}", second=index))

    stream = cluster_entries(entries, lookback=3)

    refused = [row for row in stream.rows if isinstance(row, Cluster)]
    assert any(cluster.count == 30 for cluster in refused)


def test_the_default_lookback_is_a_bound_not_a_preference() -> None:
    assert DEFAULT_LOOKBACK >= 2


# --- the no-loss guarantee --------------------------------------------------


def test_expanding_a_cluster_gives_back_every_original_line() -> None:
    """Byte-identical, in order, and none missing. Item 15 fails without this."""

    lines = [
        f"2026-08-07 09:25:{index:02d} - ERROR - connection refused for 10.0.0.{index}:54{index:02d}"
        for index in range(25)
    ]
    lines.insert(10, "2026-08-07 09:25:10 - WARN - disk almost full")
    entries = parse_lines(lines)

    stream = cluster_entries(entries)
    restored = expand(stream.rows)

    # Nothing lost and nothing invented.
    assert sorted(entry.raw for entry in restored) == sorted(entry.raw for entry in entries)
    assert stream.entries == len(entries)
    # Each cluster hands back its own members in the order they were read. The
    # *pane* order does change — a cluster gathers its members at the position
    # of the first one, which is what "collapse a run" means — so the guarantee
    # is per cluster rather than over the whole list.
    for row in stream.rows:
        if isinstance(row, Cluster):
            positions = [entries.index(member) for member in row.entries]
            assert positions == sorted(positions)
            assert [member.raw for member in row.entries] == [
                entries[position].raw for position in positions
            ]


def test_clustering_never_drops_an_unparsed_line() -> None:
    entries = parse_lines(["alpha", "beta", "alpha", "   ", "gamma"])

    stream = cluster_entries(entries)

    assert stream.entries == len(entries)
    assert sorted(entry.raw for entry in expand(stream.rows)) == sorted(
        entry.raw for entry in entries
    )


# --- the incremental path ---------------------------------------------------


def test_incremental_clustering_matches_a_full_recompute() -> None:
    entries = [
        _entry(f"connection refused for 10.0.0.{index}", second=index % 60)
        for index in range(50)
    ]
    entries += [_entry("disk almost full", level="WARN", second=59)]

    full = cluster_entries(entries)
    incremental = ClusterStream(lookback=DEFAULT_LOOKBACK)
    for entry in entries:
        incremental.add(entry)

    def summary(stream: ClusterStream) -> list[tuple[str, int]]:
        return [
            (row.representative.raw, row.count) if isinstance(row, Cluster) else (row.raw, 1)
            for row in stream.rows
        ]

    assert summary(incremental) == summary(full)


def test_a_growing_cluster_reports_which_row_changed() -> None:
    stream = ClusterStream(lookback=DEFAULT_LOOKBACK)

    first = stream.add(_entry("connection refused for 10.0.0.1", second=1))
    second = stream.add(_entry("connection refused for 10.0.0.2", second=2))

    assert first.appended is True
    assert second.appended is False
    assert second.index == first.index
    assert isinstance(second.row, Cluster)


# --- the clustered export ---------------------------------------------------


def test_summarise_turns_a_cluster_into_an_ordinary_entry() -> None:
    """So every exporter writes it without knowing clusters exist."""

    entries = [_entry(f"request {index} failed", level="ERROR", second=index) for index in range(3)]
    cluster = cluster_entries(entries).rows[0]
    assert isinstance(cluster, Cluster)

    summary = summarise(cluster)

    assert isinstance(summary, LogEntry)
    assert summary.raw.startswith("×3")
    assert entries[0].raw in summary.raw
    assert summary.fields["cluster.count"] == "3"
    assert summary.fields["cluster.first"].startswith("2026-08-07 09:25:00")
    assert summary.fields["cluster.last"].startswith("2026-08-07 09:25:02")
    assert summary.level == "ERROR"


def test_describe_reports_what_collapsing_bought() -> None:
    entries = [_entry(f"request {index}", second=index) for index in range(4)]

    assert describe(cluster_entries(entries)) == "4 lines in 1 clusters"
    assert describe(cluster_entries([_entry("only one")])) == ""


# --- cost -------------------------------------------------------------------


def test_clustering_a_full_buffer_stays_within_the_frame_budget() -> None:
    """A deliberately loose ceiling.

    The point is to catch an order-of-magnitude regression, not to measure this
    machine: a tight budget on a shared CI box is a flaky test, and a flaky test
    gets deleted. The measured number goes in the commit message; this only
    fails if clustering has become something other than one pass with a dict.
    """

    entries = [
        _entry(f"connection refused for 10.0.0.{index % 255}:54{index % 100:02d}", second=index % 60)
        for index in range(5_000)
    ]

    normalise.cache_clear()
    start = time.perf_counter()
    stream = cluster_entries(entries)
    cold = time.perf_counter() - start

    start = time.perf_counter()
    cluster_entries(entries)
    warm = time.perf_counter() - start

    assert stream.entries == 5_000
    assert cold < 1.0, f"clustering 5000 unseen lines took {cold:.3f}s"
    # The re-render path — one per keystroke in the query box — is the one that
    # has to stay cheap, and it is the one memoisation exists for.
    assert warm < 0.1, f"re-clustering 5000 known lines took {warm:.3f}s"


def test_shaping_a_line_twice_costs_nothing_the_second_time() -> None:
    """The mechanism behind the warm number above, asserted directly."""

    normalise.cache_clear()
    text = "connection refused for 10.0.0.5:5432 after 1.25s"

    normalise(text)
    before = normalise.cache_info()
    normalise(text)
    after = normalise.cache_info()

    assert after.hits == before.hits + 1
    assert after.misses == before.misses


def test_a_tailed_line_costs_one_add_not_a_recompute() -> None:
    """The guard on the optimisation: work is proportional to what arrived."""

    entries = [_entry(f"connection refused for 10.0.0.{index}", second=index % 60) for index in range(2_000)]
    stream = cluster_entries(entries)

    calls = 0
    original = ClusterStream.add

    def counted(self, entry):
        nonlocal calls
        calls += 1
        return original(self, entry)

    ClusterStream.add = counted
    try:
        stream.add(_entry("connection refused for 10.0.0.9999", second=59))
    finally:
        ClusterStream.add = original

    assert calls == 1


# --- the UI path ------------------------------------------------------------


def _noisy_log(tmp_path: Path, repeats: int = 30) -> Path:
    path = tmp_path / "noisy.log"
    lines = [
        f"2026-08-07 09:25:{index % 60:02d} - ERROR - connection refused for 10.0.0.{index}:54{index % 100:02d}"
        for index in range(repeats)
    ]
    lines.append("2026-08-07 09:26:00 - WARN - disk almost full")
    lines += [
        f"2026-08-07 09:27:{index:02d} - INFO - user {4800 + index} logged in from /home/u{index}"
        for index in range(4)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _open_dialog(app: LogViewerApp, pilot) -> ExportDialog:
    """Open Ctrl+E the way the app does — `push_screen` needs a worker."""

    app.action_export_view()
    await pilot.pause()
    await pilot.pause()
    assert isinstance(app.screen, ExportDialog)
    return app.screen


async def _confirm(pilot, dialog: ExportDialog) -> None:
    dialog.query_one("#confirm-export", Button).press()
    await pilot.pause()
    await pilot.pause()


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


async def _open(pilot, app: LogViewerApp, tmp_path: Path, **kwargs) -> None:
    await pilot.pause()
    app._select_source(_noisy_log(tmp_path, **kwargs), announce=False)
    # Focus off the query input, or `c` is typed into it rather than pressed.
    app.set_focus(app.log_panel)
    await pilot.pause()


def test_c_collapses_repeats_and_says_what_it_did(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            assert len(app.log_panel.rows) == 35

            await pilot.press("c")
            await pilot.pause()

            assert app.state.clustering is True
            assert len(app.log_panel.rows) == 3
            assert "34 lines in 2 clusters" in _status(app)

            await pilot.press("c")
            await pilot.pause()

            assert app.state.clustering is False
            assert len(app.log_panel.rows) == 35

    asyncio.run(scenario())


def test_enter_expands_a_cluster_in_place_and_collapses_it_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            collapsed = len(app.log_panel.rows)

            app.log_panel.move_cursor(0)
            await pilot.press("enter")
            await pilot.pause()

            # The header stays, and its members follow it — in place, in order.
            assert len(app.log_panel.rows) == collapsed + 30
            assert app.log_panel.rows[0].cluster is not None
            members = [row.entry.raw for row in app.log_panel.rows[1:31]]
            assert members == [
                f"2026-08-07 09:25:{index % 60:02d} - ERROR - connection refused for "
                f"10.0.0.{index}:54{index % 100:02d}"
                for index in range(30)
            ]

            app.log_panel.move_cursor(0)
            await pilot.press("enter")
            await pilot.pause()

            assert len(app.log_panel.rows) == collapsed

    asyncio.run(scenario())


def test_a_cluster_row_shows_its_count_and_span(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()

            drawn = app.log_panel.rows[0].renderable.plain

            assert "×30" in drawn
            assert "09:25:00→09:25:29" in drawn
            assert "connection refused" in drawn

    asyncio.run(scenario())


def test_lines_inside_an_expanded_cluster_are_markable(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            app.log_panel.move_cursor(0)
            await pilot.press("enter")
            await pilot.pause()

            app.log_panel.move_cursor(3)
            marked_line = app.log_panel.cursor_entry
            await pilot.press("m")
            await pilot.pause()

            assert len(app._marks) == 1
            assert app._marks.contains(app._origin(marked_line), marked_line)
            assert "1 marked" in _status(app)

    asyncio.run(scenario())


def test_marking_a_collapsed_cluster_asks_you_to_expand_it_first(tmp_path: Path) -> None:
    """One keystroke must not mark thirty lines behind one gutter dot."""

    async def scenario() -> None:
        app = LogViewerApp()
        notices: list[str] = []
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            app.notify = lambda message, **kwargs: notices.append(message)

            app.log_panel.move_cursor(0)
            await pilot.press("m")
            await pilot.pause()

            assert len(app._marks) == 0
            assert "Expand this cluster" in notices[-1]

    asyncio.run(scenario())


def test_a_tailed_line_grows_the_cluster_row_it_belongs_to(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            rows = len(app.log_panel.rows)

            arrived = parse_lines(
                ["2026-08-07 09:25:31 - ERROR - connection refused for 10.0.0.99:5499"]
            )
            app._session.primary.entries.extend(arrived)
            app._append_entries(arrived)
            await pilot.pause()

            # No new row: the line joined the group already on screen.
            assert len(app.log_panel.rows) == rows
            assert "×31" in app.log_panel.rows[0].renderable.plain
            assert app._clusters.entries == 36

    asyncio.run(scenario())


def test_a_stale_row_index_redraws_instead_of_rewriting_a_line(tmp_path: Path) -> None:
    """The pane's row cap moves every index below it.

    `LogView._trim` drops a batch off the front when the cap is exceeded, so an
    index remembered during a render can point at an unrelated line by the time
    a tailed member arrives. Writing through it would silently corrupt that
    line; the guard is to notice and redraw.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            untouched = app.log_panel.rows[-1].renderable.plain

            # Simulate the shift a trim causes: the remembered index now names
            # a different row.
            app._cluster_rows = {index: index + 5 for index in app._cluster_rows}

            arrived = parse_lines(
                ["2026-08-07 09:25:32 - ERROR - connection refused for 10.0.0.98:5498"]
            )
            app._session.primary.entries.extend(arrived)
            app._append_entries(arrived)
            await pilot.pause()

            assert app.log_panel.rows[-1].renderable.plain == untouched
            assert "×31" in app.log_panel.rows[0].renderable.plain

    asyncio.run(scenario())


def test_a_tailed_line_with_a_new_shape_gets_its_own_row(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            rows = len(app.log_panel.rows)

            arrived = parse_lines(["2026-08-07 09:28:00 - ERROR - kernel panic imminent"])
            app._session.primary.entries.extend(arrived)
            app._append_entries(arrived)
            await pilot.pause()

            assert len(app.log_panel.rows) == rows + 1
            assert "kernel panic" in app.log_panel.rows[-1].renderable.plain

    asyncio.run(scenario())


def test_clustering_runs_on_the_filtered_set(tmp_path: Path) -> None:
    """After the filters, and therefore after any plugin FilterStage."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            assert app._clusters.entries == 35

            app._update_state(query="logged in")
            app._render_log()
            await pilot.pause()

            assert app._clusters.entries == 4
            assert app._clusters.clustered == 1

    asyncio.run(scenario())


def test_a_cluster_row_is_never_a_structured_panel(tmp_path: Path) -> None:
    """`o` renders payloads; a bordered panel per repeat group is the noise.

    The *columns* are a different matter and a cluster row does get those —
    see `test_a_cluster_row_gets_the_columns`. What stays forbidden is the
    border and the five rows of pane that come with it.
    """

    async def scenario() -> None:
        path = tmp_path / "json.log"
        path.write_text(
            "\n".join(
                f'{{"ts":"2026-08-07T09:25:{index:02d}Z","level":"error","msg":"boom {index}"}}'
                for index in range(6)
            )
            + "\n",
            encoding="utf-8",
        )
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            app._set_structured(True)
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            row = app.log_panel.rows[0]
            assert row.cluster is not None
            # One row, not a Group wrapping a Panel.
            assert not isinstance(row.renderable, Group)
            assert "×6" in row.renderable.plain

    asyncio.run(scenario())


def test_a_cluster_row_gets_the_columns(tmp_path: Path) -> None:
    """Alignment is what makes forty collapsed groups scannable."""

    async def scenario() -> None:
        path = tmp_path / "json.log"
        path.write_text(
            "\n".join(
                f'{{"ts":"2026-08-07T09:25:{index:02d}Z","level":"error","msg":"boom"}}'
                for index in range(6)
            )
            + "\n",
            encoding="utf-8",
        )
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            app._set_structured(True)
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            plain = app.log_panel.rows[0].renderable.plain
            assert "×6" in plain, "the count still leads the row"
            assert "ERROR" in plain, "the level cell is filled"
            assert "09:25:00" in plain, "the time cell is filled"

    asyncio.run(scenario())


def test_export_offers_clustered_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()

            destination = tmp_path / "out.txt"
            dialog = await _open_dialog(app, pilot)

            dialog.query_one("#export-clustered", Checkbox).value = True
            dialog.query_one("#export-format", OptionList).highlighted = 2  # plain text
            await pilot.pause()
            dialog.query_one("#export-path", Input).value = str(destination)
            await pilot.pause()
            await _confirm(pilot, dialog)

            written = destination.read_text(encoding="utf-8").splitlines()

            # One row per group, and the count is on it.
            assert len(written) == 3
            assert written[0].startswith("×30")
            assert "disk almost full" in written[1]

    asyncio.run(scenario())


def test_an_expanded_export_is_still_the_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()

            destination = tmp_path / "expanded.txt"
            dialog = await _open_dialog(app, pilot)
            dialog.query_one("#export-format", OptionList).highlighted = 2
            await pilot.pause()
            dialog.query_one("#export-path", Input).value = str(destination)
            await pilot.pause()
            await _confirm(pilot, dialog)

            written = destination.read_text(encoding="utf-8").splitlines()

            assert len(written) == 35
            assert not written[0].startswith("×")

    asyncio.run(scenario())


def test_the_drawer_switch_mirrors_the_c_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 44)) as pilot:
            await _open(pilot, app, tmp_path)
            app.advanced_drawer.show()
            await pilot.pause()

            switch = app.query_one("#drawer-clustering", Switch)
            assert switch.value is False

            await pilot.press("c")
            await pilot.pause()
            assert switch.value is True

            switch.value = False
            await pilot.pause()
            assert app.state.clustering is False
            assert len(app.log_panel.rows) == 35

    asyncio.run(scenario())


def test_clustering_persists_but_which_clusters_were_open_does_not(tmp_path: Path) -> None:
    """An expansion refers to log content, and session state holds neither."""

    async def scenario() -> None:
        store = StateStore(root=tmp_path / "cache")
        app = LogViewerApp(store=store)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("c")
            await pilot.pause()
            app.log_panel.move_cursor(0)
            await pilot.press("enter")
            await pilot.pause()
            assert app._expanded_clusters
            app.action_save_session()
            await pilot.pause()

        restored = store.load()
        assert restored.clustering is True
        assert "clustering" in SessionState.PERSISTED_FIELDS
        assert not any("expand" in field or "cluster." in field for field in SessionState.PERSISTED_FIELDS)

        raw = (tmp_path / "cache" / "session.json").read_text(encoding="utf-8")
        assert "connection refused" not in raw

    asyncio.run(scenario())


# --- remote sources ---------------------------------------------------------
#
# Phase 6's parity sweep. `shape_of` already keys on ORIGIN_FIELD, which
# `tag_origins` writes as the host-qualified `format_ref` string — so clustering
# is remote-correct with no change. Pinned rather than reasoned about, because
# the failure it prevents is one an operator could not see: five machines each
# reporting one outage, folded into a single row reading "5 ×", which reads as
# one machine failing five times.


def _remote_lines(*hosts: str):
    """One identical ERROR line, read from each of *hosts* in turn."""

    rows = []
    for host in hosts:
        entries = parse_lines(["2026-08-07 09:25:00 - ERROR - connection refused"])
        rows.extend(tag_origins(entries, [RemoteRef.build(host, "/var/log/syslog")]))
    return rows


def test_the_same_line_on_two_machines_is_two_clusters() -> None:
    rows = _remote_lines("web01", "web02")

    assert shape_of(rows[0]) != shape_of(rows[1])

    stream = cluster_entries(rows, lookback=DEFAULT_LOOKBACK)
    assert stream.clustered == 0
    assert len(stream.rows) == 2


def test_repeats_on_one_machine_still_fold_together() -> None:
    """The other direction, so the test above is a scoping assertion rather
    than an assertion that clustering has stopped working."""

    rows = _remote_lines("web01", "web01", "web01")

    assert len({shape_of(row) for row in rows}) == 1

    stream = cluster_entries(rows, lookback=DEFAULT_LOOKBACK)
    assert len(stream.rows) == 1
    assert isinstance(stream.rows[0], Cluster)
    assert stream.rows[0].count == 3


def test_a_remote_shape_is_distinct_from_the_local_file_of_that_name() -> None:
    local = tag_origins(
        parse_lines(["2026-08-07 09:25:00 - ERROR - connection refused"]),
        [Path("/var/log/syslog")],
    )
    remote = _remote_lines("web01")

    assert shape_of(local[0]) != shape_of(remote[0])
