"""Pick which machines from ``~/.ssh/config`` become CLV hosts.

Modelled on :class:`~clv.widgets.remote_hosts_dialog.RemoteHostsDialog`, which is
the closest existing shape: a list of records, a one-field editor that takes the
list's place, and validation reported where the operator is looking rather than
as a toast.

**Review, not import-everything.** An ``~/.ssh/config`` with forty aliases in it
describes a fleet, a jump box and three boxes that no longer exist. Turning all
of them into sources because a button was pressed would be the surprising
reading of "scan", so nothing is ticked until somebody ticks it.

**Records in, records out.** The scan happened before this screen opened and the
write happens after it closes; this dialog reads no file, spawns nothing, and
imports nothing from :mod:`clv.plugins`. Escape genuinely cancels.

**Only ``log_dirs`` is editable, because it is the only field OpenSSH cannot
answer.** The address, user, port and identity file are shown and never
imported — see :mod:`clv.services.ssh_config` for why the alias alone is what
gets written.
"""

from __future__ import annotations

from typing import Optional, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..services.config import RemoteHost
from ..services.ssh_config import DEFAULT_LOG_DIRS, SSHConfigHost, as_remote_host


class SSHConfigImportDialog(ModalScreen[Optional[tuple[RemoteHost, ...]]]):
    """Tick the aliases worth importing, and say where their logs live."""

    DEFAULT_CSS = """
    SSHConfigImportDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #import-dialog {
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

    #import-list {
        height: auto;
        max-height: 8;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    /* The list gives way to the editor, as in the remote hosts dialog: what an
       operator is reading while typing a path is the path, and `Esc` brings the
       list straight back. */
    #import-dialog.-editing #import-list { display: none; }

    #import-editor {
        layout: vertical;
        height: auto;
        display: none;
    }

    #import-editor.-active { display: block; }

    #import-editor Input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        width: 1fr;
    }

    #import-editor .editor-label {
        color: $text-muted;
        height: 1;
    }

    #import-hint {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    #import-hint.-warning {
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

    HINT = "space picks · a picks all · Enter sets log dirs · Esc cancels"
    #: Two lines, measured against the dialog's 72-column text width. A wrapped
    #: instruction costs a row apiece and pushes the buttons off a 24-row
    #: terminal, so this copy is measured rather than merely written.
    EDIT_HINT = (
        "Folders on the remote host, comma separated, absolute or ~-relative.\n"
        "Enter saves · Esc goes back"
    )

    def __init__(
        self,
        candidates: Sequence[SSHConfigHost] = (),
        *,
        notes: Sequence[str] = (),
        default_log_dirs: str = DEFAULT_LOG_DIRS,
    ) -> None:
        super().__init__()
        self._candidates: list[SSHConfigHost] = list(candidates)
        #: What the scan could not make sense of, already phrased. Shown and
        #: never acted on — the file belongs to OpenSSH, and a dialog that
        #: offered to fix it would be offering to write it.
        self._notes = list(notes)
        #: Per-alias, because a jump box and an app server rarely keep their
        #: logs in the same place, and re-typing the common answer for each is
        #: the tedium this whole feature exists to remove.
        self._dirs: dict[str, str] = {
            entry.name: default_log_dirs for entry in self._candidates
        }
        self._picked: set[str] = set()
        self._editing_name: Optional[str] = None

    # --- composition ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="import-dialog"):
            yield Label("Import hosts from ~/.ssh/config", id="dialog-title")
            yield OptionList(id="import-list")
            with Container(id="import-editor"):
                with Vertical():
                    yield Label("Log dirs *", classes="editor-label")
                    yield Input(placeholder="/var/log, /srv/app/logs", id="import-dirs")
            yield Static(self.HINT, id="import-hint")
            with Container(id="dialog-actions"):
                yield Button("All", id="import-all")
                yield Button("Cancel", id="import-cancel")
                yield Button("Import", id="import-confirm", variant="primary")

    def on_mount(self) -> None:
        self._refresh_list()
        self._hint(self._resting_hint())
        self.query_one("#import-list", OptionList).focus()

    def _resting_hint(self) -> str:
        """The hint shown whenever nothing more specific needs saying."""

        if self._notes:
            return "\n".join([*self._notes, self.HINT])
        return self.HINT

    # --- state ---------------------------------------------------------------

    @property
    def picked(self) -> tuple[str, ...]:
        """Which aliases are ticked, in the order they are listed."""

        return tuple(
            entry.name for entry in self._candidates if entry.name in self._picked
        )

    @property
    def editing(self) -> bool:
        """Read off the CSS class rather than a flag, as the sibling dialogs do."""

        try:
            return self.query_one("#import-editor", Container).has_class("-active")
        except NoMatches:  # pragma: no cover - not composed yet
            return False

    def _current(self) -> Optional[SSHConfigHost]:
        option_list = self.query_one("#import-list", OptionList)
        index = option_list.highlighted
        if index is None or not (0 <= index < len(self._candidates)):
            return None
        return self._candidates[index]

    # --- rendering -----------------------------------------------------------

    def _row(self, entry: SSHConfigHost) -> Text:
        """One candidate, as ``Text`` so a name with a bracket stays a name."""

        ticked = entry.name in self._picked
        line = Text(f"[{'x' if ticked else ' '}]  {entry.name}")
        line.append(f"  {self._dirs.get(entry.name, '')}", style="#7aa3d1")
        line.append(f"  — {entry.summary()}", style="dim")
        return line

    def _refresh_list(self) -> None:
        option_list = self.query_one("#import-list", OptionList)
        # Kept across the rebuild: every tick redraws the whole list, and a
        # cursor that jumped home each time would make ticking three hosts in a
        # row an exercise in arrowing back down.
        highlighted = option_list.highlighted
        option_list.clear_options()
        if not self._candidates:
            # Enabled with a placeholder rather than disabled, for the reason
            # `RemoteHostsDialog._refresh_list` records: a disabled OptionList
            # cannot take focus, and then no key ever reaches `on_key`.
            option_list.add_option(Option(Text("Nothing to import.", style="dim")))
            return
        for entry in self._candidates:
            option_list.add_option(Option(self._row(entry)))
        # Explicitly, because `clear_options` leaves it unset — and with nothing
        # highlighted `space` has no row to act on and silently does nothing.
        option_list.highlighted = (
            highlighted if highlighted is not None and 0 <= highlighted < len(self._candidates) else 0
        )

    def _hint(self, text: str, *, warning: bool = False) -> None:
        try:
            hint = self.query_one("#import-hint", Static)
        except NoMatches:  # pragma: no cover - not composed yet
            return
        hint.update(text)
        hint.set_class(warning, "-warning")

    # --- picking --------------------------------------------------------------

    def _toggle(self, entry: SSHConfigHost) -> None:
        if entry.name in self._picked:
            self._picked.discard(entry.name)
        else:
            self._picked.add(entry.name)
        self._refresh_list()
        self._hint(self._resting_hint())

    def _pick_all(self) -> None:
        if len(self._picked) == len(self._candidates):
            self._picked.clear()
        else:
            self._picked = {entry.name for entry in self._candidates}
        self._refresh_list()
        self._hint(self._resting_hint())

    # --- the log-dirs editor ---------------------------------------------------

    def _open_editor(self, entry: SSHConfigHost) -> None:
        self._editing_name = entry.name
        self.query_one("#import-dialog", Container).add_class("-editing")
        self.query_one("#import-editor", Container).add_class("-active")
        field = self.query_one("#import-dirs", Input)
        field.value = self._dirs.get(entry.name, "")
        self._hint(f"{entry.name} — {self.EDIT_HINT}")
        field.focus()

    def _close_editor(self) -> None:
        self._editing_name = None
        self.query_one("#import-dialog", Container).remove_class("-editing")
        self.query_one("#import-editor", Container).remove_class("-active")
        self._hint(self._resting_hint())
        self.query_one("#import-list", OptionList).focus()

    def _save_editor(self) -> None:
        """Keep what was typed, once the shared validator accepts it.

        Validated here as well as at import time so the complaint arrives while
        the operator is still looking at the field that caused it — the same
        reason ``RemoteHostsDialog._save_editor`` validates rather than waiting
        for the write.
        """

        name = self._editing_name
        if name is None:  # pragma: no cover - no editor open
            return
        typed = self.query_one("#import-dirs", Input).value
        entry = next(item for item in self._candidates if item.name == name)
        _, complaint = as_remote_host(entry, typed)
        if complaint is not None:
            self._hint(complaint, warning=True)
            return
        self._dirs[name] = typed.strip()
        # Setting a path is how somebody says they want this one; making them
        # then also press space would be asking the same question twice.
        self._picked.add(name)
        self._close_editor()
        self._refresh_list()

    # --- events ----------------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        entry = self._current()
        if entry is not None:
            self._open_editor(entry)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "import-dirs":
            event.stop()
            self._save_editor()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "import-all":
            event.stop()
            self._pick_all()
        elif event.button.id == "import-cancel":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "import-confirm":
            event.stop()
            self._finish()

    async def on_key(self, event: events.Key) -> None:
        # While the editor is open every letter is text. Escape is the only key
        # this screen keeps, and it means "back to the list", not "cancel".
        if self.editing:
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                self._close_editor()
            return

        if event.key == "escape":
            event.stop()
            self.dismiss(None)
            return
        if event.key == "space":
            entry = self._current()
            if entry is not None:
                event.stop()
                event.prevent_default()
                self._toggle(entry)
            return
        if event.key == "a":
            event.stop()
            event.prevent_default()
            self._pick_all()

    # --- finishing -------------------------------------------------------------

    def _finish(self) -> None:
        """Turn the ticked rows into records, or say why one cannot be.

        Nothing ticked dismisses ``None`` rather than an empty tuple, so the app
        can skip the write entirely — an import that picked nothing must not
        rewrite ``settings.conf`` to prove it.
        """

        picked = [entry for entry in self._candidates if entry.name in self._picked]
        if not picked:
            self.dismiss(None)
            return

        hosts: list[RemoteHost] = []
        for entry in picked:
            host, complaint = as_remote_host(entry, self._dirs.get(entry.name, ""))
            if complaint is not None or host is None:
                self._hint(complaint or f"Cannot import {entry.name}.", warning=True)
                return
            hosts.append(host)
        self.dismiss(tuple(hosts))


__all__ = ["SSHConfigImportDialog"]
