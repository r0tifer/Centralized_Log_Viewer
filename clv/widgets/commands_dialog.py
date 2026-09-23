"""Every plugin command, and the way to run one by name.

**Why this screen exists at all.** A plugin command may declare a key, and that
key may be refused — it collides with one of CLV's, or with a command that
sorted ahead of it — and `PLUGIN_TODO.md` Phase 12 promises that such a command
"remains invocable by name". Before this screen there was no by-name surface in
CLV, so that promise had nowhere to land: a refused key meant a command nobody
could run. Textual's own command palette was the other candidate and was
declined, because it is a surface CLV has never documented and it lists
Textual's system commands (*Change theme*, *Take screenshot*) beside the
plugin's, which makes the plugin's look like CLV's.

**It runs nothing.** Like every other dialog in this package it dismisses with a
decision — the chosen ``command_name``, or ``None`` — and the app does the work.
That is what keeps third-party code off this widget's import list and out of its
event handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from .help_overlay import format_key


@dataclass(frozen=True, slots=True)
class CommandRow:
    """One command, as much of it as an operator needs to choose between them."""

    #: The stable id the app dispatches on. Not shown: it is the machine's name
    #: for this command, and `title` is the operator's.
    command_name: str
    title: str
    #: The key it actually got, which is not always the key it asked for —
    #: empty for a command with no key and for one whose key was refused.
    key: str = ""
    #: Which plugin supplied it. Shown because two plugins may offer commands
    #: that read alike, and "which of these is the one I installed" is the
    #: question a row has to be able to answer.
    plugin: str = ""


class CommandsDialog(ModalScreen[Optional[str]]):
    """List the loaded plugin commands; dismiss with the one to run."""

    DEFAULT_CSS = """
    CommandsDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #commands-dialog {
        width: 100%;
        max-width: 76;
        height: auto;
        padding: 1 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    #commands-title {
        text-style: bold;
        height: 1;
    }

    #command-list {
        height: auto;
        max-height: 10;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    #command-hint {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    #commands-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    #commands-actions Button {
        height: 3;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }
    """

    HINT = "enter runs the highlighted command · Esc closes"

    def __init__(self, rows: Sequence[CommandRow] = ()) -> None:
        super().__init__()
        self._rows = list(rows)

    # --- composition ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="commands-dialog"):
            yield Label("Plugin commands", id="commands-title")
            yield OptionList(id="command-list")
            yield Static(self.HINT, id="command-hint")
            with Container(id="commands-actions"):
                yield Button("Run", id="command-run", variant="primary")
                yield Button("Close", id="command-close")

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one("#command-list", OptionList).focus()

    # --- rendering -----------------------------------------------------------

    def _row(self, row: CommandRow) -> Text:
        """One command, degrading by dropping from the right.

        The key column is fixed-width and first because it is what an operator
        is scanning for; at 80 columns the plugin name is what goes, leaving the
        key and the title — which is the pair a row exists to say.
        """

        key = format_key(row.key) if row.key else "–"
        line = Text(f"{key:<8}", style="#7aa3d1")
        line.append(row.title or row.command_name)
        if row.plugin:
            line.append(f"  — {row.plugin}", style="dim")
        return line

    def _refresh_list(self) -> None:
        option_list = self.query_one("#command-list", OptionList)
        option_list.clear_options()
        if not self._rows:
            # Enabled with a placeholder rather than disabled, for the reason
            # `PluginsDialog` records: a disabled OptionList cannot take focus,
            # and then no key reaches `on_key`. The app does not open this
            # screen with an empty list, so this is a guard and not a state.
            option_list.add_option(Option(Text("No plugin commands installed")))
            return
        for row in self._rows:
            option_list.add_option(Option(self._row(row)))
        option_list.highlighted = 0

    # --- input ---------------------------------------------------------------

    def _current(self) -> Optional[CommandRow]:
        index = self.query_one("#command-list", OptionList).highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    def _run_current(self) -> None:
        row = self._current()
        self.dismiss(row.command_name if row is not None else None)

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._run_current()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "command-run":
            self._run_current()
        else:
            self.dismiss(None)
