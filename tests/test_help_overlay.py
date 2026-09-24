"""The help overlay, and the footer budget that makes it necessary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Label, Static

from textual.containers import Container

import clv.widgets.help_content
from clv.app import BINDING_CATEGORIES, LogViewerApp, build_help_sections
from clv.widgets.help_content import (
    DEFAULT_PAGE,
    STATIC_PAGES,
    Example,
    Heading,
    KeyRows,
    Paragraph,
    help_pages,
)
from clv.widgets.help_overlay import HelpOverlay, HelpSection, format_key
from clv.widgets.log_view import LogView
from clv.widgets.segmented import SegmentedButtons
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
        key=[
            "Help",
            "Search",
            "Navigation",
            "View",
            "Sources",
            "Plugins",
            "Session",
        ].index,
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
            # rather than clip. Asserted on the Keys page rather than whatever
            # page help opened on: that one provably cannot fit, so a failure
            # here means the scroller broke and not that a written page was
            # trimmed.
            await pilot.press("5")
            await pilot.pause()
            assert overlay.page == "keys"
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


def test_a_binding_names_the_sub_keys_of_the_modal_it_opens() -> None:
    """Deleting a saved view was reachable and undiscoverable.

    `v` opens a modal where `r` renames and `d` deletes twice, and none of that
    appeared anywhere the overlay or the README key table could show it — the
    keys lived only in the modal's own hint line. Carried in the *description*
    rather than as synthetic rows, so the overlay stays a pure function of
    `BINDINGS` and the README table inherits the same words for free.
    """

    rows = {
        key: description
        for section in build_help_sections(LogViewerApp.BINDINGS)
        for key, description in section.rows
    }
    assert "r renames" in rows["v"] and "d deletes" in rows["v"]
    assert "a adds" in rows["W"] and "d deletes" in rows["W"]
    assert "space toggles" in rows["P"] and "r re-enables" in rows["P"]
    # "then" is what scopes them to the modal. `d` is toggle_detail globally,
    # and the overlay lists that row too.
    assert "then" in rows["v"] and "then" in rows["W"] and "then" in rows["P"]


def test_every_description_fits_the_overlay_at_eighty_columns() -> None:
    """A description that overruns its column is clipped, silently.

    `#help-dialog` is 76 wide, `padding: 1 2` takes 4, `.help-key` is a fixed 12
    and the scrollbar takes 2 — leaving 58 cells on a `.help-row` that is one
    line tall. This was folklore until a description grew; now it fails the
    build instead.
    """

    budget = 76 - 4 - 12 - 2
    for binding in (
        list(LogViewerApp.BINDINGS) + list(LogView.BINDINGS) + list(TimelineBar.BINDINGS)
    ):
        description = binding.description or binding.action
        assert len(description) <= budget, f"{binding.key}: {description!r}"


# --- pages ------------------------------------------------------------------


def _visible_pages(overlay: HelpOverlay) -> list[str]:
    return [
        page.id or ""
        for page in overlay.query(".help-page").results(Container)
        if page.display
    ]


def test_help_opens_on_the_overview_page() -> None:
    """`?` is help, not a cheatsheet. The cheatsheet is one page of it."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            assert overlay.page == DEFAULT_PAGE == "overview"

    asyncio.run(scenario())


def test_exactly_one_page_is_displayed() -> None:
    """Every page is mounted; only one is in the layout.

    `display: none` rather than a rebuild, so `query(Static)` still finds every
    binding and `max_scroll_y` still describes the page on screen.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            assert len(overlay.query(".help-page")) == len(STATIC_PAGES) + 1

            for index, page in enumerate(overlay._pages):
                overlay.show_page(page.key)
                await pilot.pause()
                assert _visible_pages(overlay) == [f"help-page-{page.key}"], index

    asyncio.run(scenario())


def test_arrows_change_page_and_do_not_wrap() -> None:
    """The ends of the strip are where an operator expects to stop."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()
            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)

            # Left at the first page is a no-op, not a jump to the last.
            await pilot.press("left")
            await pilot.pause()
            assert overlay.page == "overview"

            await pilot.press("right")
            await pilot.pause()
            assert overlay.page == "search"

            for _ in range(10):
                await pilot.press("right")
            await pilot.pause()
            assert overlay.page == "keys"

    asyncio.run(scenario())


