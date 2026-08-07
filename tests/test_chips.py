"""Active-filter chips: what they show and what dismissing one reverts."""

from __future__ import annotations

import asyncio

from clv.app import LogViewerApp
from clv.widgets.filter_chip import FilterChip


def _labels(app) -> list[str]:
    return [chip.label_text for chip in app.chip_bar.query(FilterChip)]


def _keys(app) -> list[str]:
    return [chip.key for chip in app.chip_bar.query(FilterChip)]


def test_chips_reflect_active_filters_only() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()

            # Nothing active: the bar hides itself entirely.
            app._update_state(query="", severity="all", time_window="all")
            await pilot.pause()
            assert _keys(app) == []
            assert app.chip_bar.has_class("-empty")

            app._update_state(query="boom", severity="error", time_window="1h")
            await pilot.pause()

            assert _keys(app) == ["query", "severity", "time"]
            assert _labels(app) == ["Query: boom", "Severity: Error", "Time: 1h"]
            assert not app.chip_bar.has_class("-empty")

    asyncio.run(scenario())


def test_custom_range_chip_shows_both_bounds() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._update_state(
                time_window="range",
                custom_start="2026-08-07 09:00",
                custom_end="2026-08-07 10:00",
            )
            await pilot.pause()

            assert _labels(app) == ["Time: 2026-08-07 09:00 → 2026-08-07 10:00"]

    asyncio.run(scenario())


def test_long_queries_are_elided_in_the_chip() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._update_state(query="x" * 100)
            await pilot.pause()

            label = _labels(app)[0]
            assert label.endswith("…")
            assert len(label) < 60

    asyncio.run(scenario())


def test_dismissing_a_chip_reverts_its_filter() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._update_state(query="boom", severity="error", time_window="6h")
            await pilot.pause()

            for chip in list(app.chip_bar.query(FilterChip)):
                chip.post_message(FilterChip.Dismissed(chip.key))
            await pilot.pause()
            await pilot.pause()

            assert app.state.query == ""
            assert app.state.severity == "all"
            assert app.state.time_window == "all"
            # The controls follow the state, not just the chips.
            assert app.query_bar.severity_segmented.value == "all"
            assert app.query_bar.time_selection == "all"
            assert app.query_bar.get_query_value() == ""

    asyncio.run(scenario())


def test_advanced_settings_surface_as_chips() -> None:
    """Filters buried in the drawer are still visible on the chip bar."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Switch

            app.advanced_drawer.query_one("#invert-match", Switch).value = True
            await pilot.pause()

            assert "invert" in _keys(app)

            for chip in list(app.chip_bar.query(FilterChip)):
                if chip.key == "invert":
                    chip.post_message(FilterChip.Dismissed(chip.key))
            await pilot.pause()
            await pilot.pause()

            assert app.advanced_drawer.settings.invert_match is False

    asyncio.run(scenario())
