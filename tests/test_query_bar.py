"""QueryBar interaction, exercised headlessly through a minimal host app."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button

from clv.widgets.query_bar import QueryBar


class _Harness(App[None]):
    """Minimal Textual app so QueryBar can be driven without the full viewer."""

    def __init__(self) -> None:
        super().__init__()
        self.custom_requests = 0
        self.time_changes: list[str] = []
        self.severity_changes: list[str] = []
        self.actions: list[str] = []

    def compose(self) -> ComposeResult:
        self.query_bar = QueryBar()
        yield self.query_bar

    def on_query_bar_custom_range_requested(self, _m: QueryBar.CustomRangeRequested) -> None:
        self.custom_requests += 1

    def on_query_bar_time_window_changed(self, m: QueryBar.TimeWindowChanged) -> None:
        self.time_changes.append(m.value)

    def on_query_bar_severity_changed(self, m: QueryBar.SeverityChanged) -> None:
        self.severity_changes.append(m.value)

    def on_query_bar_action_triggered(self, m: QueryBar.ActionTriggered) -> None:
        self.actions.append(m.action_id)


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_selecting_a_time_preset_emits_the_change() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#time-segments .segment")  # "All" is already active
            await pilot.pause()

            app.query_bar.select_time("1h", emit=True)
            await pilot.pause()

            assert app.query_bar.time_selection == "1h"
            assert "1h" in app.time_changes

    _run(scenario)


def test_custom_preset_asks_for_a_dialog_instead_of_committing() -> None:
    """'Custom' has no window until the dialog supplies one."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app.query_bar.time_segmented._activate("range")
            await pilot.pause()

            assert app.custom_requests == 1
            assert "range" not in app.time_changes

            # Applying a range does emit, and lights only Custom.
            app.query_bar.apply_custom_time_range("2026-01-01 00:00", "2026-01-01 12:00")
            await pilot.pause()

            assert app.query_bar.time_selection == "range"
            assert app.query_bar.time_segmented.value == "range"
            assert app.time_changes[-1] == "range"

    _run(scenario)


def test_reselecting_custom_reopens_the_dialog() -> None:
    """Adjusting an active range must not require clearing it first."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app.query_bar.apply_custom_time_range("2026-01-01 00:00", "2026-01-01 12:00")
            await pilot.pause()

            app.query_bar.time_segmented._activate("range")
            await pilot.pause()

            assert app.custom_requests == 1
            assert app.query_bar.time_segmented.value == "range"

    _run(scenario)


def test_cycling_time_presets_skips_custom() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            seen = [app.query_bar.cycle_time_preset() for _ in range(6)]
            await pilot.pause()

            assert "range" not in seen
            assert seen[:5] == ["15m", "1h", "6h", "24h", "all"]

    _run(scenario)


def test_severity_arrow_navigation_needs_confirmation() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            segments = app.query_bar.severity_segmented
            segments._segments["all"].focus()
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()
            # Focus moved but the selection has not committed yet.
            assert segments.value == "all"
            assert app.screen.focused is segments._segments["debug"]

            await pilot.press("enter")
            await pilot.pause()
            assert segments.value == "debug"
            assert app.severity_changes[-1] == "debug"

    _run(scenario)


def test_regex_validation_reports_hits_and_errors() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            bar = app.query_bar

            bar.set_query_value("error")
            bar.validate_regex(["an error here", "clean line", "ERROR again"])
            await pilot.pause()
            # Smart case: lowercase query matches both spellings.
            assert bar.regex_status.valid is True
            assert bar.regex_status.matches == 2

            bar.set_query_value("(unclosed")
            bar.validate_regex(["anything"])
            await pilot.pause()
            assert bar.regex_status.valid is False
            assert bar.query_one("#query-input").has_class("-regex-invalid")

    _run(scenario)


def test_action_buttons_emit_their_ids() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            for button_id in ("add-source", "run-query", "clear-query", "save-session"):
                app.query_bar.query_one(f"#{button_id}", Button).press()
                await pilot.pause()

            assert app.actions == ["add-source", "run-query", "clear-query", "save-session"]

    _run(scenario)


def test_every_control_stays_on_screen_at_eighty_columns() -> None:
    """The regression that hid Run/Clear/Save on anything under ~200 columns."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_bar.set_class(True, "-compact")
            await pilot.pause()

            for button_id in ("toggle-advanced", "add-source", "run-query", "clear-query", "save-session"):
                button = app.query_bar.query_one(f"#{button_id}", Button)
                region = button.region
                assert region.width > 0, f"{button_id} has no width"
                assert region.right <= 80, f"{button_id} extends past the right edge"

    _run(scenario)