def test_arrows_reach_the_page_while_the_body_holds_focus() -> None:
    """The reason the arrow bindings are `priority=True`.

    `on_mount` focuses the scroller so Up/Down scroll, and a focused
    `VerticalScroll` binds Left/Right to horizontal scrolling. Without the
    priority flag it wins and the tab strip is unreachable from the keyboard.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            body = overlay.query_one("#help-body", VerticalScroll)
            assert app.focused is body

            await pilot.press("right")
            await pilot.pause()
            assert overlay.page == "search"

    asyncio.run(scenario())


def test_number_keys_jump_to_a_page() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()
            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)

            await pilot.press("5")
            await pilot.pause()
            assert overlay.page == "keys"

            await pilot.press("1")
            await pilot.pause()
            assert overlay.page == "overview"

            # Past the end does nothing rather than raising.
            await pilot.press("9")
            await pilot.pause()
            assert overlay.page == "overview"

    asyncio.run(scenario())


def test_clicking_a_tab_changes_the_page() -> None:
    """Keyboard and mouse reach the same places."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            tabs = overlay.query_one("#help-tabs", SegmentedButtons)
            segment = next(
                child
                for child in tabs.children
                if tabs.owns_widget(child) and child._value == "sources"
            )
            await pilot.click(segment)
            await pilot.pause()

            assert overlay.page == "sources"
            assert tabs.value == "sources"

    asyncio.run(scenario())


def test_the_title_names_the_page_on_screen() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            title = overlay.query_one("#help-title", Label)
            assert "Getting started" in str(title.content)

            await pilot.press("5")
            await pilot.pause()
            assert "Keyboard shortcuts" in str(title.content)

    asyncio.run(scenario())


def test_reopening_returns_to_the_last_page_read() -> None:
    """`?` meant "keybindings" for a long time; this is what pays that back."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)

            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("question_mark")
            await pilot.pause()
            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            assert overlay.page == "keys"

    asyncio.run(scenario())


# --- the content ------------------------------------------------------------


def test_the_keys_page_is_generated_from_the_sections_it_is_handed() -> None:
    """The "a key cannot go missing" guarantee, restated for the page."""

    sections = build_help_sections(
        [*LogViewerApp.BINDINGS, Binding("z", "invented_action", "Invented")]
    )
    pages = help_pages(sections)

    assert [page.key for page in pages][-1] == "keys"
    rows = [
        row
        for block in pages[-1].blocks
        if isinstance(block, KeyRows)
        for row in block.rows
    ]
    assert ("z", "Invented") in rows


def test_sections_read_back_off_the_rendered_page() -> None:
    """`_sections` is derived, so it cannot disagree with what is on screen.

    Keys come back as the page shows them — `question_mark` as `?` — because
    that is what "read back off the page" means.
    """

    sections = build_help_sections(LogViewerApp.BINDINGS)
    overlay = HelpOverlay(help_pages(sections))

    assert [section.title for section in overlay._sections] == [
        section.title for section in sections
    ]
    assert overlay._sections == [
        HelpSection(
            section.title,
            tuple((format_key(key), text) for key, text in section.rows),
        )
        for section in sections
    ]


def test_every_written_page_explains_and_shows() -> None:
    """A page stubbed out and forgotten is the failure this catches."""

    for page in STATIC_PAGES:
        kinds = {type(block) for block in page.blocks}
        assert Paragraph in kinds, page.key
        assert Example in kinds, page.key
        assert Heading in kinds, page.key


def test_every_example_line_fits_at_eighty_columns() -> None:
    """An example is never wrapped, so an overlong line is a clipped line.

    `#help-dialog` is 76 wide, `padding: 1 2` takes 4 and the scrollbar takes
    2, leaving 70; `.help-example` spends 1 on its rule and 2 on padding. 64 is
    that budget with a little room, because a query cut in half is worse than a
    query that looks cramped.
    """

    for page in STATIC_PAGES:
        for block in page.blocks:
            if isinstance(block, Example):
                for line in block.lines:
                    assert len(line) <= 64, f"{page.key}: {line!r}"


def test_every_key_column_entry_fits_its_cell() -> None:
    """`.help-key` is 12 cells, one of them padding, and clips past that.

    11 rather than 12 so the cell keeps a space before its description. A
    12-character entry rendered flush against its text is not a clipped cell,
    which is why nothing catches it by looking for one.
    """

    for page in STATIC_PAGES:
        for block in page.blocks:
            if isinstance(block, KeyRows):
                for key, description in block.rows:
                    assert len(key) <= 11, f"{page.key}: {key!r}"
                    assert len(description) <= 58, f"{page.key}: {description!r}"


def test_a_written_page_key_cell_is_rendered_verbatim() -> None:
    """`format_key` belongs to the Keys page and nowhere else.

    It capitalises anything longer than one character, which is right for
    `pageup` and wrong for `key:value` — and wrong silently, since a corrupted
    piece of syntax still renders.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            rendered = _overlay_text(app)
            assert "key:value" in rendered
            assert "Key:value" not in rendered
            # And the Keys page still translates, on the same render.
            assert "PgUp" in rendered or "Pageup" in rendered

    asyncio.run(scenario())


