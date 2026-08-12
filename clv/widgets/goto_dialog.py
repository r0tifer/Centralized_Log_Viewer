"""Prompt for the timestamp `g` jumps to.

Modelled on :class:`~clv.widgets.add_source_dialog.AddSourceDialog`: one
container, its own ``DEFAULT_CSS``, `Esc` cancels, `Enter` confirms.

It does no parsing. The app hands the typed string to
:func:`clv.services.filtering.parse_moment`, which already knows both accepted
forms because they are the same two the time-window presets and the custom
range dialog are built from. Keeping the parse out here means the dialog cannot
disagree with the rest of the app about what ``-15m`` means.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class GotoDialog(ModalScreen[str | None]):
    """Ask where in time to move the cursor."""

    DEFAULT_CSS = """
    GotoDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #goto-dialog {
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

    #goto-input {
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

    #confirm-goto {
        margin-left: 1;
    }
    """

    HINT = (
        "An absolute time (2026-08-07 09:25:01) or an offset from now "
        "(-15m, -6h, -2d). The cursor moves to the first entry at or after it."
    )

    def compose(self) -> ComposeResult:
        with Container(id="goto-dialog"):
            yield Label("Go to timestamp", id="dialog-title")
            yield Static(self.HINT, id="dialog-hint")
            yield Input(placeholder="-15m", id="goto-input")
            with Container(id="dialog-actions"):
                yield Button("Cancel", id="cancel-goto")
                yield Button("Go", id="confirm-goto", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#goto-input", Input).focus()

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id == "goto-input":
            event.stop()
            self._finalize()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "cancel-goto":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "confirm-goto":
            event.stop()
            self._finalize()

    def _finalize(self) -> None:
        value = self.query_one("#goto-input", Input).value.strip()
        self.dismiss(value or None)


__all__ = ["GotoDialog"]
