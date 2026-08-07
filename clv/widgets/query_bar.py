"""Query and filter controls.

Layout note: every horizontal group in here is built from ``1fr`` children, so
the bar divides the width it is given instead of demanding a fixed amount and
pushing controls off-screen. The time presets in particular are a
:class:`SegmentedButtons` group rather than a ``RadioSet`` — a RadioSet of six
bordered buttons wanted ~95 columns and shipped the action buttons past the
right edge on any terminal narrower than a very wide one.

Styling lives entirely in ``DEFAULT_CSS``. Nothing here sets ``.styles.*`` at
runtime; the breakpoint classes on the app (``-compact``/``-narrow``) are what
adapt the layout, and they are plain CSS selectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static, Switch

from ..services.filtering import QueryError, compile_query, count_matches
from .segmented import SegmentedButtons

#: Time presets, in cycle order. "range" opens the custom range dialog.
TIME_PRESETS: list[tuple[str, str]] = [
    ("all", "All"),
    ("15m", "15m"),
    ("1h", "1h"),
    ("6h", "6h"),
    ("24h", "24h"),
    ("range", "Custom"),
]

#: Severity buckets exposed in the UI, matching services.parsing.SEVERITY_BUCKETS.
SEVERITY_OPTIONS: list[tuple[str, str]] = [
    ("all", "All"),
    ("debug", "Debug"),
    ("info", "Info"),
    ("warn", "Warn"),
    ("error", "Error"),
]


@dataclass(frozen=True)
class RegexStatus:
    valid: bool
    message: str = ""
    matches: Optional[int] = None


class LabeledField(Static):
    """A label stacked above a control. Sizes to its parent, never fixed."""

    DEFAULT_CSS = """
    LabeledField {
        layout: vertical;
        width: 1fr;
        height: auto;
    }

    LabeledField > .field-label {
        color: $text-muted;
        height: 1;
    }

    LabeledField > .field-control {
        height: 3;
        width: 1fr;
    }
    """

    def __init__(self, label: str, control: Widget, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._label = Label(label, classes="field-label")
        self._control_wrapper = Container(control, classes="field-control")

    def compose(self) -> ComposeResult:
        yield self._label
        yield self._control_wrapper


class QueryBar(Container):
    """Top band: query, severity, time window, toggles and actions."""

    DEFAULT_CSS = """
    QueryBar {
        layout: vertical;
        height: auto;
        padding: 0 2;
        background: $surface 5%;
        border-bottom: solid $surface 25%;
    }

    QueryBar .qb-row {
        layout: horizontal;
        height: auto;
        width: 1fr;
        align: left top;
    }

    QueryBar .qb-row > * {
        margin-right: 1;
    }

    QueryBar .qb-row > *:last-child {
        margin-right: 0;
    }

    /* --- query row --- */

    QueryBar #query-field { width: 2fr; }
    QueryBar #severity-field { width: 3fr; }

    QueryBar Input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        width: 1fr;
    }

    QueryBar Input.-regex-invalid {
        border: tall $error;
        background: $error 10%;
    }

    QueryBar #match-count {
        width: auto;
        min-width: 10;
        height: 3;
        padding: 1 1 0 1;
        color: $text-muted;
        text-align: right;
    }

    QueryBar #match-count.-invalid { color: $error; }

    /* --- time + toggles row --- */

    QueryBar #time-field { width: 3fr; }

    QueryBar #toggles {
        layout: horizontal;
        width: auto;
        height: auto;
        align: right top;
    }

    QueryBar #toggles > LabeledField {
        width: auto;
        min-width: 10;
    }

    QueryBar #toggles .field-control { width: auto; }

    QueryBar Switch { height: 3; }

    /* --- actions row: on its own line, so nothing competes with the presets
       for horizontal space. This is what keeps the buttons on screen. --- */

    QueryBar #actions {
        layout: horizontal;
        width: 1fr;
        height: auto;
        align: right middle;
        padding-bottom: 1;
    }

    QueryBar #actions Button {
        height: 3;
        min-height: 3;
        width: auto;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }

    QueryBar #actions Button:first-child { margin-left: 0; }

    /* --- breakpoints ----------------------------------------------------
       DEFAULT_CSS is scoped to this widget, so these key off a class the app
       mirrors onto the QueryBar itself rather than off the app node. --- */

    /* Narrow: toggles move into the Advanced drawer, freeing a whole row. */
    QueryBar.-narrow #toggles,
    QueryBar.-compact #toggles {
        display: none;
    }

    /* Compact: query and severity stack so neither is squeezed to nothing,
       and the labels go to buy back vertical rows. */
    QueryBar.-compact #query-row {
        layout: vertical;
        height: auto;
    }

    QueryBar.-compact #query-field,
    QueryBar.-compact #severity-field {
        width: 1fr;
        margin-right: 0;
    }

    QueryBar.-compact #match-count { display: none; }
    QueryBar.-compact .field-label { display: none; }

    QueryBar.-compact #actions Button {
        min-width: 5;
        padding: 0 1;
    }
    """

    regex_status: reactive[RegexStatus] = reactive(RegexStatus(True))

    def __init__(self) -> None:
        super().__init__(id="query-bar")
        self.severity_segmented = SegmentedButtons(SEVERITY_OPTIONS, id="severity-segments")
        self.time_segmented = SegmentedButtons(TIME_PRESETS, id="time-segments")
        # Canonical selection, kept here so "Custom" can be shown as active
        # while the dialog is open without trusting widget state.
        self._time_selection = "all"

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="query-row", classes="qb-row"):
            yield LabeledField(
                "Query",
                Input(placeholder="regex — try: error|timeout", id="query-input"),
                id="query-field",
            )
            yield Static("", id="match-count")
            yield LabeledField("Severity", self.severity_segmented, id="severity-field")

        with Horizontal(id="time-row", classes="qb-row"):
            yield LabeledField("Time", self.time_segmented, id="time-field")
            with Container(id="toggles"):
                yield LabeledField(
                    "Auto-scroll",
                    Switch(value=True, id="auto-scroll-toggle"),
                    id="auto-scroll-field",
                )
                yield LabeledField(
                    "Structured",
                    Switch(value=False, id="pretty-structured-toggle"),
                    id="pretty-field",
                )

        with Horizontal(id="actions"):
            yield Button("Advanced", id="toggle-advanced", variant="warning")
            yield Button("Add Source", id="add-source", variant="success")
            yield Button("Run", id="run-query", variant="primary")
            yield Button("Clear", id="clear-query", variant="error")
            yield Button("Save", id="save-session", variant="success")

    # --- query --------------------------------------------------------------

    def get_query_value(self) -> str:
        return self.query_one("#query-input", Input).value

    def set_query_value(self, value: str) -> None:
        self.query_one("#query-input", Input).value = value

    def validate_regex(self, sample: Iterable[str]) -> None:
        """Recompute regex validity and the approximate hit count."""

        query = self.get_query_value()
        if not query:
            self.regex_status = RegexStatus(True)
            return
        try:
            pattern = compile_query(query)
        except QueryError as exc:
            self.regex_status = RegexStatus(False, str(exc))
            return
        matches = sum(1 for line in sample if pattern is not None and pattern.search(line))
        self.regex_status = RegexStatus(True, matches=matches)

    def validate_entries(self, entries) -> None:
        """Variant of :meth:`validate_regex` for already-parsed entries."""

        query = self.get_query_value()
        if not query:
            self.regex_status = RegexStatus(True)
            return
        try:
            pattern = compile_query(query)
        except QueryError as exc:
            self.regex_status = RegexStatus(False, str(exc))
            return
        self.regex_status = RegexStatus(True, matches=count_matches(entries, pattern))

    def watch_regex_status(self, status: RegexStatus) -> None:
        try:
            query_input = self.query_one("#query-input", Input)
            counter = self.query_one("#match-count", Static)
        except NoMatches:  # not mounted yet
            return

        query_input.set_class(not status.valid, "-regex-invalid")
        counter.set_class(not status.valid, "-invalid")

        if not status.valid:
            query_input.tooltip = status.message or "Invalid regex"
            counter.update("invalid")
            return

        query_input.tooltip = None
        if status.matches is None:
            counter.update("")
        else:
            counter.update(f"{status.matches} hits")

    # --- severity -----------------------------------------------------------

    def set_severity(self, value: str) -> None:
        self.severity_segmented.set_value(value)

    def cycle_severity(self) -> str:
        value = self.severity_segmented.cycle()
        self.post_message(self.SeverityChanged(value))
        return value

    # --- time ---------------------------------------------------------------

    @property
    def time_selection(self) -> str:
        return self._time_selection

    def select_time(self, value: str, *, emit: bool = False) -> None:
        """Programmatically select a preset (no dialog, no side effects)."""

        if value not in self.time_segmented.values:
            value = "all"
        self._time_selection = value
        self.time_segmented.set_value(value)
        if emit:
            self.post_message(self.TimeWindowChanged(value))

    def cycle_time_preset(self) -> str:
        """Advance through the presets, skipping Custom (it needs a dialog)."""

        cycleable = [value for value, _ in TIME_PRESETS if value != "range"]
        try:
            index = cycleable.index(self._time_selection)
        except ValueError:
            index = -1
        next_value = cycleable[(index + 1) % len(cycleable)]
        self.select_time(next_value, emit=True)
        return next_value

    def apply_custom_time_range(self, start: str, end: str, *, emit: bool = True) -> None:
        """Called by the app once the custom range dialog returns a range."""

        self._time_selection = "range"
        self.time_segmented.set_value("range")
        self.time_segmented.set_segment_tooltip("range", f"{start} → {end}")
        if emit:
            self.post_message(self.TimeWindowChanged("range", start=start, end=end))

    def restore_time_selection(self) -> None:
        """Re-assert the canonical selection after a cancelled dialog."""
        self.time_segmented.set_value(self._time_selection)

    # --- toggles ------------------------------------------------------------

    def set_pretty_rendering(self, value: bool) -> None:
        self.query_one("#pretty-structured-toggle", Switch).value = value

    def set_auto_scroll(self, value: bool) -> None:
        self.query_one("#auto-scroll-toggle", Switch).value = value

    # --- events -------------------------------------------------------------

    def on_segmented_buttons_value_changed(self, event: SegmentedButtons.ValueChanged) -> None:
        if event.control is self.severity_segmented:
            self.post_message(self.SeverityChanged(event.value))
            return
        if event.control is self.time_segmented:
            if event.value == "range":
                # Don't commit the window yet; the dialog decides the bounds.
                self.post_message(self.CustomRangeRequested())
                return
            self._time_selection = event.value
            self.post_message(self.TimeWindowChanged(event.value))

    def on_segmented_buttons_reselected(self, event: SegmentedButtons.Reselected) -> None:
        # Clicking "Custom" while it is already active reopens the dialog so a
        # range can be adjusted without clearing it first.
        if event.control is self.time_segmented and event.value == "range":
            self.post_message(self.CustomRangeRequested())

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id in {"add-source", "run-query", "clear-query", "save-session"}:
            self.post_message(self.ActionTriggered(event.button.id))

    async def on_key(self, event: events.Key) -> None:
        # Enter in the query box applies; Escape clears it. Arrow keys are
        # handled by the segmented controls themselves.
        if event.key == "enter":
            self.post_message(self.ActionTriggered("run-query"))
        elif event.key == "escape":
            self.post_message(self.ActionTriggered("clear-query"))

    # --- messages -----------------------------------------------------------

    class TimeWindowChanged(Message):
        """The active time window changed."""

        def __init__(self, value: str, *, start: str | None = None, end: str | None = None) -> None:
            super().__init__()
            self.value = value
            self.start = start
            self.end = end

    class SeverityChanged(Message):
        """The severity bucket changed."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class ActionTriggered(Message):
        """An action button (or its keyboard equivalent) fired."""

        def __init__(self, action_id: str) -> None:
            super().__init__()
            self.action_id = action_id

    class CustomRangeRequested(Message):
        """The user asked for the custom time range dialog."""
