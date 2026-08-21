"""Naming and picking saved views.

Two modals in one module because they are two halves of one interaction and
neither is large: :class:`SaveViewDialog` asks what to call the filters that are
active, and :class:`ViewPickerDialog` chooses among the ones already saved.
Both follow the conventions the earlier dialogs set — one container, its own
``DEFAULT_CSS``, `Esc` cancels, `Enter` confirms.

The picker returns a :class:`ViewRequest` rather than acting: it knows nothing
about `SessionState` and cannot delete anything itself. Rename and delete each
close the modal and the app reopens it, which keeps this widget free of the
"list I am showing is now stale" problem for the price of one repaint.

The ✎ and ✕ markers on a saved view's row in the source tree are entry points
into this same modal — ``focus`` names the view they were clicked on and
``mode`` says which gesture to arrive in the middle of. They do not act where
they are, so there is one implementation of "are you sure" rather than a second
one on a tree row.

Delete is armed the same way :class:`~clv.widgets.export_dialog.ExportDialog`
arms an overwrite: the first press turns the hint into a warning, the second
does it. A stacked confirmation modal would need its own focus restore and its
own region test at 80 columns to say the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..storage import SavedView


@dataclass(frozen=True)
class ViewRequest:
    """What the operator asked the picker to do."""

    action: Literal["apply", "rename", "delete"]
    name: str
    #: Only meaningful for a rename.
    new_name: str = ""


class SaveViewDialog(ModalScreen[str | None]):
    """Ask what to call the current filters."""

    DEFAULT_CSS = """
    SaveViewDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #save-view-dialog {
        width: 100%;
        max-width: 66;
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

    #dialog-hint {
        color: $text-muted;
        height: auto;
        padding-bottom: 1;
    }

    #view-name {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
    }

    #dialog-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    #dialog-actions Button {
        height: 3;
        padding: 0 2;
    }

    #confirm-save-view {
        margin-left: 1;
    }
    """

    def __init__(self, *, default_name: str = "", summary: str = "") -> None:
        super().__init__()
        self._default_name = default_name
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Container(id="save-view-dialog"):
            yield Label("Save view", id="dialog-title")
            yield Static(
                f"Saving: {self._summary}" if self._summary else
                "Saving the filters that are active now.",
                id="dialog-hint",
            )
            yield Input(value=self._default_name, placeholder="Name", id="view-name")
            with Container(id="dialog-actions"):
                yield Button("Cancel", id="cancel-save-view")
                yield Button("Save", id="confirm-save-view", variant="primary")

    def on_mount(self) -> None:
        name_input = self.query_one("#view-name", Input)
        name_input.focus()
        # Selected, not just focused: the generated name is a suggestion, and
        # typing over a suggestion should not have to begin with four
        # backspaces. Enter alone still accepts it.
        name_input.select_all()

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id == "view-name":
            event.stop()
            self._finalize()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "cancel-save-view":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "confirm-save-view":
            event.stop()
            self._finalize()

    def _finalize(self) -> None:
        self.dismiss(self.query_one("#view-name", Input).value.strip() or None)


class ViewPickerDialog(ModalScreen[ViewRequest | None]):
    """Apply, rename or delete a saved view."""

    DEFAULT_CSS = """
    ViewPickerDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #view-picker-dialog {
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

    /* Capped so a long list scrolls rather than pushing the hint and the
       buttons off a 24-row terminal. */
    #view-list {
        height: auto;
        max-height: 8;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    #rename-input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        margin-top: 1;
        display: none;
    }

    #rename-input.-active { display: block; }

    #view-hint {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    #view-hint.-warning {
        color: #facc15;
        text-style: bold;
    }

    #dialog-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    /* WatchRulesDialog's block verbatim, because this row now holds three
       buttons like that one does. `:last-child` spaces only the last, so the
       middle button would sit flush against Close; and a second spelling of an
       action row that has already been laid out and tested at 80 columns is how
       the two drift apart. */
    #dialog-actions Button {
        height: 3;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }
    """

    HINT = "Enter applies · r renames · d deletes · Esc closes"
    RENAME_HINT = "Type a new name, then Enter. Esc goes back to the list."

    def __init__(
        self, views: Sequence[SavedView], *, focus: str = "", mode: str = ""
    ) -> None:
        super().__init__()
        self._views = list(views)
        self._armed_delete = ""
        #: Which view to open on, and which gesture to open in the middle of.
        #: Set when a marker on a tree row asked for this modal rather than the
        #: `v` binding, which opens on the list with nothing chosen.
        self._focus = focus
        self._mode = mode

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="view-picker-dialog"):
            yield Label("Saved views", id="dialog-title")
            # Text(), not markup: a view is named by the operator and a stray
            # "[" in that name is not ours to interpret.
            yield OptionList(
                *(
                    Option(Text(f"{view.name} — {view.summary()}"), id=view.name)
                    for view in self._views
                ),
                id="view-list",
            )
            yield Input(placeholder="New name", id="rename-input")
            yield Static(self.HINT, id="view-hint")
            with Container(id="dialog-actions"):
                yield Button("Close", id="close-views")
                # Between Close and Apply so the primary stays rightmost where
                # it has always been. `error` gives the destructive control its
                # own colour; the arming below is what makes a stray click safe.
                yield Button("Delete", id="delete-view", variant="error")
                yield Button("Apply", id="apply-view", variant="primary")

    def on_mount(self) -> None:
        options = self.query_one("#view-list", OptionList)
        options.focus()
        if not self._focus:
            return
        # A marker names the row it was clicked on, so the modal opens on that
        # view rather than on whatever the list highlights first. Both gestures
        # below read `_current()`, which is why setting the highlight is all
        # they need to arrive mid-way through.
        index = next(
            (i for i, view in enumerate(self._views) if view.name == self._focus), None
        )
        if index is None:  # pragma: no cover - the name came from this list
            return
        options.highlighted = index
        # After the refresh, not now: assigning `highlighted` posts an
        # `OptionHighlighted`, and this modal disarms a pending delete on one —
        # so arming here would be undone by a message already in flight. The
        # callback runs once that has been delivered.
        self.call_after_refresh(self._enter_mode)

    def _enter_mode(self) -> None:
        if self._mode == "rename":
            self._open_rename()
        elif self._mode == "delete":
            self._delete()

    # --- selection ----------------------------------------------------------

    def _current(self) -> SavedView | None:
        index = self.query_one("#view-list", OptionList).highlighted
        if index is None or not (0 <= index < len(self._views)):
            return None
        return self._views[index]

    @property
    def _renaming(self) -> bool:
        try:
            return self.query_one("#rename-input", Input).has_class("-active")
        except NoMatches:  # pragma: no cover - not composed yet
            return False

    def _hint(self, message: str, *, warning: bool = False) -> None:
        hint = self.query_one("#view-hint", Static)
        hint.set_class(warning, "-warning")
        hint.update(message)

    def _disarm(self) -> None:
        if self._armed_delete:
            self._armed_delete = ""
            self._hint(self.HINT)

    # --- events -------------------------------------------------------------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        self._disarm()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._views):
            self.dismiss(ViewRequest("apply", self._views[event.option_index].name))

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id != "rename-input":
            return
        event.stop()
        view = self._current()
        new_name = event.value.strip()
        if view is None or not new_name:
            self._hint("Enter a new name.", warning=True)
            return
        self.dismiss(ViewRequest("rename", view.name, new_name))

    async def on_key(self, event: events.Key) -> None:
        if self._renaming:
            # While renaming, letters are a name — not commands. Only Escape
            # means anything, and it backs out to the list.
            if event.key == "escape":
                event.stop()
                self._close_rename()
            return

        if event.key == "escape":
            event.stop()
            self.dismiss(None)
        elif event.key == "r":
            event.stop()
            self._open_rename()
        elif event.key in ("d", "delete"):
            event.stop()
            self._delete()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "close-views":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "delete-view":
            event.stop()
            # Straight into the same arm-then-confirm the `d` key uses. A
            # button that deleted on its first press would be the one path in
            # this modal that does not ask.
            self._delete()
        elif event.button.id == "apply-view":
            event.stop()
            view = self._current()
            if view is not None:
                self.dismiss(ViewRequest("apply", view.name))

    # --- rename and delete --------------------------------------------------

    def _open_rename(self) -> None:
        view = self._current()
        if view is None:
            return
        self._disarm()
        rename = self.query_one("#rename-input", Input)
        rename.add_class("-active")
        rename.value = view.name
        rename.focus()
        rename.cursor_position = len(rename.value)
        self._hint(self.RENAME_HINT)

    def _close_rename(self) -> None:
        rename = self.query_one("#rename-input", Input)
        rename.remove_class("-active")
        rename.value = ""
        self.query_one("#view-list", OptionList).focus()
        self._hint(self.HINT)

    def _delete(self) -> None:
        view = self._current()
        if view is None:
            return
        if self._armed_delete != view.name:
            self._armed_delete = view.name
            # Names both ways in, because the button arrived after the key and
            # "press d again" is not what a mouse user just did.
            self._hint(
                f"Delete '{view.name}'? Press d or click Delete again.", warning=True
            )
            return
        self.dismiss(ViewRequest("delete", view.name))


__all__ = ["SaveViewDialog", "ViewPickerDialog", "ViewRequest"]
