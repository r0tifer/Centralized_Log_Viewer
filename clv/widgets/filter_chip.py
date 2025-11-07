from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Button, Label, Static


class FilterChip(Static):
    """Visual pill that can be dismissed."""

    DEFAULT_CSS = """
    FilterChip {
        layout: horizontal;
        align: center middle;
        border: round $accent 50%;
        padding: 0 1;
        height: 3;
        min-height: 3;
    }

    FilterChip > .chip-label {
        color: $text;
    }

    FilterChip > .chip-dismiss {
        min-width: 1;
        padding: 0;
        color: $text-muted;
    }
    """

    def __init__(self, label: str, *, key: str) -> None:
        super().__init__(classes="filter-chip")
        self.label_text = label
        self.key = key

    def compose(self) -> ComposeResult:
        yield Label(self.label_text, classes="chip-label")
        yield Button("×", classes="chip-dismiss", variant="error", id=f"dismiss-{self.key}")
