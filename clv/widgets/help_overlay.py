"""Modal listing every keybinding, including the ones the footer cannot show.

The footer drops entries from the right as it runs out of room, and at 80
columns it has space for roughly half of what the app binds. Without a
discoverable list every hidden binding may as well not exist, so this overlay
is what makes `show=False` an acceptable answer to a full footer.

The sections are built by the app and passed in: the app owns `BINDINGS` and
the category map, and a widget must not import `clv.app`. This widget only
renders what it is handed, which also makes the grouping unit-testable without
running an app.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

#: Textual key names that do not read as themselves. Anything not listed is
#: either a single character (shown as-is) or a modifier combination.
_KEY_DISPLAY: dict[str, str] = {
    "escape": "Esc",
    "asterisk": "*",
    "question_mark": "?",
    "slash": "/",
    "plus": "+",
    "minus": "-",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "up": "Up",
    "down": "Down",
    "enter": "Enter",
    "space": "Space",
    "tab": "Tab",
}


def format_key(key: str) -> str:
    """Render a Textual key name the way an operator would type it."""

    if len(key) == 1:  # "/", "[", "+" are bound as themselves
        return key
    known = _KEY_DISPLAY.get(key)
    if known is not None:
        return known
    parts = key.split("+")
    return "+".join(
        _KEY_DISPLAY.get(part, part.upper() if len(part) == 1 else part.capitalize())
        for part in parts
    )


@dataclass(frozen=True)
class HelpSection:
    """One titled group of bindings, in the order the app declared them."""

    title: str
    #: ``(key, description)`` pairs, keys still in Textual's naming.
    rows: tuple[tuple[str, str], ...]


class HelpOverlay(ModalScreen[None]):
    """Every binding, grouped, scrollable, and readable at 80x24."""

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
    }

    #help-body {
        height: 1fr;
    }

    .help-section {
        text-style: bold;
        color: $accent;
        padding-top: 1;
    }

    .help-row {
        height: 1;
        layout: horizontal;
    }

    .help-key {
        width: 12;
        color: $text-accent;
    }

    .help-description {
        width: 1fr;
    }

    #help-hint {
        color: $text-muted;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("question_mark", "close", "Close", show=False),
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, sections: list[HelpSection]) -> None:
        super().__init__()
        self._sections = sections

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Label("Keyboard shortcuts", id="help-title")
            with VerticalScroll(id="help-body"):
                for section in self._sections:
                    yield Static(section.title, classes="help-section")
                    for key, description in section.rows:
                        with Horizontal(classes="help-row"):
                            yield Static(format_key(key), classes="help-key")
                            yield Static(description, classes="help-description")
            yield Static("? / Esc / q to close", id="help-hint")

    def on_mount(self) -> None:
        # Focus the scroller so the arrow keys reach the list rather than
        # whatever held focus behind the overlay.
        self.query_one("#help-body", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)
