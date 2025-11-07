from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from textual import events
from textual.app import ComposeResult
from textual.containers import Container
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
        self._suppress_time_event = False
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

    def select_time(self, value: str, *, emit: bool = False) -> None:
        if value in self._time_buttons:
            if value != self._time_selection:
                self._suppress_time_event = True
                self._time_buttons[value].value = True
                self._suppress_time_event = False
            self._apply_time_selection(value, emit=emit)
            return
        if self._time_order:
            fallback = self._time_order[0]
            if fallback in self._time_buttons:
                self._suppress_time_event = True
                self._time_buttons[fallback].value = True
                self._suppress_time_event = False
                self._apply_time_selection(fallback, emit=emit)
                return
        self._apply_time_selection(value, emit=emit)

    def _apply_time_selection(
        self,
        value: str,
        *,
        start: str | None = None,
        end: str | None = None,
        emit: bool = True,
    ) -> None:
        self._time_selection = value
        range_button = self._time_buttons.get("range")
        if value == "range" and range_button is not None:
            if start and end:
                range_button.tooltip = f"{start} to {end}"
            else:
                range_button.tooltip = None
        elif range_button is not None and value != "range":
            # keep tooltip so users can see the last custom selection
            pass
        if emit:
            self.post_message(self.TimeWindowChanged(value, start=start, end=end))

    def apply_custom_time_range(self, start: str, end: str, *, emit: bool = True) -> None:
        range_button = self._time_buttons.get("range")
        if range_button is None:
            return
        self._suppress_time_event = True
        try:
            range_button.value = True
        finally:
            self._suppress_time_event = False
        range_button.tooltip = f"{start} to {end}"
        self._apply_time_selection("range", start=start, end=end, emit=emit)

    def set_severity(self, value: str) -> None:
        self.severity_segmented.set_value(value)

    def cycle_severity(self) -> str:
        value = self.severity_segmented.cycle()
        self.post_message(self.SeverityChanged(value))
        return value

    def on_segmented_buttons_value_changed(self, event: SegmentedButtons.ValueChanged) -> None:
        self.post_message(self.SeverityChanged(event.value))

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:  # type: ignore[override]
        if self._suppress_time_event:
            self._suppress_time_event = False
            return
        if event.control is not self.time_set:
            return
        button_id = event.pressed.id or ""
        if button_id.startswith("time-"):
            value = button_id.removeprefix("time-")
        else:
            value = self._time_selection
        if value == "range":
            previous = self._time_selection
            if previous in self._time_buttons:
                self._suppress_time_event = True
                try:
                    self._time_buttons[previous].value = True
                finally:
                    self._suppress_time_event = False
            elif self._time_order:
                fallback = self._time_order[0]
                if fallback in self._time_buttons:
                    self._suppress_time_event = True
                    try:
                        self._time_buttons[fallback].value = True
                    finally:
                        self._suppress_time_event = False
            self.post_message(self.CustomRangeRequested())
            return
        self._apply_time_selection(value)

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
        if event.key == "enter":
            self.post_message(self.ActionTriggered("run-query"))
        elif event.key == "escape":
            query = self.query_one("#query-input", Input)
            query.value = ""
            self.validate_regex([])
            self.post_message(self.ActionTriggered("clear-query"))
