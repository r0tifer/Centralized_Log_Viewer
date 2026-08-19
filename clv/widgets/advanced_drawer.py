"""Secondary controls: discovery rules, search options and plugin status.

Every control in here is bound to real state. The drawer emits
:class:`AdvancedFiltersDrawer.SettingsChanged` with a complete snapshot; the
app decides what a change implies (a glob edit re-runs discovery, a search
option only re-renders).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Input, Label, Static, Switch

from ..services.discovery import DiscoverySettings

#: One-line reminder of the query grammar, shown under Search options. Kept
#: short enough to survive 80 columns without wrapping into the plugin status.
QUERY_SYNTAX_HINT = (
    'Query: plain text is a regex · field terms: host:web01 · status>=500 · '
    'tag!=cron · msg:"disk full"'
)


@dataclass(frozen=True)
class AdvancedSettings:
    """Everything the drawer controls, as one value."""

    include_globs: str = ""
    exclude_globs: str = ""
    follow_symlinks: bool = False
    skip_binary: bool = True
    max_buffer_lines: int = 5000
    case_sensitive: bool = False
    use_regex: bool = True
    invert_match: bool = False
    group_rotated: bool = True

    def to_discovery(self, base: DiscoverySettings) -> DiscoverySettings:
        """Fold the discovery-related fields into a DiscoverySettings."""
        return replace(
            base,
            include_globs=_split(self.include_globs),
            exclude_globs=_split(self.exclude_globs) or base.exclude_globs,
            follow_symlinks=self.follow_symlinks,
            skip_binary=self.skip_binary,
            group_rotated=self.group_rotated,
        )

    def affects_discovery(self, other: "AdvancedSettings") -> bool:
        """True when a change between the two requires re-scanning sources."""
        return (
            self.include_globs != other.include_globs
            or self.exclude_globs != other.exclude_globs
            or self.follow_symlinks != other.follow_symlinks
            or self.skip_binary != other.skip_binary
            # Grouping only changes how the tree is built, not what the walk
            # finds — but the tree is built from a report, so it is rebuilt the
            # same way as any other discovery change rather than by a second
            # path that exists only for this.
            or self.group_rotated != other.group_rotated
        )


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class AdvancedFiltersDrawer(Static):
    """Collapsible panel of secondary options."""

    DEFAULT_CSS = """
    AdvancedFiltersDrawer {
        height: auto;
        max-height: 16;
        overflow-y: auto;
        border-bottom: solid $surface 25%;
        padding: 1 2;
        background: $surface 3%;
    }

    AdvancedFiltersDrawer.-hidden { display: none; }

    /* height must cover the padding as well as the text: `height: 1` with
       `padding-bottom: 1` left zero rows for content, so every section
       heading in this drawer laid out correctly but painted nothing. */
    AdvancedFiltersDrawer .drawer-heading {
        text-style: bold;
        color: $text-muted;
        height: auto;
        padding-bottom: 1;
    }

    AdvancedFiltersDrawer .drawer-row {
        layout: horizontal;
        height: auto;
        width: 1fr;
    }

    AdvancedFiltersDrawer .drawer-row > * { margin-right: 2; }
    AdvancedFiltersDrawer .drawer-row > *:last-child { margin-right: 0; }

    AdvancedFiltersDrawer .drawer-field {
        layout: vertical;
        width: 1fr;
        height: auto;
    }

    AdvancedFiltersDrawer .drawer-field > Label {
        color: $text-muted;
        height: 1;
    }

    AdvancedFiltersDrawer .drawer-toggle {
        layout: vertical;
        width: auto;
        min-width: 12;
        height: auto;
    }

    AdvancedFiltersDrawer Input {
        border: tall $surface 20%;
        background: $surface 8%;
        width: 1fr;
        height: 3;
    }

    AdvancedFiltersDrawer Switch { height: 3; }

    /* Wraps rather than truncates at 80 columns: the syntax is only useful
       if all of it is readable. */
    AdvancedFiltersDrawer #query-syntax {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    AdvancedFiltersDrawer #plugin-status {
        color: $text-muted;
        height: auto;
        padding-top: 1;
    }

    /* Read-only list of what Ctrl+E can write, so the exporters are visible
       without opening the dialog. No top padding: it sits directly under the
       plugin line as a second detail of the same block. */
    AdvancedFiltersDrawer #export-status,
    AdvancedFiltersDrawer #journald-status,
    AdvancedFiltersDrawer #ssh-status,
    AdvancedFiltersDrawer #watch-status {
        color: $text-muted;
        height: auto;
    }

    AdvancedFiltersDrawer #drawer-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    AdvancedFiltersDrawer #drawer-actions Button {
        height: 3;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }

    AdvancedFiltersDrawer #view-toggles {
        height: auto;
        width: 1fr;
    }

    /* When the query bar is wide enough to show its own auto-scroll and
       structured switches, this mirror is redundant and would be a second
       place to change the same setting. */
    AdvancedFiltersDrawer.-merged #view-toggles { display: none; }

    /* Deliberately *not* inside #view-toggles: the clipboard and detail-pane
       switches each have only one home, so hiding them at -merged (where the
       query bar shows its own copies of the other two) would make them vanish
       above 148 columns. Giving the detail pane a query-bar copy instead would
       push that row past BREAKPOINT_MERGE, which was measured for two. */
    AdvancedFiltersDrawer #output-options {
        height: auto;
        width: 1fr;
    }

    /* Stack the rows when there isn't width to share. The breakpoint class is
       mirrored onto this widget by the app, because DEFAULT_CSS is scoped and
       cannot reach the app node. */
    AdvancedFiltersDrawer.-compact .drawer-row {
        layout: vertical;
    }

    AdvancedFiltersDrawer.-compact .drawer-row > * {
        margin-right: 0;
        width: 1fr;
    }
    """

    def __init__(self, settings: AdvancedSettings | None = None) -> None:
        super().__init__(id="advanced-drawer")
        self._settings = settings or AdvancedSettings()
        self._visible = False
        # View state is owned by the app (it lives in SessionState); the drawer
        # only mirrors it. Held here so compose() can seed the switches.
        self._auto_scroll = True
        self._structured = False
        self._clipboard = True
        self._detail_pane = False
        self._watch_rules = True
        self._timeline = False
        self._clustering = False
        self._journald = False
        self._ssh = False
        self.add_class("-hidden")

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Mirrors the query bar's auto-scroll / structured switches, which are
        # only rendered when that bar is wide enough to merge its rows. This
        # section is hidden in that case, so exactly one copy is ever visible.
        with Container(id="view-toggles"):
            yield Label("View", classes="drawer-heading")
            with Horizontal(classes="drawer-row"):
                with Vertical(classes="drawer-toggle"):
                    yield Label("Auto-scroll")
                    yield Switch(value=self._auto_scroll, id="drawer-auto-scroll")
                with Vertical(classes="drawer-toggle"):
                    yield Label("Structured")
                    yield Switch(value=self._structured, id="drawer-structured")
                yield Static("", classes="drawer-field")

        # Its own container rather than more toggles in the View row above:
        # that row disappears when the query bar shows its own copies, and
        # these two switches have nowhere else to live. What they have in
        # common is being *single-home* — the keyboard is their only other
        # path — which is why they share a section rather than a subject.
        #
        # They also share a row, which is not cosmetic: the drawer is capped at
        # max-height 16 and scrolls, so a section per switch pushed "Source
        # discovery" below the fold where it laid out and painted nothing.
        with Container(id="output-options"):
            yield Label("Output & panes", classes="drawer-heading")
            with Horizontal(classes="drawer-row"):
                with Vertical(classes="drawer-toggle"):
                    yield Label("Clipboard (OSC 52)")
                    yield Switch(value=self._clipboard, id="drawer-clipboard")
                with Vertical(classes="drawer-toggle"):
                    yield Label("Detail pane")
                    yield Switch(value=self._detail_pane, id="drawer-detail-pane")
                # Takes the spacer's place rather than adding a row: this
                # drawer scrolls at max-height 16, and a new row here is what
                # pushes "Source discovery" below the fold. Single-home like
                # its neighbours — `W` manages the rules, this switches the
                # whole set on or off.
                with Vertical(classes="drawer-toggle"):
                    yield Label("Watch rules")
                    yield Switch(value=self._watch_rules, id="drawer-watch-rules")
                # Items 14 and 15 both asked for the View section above. They
                # join this row instead, for the two reasons already recorded
                # here: #view-toggles vanishes above 148 columns and neither
                # switch has a query-bar copy, and a *new* row is what pushes
                # "Source discovery" past max-height 16.
                with Vertical(classes="drawer-toggle"):
                    yield Label("Timeline")
                    yield Switch(value=self._timeline, id="drawer-timeline")
                with Vertical(classes="drawer-toggle"):
                    yield Label("Collapse repeats")
                    yield Switch(value=self._clustering, id="drawer-clustering")

        yield Label("Source discovery", classes="drawer-heading")
        with Horizontal(classes="drawer-row"):
            with Vertical(classes="drawer-field"):
                yield Label("Include (globs, comma separated)")
                yield Input(
                    value=self._settings.include_globs,
                    placeholder="*.log, *.txt, syslog*  — empty means every text file",
                    id="include-globs",
                )
            with Vertical(classes="drawer-field"):
                yield Label("Exclude (globs, comma separated)")
                yield Input(
                    value=self._settings.exclude_globs,
                    placeholder="*.gz, nested/*",
                    id="exclude-globs",
                )

        with Horizontal(classes="drawer-row"):
            with Vertical(classes="drawer-toggle"):
                yield Label("Follow symlinks")
                yield Switch(value=self._settings.follow_symlinks, id="follow-symlinks")
            with Vertical(classes="drawer-toggle"):
                yield Label("Skip binary")
                yield Switch(value=self._settings.skip_binary, id="skip-binary")
            # Added to this row rather than a row of its own, for the reason
            # recorded above the watch-rules switch: the drawer is capped at
            # max-height 16 and every new row pushes what follows below the
            # fold, where it lays out and paints nothing.
            with Vertical(classes="drawer-toggle"):
                yield Label("Group rotated")
                yield Switch(value=self._settings.group_rotated, id="group-rotated")
            # Discovery, not output: the journal is a source, and this is where
            # an operator looks for "what counts as a source". Disabled with a
            # caption where journalctl is unavailable, so the answer to "why is
            # there no journal here" is on screen rather than in the docs.
            with Vertical(classes="drawer-toggle"):
                yield Label("Journal (systemd)")
                yield Switch(value=self._journald, id="drawer-journald")
            # `Remote (SSH)` rather than `Remote sources (SSH)`, and the reason
            # is measurable: `.drawer-toggle` is `width: auto`, so this row's
            # width is the sum of its label lengths, and `-compact` only stacks
            # them below 90 columns. The longer label overflows the row between
            # 90 and 96 — where nothing stacks and the 80-column test cannot see
            # it — before the Input beside it gets a single cell.
            with Vertical(classes="drawer-toggle"):
                yield Label("Remote (SSH)")
                yield Switch(value=self._ssh, id="drawer-ssh")
            with Vertical(classes="drawer-field"):
                yield Label("Buffered lines per source")
                yield Input(
                    value=str(self._settings.max_buffer_lines),
                    placeholder="5000",
                    id="max-buffer-lines",
                )

        yield Label("Search options", classes="drawer-heading")
        with Horizontal(classes="drawer-row"):
            with Vertical(classes="drawer-toggle"):
                yield Label("Case sensitive")
                yield Switch(value=self._settings.case_sensitive, id="case-sensitive")
            with Vertical(classes="drawer-toggle"):
                yield Label("Regex")
                yield Switch(value=self._settings.use_regex, id="use-regex")
            with Vertical(classes="drawer-toggle"):
                yield Label("Invert match")
                yield Switch(value=self._settings.invert_match, id="invert-match")
            yield Static("", classes="drawer-field")

        # One line, not a section: the drawer is capped at max-height 16 and a
        # whole new heading here is what once pushed "Source discovery" below
        # the fold, where it laid out and painted nothing.
        yield Static(QUERY_SYNTAX_HINT, id="query-syntax")

        yield Static("", id="plugin-status")
        yield Static("", id="export-status")
        yield Static("", id="watch-status")
        yield Static("", id="journald-status")
        yield Static("", id="ssh-status")

        # A third button here rather than a fourth switch above, and the
        # reason is the one recorded four times in this file: `#drawer-actions`
        # is `layout: horizontal`, so a button joins this row and costs *zero*
        # rows against `max-height: 16`, while any new toggle row pushes what
        # follows below the fold where it lays out and paints nothing. It is
        # also honest about what it is — a one-shot scan of somebody else's
        # file, not a setting that stays on.
        with Container(id="drawer-actions"):
            yield Button("Scan SSH config", id="scan-ssh-config")
            yield Button("Rescan sources", id="rescan-sources", variant="primary")
            yield Button("Close", id="close-advanced")

    # --- state --------------------------------------------------------------

    @property
    def settings(self) -> AdvancedSettings:
        return self._settings

    def set_plugin_status(self, text: str) -> None:
        try:
            self.query_one("#plugin-status", Static).update(text)
        except NoMatches:
            pass

    def set_export_status(self, text: str) -> None:
        """Show what the export dialog offers. Read-only: `Ctrl+E` runs it."""
        try:
            self.query_one("#export-status", Static).update(text)
        except NoMatches:
            pass

    def set_watch_status(self, text: str) -> None:
        """Show how many watch rules are live. Read-only: `W` manages them."""
        try:
            self.query_one("#watch-status", Static).update(text)
        except NoMatches:
            pass

    def set_journald(self, enabled: bool, *, available: bool = True, reason: str = "") -> None:
        """Show whether the journal is on, and disable the switch when it cannot be.

        A switch that silently does nothing is worse than no switch: where
        `journalctl` is missing this one is disabled and the status line says
        why, so "there is no journal here" is answered on screen.
        """

        self._journald = enabled
        try:
            switch = self.query_one("#drawer-journald", Switch)
            with self.prevent(Switch.Changed):
                switch.value = enabled
            switch.disabled = not available
            self.query_one("#journald-status", Static).update(
                f"Journal: {reason}" if reason else ""
            )
        except NoMatches:  # not composed yet
            pass

    def set_ssh(self, enabled: bool, *, available: bool = True, reason: str = "") -> None:
        """Show whether remote sources are on, and summarise the hosts in a line.

        One line for the whole fleet, deliberately. The drawer is capped at
        `max-height: 16` and every row added here pushes `#drawer-actions` below
        the fold, where it lays out and paints nothing — so five hosts get
        "3 hosts · 2 reachable · web03 unreachable" and the per-host detail lives
        in the dialog `R` opens, which has room for it.
        """

        self._ssh = enabled
        try:
            switch = self.query_one("#drawer-ssh", Switch)
            with self.prevent(Switch.Changed):
                switch.value = enabled
            switch.disabled = not available
            self.query_one("#ssh-status", Static).update(
                f"Remote: {reason}" if reason else ""
            )
        except NoMatches:  # not composed yet
            pass

    def _emit(self, previous: AdvancedSettings) -> None:
        self.post_message(self.SettingsChanged(self._settings, previous))

    def sync_settings(self, settings: AdvancedSettings) -> None:
        """Adopt a settings snapshot and show it, without emitting.

        For the times something other than this drawer decides a search or
        discovery option — a saved view being applied, a chip being dismissed.
        Without it the switches keep displaying the old answer while the filter
        uses the new one. Suppression is ``prevent`` rather than a flag, for the
        reason spelled out in :meth:`sync_view_toggles`.
        """

        self._settings = settings
        try:
            with self.prevent(Switch.Changed, Input.Changed):
                self.query_one("#include-globs", Input).value = settings.include_globs
                self.query_one("#exclude-globs", Input).value = settings.exclude_globs
                self.query_one("#follow-symlinks", Switch).value = settings.follow_symlinks
                self.query_one("#skip-binary", Switch).value = settings.skip_binary
                self.query_one("#group-rotated", Switch).value = settings.group_rotated
                self.query_one("#case-sensitive", Switch).value = settings.case_sensitive
                self.query_one("#use-regex", Switch).value = settings.use_regex
                self.query_one("#invert-match", Switch).value = settings.invert_match
        except NoMatches:  # not composed yet
            pass

    def sync_view_toggles(
        self,
        *,
        auto_scroll: bool,
        structured: bool,
        clipboard: bool | None = None,
        detail_pane: bool | None = None,
        watch_rules: bool | None = None,
        timeline: bool | None = None,
        clustering: bool | None = None,
    ) -> None:
        """Mirror the app's view state onto this drawer's switches.

        Does not emit: the app is the owner, and echoing back would bounce
        between the two copies of these controls. Suppression uses ``prevent``
        rather than a flag because Switch.Changed is posted asynchronously --
        a flag cleared at the end of this method is already back to False by
        the time the handler runs.

        ``clipboard``, ``detail_pane`` and ``watch_rules`` are optional because
        none of those switches has a second copy in the query bar: they only
        need seeding, not continuous mirroring.
        """

        self._auto_scroll = auto_scroll
        self._structured = structured
        if clipboard is not None:
            self._clipboard = clipboard
        if detail_pane is not None:
            self._detail_pane = detail_pane
        if watch_rules is not None:
            self._watch_rules = watch_rules
        if timeline is not None:
            self._timeline = timeline
        if clustering is not None:
            self._clustering = clustering
        try:
            with self.prevent(Switch.Changed):
                self.query_one("#drawer-auto-scroll", Switch).value = auto_scroll
                self.query_one("#drawer-structured", Switch).value = structured
                if clipboard is not None:
                    self.query_one("#drawer-clipboard", Switch).value = clipboard
                if detail_pane is not None:
                    self.query_one("#drawer-detail-pane", Switch).value = detail_pane
                if watch_rules is not None:
                    self.query_one("#drawer-watch-rules", Switch).value = watch_rules
                if timeline is not None:
                    self.query_one("#drawer-timeline", Switch).value = timeline
                if clustering is not None:
                    self.query_one("#drawer-clustering", Switch).value = clustering
        except NoMatches:  # not composed yet
            pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
        switch_id = event.switch.id or ""

        # Switches inside the drawer are ours; stop the event either way so the
        # app's global handler does not also try to interpret it.
        view_field = {
            "drawer-auto-scroll": "auto_scroll",
            "drawer-structured": "structured",
            "drawer-clipboard": "clipboard",
            "drawer-detail-pane": "detail_pane",
            "drawer-watch-rules": "watch_rules",
            "drawer-timeline": "timeline",
            "drawer-clustering": "clustering",
            # A view toggle in mechanism only: the app owns it, acts on it, and
            # syncs it back. What it actually does is grant consent to run a
            # subprocess, which is why it is the app's decision and not this
            # widget's — see LogViewerApp._set_journald.
            "drawer-journald": "journald",
            # Same mechanism and the same reason as the journal above: consent to
            # spawn a subprocess is the app's decision, not this widget's. A
            # *network* subprocess raises that bar rather than lowering it.
            "drawer-ssh": "ssh",
        }.get(switch_id)
        if view_field is not None:
            event.stop()
            setattr(self, f"_{view_field}", event.value)
            self.post_message(self.ViewToggleChanged(view_field, event.value))
            return

        field = {
            "follow-symlinks": "follow_symlinks",
            "skip-binary": "skip_binary",
            "group-rotated": "group_rotated",
            "case-sensitive": "case_sensitive",
            "use-regex": "use_regex",
            "invert-match": "invert_match",
        }.get(switch_id)
        if field is None:
            return
        event.stop()
        previous = self._settings
        self._settings = replace(self._settings, **{field: event.value})
        self._emit(previous)

    def on_input_changed(self, event: Input.Changed) -> None:
        field = {
            "include-globs": "include_globs",
            "exclude-globs": "exclude_globs",
        }.get(event.input.id or "")
        if field is not None:
            event.stop()
            previous = self._settings
            self._settings = replace(self._settings, **{field: event.value})
            self._emit(previous)
            return

        if event.input.id == "max-buffer-lines":
            event.stop()
            text = event.value.strip()
            if not text.isdigit():
                return  # ignore partial input rather than fighting the user
            previous = self._settings
            self._settings = replace(self._settings, max_buffer_lines=max(1, int(text)))
            self._emit(previous)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-ssh-config":
            event.stop()
            self.post_message(self.ScanSSHConfigRequested())
        elif event.button.id == "rescan-sources":
            event.stop()
            self.post_message(self.RescanRequested())
        elif event.button.id == "close-advanced":
            event.stop()
            self.hide()
            self.post_message(self.Closed())

    # --- visibility ---------------------------------------------------------

    def show(self) -> None:
        self.remove_class("-hidden")
        self._visible = True

    def hide(self) -> None:
        self.add_class("-hidden")
        self._visible = False

    def toggle(self) -> bool:
        self.hide() if self._visible else self.show()
        return self._visible

    @property
    def visible(self) -> bool:
        return self._visible

    # --- messages -----------------------------------------------------------

    class SettingsChanged(Message):
        """Any drawer control changed. Carries the full before/after snapshot."""

        def __init__(self, settings: AdvancedSettings, previous: AdvancedSettings) -> None:
            super().__init__()
            self.settings = settings
            self.previous = previous

        @property
        def needs_rescan(self) -> bool:
            return self.settings.affects_discovery(self.previous)

    class ViewToggleChanged(Message):
        """A view switch was flipped from inside the drawer.

        ``field`` is one of ``auto_scroll``, ``structured`` or ``clipboard``.
        Separate from SettingsChanged because these are view state owned by the
        app, not discovery or search settings, and they never trigger a rescan.
        """

        def __init__(self, field: str, value: bool) -> None:
            super().__init__()
            self.field = field
            self.value = value

    class RescanRequested(Message):
        """The user asked to re-run discovery now."""

    class ScanSSHConfigRequested(Message):
        """The user asked to look in ``~/.ssh/config`` for machines to import.

        Carries nothing, because this drawer knows nothing to carry: it does not
        read that file, does not know what a host is, and does not write
        ``settings.conf``. The app owns the scan, the picker and the write —
        which is the same division the SSH switch above already follows.
        """

    class Closed(Message):
        """The drawer was dismissed."""
