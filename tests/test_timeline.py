"""The severity timeline: bucketing, colouring, and the bar as a control.

The service half is pure arithmetic and is tested without a screen. The widget
half is tested through the whole app, because the thing worth asserting is that
selecting a bucket produces the same filtered view an operator would get by
typing that range into the custom range dialog.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from textual.widgets import Switch

from clv.app import LogViewerApp
from clv.services.parsing import LogEntry, parse_lines
from clv.services.timeline import (
    build_timeline,
    describe_bucket,
    describe_undated,
)
from clv.storage import SessionState, StateStore
from clv.widgets.filter_chip import FilterChip
from clv.widgets.timeline import BLOCKS, EMPTY_GLYPH

BASE = datetime(2026, 8, 7, 9, 0, 0)


def _entries(count: int, *, step: int = 1, level: str = "INFO", start: int = 0) -> list[LogEntry]:
    return [
        LogEntry(
            raw=f"line {index}",
            timestamp=BASE + timedelta(seconds=(start + index) * step),
            level=level,
        )
        for index in range(count)
    ]


# --- bucketing --------------------------------------------------------------


def test_the_bar_never_asks_for_more_columns_than_it_has() -> None:
    """The bucket count *is* the width; one column of overflow is off screen."""

    for width in (1, 8, 40, 76, 200):
        for span in (1, 5, 60, 599, 3600, 86_400):
            entries = [
                LogEntry(raw="a", timestamp=BASE),
                LogEntry(raw="b", timestamp=BASE + timedelta(seconds=span)),
            ]
            timeline = build_timeline(entries, width=width)
            assert 1 <= len(timeline.buckets) <= width, (width, span)


def test_every_entry_lands_in_exactly_one_bucket() -> None:
    entries = _entries(600, step=1)

    timeline = build_timeline(entries, width=70)

    assert timeline.total == len(entries)
    assert timeline.undated == 0
    assert sum(bucket.count for bucket in timeline.buckets) == 600


def test_buckets_tile_the_span_without_gaps() -> None:
    timeline = build_timeline(_entries(200, step=3), width=50)

    for left, right in zip(timeline.buckets, timeline.buckets[1:]):
        assert left.end == right.start
    assert timeline.buckets[0].start == BASE
    assert timeline.buckets[-1].end > BASE + timedelta(seconds=199 * 3)


def test_a_bucket_takes_the_highest_severity_it_contains() -> None:
    """Not the last, not the most common — the worst. That is what a spike is."""

    entries = [
        LogEntry(raw="a", timestamp=BASE, level="INFO"),
        LogEntry(raw="b", timestamp=BASE + timedelta(seconds=1), level="ERROR"),
        LogEntry(raw="c", timestamp=BASE + timedelta(seconds=2), level="DEBUG"),
        LogEntry(raw="d", timestamp=BASE + timedelta(seconds=400), level="WARN"),
    ]

    timeline = build_timeline(entries, width=4)

    assert timeline.buckets[0].level == "ERROR"
    assert timeline.buckets[-1].level == "WARN"


def test_a_bucket_of_lines_with_no_level_reports_none_rather_than_trace() -> None:
    timeline = build_timeline([LogEntry(raw="a", timestamp=BASE)], width=10)

    assert timeline.buckets[0].level is None


def test_undated_entries_are_counted_and_never_bucketed() -> None:
    """The 'never silently lose a line' rule, applied to a time axis."""

    entries = _entries(10) + [LogEntry(raw="stack trace line"), LogEntry(raw="another")]

    timeline = build_timeline(entries, width=20)

    assert timeline.undated == 2
    assert timeline.total == 10


def test_a_source_with_no_timestamps_explains_itself() -> None:
    timeline = build_timeline([LogEntry(raw="a"), LogEntry(raw="b")], width=40)

    assert timeline.buckets == ()
    assert "no detected timestamp" in describe_undated(timeline)
    assert "2 line(s)" in describe_undated(timeline)


def test_mixed_aware_and_naive_stamps_bucket_together() -> None:
    """The same rule the k-way merge uses: drop the offsets rather than refuse."""

    entries = [
        LogEntry(raw="naive", timestamp=BASE),
        LogEntry(raw="aware", timestamp=(BASE + timedelta(seconds=30)).replace(tzinfo=timezone.utc)),
    ]

    timeline = build_timeline(entries, width=30)

    assert timeline.total == 2
    assert timeline.naive is True


def test_an_entirely_aware_source_keeps_its_offsets() -> None:
    east = timezone(timedelta(hours=2))
    entries = [
        LogEntry(raw="a", timestamp=BASE.replace(tzinfo=timezone.utc)),
        LogEntry(raw="b", timestamp=(BASE + timedelta(hours=2, seconds=30)).replace(tzinfo=east)),
    ]

    timeline = build_timeline(entries, width=30)

    # 09:00Z and 11:00:30+02:00 are half a minute apart, not two hours.
    assert timeline.naive is False
    assert timeline.buckets[-1].end - timeline.buckets[0].start <= timedelta(minutes=1)


def test_window_for_covers_the_bucket_it_names() -> None:
    timeline = build_timeline(_entries(120, step=5), width=40)

    window = timeline.window_for(3)

    assert window is not None
    assert window.start == timeline.buckets[3].start
    assert window.end == timeline.buckets[3].end
    assert window.contains(timeline.buckets[3].start)
    assert timeline.window_for(len(timeline.buckets)) is None
    assert timeline.window_for(-1) is None


def test_describe_bucket_names_the_range_the_count_and_the_level() -> None:
    timeline = build_timeline(_entries(60, step=10, level="ERROR"), width=30)

    caption = describe_bucket(timeline, 0)

    assert "2026-08-07 09:00:00" in caption
    assert "events" in caption
    assert "ERROR" in caption


# --- the incremental path ---------------------------------------------------


def test_extending_matches_a_full_rebuild() -> None:
    """The correctness guard on the optimisation Item 14 asks for.

    The arrivals here land inside the last bucket, which is the tail case: at
    two polls a second the newest line is almost always in the bucket the one
    before it opened. An arrival past the grid is the other branch, below.
    """

    seed = _entries(300, step=2)
    # 591–599s, inside the final bucket of the grid seed produces.
    arrived = [
        LogEntry(raw=f"late {index}", timestamp=BASE + timedelta(seconds=591 + index * 2), level="ERROR")
        for index in range(5)
    ]

    incremental = build_timeline(seed, width=60).extend(arrived)
    full = build_timeline(seed + arrived, width=60)

    assert incremental is not None
    assert [bucket.count for bucket in incremental.buckets] == [
        bucket.count for bucket in full.buckets
    ]
    assert [bucket.level for bucket in incremental.buckets] == [
        bucket.level for bucket in full.buckets
    ]
    assert incremental.total == full.total


def test_extending_past_the_grid_asks_for_a_rebuild_instead_of_guessing() -> None:
    timeline = build_timeline(_entries(100), width=40)

    assert timeline.extend([LogEntry(raw="later", timestamp=BASE + timedelta(days=1))]) is None
    assert timeline.extend([LogEntry(raw="earlier", timestamp=BASE - timedelta(days=1))]) is None


def test_extending_counts_undated_arrivals_without_a_rebuild() -> None:
    timeline = build_timeline(_entries(100), width=40)

    extended = timeline.extend([LogEntry(raw="continuation")])

    assert extended is not None
    assert extended.undated == 1
    assert extended.total == timeline.total


def test_extending_an_empty_timeline_asks_for_a_rebuild() -> None:
    """No grid to fold into: the first timestamped line changes everything."""

    timeline = build_timeline([LogEntry(raw="a")], width=40)

    assert timeline.extend([LogEntry(raw="b", timestamp=BASE)]) is None


# --- the widget and the app -------------------------------------------------


def _log_file(tmp_path: Path, count: int = 120, step: int = 5) -> Path:
    path = tmp_path / "app.log"
    lines = []
    for index in range(count):
        stamp = (BASE + timedelta(seconds=index * step)).strftime("%Y-%m-%d %H:%M:%S")
        level = "ERROR" if index % 30 == 0 else "INFO"
        lines.append(f"{stamp} - {level} - request {index} handled")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _painted(app: LogViewerApp) -> list[str]:
    strips = app.screen._compositor.render_strips()
    return ["".join(segment.text for segment in strip) for strip in strips]


def _bar_rows(app: LogViewerApp) -> tuple[str, str]:
    region = app.timeline_bar.region
    painted = _painted(app)
    left, right = region.x, region.x + region.width
    return painted[region.y][left:right], painted[region.y + 1][left:right]


async def _open(pilot, app: LogViewerApp, tmp_path: Path, **kwargs) -> None:
    await pilot.pause()
    app._select_source(_log_file(tmp_path, **kwargs), announce=False)
    # Focus off the query input, or `b` is typed into it rather than pressed.
    app.set_focus(app.log_panel)
    await pilot.pause()


def test_b_shows_the_bar_and_fills_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            assert not app.timeline_bar.has_class("-visible")

            await pilot.press("b")
            await pilot.pause()

            assert app.timeline_bar.has_class("-visible")
            assert app.state.timeline is True
            # Bucketed to the width the bar actually got, never past it — a
            # column that does not exist is a column nobody can see or click.
            assert 0 < len(app._timeline.buckets) <= app.timeline_bar.size.width
            assert app.focused is app.timeline_bar

            bar, _caption = _bar_rows(app)
            assert any(glyph in bar for glyph in BLOCKS)

            await pilot.press("b")
            await pilot.pause()
            assert not app.timeline_bar.has_class("-visible")
            assert app.state.timeline is False

    asyncio.run(scenario())


def test_selecting_a_bucket_sets_the_time_window(tmp_path: Path) -> None:
    """The histogram is a control, not decoration."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.pause()

            await pilot.press("home", "right", "right")
            await pilot.pause()
            index = app.timeline_bar.selected
            bucket = app._timeline.buckets[index]

            await pilot.press("enter")
            await pilot.pause()

            assert app.state.time_window == "range"
            assert app.state.custom_start == bucket.start.isoformat(sep=" ", timespec="seconds")
            assert app.state.custom_end == bucket.end.isoformat(sep=" ", timespec="seconds")
            # It shows up as an ordinary Time chip, so it is dismissed the same
            # way every other filter is.
            chips = [chip.label_text for chip in app.chip_bar.query(FilterChip)]
            assert any("Time:" in label for label in chips)

            window = app._time_window()
            assert window.start == bucket.start
            assert window.end == bucket.end

    asyncio.run(scenario())


