"""Active-filter chips.

Each chip is a single one-row widget rather than a container holding a Button:
Textual Buttons reserve three rows for their chrome, which a one-row pill
cannot show. Clicking (or pressing Enter/Space on) a chip dismisses it and
emits :class:`FilterChip.Dismissed`.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.containers import Container
from textual.message import Message
from textual.widgets import Static


class FilterChip(Static):
    """Dismissible pill describing one active filter."""

    DEFAULT_CSS = """
    FilterChip {
        width: auto;
        height: 1;
        background: $accent 30%;
        color: $text;
        padding: 0 1;
        margin-right: 1;
    }

    FilterChip:hover {
        background: $accent 55%;
    }

    FilterChip:focus {
        background: $accent 55%;
        text-style: bold;
    }
    """

    def __init__(self, label: str, *, key: str) -> None:
        super().__init__(classes="filter-chip")
        self.label_text = label
        self.key = key
        self.can_focus = True
        self.tooltip = f"Dismiss: {label}"

    def render(self) -> Text:
        return Text(f"{self.label_text} ✕")

    def _dismiss(self) -> None:
        self.post_message(self.Dismissed(self.key))

    def on_click(self, _event: events.Click) -> None:
        self._dismiss()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "space", "backspace", "delete"):
            event.stop()
            self._dismiss()

    class Dismissed(Message):
        """A chip was dismissed; the app should revert that filter."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key


class FilterChips(Container):
    """Row of active filter chips; hides itself when there are none."""

    DEFAULT_CSS = """
    FilterChips {
        layout: horizontal;
        height: 1;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2;
        background: $surface 6%;
    }

    FilterChips.-empty { display: none; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_class("-empty")

    def update_chips(self, chips: list[FilterChip]) -> None:
        """Replace the displayed chips."""

        self.remove_children()
        self.set_class(not chips, "-empty")
        if chips:
            self.mount_all(chips)
