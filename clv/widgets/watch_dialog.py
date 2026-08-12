"""Managing watch rules.

One modal for the whole lifecycle: add, edit, enable, disable, delete. Unlike
the view picker it *does* hold the working list, because editing a rule is a
form and reopening the dialog between every keystroke of a pattern would be
absurd. It still owns no application state — the list goes in as rules and
comes back as rules, and the app decides what that means.

Layout is two stacked halves: the rule list on top, an editor below that is
hidden until something is being added or changed. That keeps the whole thing
inside 24 rows at 80 columns, which a side-by-side arrangement could not.

Conventions are the ones the earlier dialogs set: one container, its own
``DEFAULT_CSS``, `Esc` backs out, and a delete is armed by a first press and
done by a second rather than by stacking another modal.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..services.watch import ACTION_BOTH, ACTIONS, WatchRule, validate_pattern

#: Cycled through by the editor's Action button, in this order.
ACTION_LABELS = {
    "highlight": "Highlight only",
    "notify": "Notify only",
    "both": "Highlight + notify",
}


class WatchRulesDialog(ModalScreen[tuple[WatchRule, ...] | None]):
    """Add, edit, enable, disable and delete watch rules."""

    DEFAULT_CSS = """
    WatchRulesDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #watch-dialog {
        width: 100%;
        max-width: 76;
        height: auto;
        padding: 1 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    #dialog-title {
        text-style: bold;
        height: 1;
    }

    /* Five rows, not more: with the editor open the whole dialog has to stay
       inside 24, and the list is the part that can afford to scroll. */
    #watch-list {
        height: auto;
        max-height: 5;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    #watch-editor {
        layout: vertical;
        height: auto;
        display: none;
    }

    #watch-editor.-active { display: block; }

    /* Name and pattern share a row. Stacked they cost four more rows than a
       24-row terminal has to give once the list and the buttons are counted. */
    #watch-fields {
        layout: horizontal;
        height: auto;
        width: 1fr;
    }

    #watch-editor .editor-field {
        layout: vertical;
        width: 1fr;
        height: auto;
        margin-right: 2;
    }

    #watch-editor .editor-field:last-child { margin-right: 0; }

    #watch-editor Input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        width: 1fr;
    }

    #watch-editor .editor-label {
        color: $text-muted;
        height: 1;
    }

    #watch-action {
        height: 3;
        width: auto;
        margin-top: 1;
    }

    #watch-hint {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    #watch-hint.-warning {
        color: #facc15;
        text-style: bold;
    }

    #dialog-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    #dialog-actions Button {
        height: 3;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }
    """

    HINT = "a adds · Enter edits · space enables/disables · d deletes · Esc closes"
    EDIT_HINT = "Pattern uses the query syntax. Enter saves · Esc goes back."

    def __init__(
        self, rules: Sequence[WatchRule] = (), known_fields: Iterable[str] = ()
    ) -> None:
        super().__init__()
        self._rules = list(rules)
        self._known_fields = frozenset(known_fields)
        self._editing: str | None = None
        self._action = ACTION_BOTH
        self._armed_delete = ""
        self._dirty = False

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="watch-dialog"):
            yield Label("Watch rules", id="dialog-title")
            yield OptionList(id="watch-list")
            with Container(id="watch-editor"):
                with Horizontal(id="watch-fields"):
                    with Container(classes="editor-field"):
                        yield Label("Name", classes="editor-label")
                        yield Input(placeholder="oom", id="watch-name")
                    with Container(classes="editor-field"):
                        yield Label("Pattern", classes="editor-label")
                        yield Input(
                            placeholder="oom-killer | tag:kernel", id="watch-pattern"
                        )
                yield Button(
                    ACTION_LABELS[ACTION_BOTH], id="watch-action", variant="primary"
                )
            yield Static(self.HINT, id="watch-hint")
            with Container(id="dialog-actions"):
                yield Button("Close", id="close-watch")
                yield Button("Add", id="add-watch", variant="success")
                yield Button("Save", id="save-watch", variant="primary")

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one("#watch-list", OptionList).focus()

    # --- the list -----------------------------------------------------------

    def _refresh_list(self, highlight: int | None = None) -> None:
        option_list = self.query_one("#watch-list", OptionList)
        current = highlight if highlight is not None else option_list.highlighted
        option_list.clear_options()
        if not self._rules:
            # Left enabled deliberately. A disabled OptionList cannot take
            # focus, and with nothing else focusable the `a` that adds the
            # first rule would never reach this screen's key handler.
            option_list.add_option(
                Option(Text("No rules yet — press a to add one."), id="-")
            )
            return
        # Text(), not markup: the name and the pattern are the operator's, and
        # a "[" in a regex is not ours to interpret.
        option_list.add_options(
            [
                Option(
                    Text(
                        f"{'[on] ' if rule.enabled else '[off]'} {rule.name} — "
                        f"{rule.pattern}  ({ACTION_LABELS[rule.action]})"
                    ),
                    id=rule.name,
                )
                for rule in self._rules
            ]
        )
        if current is not None and self._rules:
            option_list.highlighted = min(current, len(self._rules) - 1)

    def _current(self) -> WatchRule | None:
        if not self._rules:
            return None
        index = self.query_one("#watch-list", OptionList).highlighted
        if index is None or not (0 <= index < len(self._rules)):
            return None
        return self._rules[index]

    def _hint(self, message: str, *, warning: bool = False) -> None:
        hint = self.query_one("#watch-hint", Static)
        hint.set_class(warning, "-warning")
        hint.update(message)

    def _disarm(self) -> None:
        if self._armed_delete:
            self._armed_delete = ""
            self._hint(self.HINT)

    # --- the editor ---------------------------------------------------------

    @property
    def editing(self) -> bool:
        try:
            return self.query_one("#watch-editor", Container).has_class("-active")
        except NoMatches:  # pragma: no cover - not composed yet
            return False

    def _open_editor(self, rule: WatchRule | None) -> None:
        self._disarm()
        self._editing = rule.name if rule else None
        self._action = rule.action if rule else ACTION_BOTH
        self.query_one("#watch-editor", Container).add_class("-active")
        self.query_one("#watch-action", Button).label = ACTION_LABELS[self._action]
        name = self.query_one("#watch-name", Input)
        pattern = self.query_one("#watch-pattern", Input)
        name.value = rule.name if rule else ""
        pattern.value = rule.pattern if rule else ""
        self._hint(self.EDIT_HINT)
        name.focus()

    def _close_editor(self) -> None:
        self.query_one("#watch-editor", Container).remove_class("-active")
        self._editing = None
        self.query_one("#watch-list", OptionList).focus()
        self._hint(self.HINT)

    def _save_editor(self) -> None:
        name = self.query_one("#watch-name", Input).value.strip()
        pattern = self.query_one("#watch-pattern", Input).value.strip()
        if not name:
            self._hint("Give the rule a name.", warning=True)
            return
        problem = validate_pattern(pattern, self._known_fields)
        if problem is not None:
            # Reported where it was typed. A rule that silently never matches
            # is indistinguishable from one that is simply not firing yet.
            self._hint(problem, warning=True)
            return

        enabled = True
        replacing = self._editing
        if replacing is not None:
            existing = next((r for r in self._rules if r.name == replacing), None)
            if existing is not None:
                enabled = existing.enabled
        rule = WatchRule(name=name, pattern=pattern, action=self._action, enabled=enabled)
        self._rules = [
            other for other in self._rules if other.name not in {replacing, name}
        ] + [rule]
        self._rules.sort(key=lambda item: item.name.lower())
        self._dirty = True
        self._close_editor()
        self._refresh_list(self._rules.index(rule))

    # --- events -------------------------------------------------------------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        self._disarm()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        rule = self._current()
        if rule is not None:
            self._open_editor(rule)

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id in ("watch-name", "watch-pattern"):
            event.stop()
            self._save_editor()

    async def on_key(self, event: events.Key) -> None:
        if self.editing:
            if event.key == "escape":
                event.stop()
                self._close_editor()
            return

        if event.key == "escape":
            event.stop()
            self._finish()
        elif event.key == "a":
            event.stop()
            self._open_editor(None)
        elif event.key == "space":
            event.stop()
            self._toggle_current()
        elif event.key in ("d", "delete"):
            event.stop()
            self._delete_current()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        event.stop()
        if event.button.id == "close-watch":
            self._finish()
        elif event.button.id == "add-watch":
            self._open_editor(None)
        elif event.button.id == "save-watch":
            if self.editing:
                self._save_editor()
            else:
                self._finish()
        elif event.button.id == "watch-action":
            self._cycle_action()

    def _cycle_action(self) -> None:
        self._action = ACTIONS[(ACTIONS.index(self._action) + 1) % len(ACTIONS)]
        self.query_one("#watch-action", Button).label = ACTION_LABELS[self._action]

    def _toggle_current(self) -> None:
        rule = self._current()
        if rule is None:
            return
        index = self._rules.index(rule)
        self._rules[index] = WatchRule(
            name=rule.name,
            pattern=rule.pattern,
            action=rule.action,
            enabled=not rule.enabled,
        )
        self._dirty = True
        self._refresh_list(index)

    def _delete_current(self) -> None:
        rule = self._current()
        if rule is None:
            return
        if self._armed_delete != rule.name:
            self._armed_delete = rule.name
            self._hint(f"Delete '{rule.name}'? Press d again.", warning=True)
            return
        index = self._rules.index(rule)
        self._rules = [other for other in self._rules if other.name != rule.name]
        self._armed_delete = ""
        self._dirty = True
        self._refresh_list(max(0, index - 1))
        self._hint(self.HINT)

    def _finish(self) -> None:
        # None means "nothing changed", so the app can skip re-indexing every
        # buffered line for a dialog that was only looked at.
        self.dismiss(tuple(self._rules) if self._dirty else None)


__all__ = ["ACTION_LABELS", "WatchRulesDialog"]
