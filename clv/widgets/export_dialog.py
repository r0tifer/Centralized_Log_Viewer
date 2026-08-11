"""Modal for writing the filtered view to a file.

Modelled on :class:`~clv.widgets.add_source_dialog.AddSourceDialog`: one
container, its own ``DEFAULT_CSS``, `Esc` cancels, `Enter` confirms.

Two things it does that a plain path prompt does not:

* It states the number of entries it is about to write, because "export the
  view" is ambiguous the moment a filter is active and an operator should not
  have to guess whether the count is the buffer or the filtered set.
* Overwriting an existing file takes a second press of Export. The first press
  turns the hint into a warning and arms the button; editing the path or
  changing format disarms it again. This keeps the confirmation inside one
  modal — a second stacked modal would need its own focus restore and its own
  region test at 80 columns for no gain.

The dialog knows nothing about exporters. The app hands it a list of
:class:`ExportChoice` (built-ins plus whatever the plugin registry supplied) and
gets back an :class:`ExportRequest`, so this widget never imports ``clv.app`` or
the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class ExportChoice:
    """One row of the format list, as the app describes it."""

    key: str
    label: str
    #: Suffix the default filename gets. Empty when the choice supplies no path.
    extension: str = ""
    #: False for a plugin exporter: :meth:`clv.plugins.Exporter.export` is handed
    #: no destination, so it chooses its own and the path input does not apply.
    needs_path: bool = True


@dataclass(frozen=True)
class ExportRequest:
    """What the operator asked for. ``path`` is None for a self-routing exporter."""

    key: str
    path: Path | None


class ExportDialog(ModalScreen[ExportRequest | None]):
    """Pick a format and a destination for the entries currently filtered in."""

    DEFAULT_CSS = """
    ExportDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #export-dialog {
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

    #export-count {
        color: $text-muted;
        height: auto;
        padding-bottom: 1;
    }

    /* Capped so a long list of plugin exporters scrolls instead of pushing the
       path input and the buttons off a 24-row terminal. */
    #export-format {
        height: auto;
        max-height: 6;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    #export-path {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        margin-top: 1;
    }

    #export-hint {
        color: $text-muted;
        height: auto;
    }

    #export-hint.-warning {
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
        padding: 0 2;
    }

    #confirm-export {
        margin-left: 1;
    }
    """

    HINT = "Enter a destination path. Relative paths resolve from the working directory."
    PLUGIN_HINT = "This exporter chooses its own destination."

    def __init__(
        self,
        choices: Sequence[ExportChoice],
        *,
        entry_count: int,
        default_name: str = "clv-export",
    ) -> None:
        super().__init__()
        self._choices = list(choices)
        self._entry_count = entry_count
        # The stem the app derived from the source; the extension follows the
        # highlighted format.
        self._default_name = default_name
        self._generated = ""
        self._armed = False

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="export-dialog"):
            yield Label("Export view", id="dialog-title")
            yield Static(self._count_text(), id="export-count")
            # Text() rather than a markup string: a plugin's own name is not
            # ours to interpret, and a stray "[" must not be read as markup.
            yield OptionList(
                *(Option(Text(choice.label), id=choice.key) for choice in self._choices),
                id="export-format",
            )
            yield Input(
                value=self._filename_for(self._choice_at(0)),
                placeholder="/tmp/export.jsonl",
                id="export-path",
            )
            yield Static(self.HINT, id="export-hint")
            with Container(id="dialog-actions"):
                yield Button("Cancel", id="cancel-export")
                yield Button("Export", id="confirm-export", variant="primary")

    def on_mount(self) -> None:
        # The format is the decision; the default path is usually already right.
        self.query_one("#export-format", OptionList).focus()

    def _count_text(self) -> str:
        if self._entry_count == 1:
            return "1 entry matches the current filters."
        return f"{self._entry_count} entries match the current filters."

    # --- choices ------------------------------------------------------------

    def _choice_at(self, index: int | None) -> ExportChoice | None:
        if index is None or not (0 <= index < len(self._choices)):
            return None
        return self._choices[index]

    def _current_choice(self) -> ExportChoice | None:
        return self._choice_at(self.query_one("#export-format", OptionList).highlighted)

    def _filename_for(self, choice: ExportChoice | None) -> str:
        if choice is None or not choice.needs_path:
            self._generated = ""
            return ""
        extension = choice.extension or "log"
        self._generated = f"{self._default_name}.{extension}"
        return self._generated

    # --- events -------------------------------------------------------------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        self._disarm()
        choice = self._choice_at(event.option_index)
        path_input = self.query_one("#export-path", Input)
        hint = self.query_one("#export-hint", Static)

        if choice is not None and not choice.needs_path:
            path_input.value = ""
            path_input.disabled = True
            hint.update(self.PLUGIN_HINT)
            return

        path_input.disabled = False
        hint.update(self.HINT)
        # Only re-derive the name while it is still ours. Once the operator has
        # typed a path, switching format must not overwrite what they wrote.
        if path_input.value == self._generated or not path_input.value:
            path_input.value = self._filename_for(choice)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter on a format means "this one" and moves on to the destination,
        # rather than exporting immediately with a path nobody has looked at.
        event.stop()
        path_input = self.query_one("#export-path", Input)
        if path_input.disabled:
            self._finalize()
        else:
            path_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[override]
        if event.input.id == "export-path":
            event.stop()
            self._disarm()

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id == "export-path":
            event.stop()
            self._finalize()

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "cancel-export":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "confirm-export":
            event.stop()
            self._finalize()

    # --- confirmation -------------------------------------------------------

    def _disarm(self) -> None:
        if not self._armed:
            return
        self._armed = False
        hint = self.query_one("#export-hint", Static)
        hint.remove_class("-warning")
        hint.update(self.HINT)

    def _warn(self, message: str) -> None:
        hint = self.query_one("#export-hint", Static)
        hint.add_class("-warning")
        hint.update(message)

    def _finalize(self) -> None:
        choice = self._current_choice()
        if choice is None:
            self._warn("Choose an export format.")
            return

        if not choice.needs_path:
            self.dismiss(ExportRequest(choice.key, None))
            return

        text = self.query_one("#export-path", Input).value.strip()
        if not text:
            self._warn("Enter a destination path.")
            return

        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        # exists() is a stat, never a read: the dialog does not open a file the
        # operator has not confirmed writing to.
        if path.exists() and not self._armed:
            self._armed = True
            self._warn(f"{path.name} exists — press Export again to overwrite.")
            return

        self.dismiss(ExportRequest(choice.key, path))