def test_the_caption_names_the_selected_bucket(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.pause()
            await pilot.press("home")
            await pilot.pause()

            _bar, caption = _bar_rows(app)

            assert "2026-08-07 09:00:00" in caption
            assert "event" in caption

    asyncio.run(scenario())


def test_the_bar_reflects_the_filtered_set_not_the_buffer(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.pause()
            buffered = app._timeline.total
            assert buffered == 120

            app.query_bar.set_query_value("ERROR")
            app._update_state(query="ERROR")
            app._render_log()
            await pilot.pause()

            assert app._timeline.total == 4
            assert app._timeline.total < buffered

    asyncio.run(scenario())


def test_a_source_with_no_timestamps_says_so_instead_of_drawing_a_bar(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "plain.log"
        path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()

            bar, caption = _bar_rows(app)

            assert app._timeline.buckets == ()
            assert set(bar.strip()) <= {""}
            assert "No timeline" in caption
            assert "no detected timestamp" in caption

    asyncio.run(scenario())


def test_the_bar_and_the_log_are_both_on_screen_at_80_columns(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            # At -compact the viewer replaces the tree; opening a source already
            # switches to it, which is why there is no ctrl+b here.
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.pause()

            bar = app.timeline_bar.region
            log = app.log_panel.region

            assert bar.width > 0 and bar.height == 2
            assert bar.right <= 80
            assert bar.bottom <= log.y
            assert log.height > 0
            assert log.bottom <= 24

    asyncio.run(scenario())


def test_tailing_folds_new_lines_into_the_existing_grid(tmp_path: Path) -> None:
    """The incremental path, driven through the app rather than the service."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            path = _log_file(tmp_path, count=120)
            await pilot.pause()
            app._select_source(path, announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            before = app._timeline
            assert before.total == 120

            # Inside the grid the seed produced, which is the tail case: a line
            # arriving now belongs to the bucket the line before it opened.
            inside = before.buckets[-1].start + timedelta(seconds=1)
            arrived = parse_lines([f"{inside:%Y-%m-%d %H:%M:%S} - ERROR - late line"])
            app._session.primary.entries.extend(arrived)
            app._append_entries(arrived)
            await pilot.pause()

            assert app._timeline.total == 121
            # Same grid, folded into — not a new one built from the buffer.
            assert app._timeline.origin == before.origin
            assert app._timeline.step == before.step
            assert app._timeline.buckets[-1].level == "ERROR"

            # Past the grid, the other branch: rebuilt rather than guessed at.
            outside = before.buckets[-1].end + timedelta(hours=1)
            later = parse_lines([f"{outside:%Y-%m-%d %H:%M:%S} - INFO - much later"])
            app._session.primary.entries.extend(later)
            app._append_entries(later)
            await pilot.pause()

            assert app._timeline.total == 122
            assert app._timeline.step > before.step

    asyncio.run(scenario())


def test_an_empty_bucket_is_drawn_as_a_tick_rather_than_a_gap(tmp_path: Path) -> None:
    """A blank column reads as the end of the data; the axis has to stay visible."""

    async def scenario() -> None:
        path = tmp_path / "sparse.log"
        early = [f"{(BASE + timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S')} - INFO - a" for i in range(3)]
        late = [
            f"{(BASE + timedelta(hours=1) + timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S')} - INFO - b"
            for i in range(3)
        ]
        path.write_text("\n".join(early + late) + "\n", encoding="utf-8")

        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()

            bar, _caption = _bar_rows(app)

            assert EMPTY_GLYPH in bar
            assert any(glyph in bar for glyph in BLOCKS)

    asyncio.run(scenario())


def test_the_drawer_switch_mirrors_the_b_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot, app, tmp_path)
            app.advanced_drawer.show()
            await pilot.pause()

            switch = app.query_one("#drawer-timeline", Switch)
            assert switch.value is False

            await pilot.press("b")
            await pilot.pause()
            assert switch.value is True, "the key must push its state back to the switch"

            switch.value = False
            await pilot.pause()
            assert app.state.timeline is False
            assert not app.timeline_bar.has_class("-visible")

    asyncio.run(scenario())


def test_the_drawer_still_paints_source_discovery_with_the_new_switch() -> None:
    """The regression the fifth toggle had to avoid: max-height 16 and a fold.

    Adding a *row* to "Output & panes" pushes "Source discovery" below it,
    where it lays out correctly and paints nothing.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            await pilot.pause()
            app.advanced_drawer.show()
            await pilot.pause()
            await pilot.pause()

            painted = "\n".join(_painted(app))

            assert "Timeline" in painted
            assert "Source discovery" in painted

    asyncio.run(scenario())


def test_whether_the_bar_is_open_survives_a_restart(tmp_path: Path) -> None:
    """A preference, so it persists. The *selected bucket* deliberately does not."""

    async def scenario() -> None:
        store = StateStore(root=tmp_path / "cache")
        app = LogViewerApp(store=store)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            await pilot.press("b")
            await pilot.pause()
            app.action_save_session()
            await pilot.pause()

        restored = store.load()
        assert restored.timeline is True
        assert "timeline" in SessionState.PERSISTED_FIELDS
        assert not any("bucket" in field for field in SessionState.PERSISTED_FIELDS)

    asyncio.run(scenario())


def test_a_hidden_bar_costs_nothing_to_maintain(tmp_path: Path) -> None:
    """No histogram is built while nobody is looking at one."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(pilot, app, tmp_path)

            assert app._timeline.buckets == ()
            assert app.timeline_bar.timeline.buckets == ()

    asyncio.run(scenario())
