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
    /* Horizontal pill group.
       No border or background of its own: a `border: round` on a height-3 group
       leaves a single interior row, which clipped each segment's fill down to
       its middle line. Dropping it lets the segments render as full-height
       blocks (matching the action buttons) and returns two columns of width. */
    SegmentedButtons {
        layout: horizontal;
        background: transparent;
        border: none;
        padding: 0;
        height: 3;
        width: auto;
        overflow: hidden;
    }

    /* Segments size to their label so the fill marks the clickable area only,
       rather than stretching across the whole row.

       The fill is $panel-lighten-1 rather than a $surface tint: $surface is
       #1E1E1E against a #121212 background, so `$surface 14%` blended to
       #131313 -- one step off invisible. This is a cool slate that reads
       clearly and complements the warm accent used for the active segment. */
    SegmentedButtons > .segment {
        border: none;
        background: $panel-lighten-1;
        color: $text;
        text-style: bold;           /* base: bold text */
        content-align: center middle;
        padding: 0 2;
        height: 3;                  /* fixed height so underline doesn't shift layout */
        min-width: 5;
        width: auto;
        margin-right: 1;
        outline: none;              /* avoid thick focus outlines that could clip text */
    }

    SegmentedButtons > .segment:last-child {
        margin-right: 0;
    }

    /* Hover: one step brighter than the resting fill; no layout shift */
    SegmentedButtons > .segment.-hover {
        background: $panel-lighten-2;
        color: $text;
        text-style: bold underline;
    }

    /* Keyboard focus: same lift as hover, marked by an underline.
       States are carried by fill and text-style rather than by a border,
       because a border on a fixed height-3 widget consumes an interior row
       and pushes the label off centre. */
    SegmentedButtons > .segment:focus {
        background: $panel-lighten-2;
        color: $text;
        text-style: bold underline;
    }

    /* Active: solid warm accent against the cool resting fill, so the
       selection is distinguishable by hue as well as by brightness. */
    SegmentedButtons > .segment.-active {
        background: $accent 55%;
        color: $text;
        text-style: bold;
    }

    SegmentedButtons > .segment.-active.-hover {
        background: $accent 70%;
        color: $text;
        text-style: bold;
    }

    /* Active + keyboard focus: keep the accent fill and add the underline, so
       "selected" and "focused" stay separately readable. */
    SegmentedButtons > .segment.-active:focus {
        background: $accent 70%;
        color: $text;
        text-style: bold underline;
    }
    """

    def __init__(
        self,
        options: list[tuple[str, str]],
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._options = options
        self._current = options[0][0]
        self._segments: dict[str, SegmentedButtons._Segment] = {}
        self._hovered: str | None = None
        self._focused: str | None = None

    @property
    def value(self) -> str:
        return self._current

    @property
    def values(self) -> list[str]:
        """The option identifiers, in display order."""
        return [value for value, _ in self._options]

    def set_segment_tooltip(self, value: str, tooltip: str | None) -> None:
        """Attach a tooltip to one segment (used for the custom range summary)."""
        segment = self._segments.get(value)
        if segment is not None:
            segment.tooltip = tooltip

    @property
    def hovered_value(self) -> str | None:
        """The value of the segment currently hovered by the mouse, if any."""
        return self._hovered

    @property
    def focused_value(self) -> str | None:
        """The value of the segment that currently has keyboard focus, if any."""
        return self._focused

    def set_value(self, value: str) -> None:
        """Select *value* without emitting ValueChanged (programmatic sync)."""
        if value not in self.values:
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
            # Re-activating the current segment is meaningful for options that
            # open a dialog (the custom time range): the user wants to adjust
            # it, not clear it first.
            self.post_message(self.Reselected(self, value))
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

    class Reselected(Message):
        """Emitted when the already-active segment is activated again."""

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
