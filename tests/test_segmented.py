"""SegmentedButtons: behaviour, and the visual contract its styling must meet.

The fill on these segments went effectively invisible once before. The CSS
still said `background: $surface 14%`, but $surface is #1E1E1E against a
#121212 background, so the blend landed on #131313 -- one RGB step from the
background. The group also carried a `border: round` at height 3, leaving a
single interior row, which clipped each segment's fill to its middle line.

Neither failure shows up in a character-level snapshot, so these tests assert
on painted colour and on geometry instead.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from clv.widgets.segmented import SegmentedButtons

OPTIONS = [("all", "All"), ("debug", "Debug"), ("info", "Info"), ("error", "Error")]

#: Minimum contrast between a resting segment and the app background. Low
#: enough to allow a subtle surface, high enough to exclude "invisible":
#: the regression this guards against measured 1.01:1.
MIN_FILL_CONTRAST = 1.4


def _relative_luminance(color) -> float:
    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b)


def _contrast(first, second) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.changes: list[str] = []

    def compose(self) -> ComposeResult:
        self.group = SegmentedButtons(OPTIONS, id="group")
        yield self.group

    def on_segmented_buttons_value_changed(self, event: SegmentedButtons.ValueChanged) -> None:
        self.changes.append(event.value)


def _painted_rows(app: _Harness, segment) -> list:
    """The background colour actually painted on each row of a segment."""
    strips = app.screen._compositor.render_strips()
    region = segment.region
    rows = []
    for y in range(region.y, region.y + region.height):
        if y >= len(strips):
            continue
        x = 0
        for strip_segment in strips[y]:
            if x <= region.x + 1 < x + len(strip_segment.text):
                rows.append(strip_segment.style.bgcolor)
                break
            x += len(strip_segment.text)
    return rows


def _run(scenario) -> None:
    asyncio.run(scenario())


# --- visual contract --------------------------------------------------------


def test_resting_segments_are_visibly_filled() -> None:
    """The regression: fill blended to within one RGB step of the background."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            background = app.screen.background_colors[1]

            for value in ("debug", "info", "error"):  # non-active segments
                fill = app.group._segments[value].background_colors[1]
                contrast = _contrast(fill, background)
                assert contrast >= MIN_FILL_CONTRAST, (
                    f"segment {value!r} fill {fill} is only {contrast:.2f}:1 "
                    f"against the background {background} — effectively invisible"
                )

    _run(scenario)


def test_fill_covers_the_whole_segment_not_just_one_row() -> None:
    """A border on the height-3 group previously clipped the fill to one row."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            segment = app.group._segments["debug"]
            rows = _painted_rows(app, segment)

            assert segment.region.height == 3
            assert len(rows) == 3
            assert len(set(map(str, rows))) == 1, f"fill is not uniform: {rows}"

    _run(scenario)


def test_active_segment_is_distinguishable_from_resting_ones() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            active = app.group._segments["all"].background_colors[1]
            resting = app.group._segments["debug"].background_colors[1]

            assert active != resting
            # Distinguishable by hue, not brightness alone: the active fill is
            # warm and the resting fill cool, so the selection reads even where
            # a brightness difference alone would be ambiguous.
            assert active.r > active.b, "active fill should be warm"
            assert resting.b > resting.r, "resting fill should be cool"

    _run(scenario)


def test_fill_marks_only_the_clickable_area() -> None:
    """Segments size to their label rather than stretching across the row."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(160, 12)) as pilot:
            await pilot.pause()
            group_width = app.group.region.width
            total = sum(s.region.width for s in app.group._segments.values())

            # The group hugs its content instead of filling the 160-cell row.
            assert group_width < 80, f"group stretched to {group_width}"
            assert total <= group_width

            for value, segment in app.group._segments.items():
                label = dict(OPTIONS)[value]
                # Wide enough for the label plus padding, not arbitrarily wide.
                assert segment.region.width >= len(label)
                assert segment.region.width <= len(label) + 6, (
                    f"{value!r} is {segment.region.width} wide for a "
                    f"{len(label)}-character label"
                )

            # Neighbouring segments are separated, not butted together.
            regions = sorted(
                (s.region for s in app.group._segments.values()), key=lambda r: r.x
            )
            for left, right in zip(regions, regions[1:]):
                assert right.x > left.right, "segments should not touch"

    _run(scenario)


def test_segments_stay_on_screen_when_the_terminal_is_narrow() -> None:
    """Content sizing must not reintroduce the overflow it replaced."""

    async def scenario() -> None:
        for width in (60, 70, 80, 100):
            app = _Harness()
            async with app.run_test(size=(width, 12)) as pilot:
                await pilot.pause()
                for value, segment in app.group._segments.items():
                    region = segment.region
                    assert region.width > 0, f"{value} collapsed at {width} cols"
                    assert region.right <= width, f"{value} off-screen at {width} cols"

    _run(scenario)


# --- behaviour --------------------------------------------------------------


def test_clicking_a_segment_activates_and_emits() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            app.group._segments["info"]._parent._activate("info")
            await pilot.pause()

            assert app.group.value == "info"
            assert app.changes == ["info"]
            assert "-active" in app.group._segments["info"].classes
            assert "-active" not in app.group._segments["all"].classes

    _run(scenario)


def test_reactivating_the_current_segment_emits_reselected_not_changed() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            app.group._activate("all")  # already active
            await pilot.pause()

            assert app.changes == []
            assert app.group.value == "all"

    _run(scenario)


def test_set_value_syncs_without_emitting() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            app.group.set_value("error")
            await pilot.pause()

            assert app.group.value == "error"
            assert app.changes == []
            assert "-active" in app.group._segments["error"].classes

            app.group.set_value("not-an-option")
            assert app.group.value == "error"

    _run(scenario)


def test_cycle_wraps_through_every_option() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            seen = [app.group.cycle() for _ in range(len(OPTIONS))]
            assert seen == ["debug", "info", "error", "all"]

    _run(scenario)
