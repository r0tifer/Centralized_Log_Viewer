"""QueryBar interaction, exercised headlessly through a minimal host app."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, Input

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


def test_query_input_absorbs_the_free_space() -> None:
    """The sized controls take what they need; the input gets the remainder."""

    async def scenario() -> None:
        widths = {}
        severities = {}
        for terminal in (100, 140, 200):
            app = _Harness()
            async with app.run_test(size=(terminal, 24)) as pilot:
                await pilot.pause()
                widths[terminal] = app.query_bar.query_one("#query-input", Input).region.width
                severities[terminal] = app.query_bar.query_one("#severity-field").region.width

        # The input grows roughly one-for-one with the terminal...
        assert widths[140] - widths[100] == 40
        assert widths[200] - widths[140] == 60
        # ...while severity keeps hugging its segments.
        assert len(set(severities.values())) == 1

    _run(scenario)


def test_breathing_room_between_the_input_and_the_severity_pills() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(140, 24)) as pilot:
            await pilot.pause()
            query_input = app.query_bar.query_one("#query-input", Input).region
            first_pill = app.query_bar.severity_segmented._segments["all"].region

            gap = first_pill.x - query_input.right
            assert gap >= 2, "pills are butted against the input"
            # The counter lives in that space, so it is not simply padding.
            counter = app.query_bar.query_one("#match-count").region
            assert query_input.right <= counter.x < first_pill.x

    _run(scenario)


def test_hit_counter_aligns_with_the_input_text() -> None:
    """The counter is a bare Static in the row, so it needs its own offset."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(140, 24)) as pilot:
            await pilot.pause()
            app.query_bar.set_query_value("error")
            app.query_bar.validate_regex(["an error", "ERROR too", "clean"])
            await pilot.pause()

            assert app.query_bar.regex_status.matches == 2  # smart case
            counter = app.query_bar.query_one("#match-count")
            query_input = app.query_bar.query_one("#query-input", Input)
            # Same text row as the input's content, not its top border.
            assert counter.content_region.y == query_input.content_region.y

    _run(scenario)


def test_time_toggles_and_actions_merge_onto_one_row_when_wide() -> None:
    """Presets left, toggles centred, actions right — all on a single line."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(190, 30)) as pilot:
            await pilot.pause()
            bar = app.query_bar
            bar.set_class(True, "-merged")
            await pilot.pause()

            # Compare the three containers, not their inner controls: a
            # LabeledField's own region starts at its label row, one line above
            # the control inside it.
            presets = bar.query_one("#time-field").region
            toggles = bar.query_one("#toggles").region
            actions = bar.query_one("#actions").region

            # One row: same vertical band.
            assert presets.y == toggles.y == actions.y

            # Left / middle / right ordering with real gaps between them.
            assert presets.right < toggles.x
            assert toggles.right < actions.x

            # The toggles sit between the two, not hard against either side.
            left_gap = toggles.x - presets.right
            right_gap = actions.x - toggles.right
            assert left_gap > 4 and right_gap > 4

    _run(scenario)


def test_merged_row_aligns_presets_switches_and_buttons() -> None:
    """The action buttons have no label row, so they need the offset."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(190, 30)) as pilot:
            await pilot.pause()
            bar = app.query_bar
            bar.set_class(True, "-merged")
            await pilot.pause()

            preset = bar.time_segmented._segments["all"].region
            switch = bar.query_one("#auto-scroll-toggle").region
            button = bar.query_one("#run-query", Button).region

            assert preset.y == switch.y == button.y

    _run(scenario)


def test_unmerged_layout_stacks_and_hides_the_toggles() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.query_bar
            bar.set_class(False, "-merged")
            await pilot.pause()

            presets = bar.time_segmented.region
            actions = bar.query_one("#actions").region

            assert not bar.query_one("#toggles").display
            assert actions.y > presets.y, "actions should stack below the presets"

    _run(scenario)


def test_merging_saves_vertical_space() -> None:
    async def scenario() -> None:
        heights = {}
        for merged in (False, True):
            app = _Harness()
            async with app.run_test(size=(190, 30)) as pilot:
                await pilot.pause()
                app.query_bar.set_class(merged, "-merged")
                await pilot.pause()
                heights[merged] = app.query_bar.region.height

        assert heights[True] < heights[False]

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


# --- app-level: the merge breakpoint and the keyboard fallbacks -------------


def test_merge_breakpoint_matches_what_the_row_actually_needs() -> None:
    """BREAKPOINT_MERGE is a measured minimum, not a guess.

    One column below it the merged row would overflow, so the layout must
    still be stacked; at the breakpoint everything fits on one line.
    """

    async def scenario() -> None:
        from clv.app import BREAKPOINT_MERGE, LogViewerApp

        for width, expect_merged in ((BREAKPOINT_MERGE - 1, False), (BREAKPOINT_MERGE, True)):
            app = LogViewerApp()
            async with app.run_test(size=(width, 32)) as pilot:
                await pilot.pause()
                await pilot.pause()
                bar = app.query_bar
                assert bar.has_class("-merged") is expect_merged, width

                presets = bar.query_one("#time-field").region
                actions = bar.query_one("#actions").region
                on_one_row = presets.y == actions.y
                assert on_one_row is expect_merged, width

                # Whatever the layout, nothing may leave the screen.
                for button_id in (
                    "toggle-advanced",
                    "add-source",
                    "run-query",
                    "clear-query",
                    "save-session",
                ):
                    region = bar.query_one(f"#{button_id}", Button).region
                    assert region.width > 0 and region.right <= width, (button_id, width)

    _run(scenario)


def test_hidden_switches_remain_reachable_from_the_keyboard() -> None:
    """The switches are only rendered in the merged layout.

    Before these bindings existed they were unreachable below the merge width:
    absent from the Advanced drawer and bound to no key, so auto-scroll and
    structured output simply could not be changed on a narrow terminal.

    Focus is moved off the query input first: single-letter bindings are
    swallowed by a focused Input, which is true of the existing a/t/s/f
    bindings too.
    """

    async def scenario() -> None:
        from clv.app import LogViewerApp

        app = LogViewerApp()
        async with app.run_test(size=(80, 32)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert not app.query_bar.query_one("#toggles").display

            app.set_focus(app.log_panel)
            await pilot.pause()

            auto_before = app.state.auto_scroll
            pretty_before = app.state.pretty_rendering

            await pilot.press("w")
            await pilot.pause()
            assert app.state.auto_scroll is not auto_before

            await pilot.press("o")
            await pilot.pause()
            assert app.state.pretty_rendering is not pretty_before

            # And back again.
            await pilot.press("w")
            await pilot.pause()
            assert app.state.auto_scroll is auto_before

    _run(scenario)
