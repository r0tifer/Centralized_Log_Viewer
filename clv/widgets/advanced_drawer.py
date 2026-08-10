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

    def to_discovery(self, base: DiscoverySettings) -> DiscoverySettings:
        """Fold the discovery-related fields into a DiscoverySettings."""
        return replace(
            base,
            include_globs=_split(self.include_globs),
            exclude_globs=_split(self.exclude_globs) or base.exclude_globs,
            follow_symlinks=self.follow_symlinks,
            skip_binary=self.skip_binary,
        )

    def affects_discovery(self, other: "AdvancedSettings") -> bool:
        """True when a change between the two requires re-scanning sources."""
        return (
            self.include_globs != other.include_globs
            or self.exclude_globs != other.exclude_globs
            or self.follow_symlinks != other.follow_symlinks
            or self.skip_binary != other.skip_binary
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

    AdvancedFiltersDrawer #plugin-status {
        color: $text-muted;
        height: auto;
        padding-top: 1;
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

        yield Static("", id="plugin-status")

        with Container(id="drawer-actions"):
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

    def _emit(self, previous: AdvancedSettings) -> None:
        self.post_message(self.SettingsChanged(self._settings, previous))

    def sync_view_toggles(self, *, auto_scroll: bool, structured: bool) -> None:
        """Mirror the app's view state onto this drawer's switches.

        Does not emit: the app is the owner, and echoing back would bounce
        between the two copies of these controls. Suppression uses ``prevent``
        rather than a flag because Switch.Changed is posted asynchronously --
        a flag cleared at the end of this method is already back to False by
        the time the handler runs.
        """

        self._auto_scroll = auto_scroll
        self._structured = structured
        try:
            with self.prevent(Switch.Changed):
                self.query_one("#drawer-auto-scroll", Switch).value = auto_scroll
                self.query_one("#drawer-structured", Switch).value = structured
        except NoMatches:  # not composed yet
            pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
        switch_id = event.switch.id or ""

        # Switches inside the drawer are ours; stop the event either way so the
        # app's global handler does not also try to interpret it.
        view_field = {
            "drawer-auto-scroll": "auto_scroll",
            "drawer-structured": "structured",
        }.get(switch_id)
        if view_field is not None:
            event.stop()
            setattr(self, f"_{view_field}", event.value)
            self.post_message(self.ViewToggleChanged(view_field, event.value))
            return

        field = {
            "follow-symlinks": "follow_symlinks",
            "skip-binary": "skip_binary",
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
        if event.button.id == "rescan-sources":
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
        """Auto-scroll or structured output was flipped from inside the drawer.

        Separate from SettingsChanged because these are view state owned by the
        app, not discovery or search settings, and they never trigger a rescan.
        """

        def __init__(self, field: str, value: bool) -> None:
            super().__init__()
            self.field = field
            self.value = value

    class RescanRequested(Message):
        """The user asked to re-run discovery now."""

    class Closed(Message):
        """The drawer was dismissed."""
