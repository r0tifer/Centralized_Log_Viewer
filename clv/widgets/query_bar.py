from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from contextlib import contextmanager

from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static, Switch

from .segmented import SegmentedButtons


@dataclass
class RegexStatus:
    valid: bool
    message: str
    matches: int | None = None


class LabeledField(Static):
    """Utility container with label above control."""

    DEFAULT_CSS = """
    LabeledField {
        layout: vertical;
        width: 1fr;          /* stretch to grid cell */
        min-width: 16;
    }

    LabeledField > .field-label {
        color: $text-muted;
    }

    /* every control we stick in here gets usable size by default */
    LabeledField > .field-control {
        height: 3;
        width: 1fr;
    }
    """

    def __init__(self, label: str, control: Widget, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._label = Label(label, classes="field-label")
        # Wrap the control to match the CSS selector expectations
        self._control_wrapper = Container(control, classes="field-control")

    def compose(self) -> ComposeResult:
        yield self._label
        yield self._control_wrapper


class QueryBar(Container):
    """Top horizontal query band."""

    DEFAULT_CSS = """
    QueryBar {
        layout: vertical;
        padding: 0 2 0 2;
        background: $surface 5%;
        border-top: solid $surface 25%;
        border-bottom: solid $surface 25%;
        height: auto;
        overflow-y: hidden;
    }

    QueryBar > #query-grid {
        layout: vertical;
    }

    #query-grid > .row {
        layout: horizontal;
        height: auto;
        margin: 0;
        padding: 0;
        align: left middle;
        width: 1fr;
    }

    #query-grid > .row > LabeledField {
        width: 1fr;
        margin-right: 1;
    }

    #query-grid > .row > LabeledField:last-child { margin-right: 0; }
    #time-row { align: left top; }
    #time-row > #time-controls {
        layout: horizontal;
        align: right top;
        height: auto;
        width: auto;
    }
    QueryBar #auto-scroll-field,
    QueryBar #pretty-field {
        padding-top: 0;
        width: auto;
    }
    QueryBar #time-controls > #auto-scroll-field { margin-right: 1; }
    QueryBar #time-controls > #pretty-field { margin-right: 1; }
    #time-spacer { width: 1fr; min-width: 0; }

    #actions-field {
        layout: horizontal;
        align: right top;
        height: auto;
        min-height: 3;
        padding: 1 0 0 0;
    }

    #actions-field Button {
        margin-left: 1;
        height: 3;
        min-height: 3;
        padding: 0 2;
    }

    #actions-field Button:first-child { margin-left: 0; }

    #advanced-button-field {
        layout: horizontal;
        align: right top;
        height: auto;
        min-height: 3;
        padding: 0 0 0 0;
        margin-right: 1;
    }

    #advanced-button-field Button {
        height: 3;
        min-height: 3;
        padding: 0 2;
        margin: 0;
    }

    QueryBar LabeledField > .field-label {
        margin-bottom: 0;
        height: 1;
        color: $text-muted;
    }

    QueryBar #time-field .field-control {
        height: auto;
        min-height: 3;
        padding-bottom: 0;
        align: center middle;
    }

    QueryBar #time-field RadioSet {
        align: center middle;
    }

    QueryBar Input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        width: 1fr;
    }

    QueryBar Switch {
        height: 3;
    }

    QueryBar RadioSet { layout: horizontal; }

    QueryBar RadioButton {
        border: tall $surface 35%;
        background: $surface 18%;
        color: $text;
        text-style: bold;
        padding: 0 1;
        height: 3;
        min-width: 5;
        width: auto;
        align: center middle;
        margin-right: 0;
    }
    QueryBar RadioSet RadioButton:last-child { margin-right: 0; }

    QueryBar RadioButton.-checked {
        background: $accent 50%;
        border: tall $accent 45%;
        color: $text;
        text-style: bold;
    }
    QueryBar RadioButton.-selected {
        outline: wide $accent 30%;
        background: $surface 24%;
    }

    QueryBar RadioButton:hover,
    QueryBar RadioButton:focus {
        background: $surface 18%;
        outline: wide $accent 30%;
    }

    QueryBar #severity-field SegmentedButtons { height: 3; width: 1fr; }

    QueryBar #actions-field { padding-top: 0; }

    QueryBar #auto-scroll-field .field-control { width: auto; }

    QueryBar Input.-regex-invalid {
        border: tall #f87171;
        background: $surface 14%;
    }

    /* Time field: span available width, keep presets left-aligned */
    QueryBar #time-field { width: auto; }
    QueryBar #time-field .field-control {
        width: auto;
        layout: horizontal;
        align: left middle;
        content-align: left middle;
        height: auto;
        min-height: 3;
        padding: 0 1 0 1;
        margin: 0;
    }
    QueryBar #time-field RadioSet {
        width: auto;
        align: left middle;
    }
    QueryBar #time-field RadioButton {
        height: 3;
        align: center middle;
        margin-top: 0;
        margin-bottom: 0;
    }
    """
    regex_status = reactive(RegexStatus(True, ""))

    def __init__(self) -> None:
        super().__init__(id="query-bar")
        self._time_buttons: dict[str, RadioButton] = {}
        self._time_order: list[str] = []
        self._time_selection = "all"
        self._time_focus_value: str | None = "all"

        # Previous single boolean guard
        self._suppress_time_event = False
        self._suppress_depth = 0
        self._ignore_time_change_count = 0
        self._ignore_next_radio_changed = 0

        self.time_set: RadioSet = self._build_time_controls()
        self.severity_segmented = SegmentedButtons(
            [
                ("all", "All"),
                ("info", "Info"),
                ("warn", "Warn"),
                ("error", "Error"),
                ("debug", "Debug"),
            ],
            id="severity-segments",
        )

    def compose(self) -> ComposeResult:
        query_input = Input(placeholder="ERROR|WARN", id="query-input")
        query_field = LabeledField("Query", query_input, id="query-field")
        time_field = LabeledField("Time", self.time_set, id="time-field")
        severity_field = LabeledField("Severity", self.severity_segmented, id="severity-field")
        auto_toggle = LabeledField("Auto-scroll", Switch(value=True, id="auto-scroll-toggle"), id="auto-scroll-field")
        pretty_toggle = LabeledField(
            "Structured output",
            Switch(value=False, id="pretty-structured-toggle"),
            id="pretty-field",
        )
        advanced_toggle = Container(
            Button("Advanced Filters", id="toggle-advanced", variant="warning"),
            id="advanced-button-field",
        )
        actions = Container(
            Button("Add Source", id="add-source", variant="success"),
            Button("Run", id="run-query", variant="primary"),
            Button("Clear", id="clear-query", variant="error"),
            Button("Save", id="save-session", variant="success"),
            id="actions-field",
        )
        time_controls = Container(auto_toggle, pretty_toggle, advanced_toggle, actions, id="time-controls")
        spacer = Container(id="time-spacer")

        with Container(id="query-grid"):
            with Container(id="query-row", classes="row query-row"):
                yield query_field
                yield severity_field
            with Container(id="time-row", classes="row time-row"):
                yield time_field
                yield spacer
                yield time_controls

    def _build_time_controls(self) -> RadioSet:
        presets = [
            ("all", "All"),
            ("15m", "15m"),
            ("1h", "1h"),
            ("6h", "6h"),
            ("24h", "24h"),
        ]
        self._time_buttons.clear()
        self._time_order = [value for value, _ in presets]
        buttons: list[RadioButton] = []
        for value, label in presets:
            button = RadioButton(label, id=f"time-{value}")
            button.value = value == self._time_selection
            self._time_buttons[value] = button
            buttons.append(button)
        range_button = RadioButton("Custom", id="time-range")
        range_button.value = self._time_selection == "range"
        self._time_buttons["range"] = range_button
        buttons.append(range_button)
        return RadioSet(*buttons, id="time-presets")

    def on_mount(self) -> None:
        self._apply_time_selection(self._time_selection, emit=False)

        # ---- DEFENSIVE LAYOUT (works even if CSS isn't applied) ----
        grid = self.query_one("#query-grid", Container)
        grid.styles.layout = "vertical"
        grid.styles.row_gap = 0
        grid.styles.column_gap = 0

        for row_id in ("query-row", "time-row"):
            row = self.query_one(f"#{row_id}", Container)
            row.styles.layout = "horizontal"
            row.styles.align = ("left", "middle")
            row.styles.margin = 0
            row.styles.padding = 0
            row.styles.width = "1fr"

        row_time = self.query_one("#time-row", Container)
        row_time.styles.align = ("left", "top")

        for labeled_field in self.query("#query-grid LabeledField"):
            labeled_field.styles.width = "1fr"
            labeled_field.styles.height = "auto"
            control_wrapper = labeled_field.query_one(".field-control", Container)
            if labeled_field.id == "time-field":
                labeled_field.styles.width = "auto"
                control_wrapper.styles.width = "auto"
                control_wrapper.styles.height = "auto"
                control_wrapper.styles.min_height = 3
                control_wrapper.styles.padding = (0, 1, 0, 1)
                control_wrapper.styles.align = ("left", "middle")
                control_wrapper.styles.content_align = ("left", "middle")
                control_wrapper.styles.margin_left = 0
                control_wrapper.styles.margin_right = 0
            else:
                control_wrapper.styles.height = 3
                control_wrapper.styles.width = "1fr"

        for field_id in ("auto-scroll-field", "pretty-field"):
            self.query_one(f"#{field_id} .field-control", Container).styles.width = "auto"

        self.query_one("#query-input", Input).styles.width = "1fr"
        self.time_set.styles.width = "auto"
        self.time_set.styles.layout = "horizontal"
        self.time_set.styles.align = ("left", "middle")
        self.time_set.styles.margin_left = 0
        self.time_set.styles.margin_right = 0
        self.query_one("#severity-field SegmentedButtons", SegmentedButtons).styles.width = "1fr"

        actions = self.query_one("#actions-field", Container)
        actions.styles.layout = "horizontal"
        actions.styles.align = ("right", "top")
        actions.styles.margin = 0
        actions.styles.padding = (1, 0, 0, 0)
        actions.styles.height = "auto"
        actions.styles.min_height = 3
        actions.styles.width = "auto"

        advanced = self.query_one("#advanced-button-field", Container)
        advanced.styles.layout = "horizontal"
        advanced.styles.align = ("right", "top")
        advanced.styles.margin = 0
        advanced.styles.padding = (1, 0, 0, 0)
        advanced.styles.height = "auto"
        advanced.styles.min_height = 3
        advanced.styles.width = "auto"
        advanced.styles.margin_right = 1

        auto_scroll_field = self.query_one("#auto-scroll-field", LabeledField)
        auto_scroll_field.styles.width = "auto"
        auto_scroll_field.styles.padding_top = 0
        auto_scroll_field.styles.margin_right = 1

        pretty_field = self.query_one("#pretty-field", LabeledField)
        pretty_field.styles.width = "auto"
        pretty_field.styles.padding_top = 0
        pretty_field.styles.margin_right = 1

        spacer = self.query_one("#time-spacer", Container)
        spacer.styles.width = "1fr"
        spacer.styles.min_width = 0

        time_controls = self.query_one("#time-controls", Container)
        time_controls.styles.layout = "horizontal"
        time_controls.styles.align = ("right", "top")
        time_controls.styles.width = "auto"
        time_controls.styles.height = "auto"

        for button in actions.query("Button"):
            button.styles.height = 3
            button.styles.min_height = 3
            button.styles.margin_left = 1
            button.styles.padding_left = 2
            button.styles.padding_right = 2

        first_button = actions.query_one("Button", expect_type=Button)
        first_button.styles.margin_left = 0

        advanced_button = advanced.query_one("Button", expect_type=Button)
        advanced_button.styles.height = 3
        advanced_button.styles.min_height = 3
        advanced_button.styles.margin_left = 0
        advanced_button.styles.margin_right = 0
        advanced_button.styles.padding_left = 2
        advanced_button.styles.padding_right = 2

        for rb in self.time_set.query("RadioButton"):
            rb.styles.width = "auto"
            rb.styles.min_width = 5
            rb.styles.height = 3

        for btn in self.query_one("#severity-field").query("SegmentedButtons .segment"):
            btn.styles.width = "auto"
            btn.styles.min_width = 6
            btn.styles.height = 3

        self.styles.height = "auto"
        self.styles.overflow_y = "hidden"

    def watch_regex_status(self, status: RegexStatus) -> None:
        query_input = self.query_one("#query-input", Input)
        if status.valid:
            query_input.set_class(False, "-regex-invalid")
            if status.matches is not None:
                query_input.tooltip = f"≈ {status.matches} hits"
            else:
                query_input.tooltip = None
        else:
            query_input.set_class(True, "-regex-invalid")
            query_input.tooltip = status.message or "Invalid regex"

    def get_query_value(self) -> str:
        query_input = self.query_one("#query-input", Input)
        return query_input.value

    def set_query_value(self, value: str) -> None:
        query_input = self.query_one("#query-input", Input)
        query_input.value = value

    def set_pretty_rendering(self, value: bool) -> None:
        toggle = self.query_one("#pretty-structured-toggle", Switch)
        toggle.value = value

    @contextmanager
    def _suppress_time_events_ctx(self):
        """Ignore RadioSet.Changed emitted by programmatic flips, released after a refresh."""
        self._suppress_depth += 1
        self._suppress_time_event = True
        try:
            yield
        finally:
            def _release_once():
                self._suppress_depth = max(0, self._suppress_depth - 1)
                if self._suppress_depth == 0:
                    self._suppress_time_event = False
            if self.app and self.app.is_running:
                self.call_after_refresh(_release_once)
            else:
                _release_once()
    
    def _set_time_radio_exclusive(self, target: str) -> None:
        """Ensure exactly one time radio is checked."""
        button = self._time_buttons.get(target)
        if button is None:
            self._time_selection = target
            return
        with self._suppress_time_events_ctx():
            with self.time_set.prevent(RadioButton.Changed):
                for name, candidate in self._time_buttons.items():
                    candidate.value = name == target
                # Keep RadioSet bookkeeping aligned with the manual flip
                self.time_set._pressed_button = button
                nodes = getattr(self.time_set, "_nodes", [])
                try:
                    index = nodes.index(button)
                except ValueError:
                    index = None
                self.time_set._selected = index
                self._time_focus_value = target
        self._time_selection = target
    
    def _reconcile_time_radios(self) -> None:
        """Force radio visuals to match the canonical self._time_selection."""
        target = self._time_selection
        for name, button in self._time_buttons.items():
            wanted = (name == target)
            if button.value != wanted:
                button.value = wanted

    def _suppress_time_events(self) -> None:
        # Prefer using the context manager. This is a safe, immediate suppress.
        self._suppress_depth += 1
        self._suppress_time_event = True

    def _release_time_event_suppression(self) -> None:
        self._suppress_depth = max(0, self._suppress_depth - 1)
        if self._suppress_depth == 0:
            self._suppress_time_event = False

    def _time_nav_values(self) -> list[str]:
        """Return ordered list of time button identifiers."""
        values = [value for value in self._time_order if value in self._time_buttons]
        if "range" in self._time_buttons:
            values.append("range")
        return values

    def cycle_time_preset(self) -> str:
        if not self._time_order:
            return self._time_selection
        if self._time_selection in self._time_order:
            index = self._time_order.index(self._time_selection)
        else:
            index = -1
        next_value = self._time_order[(index + 1) % len(self._time_order)]
        self.select_time(next_value, emit=True)
        return self._time_selection

    def select_time(
        self,
        value: str,
        *,
        start: str | None = None,
        end: str | None = None,
        emit: bool = False,
    ) -> None:
        """Programmatically select a preset or 'range'."""
        if value in self._time_buttons:
            self._set_time_radio_exclusive(value)
            self._apply_time_selection(value, start=start, end=end, emit=emit)
            return
        # Fallback to first preset if unknown
        if self._time_order:
            fallback = self._time_order[0]
            if fallback in self._time_buttons:
                self._set_time_radio_exclusive(fallback)
                self._apply_time_selection(fallback, emit=emit)
                return
        self._apply_time_selection(value, start=start, end=end, emit=emit)


    def _apply_time_selection(
        self,
        value: str,
        *,
        start: str | None = None,
        end: str | None = None,
        emit: bool = True,
    ) -> None:
        """Set canonical selection + tooltip; emit message if requested."""
        self._time_selection = value

        # Keep 'Custom' tooltip accurate (and persistent when leaving it).
        rb = self._time_buttons.get("range")
        if rb is not None and value == "range":
            rb.tooltip = f"{start} to {end}" if (start and end) else None

        # Double-reconcile to be extra safe against any late synthetic events.
        self._set_time_radio_exclusive(self._time_selection)
        if self.app and self.app.is_running:
            def _after():
                self._set_time_radio_exclusive(self._time_selection)
            self.call_after_refresh(_after)

        if emit:
            self.post_message(self.TimeWindowChanged(value, start=start, end=end))

    def apply_custom_time_range(self, start: str, end: str, *, emit: bool = True) -> None:
        """Public entry from the App after Custom dialog returns (start, end)."""
        if "range" not in self._time_buttons:
            return
        # Swallow the synthetic RadioSet.Changed that may be emitted by set_time
        self._ignore_next_radio_changed = 1
        self.select_time("range", start=start, end=end, emit=emit)

    def set_severity(self, value: str) -> None:
        self.severity_segmented.set_value(value)

    def cycle_severity(self) -> str:
        value = self.severity_segmented.cycle()
        self.post_message(self.SeverityChanged(value))
        return value

    def on_segmented_buttons_value_changed(self, event: SegmentedButtons.ValueChanged) -> None:
        self.post_message(self.SeverityChanged(event.value))

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:  # type: ignore[override]
        if event.control is not self.time_set:
            return
        if self._suppress_time_event:
            return
        if getattr(self, "_ignore_next_radio_changed", 0) > 0:
            self._ignore_next_radio_changed -= 1
            return

        button_id = event.pressed.id or ""
        value = button_id.removeprefix("time-") if button_id.startswith("time-") else self._time_selection

        self._handle_time_button_activation(value)

    def _handle_time_button_activation(self, value: str) -> None:
        if value == "range":
            # If we're already on a custom range, keep its indicator lit so the user
            # can adjust the values without clearing first.
            if self._time_selection != "range":
                prev = self._time_selection
                if prev not in self._time_buttons and self._time_order:
                    prev = self._time_order[0]
                self._set_time_radio_exclusive(prev)
            else:
                self._set_time_radio_exclusive("range")

            # Ask the App to open the custom dialog; do not emit TimeWindowChanged yet.
            self.post_message(self.CustomRangeRequested())
            return

        # Normal preset: apply immediately
        self._set_time_radio_exclusive(value)
        self._apply_time_selection(value, emit=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id in {"add-source", "run-query", "clear-query", "save-session"}:
            self.post_message(self.ActionTriggered(event.button.id))

    def validate_regex(self, sample: Iterable[str]) -> None:
        query = self.get_query_value()
        if not query:
            self.regex_status = RegexStatus(True, "")
            return
        try:
            compiled = re.compile(query)
        except re.error as exc:  # pragma: no cover - defensive
            self.regex_status = RegexStatus(False, str(exc))
            return
        matches = sum(1 for line in sample if compiled.search(line))
        self.regex_status = RegexStatus(True, "", matches=matches)

    class TimeWindowChanged(Message):
        def __init__(self, value: str, *, start: str | None = None, end: str | None = None) -> None:
            super().__init__()
            self.value = value
            self.start = start
            self.end = end

    class SeverityChanged(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class ActionTriggered(Message):
        def __init__(self, action_id: str) -> None:
            super().__init__()
            self.action_id = action_id

    class CustomRangeRequested(Message):
        def __init__(self) -> None:
            super().__init__()

    async def on_key(self, event: events.Key) -> None:
        if event.key in {"left", "right"}:
            if (
                self._navigate_time_buttons(event.key)
                or self._navigate_severity_segments(event.key)
                or self._navigate_action_buttons(event.key)
            ):
                event.stop()
                return

        if event.key in {"enter", "space"} and self._commit_time_focus():
            event.stop()
            return

        if event.key == "enter":
            self.post_message(self.ActionTriggered("run-query"))
        elif event.key == "escape":
            query = self.query_one("#query-input", Input)
            query.value = ""
            self.validate_regex([])
            self.post_message(self.ActionTriggered("clear-query"))

    def _navigate_time_buttons(self, direction_key: str) -> bool:
        if not self.screen:
            return False

        focused = self.screen.focused
        is_time_focus = False
        current_value: str | None = None

        if focused is self.time_set:
            is_time_focus = True
            current_value = self._time_focus_value or self._time_selection
        elif isinstance(focused, RadioButton):
            for value, button in self._time_buttons.items():
                if button is focused:
                    current_value = value
                    is_time_focus = True
                    break

        if not is_time_focus:
            return False

        values = self._time_nav_values()
        if not values:
            return False
        if current_value not in values:
            current_value = values[0]

        step = -1 if direction_key == "left" else 1
        index = values.index(current_value)
        next_index = index + step
        if next_index < 0 or next_index >= len(values):
            return False

        next_value = values[next_index]
        self.time_set.focus()
        self._set_time_nav_focus(next_value)
        return True

    def _set_time_nav_focus(self, value: str | None) -> None:
        self._time_focus_value = value
        nodes = getattr(self.time_set, "_nodes", [])
        index = None
        if value is not None:
            button = self._time_buttons.get(value)
            if button in nodes:
                index = nodes.index(button)
        self.time_set._selected = index

    def _commit_time_focus(self) -> bool:
        if not self.screen:
            return False
        focused = self.screen.focused
        if focused is not self.time_set and not isinstance(focused, RadioButton):
            return False
        target = self._time_focus_value
        if not target or target == self._time_selection:
            return False
        self._handle_time_button_activation(target)
        return True

    def _navigate_severity_segments(self, direction_key: str) -> bool:
        if not self.screen:
            return False
        focused = self.screen.focused
        if focused is None:
            return False
        if focused is self.severity_segmented:
            anchor = self.severity_segmented.value
        elif self.severity_segmented.owns_widget(focused):
            anchor = self.severity_segmented.focused_value
        else:
            return False
        direction = -1 if direction_key == "left" else 1
        return self.severity_segmented.nudge(direction, anchor=anchor)

    def _navigate_action_buttons(self, direction_key: str) -> bool:
        if not self.screen:
            return False
        focused = self.screen.focused
        if not isinstance(focused, Button):
            return False
        button_id = focused.id
        if button_id is None:
            return False
        nav_order = ["toggle-advanced", "add-source", "run-query", "clear-query", "save-session"]
        if button_id not in nav_order:
            return False
        direction = -1 if direction_key == "left" else 1
        index = nav_order.index(button_id)
        next_index = index + direction
        while 0 <= next_index < len(nav_order):
            next_id = nav_order[next_index]
            try:
                target = self.query_one(f"#{next_id}", Button)
            except NoMatches:
                target = None
            if target is not None:
                target.focus()
                return True
            next_index += direction
        return False

    async def on_click(self, event: events.Click) -> None:
        target = event.widget
        if (
            isinstance(target, RadioButton)
            and target is self._time_buttons.get("range")
            and self._time_selection == "range"
        ):
            self.post_message(self.CustomRangeRequested())
