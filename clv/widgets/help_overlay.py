"""Modal help: five pages behind a tab strip, one of them the keybindings.

The footer drops entries from the right as it runs out of room, and at 80
columns it has space for roughly half of what the app binds. Without a
discoverable list every hidden binding may as well not exist, so the **Keys**
page is what makes `show=False` an acceptable answer to a full footer.

The other four pages exist for the half of CLV that is not a keystroke — what
a field query is, why a pane is empty, what `x` then `u` are for. That material
was only ever in `README.md`, which does not ship in the wheel.

The Keys page is built by the app and passed in: the app owns `BINDINGS` and
the category map, and a widget must not import `clv.app`. This widget only
renders what it is handed, which also makes the grouping unit-testable without
running an app. The written pages come from `help_content`, which is data
rather than a widget for the same reason.
"""

from __future__ import annotations

from typing import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from .help_content import (
    Bullets,
    DEFAULT_PAGE,
    Example,
    Heading,
    HelpBlock,
    HelpPage,
    HelpSection,
    KeyRows,
    Paragraph,
    format_key,
    help_pages,
    keys_page,
)
from .segmented import SegmentedButtons


class HelpOverlay(ModalScreen[str]):
    """Every binding and everything around it, readable at 80x24.

    Dismisses with the page that was on screen, so the app can reopen there.
    """

    DEFAULT_CSS = """
    HelpOverlay {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #help-dialog {
        width: 100%;
        max-width: 76;
        height: 100%;
        padding: 1 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    /* No bottom padding: the first section header brings its own top
       padding, and at 24 rows a doubled gap costs a visible binding. */
    #help-title {
        text-style: bold;
        height: 1;
    }

    /* The strip costs three of the twenty interior rows at 80x24, which is
       why nothing else above #help-body may grow. */
    #help-tabs {
        height: 3;
    }

    #help-body {
        height: 1fr;
    }

    /* One scroller, one page shown. `display: none` leaves the layout, so
       `max_scroll_y` describes the visible page and nothing else. */
    .help-page {
        height: auto;
        layout: vertical;
    }

    .help-section {
        text-style: bold;
        color: $accent;
        padding-top: 1;
    }

    .help-para {
        height: auto;
        padding-top: 1;
    }

    /* A table needs air after prose and none after its own heading, which
       already brings `padding-top`. Spending a row on both would cost eight
       of them on the Keys page, where density is the point. */
    .help-rows {
        height: auto;
        layout: vertical;
        padding-top: 1;
    }

    .help-rows.-tight {
        padding-top: 0;
    }

    .help-row {
        height: 1;
        layout: horizontal;
    }

    /* 12 wide, but one of those is padding, so a full-width left cell still
       has a space before its description instead of running into it. The
       description keeps its 58 cells either way. */
    .help-key {
        width: 12;
        padding-right: 1;
        color: $text-accent;
    }

    .help-description {
        width: 1fr;
    }

    /* The gap belongs to the list, not to each item: padding on the row
       puts a blank line between every bullet, which at 14 rows costs more
       than the list is worth. */
    .help-bullets {
        height: auto;
        layout: vertical;
        padding-top: 1;
    }

    /* A real hanging indent: the mark is its own cell, so a wrapped item
       lines up under its text rather than under its bullet. */
    .help-bullet {
        height: auto;
        layout: horizontal;
    }

    .help-bullet-mark {
        width: 2;
        color: $text-accent;
    }

    .help-bullet-text {
        width: 1fr;
    }

    .help-example {
        height: auto;
        layout: vertical;
        padding: 0 1;
        margin-top: 1;
        border-left: outer $accent 40%;
        background: $surface 15%;
    }

    .help-example-line {
        height: 1;
        color: $text-accent;
    }

    .help-example-caption {
        height: auto;
        color: $text-muted;
    }

    /* Height auto, not 1: box-sizing is border-box, so `height: 1` with
       `padding-top: 1` leaves zero rows for the text and the line paints
       blank — which is how the only on-screen mention of the paging keys
       disappears without anything failing. */
    #help-hint {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }
    """

    #: `{last}` is the highest page number, so the hint cannot claim a range
    #: the strip does not have if a page is ever added or removed.
    HINT = "← → change page · 1-{last} jump · ? Esc q close"

    BINDINGS = [
        Binding("question_mark", "close", "Close", show=False),
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
        # Priority, because the focused VerticalScroll binds left/right to
        # horizontal scrolling and would win otherwise. Taking them costs
        # nothing: help content wraps, so there is no horizontal axis to
        # scroll.
        Binding("left", "previous_page", "Previous page", show=False, priority=True),
        Binding("right", "next_page", "Next page", show=False, priority=True),
        # 1-9 rather than 1-<page count>: BINDINGS is a class attribute and
        # cannot know how many pages an instance was handed. Out of range is a
        # no-op.
        *[
            Binding(str(n), f"jump_page({n - 1})", f"Page {n}", show=False)
            for n in range(1, 10)
        ],
    ]

    def __init__(
        self,
        pages: Sequence[HelpPage],
        *,
        start: str = DEFAULT_PAGE,
    ) -> None:
        super().__init__()
        self._pages = list(pages)
        keys = [page.key for page in self._pages]
        self._page = start if start in keys else (keys[0] if keys else DEFAULT_PAGE)

    @property
    def page(self) -> str:
        """The page currently on screen."""

        return self._page

    @property
    def _sections(self) -> list[HelpSection]:
        """The generated cheatsheet, read back off the page that renders it.

        Derived rather than stored beside it, so the answer a caller gets is
        the thing on screen rather than a second copy that can drift from it.
        """

        sections: list[HelpSection] = []
        for page in self._pages:
            if page.key != "keys":
                continue
            title: str | None = None
            for block in page.blocks:
                if isinstance(block, Heading):
                    title = block.text
                elif isinstance(block, KeyRows) and title is not None:
                    sections.append(HelpSection(title, tuple(block.rows)))
                    title = None
        return sections

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Label(self._title_for(self._page), id="help-title")
            yield SegmentedButtons(
                [(page.key, page.label) for page in self._pages],
                id="help-tabs",
            )
            with VerticalScroll(id="help-body"):
                for page in self._pages:
                    container = Container(
                        id=f"help-page-{page.key}", classes="help-page"
                    )
                    container.display = page.key == self._page
                    with container:
                        previous: HelpBlock | None = None
                        for block in page.blocks:
                            yield from self._render_block(
                                block, after_heading=isinstance(previous, Heading)
                            )
                            previous = block
            yield Static(
                Text(self.HINT.format(last=len(self._pages))), id="help-hint"
            )

    def on_mount(self) -> None:
        self.query_one("#help-tabs", SegmentedButtons).set_value(self._page)
        # Focus the scroller so the arrow keys reach the page rather than
        # whatever held focus behind the overlay.
        self.query_one("#help-body", VerticalScroll).focus()

    # --- rendering ----------------------------------------------------------

    def _render_block(
        self, block: HelpBlock, *, after_heading: bool = False
    ) -> ComposeResult:
        """One block, as widgets.

        Every string goes through `Text`. `Static` parses console markup, so a
        config snippet like `[log_viewer]` passed as a plain string renders as
        an empty styled span — silently, which is the whole problem with it.
        """

        if isinstance(block, Heading):
            yield Static(Text(block.text), classes="help-section")
        elif isinstance(block, Paragraph):
            yield Static(Text(block.text), classes="help-para")
        elif isinstance(block, Bullets):
            with Container(classes="help-bullets"):
                for item in block.items:
                    with Horizontal(classes="help-bullet"):
                        yield Static(Text("•"), classes="help-bullet-mark")
                        yield Static(Text(item), classes="help-bullet-text")
        elif isinstance(block, Example):
            with Container(classes="help-example"):
                for line in block.lines:
                    yield Static(
                        Text(line, no_wrap=True), classes="help-example-line"
                    )
                if block.caption:
                    yield Static(
                        Text(block.caption), classes="help-example-caption"
                    )
        elif isinstance(block, KeyRows):
            classes = "help-rows -tight" if after_heading else "help-rows"
            with Container(classes=classes):
                for key, description in block.rows:
                    with Horizontal(classes="help-row"):
                        yield Static(Text(key), classes="help-key")
                        yield Static(
                            Text(description), classes="help-description"
                        )

    def _title_for(self, key: str) -> str:
        for page in self._pages:
            if page.key == key:
                return f"Help · {page.title}"
        return "Help"

    # --- paging -------------------------------------------------------------

    def show_page(self, key: str) -> None:
        """Put *key* on screen, or do nothing if there is no such page."""

        if key == self._page or key not in {page.key for page in self._pages}:
            return
        self.query_one(f"#help-page-{self._page}", Container).display = False
        self.query_one(f"#help-page-{key}", Container).display = True
        self._page = key
        self.query_one("#help-title", Label).update(self._title_for(key))
        self.query_one("#help-tabs", SegmentedButtons).set_value(key)
        # A new page starts at the top; carrying the old offset lands the
        # reader in the middle of something they have not seen.
        self.query_one("#help-body", VerticalScroll).scroll_home(animate=False)

    def _step(self, direction: int) -> None:
        keys = [page.key for page in self._pages]
        index = keys.index(self._page) + direction
        # No wrap, matching SegmentedButtons.nudge: the ends of the strip are
        # where an operator expects to stop.
        if 0 <= index < len(keys):
            self.show_page(keys[index])

    def action_next_page(self) -> None:
        self._step(1)

    def action_previous_page(self) -> None:
        self._step(-1)

    def action_jump_page(self, index: int) -> None:
        if 0 <= index < len(self._pages):
            self.show_page(self._pages[index].key)

    def on_segmented_buttons_value_changed(
        self, event: SegmentedButtons.ValueChanged
    ) -> None:
        event.stop()
        self.show_page(event.value)

    def on_segmented_buttons_reselected(
        self, event: SegmentedButtons.Reselected
    ) -> None:
        event.stop()
        self.query_one("#help-body", VerticalScroll).scroll_home(animate=False)

    def action_close(self) -> None:
        self.dismiss(self._page)


__all__ = [
    "HelpOverlay",
    "HelpPage",
    "HelpSection",
    "format_key",
    "help_pages",
    "keys_page",
]
