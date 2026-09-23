"""One row per installed plugin, and the two controls that act on them.

Modelled on :mod:`clv.widgets.remote_hosts_dialog`, which solved the same
problem one layer down: a list of records that the operator needs to see all of,
in a drawer that has room for none of it.

**Why this is a modal and not a drawer section.** ``PLUGIN_TODO.md``'s Phase 4
asks for "a plugin section in the Advanced drawer". The drawer is capped at
``max-height: 16`` and ``clv/widgets/AGENTS.md`` records what that costs: a new
*row* pushes what follows below the fold, where it lays out and paints nothing.
Four installed plugins is four rows before any of them has said anything, and
Stage C makes twelve interfaces available. The fleet of SSH hosts hit this exact
wall and settled it the same way — one summary line in the drawer, the detail in
a modal — so plugins follow the precedent rather than inventing a second answer.

**The dialog decides nothing.** It holds a working copy, flips :attr:`enabled`
and :attr:`reinstate` on it, and hands the whole set back on dismiss. Escape
therefore genuinely cancels, and one confirm is one write to an INI full of the
operator's comments. What a toggle *means* — a line rewritten in
``settings.conf``, a session-only disable, a restart — is the app's to carry out
and is stated on this screen only so the operator knows before they press it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..plugins import PluginStatus

#: The glyph for each state. A leading mark rather than colour alone: this pane
#: is read at 80 columns on terminals whose palettes CLV does not choose, and
#: "which of these four is broken" may not rest on a shade of amber.
STATE_MARKS = {
    "loaded": "*",
    "not enabled": "o",
    "failed": "x",
    "incompatible": "!",
    "isolated": "#",
}

#: States that are the operator's own doing rather than a fault. Rendered dim
#: rather than amber, and not counted as problems anywhere.
#:
#: ``isolated`` is one of them: a plugin running in a child process is healthy,
#: and rendering it in the colour a broken one gets would make containment look
#: like a problem an operator has to fix.
QUIET_STATES = ("loaded", "not enabled", "isolated")

#: What an isolated row says, under the origin. Short enough for the detail pane
#: at 80 columns and honest in both halves: a killable plugin is not a safe one,
#: and this is the only place an operator meets that fact while deciding.
ISOLATED_NOTE = (
    "Runs in a separate process CLV can stop if it hangs or crashes. "
    "It still runs as you, with your files."
)


class PluginsDialog(ModalScreen[Optional[tuple[PluginStatus, ...]]]):
    """List every installed plugin, and enable, disable or re-enable one."""

    DEFAULT_CSS = """
    PluginsDialog {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #plugins-dialog {
        width: 100%;
        max-width: 76;
        height: auto;
        padding: 1 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    #plugins-title {
        text-style: bold;
        height: 1;
    }

    #plugin-list {
        height: auto;
        max-height: 8;
        border: tall $surface 20%;
        background: $surface 8%;
    }

    /* The highlighted row's origin and its recorded message, in full. The
       truncated three-error concatenation this replaces lived in a line that
       also had to say how many exporters had loaded. */
    #plugin-detail {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    #plugin-detail.-warning {
        color: #facc15;
    }

    #plugin-hint {
        color: $text-muted;
        height: auto;
    }

    #plugins-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    #plugins-actions Button {
        height: 3;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }
    """

    HINT = "space enables/disables · r re-enables after a failure · Esc closes"

    def __init__(self, rows: Sequence[PluginStatus] = ()) -> None:
        super().__init__()
        self._rows: list[PluginStatus] = list(rows)
        self._original = tuple(rows)

    # --- composition ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="plugins-dialog"):
            yield Label("Plugins", id="plugins-title")
            yield OptionList(id="plugin-list")
            yield Static("", id="plugin-detail")
            yield Static(self.HINT, id="plugin-hint")
            with Container(id="plugins-actions"):
                yield Button("Enable", id="plugin-toggle")
                yield Button("Re-enable", id="plugin-reinstate")
                yield Button("Close", id="plugin-close", variant="primary")

    def on_mount(self) -> None:
        self._refresh_list()
        self._sync_controls()
        self.query_one("#plugin-list", OptionList).focus()

    # --- state ---------------------------------------------------------------

    @property
    def rows(self) -> tuple[PluginStatus, ...]:
        return tuple(self._rows)

    def _current(self) -> Optional[PluginStatus]:
        index = self.query_one("#plugin-list", OptionList).highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    def _replace_current(self, **changes) -> Optional[PluginStatus]:
        index = self.query_one("#plugin-list", OptionList).highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        self._rows[index] = replace(self._rows[index], **changes)
        self._refresh_list()
        self.query_one("#plugin-list", OptionList).highlighted = index
        self._sync_controls()
        return self._rows[index]

    # --- rendering -----------------------------------------------------------

    def _row(self, row: PluginStatus) -> Text:
        """One plugin, as ``Text`` so a name with a bracket in it stays a name.

        Degrades by dropping from the right, which is the order the columns are
        written in: at 80 columns the origin has already gone and the kinds go
        next, leaving name and state — the two things a row exists to say.
        """

        state = self._state_label(row)
        line = Text(
            f"{STATE_MARKS.get(row.state, '?')}  {row.name}",
            style="" if row.enabled else "dim",
        )
        if row.kinds:
            line.append(f"  {', '.join(row.kinds)}", style="#7aa3d1")
        if row.reads_content:
            # Between the kinds and the state, and in the warning colour rather
            # than the kinds' own: "sink" says where this plugin sends things,
            # and this says *what* it sends. It survives to 80 columns because
            # it is two characters, which is the point -- the fact an operator
            # most needs before enabling something should not be the first
            # thing a narrow terminal drops.
            line.append("  ⚑", style="#facc15")
        line.append(
            f"  — {state}",
            style="dim" if row.state in QUIET_STATES else "#facc15",
        )
        return line

    def _state_label(self, row: PluginStatus) -> str:
        """The state, plus what the operator's own pending edit will change it to.

        A working copy that shows only the *saved* state makes the toggle look
        broken; one that shows only the pending state loses what it is pending
        against. Both, in the order they happen.
        """

        saved = self._saved_for(row)
        pending = ""
        if row.reinstate:
            pending = " → back on"
        elif row.enabled != saved.enabled:
            pending = " → on" if row.enabled else " → off"
        return f"{row.state}{pending}"

    def _refresh_list(self) -> None:
        option_list = self.query_one("#plugin-list", OptionList)
        option_list.clear_options()
        if not self._rows:
            # Enabled with a placeholder rather than disabled: a disabled
            # OptionList cannot take focus, and then no key reaches on_key.
            option_list.add_option(Option(Text("No plugins installed")))
            return
        for row in self._rows:
            option_list.add_option(Option(self._row(row)))
        option_list.highlighted = 0

    def _sync_controls(self) -> None:
        """Label the toggle for the highlighted row, and say what it will cost."""

        row = self._current()
        toggle = self.query_one("#plugin-toggle", Button)
        reinstate = self.query_one("#plugin-reinstate", Button)
        detail = self.query_one("#plugin-detail", Static)

        if row is None:
            toggle.disabled = True
            reinstate.disabled = True
            detail.update("")
            detail.set_class(False, "-warning")
            return

        toggle.label = "Disable" if row.enabled else "Enable"
        # Nothing loaded and no name in the enable-list to add or remove: there
        # is no act available, and a control that quietly does nothing is worse
        # than no control. A *user* row with no kinds is the ordinary
        # not-yet-enabled case and stays live -- that is the whole feature.
        toggle.disabled = row.source != "user" and not row.kinds
        reinstate.disabled = not self._can_reinstate(row)
        detail.set_class(row.state not in QUIET_STATES, "-warning")
        detail.update(self._detail(row))

    def _can_reinstate(self, row: PluginStatus) -> bool:
        """Re-enable applies to a plugin a *fault* took out, not to a choice.

        Phase 1 made a failing stage stay disabled for the session rather than
        being retried and re-reported every render. This is the way back that
        decision assumed, and it only means anything where there is a live
        plugin to put back — an import that never produced one is fixed by
        fixing the plugin.
        """

        return row.category == "runtime" and not row.reinstate

    def _detail(self, row: PluginStatus) -> str:
        parts = [f"{row.source}: {row.origin}"]
        if row.reads_content:
            # Stated in full here, because the row only has room for a glyph
            # and this is not a fact to leave an operator guessing at.
            parts.append(
                "⚑ Reads your log lines and delivers them wherever it is "
                "configured to send them."
            )
        if row.provenance:
            # Under the origin, which says *where the file is*; this says where
            # it came from and who vouched for it. Empty for a bundled plugin
            # and for one copied in by hand, so no row grows a line saying CLV
            # has nothing to tell you.
            parts.append(row.provenance)
        if row.state == "isolated":
            # Ahead of `detail`, which for an isolated row is CLV's own summary
            # of the same fact. This is the sentence that has to survive being
            # read quickly, so it goes where the eye lands first.
            parts.append(ISOLATED_NOTE)
        if row.detail:
            parts.append(row.detail)
        consequence = self._consequence(row)
        if consequence:
            parts.append(consequence)
        return "\n".join(parts)

    def _consequence(self, row: PluginStatus) -> str:
        """What pressing the control will actually do, next to the control.

        Said here rather than as a toast afterwards. Two of these are the whole
        reason the phase needed a decision: loading is import-time and
        single-shot, so enabling something never imported cannot take effect
        now; and the enable-list governs the user directory only, so switching
        off a plugin CLV shipped lasts for the session and no longer.
        """

        saved = self._saved_for(row)
        if row.reinstate:
            return "Will be put back into service."
        if row.enabled == saved.enabled:
            return ""
        if row.enabled:
            return (
                "Will be named in settings.conf — takes effect after a restart."
                if row.source == "user"
                else "Will be back in service."
            )
        if row.source == "user":
            return "Will be removed from settings.conf, and stops now."
        return "Off for this session only — a bundled plugin returns on restart."

    def _saved_for(self, row: PluginStatus) -> PluginStatus:
        for original in self._original:
            if original.origin == row.origin:
                return original
        return row

    # --- the two controls ----------------------------------------------------

    def _toggle_current(self) -> None:
        row = self._current()
        if row is None:
            return
        self._replace_current(enabled=not row.enabled, reinstate=False)

    def _reinstate_current(self) -> None:
        row = self._current()
        if row is None or not self._can_reinstate(row):
            return
        self._replace_current(reinstate=True, enabled=True)

    # --- input ---------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self._finish()
        elif event.key == "space":
            event.stop()
            self._toggle_current()
        elif event.key == "r":
            event.stop()
            self._reinstate_current()

    def on_option_list_option_highlighted(self, _event) -> None:
        self._sync_controls()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._toggle_current()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "plugin-toggle":
            self._toggle_current()
        elif event.button.id == "plugin-reinstate":
            self._reinstate_current()
        elif event.button.id == "plugin-close":
            self._finish()

    def _finish(self) -> None:
        # None means "nothing changed", so the app can skip rewriting the
        # operator's settings file for a dialog that was only looked at.
        changed = tuple(self._rows) != self._original
        self.dismiss(tuple(self._rows) if changed else None)
