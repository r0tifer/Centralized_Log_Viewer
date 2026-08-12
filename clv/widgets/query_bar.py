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

from dataclasses import dataclass, replace
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
from ..services.query import parse_query
from .completions import FieldCompletions
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

#: Star button labels. Same width in both states, so toggling a star never
#: reflows the action row; the glyph carries the state.
STAR_OFF = "☆ Star"
STAR_ON = "⭐ Star"

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
    #: 1-based position of the cursor within the matches, when `n`/`N` have put
    #: it on one. None means "we have a total but no position", which is the
    #: state before the cursor has visited a match.
    position: Optional[int] = None


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

    /* The query input takes whatever the sized controls don't need. Severity
       hugs its segments (which size to their labels), so a fixed fr share
       would strand dead space to the right of the pills. margin-left keeps
       the pills off the input rather than butted against it. */
    QueryBar #query-field { width: 1fr; min-width: 20; }
    QueryBar #severity-field { width: auto; margin-left: 2; }
    QueryBar #severity-field .field-control { width: auto; }

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

    /* Sits directly in the row rather than inside a LabeledField, so it starts
       one row higher than its neighbours; the extra top padding drops it onto
       the input's text row. min-width reserves the counter's space so the row
       does not jump when a hit count appears. */
    QueryBar #match-count {
        width: auto;
        min-width: 10;
        height: 4;
        padding: 2 1 0 1;
        color: $text-muted;
        text-align: right;
    }

    QueryBar #match-count.-invalid { color: $error; }

    /* --- time / toggles / actions ---
       Stacked by default and merged onto one line only when the terminal is
       wide enough (the `-merged` class). Progressive enhancement rather than
       the reverse, so the fallback is the layout that always fits. */

    QueryBar #time-row {
        layout: vertical;
        height: auto;
    }

    QueryBar #time-field { width: 1fr; }
    QueryBar #time-field .field-control { width: auto; }

    /* Only meaningful on one line; as a vertical child it would claim a row. */
    QueryBar .qb-spacer { display: none; }

    /* Shown only in the merged layout. Stacked, they would claim a whole row
       for two switches; they stay reachable from the keyboard either way
       (see the toggle_auto_scroll / toggle_structured bindings). */
    QueryBar #toggles {
        layout: horizontal;
        width: auto;
        height: auto;
        align: center top;
        display: none;
    }

    QueryBar.-merged #toggles { display: block; }

    QueryBar #toggles > LabeledField {
        width: auto;
        min-width: 10;
        margin-right: 3;
    }

    QueryBar #toggles > LabeledField:last-child { margin-right: 0; }

    QueryBar #toggles .field-control { width: auto; }

    QueryBar Switch { height: 3; }

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

    /* Starred reads as filled-and-warm; unstarred stays neutral so the two
       are distinguishable by more than the glyph outline. */
    QueryBar #toggle-star.-starred {
        background: $warning 45%;
        text-style: bold;
    }

    /* --- breakpoints ----------------------------------------------------
       DEFAULT_CSS is scoped to this widget, so these key off a class the app
       mirrors onto the QueryBar itself rather than off the app node. --- */

    /* Merged: presets hug left, actions hug right, and the two flexible
       spacers push the toggles into the middle. Applied only above the merge
       width, because all three together need more room than -wide guarantees. */
    QueryBar.-merged #time-row { layout: horizontal; }

    QueryBar.-merged .qb-spacer {
        display: block;
        width: 1fr;
        min-width: 0;
        height: 1;
    }

    QueryBar.-merged #time-field { width: auto; }

    /* Sharing the row with LabeledFields, whose label occupies the first line,
       so the buttons need that line too or they ride one row high. */
    QueryBar.-merged #actions {
        width: auto;
        padding-top: 1;
    }

    /* Compact: query and severity stack so neither is squeezed to nothing,
       and the labels go to buy back vertical rows. */
    QueryBar.-compact #query-row {
        layout: vertical;
        height: auto;
    }

    /* Stacked, so neither needs to reserve width for the other and the
       severity indent would only push the pills off-centre. */
    QueryBar.-compact #query-field,
    QueryBar.-compact #severity-field {
        width: 1fr;
        margin-right: 0;
        margin-left: 0;
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
        self.completions = FieldCompletions(id="field-completions")
        # Canonical selection, kept here so "Custom" can be shown as active
        # while the dialog is open without trusting widget state.
        self._time_selection = "all"
        #: Field names the current source reports, offered as completions. The
        #: app owns the list; this is a display copy.
        self._field_names: tuple[str, ...] = ()

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="query-row", classes="qb-row"):
            yield LabeledField(
                "Query",
                Input(
                    placeholder="regex or field:value — error|timeout, host:web01, status>=500",
                    id="query-input",
                ),
                id="query-field",
            )
            yield Static("", id="match-count")
            yield LabeledField("Severity", self.severity_segmented, id="severity-field")

        # Costs no rows until it has something to offer: hidden by its own
        # class, not by anything this bar has to remember to do.
        yield self.completions

        # Time presets, toggles and actions share one row when there is width
        # for it: presets left, toggles centred between the flexible spacers,
        # actions right. Below the merge breakpoint the row switches to a
        # vertical layout and they stack, which is how they used to be laid
        # out permanently.
        with Horizontal(id="time-row", classes="qb-row"):
            yield LabeledField("Time", self.time_segmented, id="time-field")
            yield Static("", classes="qb-spacer")
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
            yield Static("", classes="qb-spacer")
            with Container(id="actions"):
                yield Button("Advanced", id="toggle-advanced", variant="warning")
                yield Button("Add Source", id="add-source", variant="success")
                yield Button(STAR_OFF, id="toggle-star")
                yield Button("Run", id="run-query", variant="primary")
                yield Button("Clear", id="clear-query", variant="error")
                yield Button("Save", id="save-session", variant="success")

    # --- query --------------------------------------------------------------

    def get_query_value(self) -> str:
        return self.query_one("#query-input", Input).value

    def set_query_value(self, value: str) -> None:
        self.query_one("#query-input", Input).value = value
        # Whatever was being completed is no longer what is in the box.
        self.completions.close()

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

    def validate_entries(self, entries, known_fields: Iterable[str] = ()) -> None:
        """Variant of :meth:`validate_regex` for already-parsed entries.

        Field terms are split off before compiling, so the hit count reflects
        the same thing the pane shows and a malformed term reports here rather
        than raising — the path Item 8 asked for.
        """

        query = self.get_query_value()
        if not query:
            self.regex_status = RegexStatus(True)
            return
        try:
            parsed = parse_query(query, known_fields)
            pattern = compile_query(parsed.text)
        except QueryError as exc:
            self.regex_status = RegexStatus(False, str(exc))
            return
        self.regex_status = RegexStatus(
            True, matches=count_matches(entries, pattern, parsed.terms)
        )

    # --- field completions --------------------------------------------------

    def set_field_names(self, names: Iterable[str]) -> None:
        """Tell the bar which field names this source reports."""

        updated = tuple(sorted(names))
        if updated == self._field_names:
            return
        self._field_names = updated
        self._refresh_completions()

    def _query_input(self) -> Input:
        return self.query_one("#query-input", Input)

    @staticmethod
    def token_bounds(text: str, caret: int) -> tuple[int, int]:
        """Start and end of the whitespace-delimited token holding *caret*."""

        caret = max(0, min(caret, len(text)))
        start = caret
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        end = caret
        while end < len(text) and not text[end].isspace():
            end += 1
        return start, end

    def _partial_key(self) -> str:
        """The token at the caret, when it could still become a field name.

        Anything with an operator in it is already a term (or a regex), and a
        completion would be an interruption rather than help.
        """

        try:
            query_input = self._query_input()
        except NoMatches:
            return ""
        start, end = self.token_bounds(query_input.value, query_input.cursor_position)
        token = query_input.value[start:end]
        if not token or any(char in token for char in ":=<>"):
            return ""
        return token

    def _refresh_completions(self) -> None:
        token = self._partial_key()
        if not token:
            self.completions.close()
            return
        folded = token.lower()
        matches = [
            name
            for name in self._field_names
            if name.lower().startswith(folded) and name.lower() != folded
        ]
        self.completions.offer(matches)

    def _accept_completion(self, name: str) -> None:
        """Replace the token at the caret with ``name:`` and carry on typing."""

        query_input = self._query_input()
        start, end = self.token_bounds(query_input.value, query_input.cursor_position)
        query_input.value = f"{query_input.value[:start]}{name}:{query_input.value[end:]}"
        query_input.cursor_position = start + len(name) + 1
        self.completions.close()
        query_input.focus()

    def on_field_completions_accepted(self, event: FieldCompletions.Accepted) -> None:
        event.stop()
        self._accept_completion(event.name)

    def on_field_completions_dismissed(self, event: FieldCompletions.Dismissed) -> None:
        event.stop()
        self.completions.close()
        try:
            self._query_input().focus()
        except NoMatches:  # pragma: no cover - defensive
            pass

    def set_match_position(self, position: Optional[int]) -> None:
        """Show where the cursor sits within the hits, as `n`/`N` move it.

        Only the position changes: recounting here would disagree with the
        count the app just navigated over. Dropped whenever the count itself is
        recomputed, because a position into a stale result set is worse than no
        position at all.
        """

        status = self.regex_status
        if status.matches is None or status.position == position:
            return
        self.regex_status = replace(status, position=position)

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
        elif status.position is None:
            counter.update(f"{status.matches} hits")
        else:
            counter.update(f"{status.position} of {status.matches} hits")

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

    def set_star_state(self, starred: Optional[bool]) -> None:
        """Reflect whether the star target is starred.

        ``None`` means there is nothing to star, which disables the button
        rather than leaving it looking usable.
        """

        try:
            button = self.query_one("#toggle-star", Button)
        except NoMatches:
            return
        button.disabled = starred is None
        button.label = STAR_ON if starred else STAR_OFF
        button.set_class(bool(starred), "-starred")
        button.tooltip = (
            "Select a log to star" if starred is None
            else "Unstar this log (*)" if starred
            else "Star this log (*)"
        )

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
        if event.button.id in {
            "add-source",
            "toggle-star",
            "run-query",
            "clear-query",
            "save-session",
        }:
            self.post_message(self.ActionTriggered(event.button.id))

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[override]
        # Deliberately *not* stopped: the app filters on this message. The bar
        # only listens in so the completion list can follow what is typed.
        if event.input.id == "query-input":
            self._refresh_completions()

    def on_descendant_blur(self, _event) -> None:
        """Close the dropdown when focus leaves the query controls entirely.

        Checked after a refresh because a blur says only what lost focus, not
        what gained it — and moving from the input *into* the list must not
        close the thing being moved into.
        """

        if not self.completions.open:
            return

        def _close_if_focus_left() -> None:
            try:
                focused = self.screen.focused
            except Exception:  # noqa: BLE001 - no screen while unmounting
                return
            if focused is not None and focused in (self.completions, *self.query("#query-input")):
                return
            self.completions.close()

        self.call_after_refresh(_close_if_focus_left)

    async def on_key(self, event: events.Key) -> None:
        # Enter in the query box applies; Escape clears it. Arrow keys are
        # handled by the segmented controls themselves.
        if self.completions.open and event.key in ("down", "tab", "escape"):
            query_input = self.query("#query-input").first(Input)
            if query_input.has_focus:
                event.stop()
                event.prevent_default()
                # Down steps into the list to browse it; Tab takes the first
                # candidate outright, which is what a shell has trained
                # everyone to expect; Escape shuts the dropdown *before* it
                # means "clear the query", so dismissing a suggestion never
                # throws away what was being typed.
                if event.key == "down":
                    self.completions.focus()
                elif event.key == "tab":
                    self.completions.accept_highlighted()
                else:
                    self.completions.close()
                return
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
