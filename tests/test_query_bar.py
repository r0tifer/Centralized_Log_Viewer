import asyncio
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Button

sys.path.append(str(Path(__file__).resolve().parents[1]))

from clv.widgets.query_bar import QueryBar


class _QueryBarHarness(App[None]):
    """Minimal Textual app so we can exercise QueryBar headlessly."""

    def __init__(self) -> None:
        super().__init__()
        self.custom_requests = 0

    def compose(self) -> ComposeResult:
        self.query_bar = QueryBar()
        yield self.query_bar

    def on_query_bar_custom_range_requested(self, message: QueryBar.CustomRangeRequested) -> None:
        self.custom_requests += 1


def test_custom_range_selection_deactivates_other_presets() -> None:
    """Applying a custom range should only leave the Custom indicator lit."""

    async def _exercise() -> None:
        app = _QueryBarHarness()
        async with app.run_test() as pilot:
            qb = app.query_bar
            await pilot.pause()
            await pilot.click("#time-range")
            await pilot.pause()
            assert app.custom_requests == 1
            qb.apply_custom_time_range("2024-01-01 00:00", "2024-01-01 12:00", emit=False)
            await pilot.pause()
            states = {name: button.value for name, button in qb._time_buttons.items()}
            assert states["range"] is True
            assert all(not active for name, active in states.items() if name != "range")

            # Clicking Custom again should reopen the dialog while keeping the Custom
            # indicator active (no need to clear or reselect presets first).
            await pilot.click("#time-range")
            await pilot.pause()
            assert app.custom_requests == 2
            states_after_reclick = {name: button.value for name, button in qb._time_buttons.items()}
            assert states_after_reclick["range"] is True
            assert all(
                not active for name, active in states_after_reclick.items() if name != "range"
            )

    asyncio.run(_exercise())


def test_severity_segments_arrow_navigation() -> None:
    async def _exercise() -> None:
        app = _QueryBarHarness()
        async with app.run_test() as pilot:
            qb = app.query_bar
            await pilot.pause()
            all_segment = qb.severity_segmented._segments["all"]
            all_segment.focus()
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()
            assert qb.severity_segmented.value == "all"
            assert qb.screen.focused is qb.severity_segmented._segments["info"]

            # Selection should update only when Enter/Space is pressed.
            await pilot.press("enter")
            await pilot.pause()
            assert qb.severity_segmented.value == "info"

            await pilot.press("left")
            await pilot.pause()
            assert qb.severity_segmented.value == "info"
            assert qb.screen.focused is qb.severity_segmented._segments["all"]

            await pilot.press("space")
            await pilot.pause()
            assert qb.severity_segmented.value == "all"

    asyncio.run(_exercise())


def test_time_presets_require_confirmation() -> None:
    async def _exercise() -> None:
        app = _QueryBarHarness()
        async with app.run_test() as pilot:
            qb = app.query_bar
            await pilot.pause()
            qb.time_set.focus()
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()
            assert qb._time_selection == "all"
            assert qb._time_focus_value == "15m"

            await pilot.press("enter")
            await pilot.pause()
            assert qb._time_selection == "15m"

            await pilot.press("left")
            await pilot.pause()
            assert qb._time_selection == "15m"
            assert qb._time_focus_value == "all"

            await pilot.press("space")
            await pilot.pause()
            assert qb._time_selection == "all"

    asyncio.run(_exercise())


def test_action_buttons_arrow_navigation() -> None:
    async def _exercise() -> None:
        app = _QueryBarHarness()
        async with app.run_test() as pilot:
            qb = app.query_bar
            await pilot.pause()
            advanced_button = qb.query_one("#toggle-advanced", Button)
            advanced_button.focus()
            await pilot.pause()

            expected_order = ["add-source", "run-query", "clear-query", "save-session"]
            for expected_id in expected_order:
                await pilot.press("right")
                await pilot.pause()
                assert qb.screen.focused is not None
                assert qb.screen.focused.id == expected_id

            # Reverse direction back to Advanced Filters
            for expected_id in ["clear-query", "run-query", "add-source", "toggle-advanced"]:
                await pilot.press("left")
                await pilot.pause()
                assert qb.screen.focused is not None
                assert qb.screen.focused.id == expected_id

    asyncio.run(_exercise())
