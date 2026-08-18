"""Add, edit, test and remove the machines CLV reads logs from.

Modelled on :mod:`clv.widgets.watch_dialog`, which is the closest existing shape:
a list of records, an editor that slides in under it, validation reported where
the operator typed rather than as a toast, and a delete that arms rather than
stacking a second modal on top of a modal.

**No password field and no sudo toggle**, stated here because this is the exact
point at which adding one would feel helpful. CLV connects with ssh-agent and key
files and reads as the configured user; ``clv.services.config`` refuses both keys
at the schema, and a UI that offered what the schema refuses would be the more
convincing of the two lies.

**The dialog holds the list; the app writes the file.** Everything here mutates a
working copy and hands the whole thing back on dismiss, so Escape genuinely
cancels and one confirm is one write. That matters more than it sounds: the
target is an INI full of the operator's comments, and the fewer times it is
rewritten the fewer chances there are to lose one.

**What it does not own, it does not touch.** Only six keys are edited — ``host``,
``user``, ``port``, ``identity_file``, ``log_dirs`` and ``enabled``. Per-host
globs, budgets and ``correct_clock_skew`` stay file-only: they are deliberate
tuning an operator sets once, there is no room for eleven fields in 24 rows, and
leaving them alone means the writeback preserves them for free — along with the
comments beside them and any refused key still earning its warning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Optional, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..services.config import (
    RemoteHost,
    validate_host_name,
    validate_identity_file,
    validate_port,
    validate_remote_dirs,
)

#: The keys this dialog is responsible for. Everything else in a host's section
#: is the operator's and survives an edit untouched — see the module docstring.
EDITABLE_KEYS = ("host", "user", "port", "identity_file", "log_dirs", "enabled")

#: Shown against a host nothing has connected to yet. A brand new
#: ``SSHConnection`` reports ``connected`` because that is its optimistic default,
#: so "reachable" and "never tried" would otherwise render identically — and the
#: operator would read a green row as evidence their new host works.
NOT_TRIED = "not tried"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one Test connection learned. Built by the app, rendered here."""

    ok: bool
    detail: str


#: Injected by the app so this widget imports nothing from ``clv.plugins`` and
#: spawns nothing itself. Called on a worker thread, never on the event loop.
HostProbe = Callable[[RemoteHost], ProbeResult]


