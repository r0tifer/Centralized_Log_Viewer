from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


class SegmentedButtons(Static):
    """Simple segmented button group built from toggle buttons."""

    DEFAULT_CSS = """
    /* Horizontal pill group that clearly reads as clickable */
    SegmentedButtons {
        layout: horizontal;
        background: $surface 6%;
        border: round $surface 18%;
        padding: 0 1;
        height: 3;
        overflow: hidden;
    }

    /* each segment should read as an interactive pill */
    SegmentedButtons > .segment {
        border: none;
        background: $surface 14%;
        color: $text;
        text-style: bold;           /* base: bold text */
        padding: 0 2;
        height: 3;                  /* fixed height so underline doesn't shift layout */
        min-width: 6;
        width: auto;
        margin-right: 1;
        outline: none;              /* avoid thick focus outlines that could clip text */
        /* Reserve space for the focus/hover/active underline so size never changes */
        border-bottom: tall transparent;
    }

    SegmentedButtons > .segment:last-child {
        margin-right: 0;
    }

    /* Hover: subtle emphasis; still no layout shift */
    SegmentedButtons > .segment.-hover {
        background: $surface 18%;
        color: $text;
        text-style: bold underline;
    }

    /* Keyboard focus: high-visibility yellow bar + underline (no resize) */
    SegmentedButtons > .segment:focus {
        background: $surface 20%;
        color: $text;
        text-style: bold underline;
        border-bottom: tall $warning 70%;  /* bright yellow focus indicator */
    }

    /* active segment stays legible and suppresses extra outlines */
    SegmentedButtons > .segment.-active {
        background: $accent 35%;
        color: $text;
        text-style: bold underline;   /* consistent underline */
        border-bottom: tall $accent 40%;  /* keep active underline in accent */
    }

    /* Active + hover: same background, no extra shift */
    SegmentedButtons > .segment.-active.-hover {
        background: $accent 35%;
        color: $text;
        text-style: bold underline;
        border-bottom: tall $accent 40%;
    }

    /* Active + keyboard focus: keep accent base, show a stronger yellow tip */
    SegmentedButtons > .segment.-active:focus {
        background: $accent 35%;
        color: $text;
        text-style: bold underline;
        /* Slightly stronger yellow over accent to clearly signal keyboard focus */
        border-bottom: tall $warning 80%;
    }
    """

    def __init__(self, options: list[tuple[str, str]], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._options = options
        self._current = options[0][0]
        self._segments: dict[str, SegmentedButtons._Segment] = {}
        self._hovered: str | None = None
        self._focused: str | None = None

    @property
    def value(self) -> str:
        return self._current

    @property
    def hovered_value(self) -> str | None:
        """The value of the segment currently hovered by the mouse, if any."""
        return self._hovered

    @property
    def focused_value(self) -> str | None:
        """The value of the segment that currently has keyboard focus, if any."""
        return self._focused

    def set_value(self, value: str) -> None:
        if value == self._current:
            return
        self._current = value
        self._refresh_state()

    def cycle(self) -> str:
        keys = [opt for opt, _ in self._options]
        index = keys.index(self._current)
        self._current = keys[(index + 1) % len(keys)]
        self._refresh_state()
        return self._current

    def compose(self) -> ComposeResult:
        self._segments.clear()
        for value, label in self._options:
            segment = self._Segment(self, value, label)
            self._segments[value] = segment
            yield segment

    def on_mount(self) -> None:
        self._refresh_state()

    def _refresh_state(self) -> None:
        for value, segment in self._segments.items():
            segment.set_class(value == self._current, "-active")
            segment.set_class(self._hovered == value, "-hover")

    def _activate(self, value: str) -> None:
        if value == self._current:
            return
        self._current = value
        self._refresh_state()
        self.post_message(self.ValueChanged(self, value))

    def owns_widget(self, widget: Widget) -> bool:
        """Return True if the widget is one of this group's segments."""
        return any(segment is widget for segment in self._segments.values())

    def nudge(self, direction: int, *, anchor: str | None = None, commit: bool = False) -> bool:
        """Move focus left or right by one segment.

        Args:
            direction: -1 for left, +1 for right.
            anchor: Optional current segment to anchor navigation from.
            commit: When True, also activate the newly-focused segment.
        """
        if direction == 0:
            return False
        values = [opt for opt, _ in self._options]
        if not values:
            return False

        current = anchor or self.focused_value or self._current
        if current not in values:
            current = values[0]

        index = values.index(current)
        next_index = index + direction
        if next_index < 0 or next_index >= len(values):
            return False

        next_value = values[next_index]
        segment = self._segments.get(next_value)
        if segment is None:
            return False
        segment.focus()
        if commit:
            self._activate(next_value)
        else:
            self._set_focused(next_value)
        return True

    def _set_hovered(self, value: str | None) -> None:
        if value == self._hovered:
            return
        self._hovered = value
        self._refresh_state()
        self.post_message(self.HoverChanged(self, value))

    def _set_focused(self, value: str | None) -> None:
        if value == self._focused:
            return
        self._focused = value

    class _Segment(Static):
        def __init__(self, parent: "SegmentedButtons", value: str, label: str) -> None:
            super().__init__(label, classes="segment")
            self._parent = parent
            self._value = value
            self._label = label
            self.can_focus = True

        def render(self) -> Text:
            return Text(self._label, justify="center")

        def on_click(self, event: events.Click) -> None:
            self._parent._activate(self._value)

        def on_key(self, event: events.Key) -> None:
            if event.key in ("enter", "space"):
                self._parent._activate(self._value)
                event.stop()
            elif event.key in ("left", "right"):
                direction = -1 if event.key == "left" else 1
                if self._parent.nudge(direction, anchor=self._value, commit=False):
                    event.stop()

        def on_mouse_enter(self, event: events.MouseEnter) -> None:  # type: ignore[override]
            self._parent._set_hovered(self._value)

        def on_mouse_leave(self, event: events.MouseLeave) -> None:  # type: ignore[override]
            self._parent._set_hovered(None)

        def on_focus(self, event: events.Focus) -> None:  # type: ignore[override]
            self._parent._set_focused(self._value)

        def on_blur(self, event: events.Blur) -> None:  # type: ignore[override]
            self._parent._set_focused(None)

    class ValueChanged(Message):
        def __init__(self, segmented: "SegmentedButtons", value: str) -> None:
            super().__init__()
            self.segmented = segmented
            self.value = value

        @property
        def control(self) -> "SegmentedButtons":
            return self.segmented

    class HoverChanged(Message):
        """Emitted when the mouse enters or leaves a segment."""

        def __init__(self, segmented: "SegmentedButtons", value: str | None) -> None:
            super().__init__()
            self.segmented = segmented
            self.value = value

        @property
        def control(self) -> "SegmentedButtons":
            return self.segmented