def test_content_with_brackets_is_not_read_as_markup() -> None:
    """`Static` parses console markup, and would eat `[log_viewer]` silently."""

    snippets = [
        line
        for page in STATIC_PAGES
        for block in page.blocks
        if isinstance(block, Example)
        for line in block.lines
        if line.startswith("[")
    ]
    assert snippets, "no bracketed snippet left to guard"

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            rendered = _overlay_text(app)
            for snippet in snippets:
                assert snippet in rendered, snippet

    asyncio.run(scenario())


# --- layout -----------------------------------------------------------------


def test_the_tab_strip_fits_at_eighty_columns() -> None:
    """A sixth tab is what this fails on.

    The horizontal analogue of the description budget above:
    `SegmentedButtons` is `overflow: hidden`, so a strip that outgrows the
    dialog loses its last tabs without a word — and a tab nobody can see or
    click is a page that does not exist.
    """

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
            tabs = overlay.query_one("#help-tabs", SegmentedButtons)
            segments = [
                child for child in tabs.children if tabs.owns_widget(child)
            ]
            assert len(segments) == len(STATIC_PAGES) + 1

            for segment in segments:
                assert segment.region.width > 0, segment._value
                assert segment.region.right <= dialog.region.right, segment._value

    asyncio.run(scenario())


def test_the_hint_line_survives_the_vertical_budget_at_80x24() -> None:
    """24 rows minus border, padding, title and a 3-row strip leaves 14.

    The hint is the only thing that says the arrow keys change the page, so it
    is the one row that must not be the one squeezed out.
    """

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
            hint = overlay.query_one("#help-hint", Static)
            assert hint.region.width > 0
            assert hint.region.bottom <= dialog.region.bottom

            body = overlay.query_one("#help-body", VerticalScroll)
            assert body.region.height >= 10

    asyncio.run(scenario())


def test_the_content_module_imports_no_textual() -> None:
    """`help_content` is data, and the claim in its docstring is load-bearing.

    It sits in `widgets/` for the reason `severity.py` does — a widget may not
    import `clv.app`, so the text has to live somewhere a widget can reach.
    That only stays a sound argument while the module is renderer-agnostic;
    the first `from textual...` makes it a widget with the wrong name.
    """

    import ast
    from pathlib import Path

    source = Path(clv.widgets.help_content.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "textual" not in imported
    assert "rich" not in imported


def test_the_hint_names_the_range_the_strip_actually_has() -> None:
    """A hint that promises `1-5` when there are four pages is a lie."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            hint = str(overlay.query_one("#help-hint", Static).content)
            assert f"1-{len(overlay._pages)} jump" in hint

    asyncio.run(scenario())