class RemoteHostsDialog(ModalScreen[Optional[tuple[RemoteHost, ...]]]):
    """List, add, edit, test and remove ``[ssh:<name>]`` hosts."""

    DEFAULT_CSS = """
    RemoteHostsDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #remote-hosts-dialog {
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

    #host-list {
        height: auto;
        max-height: 5;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    /* The list gives way, not the editor. Six fields in two rows, the worked
       example and the buttons do not fit inside 24 rows alongside it — and the
       list is not what an operator is reading while typing into the editor.
       `Esc` brings it straight back. */
    #remote-hosts-dialog.-editing #host-list { display: none; }

    #host-editor {
        layout: vertical;
        height: auto;
        display: none;
    }

    #host-editor.-active { display: block; }

    .editor-row {
        layout: horizontal;
        height: auto;
        width: 1fr;
    }

    #host-editor .editor-field {
        layout: vertical;
        width: 1fr;
        height: auto;
        margin-right: 2;
    }

    #host-editor .editor-field:last-child { margin-right: 0; }

    #host-editor Input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
        width: 1fr;
    }

    #host-editor .editor-label {
        color: $text-muted;
        height: 1;
    }

    #host-hint {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    #host-hint.-warning {
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

    HINT = "a adds · Enter edits · space enables/disables · t tests · d deletes · Esc closes"
    #: Shown when hosts exist but `enable_ssh` does not. A dialog that accepts a
    #: host and then produces no sources is the "control that quietly does
    #: nothing" this project argues against everywhere else — but the switch
    #: stays the single writer of that setting, so this is a status line and a
    #: pointer, not a second place to change it.
    OFF_HINT = "Remote sources are off — press f and turn on Remote (SSH) to use these."
    #: Four lines, each written to fit the dialog's 72-column text width without
    #: wrapping. Wrapped instructions cost two rows apiece and push the buttons
    #: off a 24-row terminal, so the copy is measured, not just written.
    EDIT_HINT = (
        "* required · Address defaults to Name (a ~/.ssh/config alias works)\n"
        "Log dirs: folders on the remote host, comma separated, absolute or ~\n"
        "Example — web01 · web01.internal · ops · /var/log, /srv/app/logs\n"
        "Enter saves · Esc goes back · no passwords: ssh-agent and keys only"
    )

    def __init__(
        self,
        hosts: Sequence[RemoteHost] = (),
        *,
        statuses: Optional[Mapping[str, str]] = None,
        skipped: Iterable[str] = (),
        probe: Optional[HostProbe] = None,
        enabled: bool = True,
    ) -> None:
        super().__init__()
        #: Whether `enable_ssh` is on. Read only, to say so — never written here.
        self._enabled = enabled
        self._hosts: list[RemoteHost] = list(hosts)
        self._statuses = dict(statuses or {})
        #: Sections ``config.py`` refused, as already-formatted messages. Shown
        #: and never edited: a host with an impossible port never reaches
        #: ``LogConfig.hosts``, so the one place an operator would go to fix it is
        #: the one place it was invisible — and a dialog that cannot see a
        #: section must not be the thing that deletes it.
        self._skipped = list(skipped)
        self._probe = probe
        self._editing_name: Optional[str] = None
        self._armed_delete = ""
        self._dirty = False
        #: Guards a Test result against arriving after the operator moved on, the
        #: same generation-token idiom the app uses for a remote open.
        self._probe_token: object = object()

    # --- composition ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="remote-hosts-dialog"):
            yield Label("Remote hosts (SSH)", id="dialog-title")
            yield OptionList(id="host-list")
            with Container(id="host-editor"):
                with Horizontal(classes="editor-row"):
                    with Vertical(classes="editor-field"):
                        yield Label("Name *", classes="editor-label")
                        yield Input(placeholder="web01", id="host-name")
                    with Vertical(classes="editor-field"):
                        yield Label("Address", classes="editor-label")
                        yield Input(placeholder="web01.internal", id="host-address")
                    with Vertical(classes="editor-field"):
                        yield Label("User", classes="editor-label")
                        yield Input(placeholder="ops", id="host-user")
                with Horizontal(classes="editor-row"):
                    with Vertical(classes="editor-field"):
                        yield Label("Port", classes="editor-label")
                        yield Input(placeholder="22", id="host-port")
                    with Vertical(classes="editor-field"):
                        yield Label("Identity file", classes="editor-label")
                        yield Input(placeholder="~/.ssh/id_rsa", id="host-identity")
                    with Vertical(classes="editor-field"):
                        yield Label("Log dirs *", classes="editor-label")
                        yield Input(placeholder="/var/log, /srv", id="host-dirs")
            yield Static(self.HINT, id="host-hint")
            with Container(id="dialog-actions"):
                yield Button("Add", id="host-add")
                yield Button("Test", id="host-test")
                yield Button("Close", id="host-close", variant="primary")

    def on_mount(self) -> None:
        self._refresh_list()
        self._hint(self._resting_hint(), warning=not self._enabled and bool(self._hosts))
        self.query_one("#host-list", OptionList).focus()

    def _resting_hint(self) -> str:
        """The hint shown whenever nothing more specific needs saying."""

        if self._hosts and not self._enabled:
            return f"{self.OFF_HINT}  {self.HINT}"
        return self.HINT

    # --- state ---------------------------------------------------------------

    @property
    def hosts(self) -> tuple[RemoteHost, ...]:
        return tuple(self._hosts)

    @property
    def editing(self) -> bool:
        """Read off the CSS class rather than a flag, as the sibling dialogs do."""

        try:
            return self.query_one("#host-editor", Container).has_class("-active")
        except NoMatches:  # pragma: no cover - not composed yet
            return False

    def _current(self) -> Optional[RemoteHost]:
        option_list = self.query_one("#host-list", OptionList)
        index = option_list.highlighted
        if index is None or not (0 <= index < len(self._hosts)):
            return None
        return self._hosts[index]

    # --- rendering -----------------------------------------------------------

    def _row(self, host: RemoteHost) -> Text:
        """One host, as ``Text`` so a name with a bracket in it stays a name."""

        status = self._statuses.get(host.name, NOT_TRIED)
        mark = "on " if host.enabled else "off"
        line = Text(f"{mark}  {host.name}", style="" if host.enabled else "dim")
        line.append(f"  {host.host}:{host.port}", style="#7aa3d1")
        line.append(f"  — {status}", style="dim")
        return line

    def _refresh_list(self) -> None:
        option_list = self.query_one("#host-list", OptionList)
        option_list.clear_options()
        if not self._hosts and not self._skipped:
            # Left enabled with a placeholder rather than disabled: a disabled
            # OptionList cannot take focus, and then `a` never reaches on_key.
            option_list.add_option(Option(Text("No hosts yet — press a to add one")))
        else:
            for host in self._hosts:
                option_list.add_option(Option(self._row(host)))
            for message in self._skipped:
                option_list.add_option(
                    Option(Text(f"⚠ {message}", style="#facc15"), disabled=True)
                )
        if self._hosts:
            option_list.highlighted = 0

    def _hint(self, message: str, *, warning: bool = False) -> None:
        hint = self.query_one("#host-hint", Static)
        hint.set_class(warning, "-warning")
        hint.update(message)

    # --- the editor ----------------------------------------------------------

    def _open_editor(self, host: Optional[RemoteHost]) -> None:
        self._editing_name = host.name if host is not None else None
        self.query_one("#host-name", Input).value = host.name if host else ""
        self.query_one("#host-address", Input).value = (
            host.host if host and host.host != host.name else ""
        )
        self.query_one("#host-user", Input).value = (host.user or "") if host else ""
        self.query_one("#host-port", Input).value = (
            str(host.port) if host and host.port != 22 else ""
        )
        self.query_one("#host-identity", Input).value = (
            str(host.identity_file) if host and host.identity_file else ""
        )
        self.query_one("#host-dirs", Input).value = (
            ", ".join(host.log_dirs) if host else ""
        )
        self.query_one("#host-editor", Container).add_class("-active")
        self.query_one("#remote-hosts-dialog", Container).add_class("-editing")
        self._hint(self.EDIT_HINT)
        self.query_one("#host-hint", Static).styles.height = "auto"
        self.query_one("#host-name", Input).focus()

    def _close_editor(self) -> None:
        self.query_one("#host-editor", Container).remove_class("-active")
        self.query_one("#remote-hosts-dialog", Container).remove_class("-editing")
        self._editing_name = None
        self._hint(self._resting_hint())
        self.query_one("#host-list", OptionList).focus()

    def _save_editor(self) -> None:
        """Validate, and on any complaint keep the editor open and say so.

        Every message comes from ``clv.services.config``, which is also what the
        parser uses, so a port CLV refuses in a file and a port it refuses here
        are refused in the same words.
        """

        name = self.query_one("#host-name", Input).value.strip()
        others = [h.name for h in self._hosts if h.name != self._editing_name]
        problem = validate_host_name(name, others)
        if problem is not None:
            self._hint(problem, warning=True)
            return

        port, complaint = validate_port(self.query_one("#host-port", Input).value)
        if complaint is not None:
            self._hint(complaint, warning=True)
            return

        dirs, complaints = validate_remote_dirs(self.query_one("#host-dirs", Input).value)
        if complaints:
            self._hint(complaints[0], warning=True)
            return
        if not dirs:
            self._hint(
                "Name at least one absolute folder or file on the remote host.",
                warning=True,
            )
            return

        identity, warning = validate_identity_file(
            self.query_one("#host-identity", Input).value
        )

        address = self.query_one("#host-address", Input).value.strip()
        previous = next(
            (h for h in self._hosts if h.name == self._editing_name), None
        )
        host = RemoteHost(
            name=name,
            host=address or name,
            user=self.query_one("#host-user", Input).value.strip() or None,
            port=port or 22,
            identity_file=identity,
            log_dirs=dirs,
            # Carried rather than edited: `space` on the list row owns it, and
            # everything below is per-host tuning this dialog does not offer.
            enabled=previous.enabled if previous is not None else True,
            correct_clock_skew=previous.correct_clock_skew if previous else False,
            include_globs=previous.include_globs if previous else None,
            exclude_globs=previous.exclude_globs if previous else None,
            max_files=previous.max_files if previous else None,
            max_buffer_lines=previous.max_buffer_lines if previous else None,
        )

        if previous is None:
            self._hosts.append(host)
        else:
            self._hosts[self._hosts.index(previous)] = host
        self._dirty = True
        self._close_editor()
        self._refresh_list()
        if warning is not None:
            # A warning, not a refusal: ssh-agent may already hold the key.
            self._hint(warning, warning=True)

    # --- list verbs ----------------------------------------------------------

    def _toggle_current(self) -> None:
        host = self._current()
        if host is None:
            return
        index = self._hosts.index(host)
        self._hosts[index] = replace(host, enabled=not host.enabled)
        self._dirty = True
        self._refresh_list()
        self.query_one("#host-list", OptionList).highlighted = index

    def _delete_current(self) -> None:
        host = self._current()
        if host is None:
            return
        if self._armed_delete != host.name:
            self._armed_delete = host.name
            self._hint(f"Press d again to remove {host.name}.", warning=True)
            return
        self._hosts.remove(host)
        self._armed_delete = ""
        self._dirty = True
        self._refresh_list()
        self._hint(f"Removed {host.name}. {self._resting_hint()}")

    def _test_current(self) -> None:
        """Probe the host under the cursor, or the one being edited.

        The only thing in this dialog that touches the network, and only ever
        because someone pressed a button labelled Test.
        """

        if self._probe is None:
            self._hint("Testing is unavailable in this build.", warning=True)
            return
        host = self._editor_host() if self.editing else self._current()
        if host is None:
            self._hint("Move to a host to test it.", warning=True)
            return
        self._hint(f"Testing {host.name}…")
        self._probe_token = token = object()
        self.run_worker(
            self._run_probe(host, token), name="host-probe", exit_on_error=False
        )

    def _editor_host(self) -> Optional[RemoteHost]:
        """What the editor currently says, so Test tests what was just typed.

        Not the saved record: the whole question a Test answers is whether the
        address and port in front of the operator work, and answering it about
        the last thing they confirmed would be answering a different one.
        """

        name = self.query_one("#host-name", Input).value.strip()
        if not name:
            return None
        port, complaint = validate_port(self.query_one("#host-port", Input).value)
        if complaint is not None:
            return None
        identity, _ = validate_identity_file(
            self.query_one("#host-identity", Input).value
        )
        dirs, _ = validate_remote_dirs(self.query_one("#host-dirs", Input).value)
        return RemoteHost(
            name=name,
            host=self.query_one("#host-address", Input).value.strip() or name,
            user=self.query_one("#host-user", Input).value.strip() or None,
            port=port or 22,
            identity_file=identity,
            log_dirs=dirs or ("/var/log",),
        )

    async def _run_probe(self, host: RemoteHost, token: object) -> None:
        probe = self._probe
        assert probe is not None
        worker = self.run_worker(
            lambda: probe(host), thread=True, name="host-probe-io", exit_on_error=False
        )
        try:
            outcome = await worker.wait()
        except Exception as exc:  # noqa: BLE001 - a connection fails many ways
            outcome = ProbeResult(False, str(exc))
        if token is not self._probe_token or not self.is_mounted:
            # Escape popped the screen, or a second Test overtook this one.
            return
        self._statuses[host.name] = outcome.detail
        self._hint(f"{host.name}: {outcome.detail}", warning=not outcome.ok)
        if not self.editing:
            index = next(
                (i for i, h in enumerate(self._hosts) if h.name == host.name), None
            )
            self._refresh_list()
            if index is not None:
                self.query_one("#host-list", OptionList).highlighted = index

    # --- input ---------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        if self.editing:
            # Every letter is text while the editor is open; only Escape and the
            # Input's own Enter mean anything.
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
        elif event.key == "t":
            event.stop()
            self._test_current()
        elif event.key in ("d", "delete"):
            event.stop()
            self._delete_current()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        host = self._current()
        if host is not None:
            self._open_editor(host)

    def on_option_list_option_highlighted(self, _event) -> None:
        # Moving off an armed row disarms it, so `d` never deletes the wrong host.
        if self._armed_delete:
            self._armed_delete = ""
            self._hint(self._resting_hint())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._save_editor()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "host-add":
            self._open_editor(None)
        elif event.button.id == "host-test":
            self._test_current()
        elif event.button.id == "host-close":
            if self.editing:
                self._save_editor()
            else:
                self._finish()

    def _finish(self) -> None:
        # None means "nothing changed", so the app can skip rewriting a settings
        # file and reloading every source for a dialog that was only looked at.
        self.dismiss(tuple(self._hosts) if self._dirty else None)
