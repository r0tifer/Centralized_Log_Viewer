"""A plugin's modal, built from a description rather than from a widget.

**The whole of Requirement 11 in one screen.** A plugin hands over a
:class:`~clv.plugins.Panel` — a title and a tuple of
:class:`~clv.plugins.Control` descriptions — and this widget decides what that
looks like. CLV owns the CSS, the layout and the breakpoint behaviour, so a
panel cannot move a breakpoint, cannot overflow 80 columns, and cannot make any
existing layout test conditional on what happens to be installed. **Plugins ship
no CSS**, and this file is why that rule costs them nothing worth having.

It is a modal for the same reason `PLUGIN_TODO.md` gives: full-screen means no
interaction with the main layout at all, which is what makes it the one place a
plugin can be given real room.

**This widget knows nothing about plugins.** It is handed a panel and a
callback, exactly as :class:`~clv.widgets.remote_hosts_dialog.RemoteHostsDialog`
is handed a probe — so it imports nothing from ``clv.plugins``, and the guard,
the budget and the disable-on-raise all live in the app where the registry is.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

#: What the app hands back when a control changes: a replacement panel, or
#: ``None`` to leave the screen as it is. Typed here rather than imported so
#: this module stays free of ``clv.plugins``.
ControlCallback = Callable[[str, Any, Mapping[str, Any]], Optional[Any]]

#: The control kinds that hold a value and therefore appear in the form handed
#: to a plugin. The other three — ``label``, ``static``, ``button`` — say
#: something or do something; none of them has a value to be read back.
_VALUE_KINDS = frozenset({"switch", "input", "select"})


class PluginPanelScreen(ModalScreen[None]):
    """Draw a plugin's panel, and report every change back to it."""

    DEFAULT_CSS = """
    PluginPanelScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #panel-dialog {
        width: 100%;
        max-width: 76;
        height: 100%;
        max-height: 24;
        padding: 1 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    #panel-title {
        text-style: bold;
        height: 1;
    }

    /* Scrolls rather than clipping: a panel may hold up to
       MAX_PANEL_CONTROLS controls and 24 rows does not fit them, which is the
       same answer the help overlay gives to the same problem. */
    #panel-body {
        height: 1fr;
    }

    #panel-body .panel-control {
        layout: vertical;
        height: auto;
        width: 1fr;
        padding-bottom: 1;
    }

    #panel-body .panel-control > Label {
        color: $text-muted;
        height: 1;
    }

    #panel-body .panel-static {
        height: auto;
        width: 1fr;
        padding-bottom: 1;
    }

    #panel-body .panel-heading {
        color: $text-muted;
        text-style: bold;
        height: auto;
        padding-bottom: 1;
    }

    #panel-body Input {
        border: tall $surface 20%;
        background: $surface 8%;
        width: 1fr;
        height: 3;
    }

    #panel-body Switch { height: 3; }

    #panel-body Button {
        height: 3;
        min-width: 8;
        padding: 0 1;
    }

    #panel-hint {
        color: $text-muted;
        height: auto;
    }

    #panel-actions {
        layout: horizontal;
        align: right middle;
        height: auto;
        padding-top: 1;
    }

    #panel-actions Button {
        height: 3;
        min-width: 8;
        margin-left: 1;
        padding: 0 1;
    }
    """

    HINT = "Esc closes"

    def __init__(self, panel: Any, *, on_control: ControlCallback) -> None:
        super().__init__()
        self._panel = panel
        self._on_control = on_control
        #: Every control's current value, keyed by id. Kept here rather than
        #: read off the widgets on demand because a redraw replaces the widgets
        #: and the plugin's next callback still has to see the whole form.
        self._values: dict[str, Any] = {}

    # --- composition ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="panel-dialog"):
            yield Static(self._panel.title, id="panel-title")
            yield VerticalScroll(id="panel-body")
            yield Static(self.HINT, id="panel-hint")
            with Container(id="panel-actions"):
                yield Button("Close", id="panel-close", variant="primary")

    async def on_mount(self) -> None:
        await self._draw(self._panel)

    # --- drawing -------------------------------------------------------------

    async def _draw(self, panel: Any) -> None:
        """Replace the body with *panel*'s controls, and reseed the values.

        Whole-body rather than a diff. A panel is at most
        ``MAX_PANEL_CONTROLS`` widgets and a redraw happens when a plugin asks
        for one, so the cost is trivial — and a diff would have to decide what
        "the same control" means across two descriptions that share only an id,
        which is a judgement this widget has no business making.
        """

        self._panel = panel
        body = self.query_one("#panel-body", VerticalScroll)
        await body.remove_children()
        self.query_one("#panel-title", Static).update(panel.title)

        # Only the kinds that *hold* something. A button is an event and a
        # label is prose, and a form that carried them would be handing the
        # plugin back an entry for `go` whose value is whatever `go` was
        # declared with -- a field nobody set, keyed by the id of a thing that
        # cannot be set.
        self._values = {
            control.id: self._initial(control)
            for control in panel.controls
            if control.kind in _VALUE_KINDS
        }

        for control in panel.controls:
            await body.mount(*self._widgets_for(control))

    def _initial(self, control: Any) -> Any:
        """What this control's value starts at, coerced to its kind.

        A plugin's declaration is trusted about *intent* and not about type: a
        switch declared with ``value="yes"`` means on, and handing the plugin
        back the string it wrote would make its own callback disagree with the
        widget an operator is looking at.
        """

        if control.kind == "switch":
            return bool(control.value)
        if control.kind == "select":
            allowed = [str(value) for value, _label in control.options]
            wanted = str(control.value) if control.value not in (None, "") else ""
            return wanted if wanted in allowed else (allowed[0] if allowed else "")
        if control.kind == "input":
            return "" if control.value is None else str(control.value)
        return control.value

    def _widgets_for(self, control: Any) -> list[Any]:
        """The widgets one control description becomes."""

        kind = control.kind
        if kind == "label":
            return [Static(control.label or "", classes="panel-heading")]
        if kind == "static":
            return [Static(control.label or str(control.value or ""), classes="panel-static")]

        if kind == "button":
            return [Button(control.label or control.id, id=self._widget_id(control.id))]

        inner: Any
        if kind == "switch":
            inner = Switch(value=bool(self._values[control.id]), id=self._widget_id(control.id))
        elif kind == "input":
            inner = Input(
                value=str(self._values[control.id]),
                placeholder=control.placeholder,
                id=self._widget_id(control.id),
            )
        else:  # select
            # `panel_fault` has already refused a select with no options, so
            # `_initial` cannot have fallen through to the empty string and
            # `allow_blank=False` always has something to hold.
            options = [(label, value) for value, label in control.options]
            inner = Select(
                options,
                value=self._values[control.id],
                allow_blank=False,
                id=self._widget_id(control.id),
            )

        return [
            Vertical(
                Label(control.label or control.id),
                inner,
                classes="panel-control",
            )
        ]

    @staticmethod
    def _widget_id(control_id: str) -> str:
        """Namespaced, because a plugin chooses its ids and CLV chooses its own.

        A plugin control called ``panel-close`` would otherwise be the Close
        button, and the screen would stop closing.
        """

        return f"panel-control-{control_id}"

    @staticmethod
    def _control_id(widget_id: str) -> Optional[str]:
        prefix = "panel-control-"
        return widget_id[len(prefix):] if widget_id.startswith(prefix) else None

    # --- input ---------------------------------------------------------------

    @property
    def values(self) -> Mapping[str, Any]:
        """The live form. Public so a test can read it without walking widgets."""

        return dict(self._values)

    async def _changed(self, control_id: str, value: Any) -> None:
        """Record *value* and ask the plugin what the panel should be now.

        **A value that did not change is not a change.** `Select` posts
        `Changed` as it mounts, and so does an `Input` seeded with a value, so
        without this every panel fired a callback per control the moment it
        opened — for controls nobody had touched, before the operator had seen
        the screen. A plugin that answers a callback with a redraw would have
        looped on it.

        Compared against the stored form rather than suppressed with
        `prevent()`, which is what the drawer uses for the same class of
        problem: `prevent` bounds a block of *this* method's execution, and
        these messages arrive from a widget's own mount, after any block here
        has ended. Comparing is also true for a reason beyond the mechanism —
        `on_control` means a control changed, and being told a switch is still
        off is not that.
        """

        if control_id in self._values:
            if self._values[control_id] == value:
                return
            self._values[control_id] = value
        answer = self._on_control(control_id, value, dict(self._values))
        if answer is None:
            return
        if getattr(answer, "dismiss", False):
            self.dismiss(None)
            return
        await self._draw(answer)

    async def on_switch_changed(self, event: Switch.Changed) -> None:
        control_id = self._control_id(event.switch.id or "")
        if control_id is None:
            return
        event.stop()
        await self._changed(control_id, event.value)

    async def on_input_changed(self, event: Input.Changed) -> None:
        control_id = self._control_id(event.input.id or "")
        if control_id is None:
            return
        event.stop()
        await self._changed(control_id, event.value)

    async def on_select_changed(self, event: Select.Changed) -> None:
        control_id = self._control_id(event.select.id or "")
        if control_id is None:
            return
        event.stop()
        value = "" if event.value is Select.BLANK else event.value
        await self._changed(control_id, value)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "panel-close":
            self.dismiss(None)
            return
        control_id = self._control_id(event.button.id or "")
        if control_id is None:
            return
        # A button carries no value of its own, so `True` is what a press means
        # and what `on_control` is told. It is deliberately not recorded into
        # `values`: a button is an event, not a field, and a form that
        # remembered which buttons had been pressed would be answering a
        # question nobody asked it.
        await self._changed(control_id, True)

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
