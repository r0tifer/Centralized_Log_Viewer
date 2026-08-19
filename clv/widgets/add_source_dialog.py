from __future__ import annotations

import os

from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

#: Dismissal value meaning "the operator wants the machine list, not a path".
#:
#: A sentinel rather than a wider result type: ``ModalScreen[str | None]`` is
#: this dialog's whole contract and the app already branches on the string. NUL
#: is what makes it safe -- ``services/refs.py`` records that a ref string
#: carries neither a comma nor NUL, so no path anyone can type reaches here
#: looking like this. The dialog does not know what the app does with it.
REMOTE_HOSTS = "\x00remote-hosts"


class AddSourceDialog(ModalScreen[str | None]):
    """Prompt the user for a directory or file to include as a log source."""

    DEFAULT_CSS = """
    AddSourceDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #add-source-dialog {
        width: 70;
        max-width: 90vw;
        padding: 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    #dialog-title {
        text-style: bold;
        padding-bottom: 1;
    }

    #dialog-hint {
        color: $text-muted;
        padding-bottom: 1;
    }

    #path-input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
    }

    #dialog-actions {
        layout: horizontal;
        align: right middle;
        padding-top: 1;
    }

    #dialog-actions Button {
        height: 3;
        padding: 0 2;
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="add-source-dialog"):
            yield Label("Add Log Source", id="dialog-title")
            yield Static(
                "Enter an absolute directory or a specific log file. Relative paths are "
                "resolved from the current working directory. For a machine on the "
                "network, use Remote hosts\u2026 instead.",
                id="dialog-hint",
            )
            placeholder = "/var/log" if os.name != "nt" else r"C:\\logs"
            yield Input(placeholder=placeholder, id="path-input")
            with Container(id="dialog-actions"):
                # First, so Cancel and Add keep the right edge they have always
                # had under `align: right middle` -- the button people reach for
                # without looking should not move because a third one arrived.
                yield Button("Remote hosts\u2026", id="remote-hosts-add-source")
                yield Button("Cancel", id="cancel-add-source")
                yield Button("Add", id="confirm-add-source", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id == "path-input":
            self._finalize()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "cancel-add-source":
            self.dismiss(None)
        elif event.button.id == "remote-hosts-add-source":
            self.dismiss(REMOTE_HOSTS)
        elif event.button.id == "confirm-add-source":
            self._finalize()

    def _finalize(self) -> None:
        value = self.query_one("#path-input", Input).value.strip()
        self.dismiss(value if value else "")
