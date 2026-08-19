"""The help overlay, and the footer budget that makes it necessary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from clv.app import BINDING_CATEGORIES, LogViewerApp, build_help_sections
from clv.widgets.help_overlay import HelpOverlay, HelpSection, format_key
from clv.widgets.log_view import LogView
from clv.widgets.timeline import TimelineBar


def _rows(sections: list[HelpSection]) -> list[tuple[str, str]]:
    return [row for section in sections for row in section.rows]


# --- grouping ---------------------------------------------------------------


def test_every_binding_has_a_category() -> None:
    """The fallback bucket is a safety net, not somewhere bindings live.

    Covers LogView and TimelineBar too: their cursor keys are bound on the
    widget rather than the app, and a key an operator cannot find is a key that
    does not exist.
    """

    uncategorised = {
        binding.action
        for binding in [*LogViewerApp.BINDINGS, *LogView.BINDINGS, *TimelineBar.BINDINGS]
        if binding.action not in BINDING_CATEGORIES
    }
    assert uncategorised == set()


def test_sections_follow_the_declared_category_order() -> None:
    titles = [section.title for section in build_help_sections(LogViewerApp.BINDINGS)]

    assert titles[0] == "Help"
    assert titles == sorted(
        titles,
        key=["Help", "Search", "Navigation", "View", "Sources", "Session"].index,
    )
    assert "Navigation" in titles


def test_an_uncategorised_binding_still_appears() -> None:
    """A binding added later cannot vanish from help, only land in Other."""

    bindings = [*LogViewerApp.BINDINGS, Binding("z", "invented_action", "Invented")]
    sections = build_help_sections(bindings)

    assert ("z", "Invented") in _rows(sections)
    assert sections[-1].title == "Other"


def test_hidden_bindings_are_listed_alongside_shown_ones() -> None:
    """The whole point: `o` is show=False and must still be discoverable."""

    rows = _rows(build_help_sections(LogViewerApp.BINDINGS))
    assert ("o", "Structured") in rows


@pytest.mark.parametrize(
    "key, expected",
    [
        ("question_mark", "?"),
        ("escape", "Esc"),
        ("asterisk", "*"),
        ("/", "/"),
        ("[", "["),
        ("+", "+"),
        ("ctrl+b", "Ctrl+B"),
        ("ctrl+l", "Ctrl+L"),
        ("q", "q"),
    ],
)
def test_keys_render_the_way_an_operator_types_them(key: str, expected: str) -> None:
    assert format_key(key) == expected


# --- the overlay ------------------------------------------------------------


def _overlay_text(app: LogViewerApp) -> str:
    overlay = app.screen
    assert isinstance(overlay, HelpOverlay)
    return "\n".join(str(static.content) for static in overlay.query(Static).results())


def test_question_mark_opens_the_overlay() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            assert isinstance(app.screen, HelpOverlay)

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["question_mark", "escape", "q"])
def test_each_dismiss_key_closes_the_overlay(key: str) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpOverlay)

            await pilot.press(key)
            await pilot.pause()

            assert not isinstance(app.screen, HelpOverlay)
            # `q` closes the overlay; it must not also quit the app.
            assert app.is_running

    asyncio.run(scenario())


def test_opening_twice_does_not_stack_overlays() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            app.action_show_help()
            await pilot.pause()
            app.action_show_help()
            await pilot.pause()

            overlays = [
                screen for screen in app.screen_stack if isinstance(screen, HelpOverlay)
            ]
            assert len(overlays) == 1

    asyncio.run(scenario())


def test_every_binding_appears_in_the_rendered_overlay() -> None:
    """The test that keeps help complete as later items add bindings."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            rendered = _overlay_text(app)
            for binding in LogViewerApp.BINDINGS:
                assert binding.description in rendered, binding.action
                assert format_key(binding.key) in rendered, binding.key

    asyncio.run(scenario())


def test_overlay_is_on_screen_and_scrollable_at_80x24() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            dialog = overlay.query_one("#help-dialog")
            assert dialog.region.x >= 0 and dialog.region.y >= 0
            assert dialog.region.right <= 80
            assert dialog.region.bottom <= 24

            # Every binding at 24 rows does not fit, so the body must scroll
            # rather than clip.
            body = overlay.query_one("#help-body", VerticalScroll)
            assert body.max_scroll_y > 0

    asyncio.run(scenario())


def test_focus_returns_to_whatever_held_it() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            panel = app.log_panel
            app.set_focus(panel)
            await pilot.pause()

            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert app.focused is panel

    asyncio.run(scenario())


def test_tailing_continues_while_the_overlay_is_open(tmp_path: Path) -> None:
    """Opening help pauses nothing."""

    source = tmp_path / "service.log"
    source.write_text("2026-08-07 09:25:01 - INFO - first\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            app._select_source(source)
            await pilot.pause()
            before = len(app._entries)

            app.action_show_help()
            await pilot.pause()
            assert isinstance(app.screen, HelpOverlay)

            with source.open("a", encoding="utf-8") as handle:
                handle.write("2026-08-07 09:25:02 - ERROR - second\n")
            app._poll_tail()
            await pilot.pause()

            assert len(app._entries) == before + 1
            assert app._entries[-1].message == "second"

    asyncio.run(scenario())


def test_the_query_input_still_receives_a_literal_question_mark() -> None:
    """`?` is a valid regex token, so the input must win while focused."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            query = app.query_one("#query-input", Input)
            query.focus()
            await pilot.pause()

            await pilot.press("question_mark")
            await pilot.pause()

            assert not isinstance(app.screen, HelpOverlay)
            assert query.value == "?"

    asyncio.run(scenario())


# --- footer budget ----------------------------------------------------------


def test_footer_budget_at_80_columns() -> None:
    """`?` costs the last footer slot, and must never be the entry that drops.

    The footer fills from the left and truncates on the right, so this pins
    which bindings are visible at the narrowest supported width. A binding
    inserted ahead of `?` would push it off screen and make the overlay
    undiscoverable, which is exactly what this asserts against.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.pause()

            footer = app.query_one(Footer)
            visible = [
                child.key
                for child in footer.children
                if child.region.width and child.region.right <= 80
            ]

            assert visible[0] == "question_mark"
            assert visible[:6] == [
                "question_mark",
                "slash",
                "escape",
                "a",
                "asterisk",
                "t",
            ]

    asyncio.run(scenario())


def test_bindings_cut_from_the_footer_are_all_in_the_overlay() -> None:
    """Nothing the footer drops at 80 columns is left undiscoverable."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.pause()

            footer = app.query_one(Footer)
            cut = {
                child.key
                for child in footer.children
                if child.region.right > 80 and getattr(child, "key", None)
            }
            assert {"w", "ctrl+b", "ctrl+l", "ctrl+s", "ctrl+r", "q"} <= cut

            app.action_show_help()
            await pilot.pause()
            rendered = _overlay_text(app)
            for key in cut:
                assert format_key(key) in rendered

    asyncio.run(scenario())
