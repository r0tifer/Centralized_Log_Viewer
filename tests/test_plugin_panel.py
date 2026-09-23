"""A plugin draws, and CLV still owns the layout.

The second half of Phase 12. A :class:`~clv.plugins.Panel` is a *description* —
a title and a tuple of :class:`~clv.plugins.Control` descriptions — and
:class:`~clv.widgets.plugin_panel.PluginPanelScreen` decides what that looks
like. So the claims worth testing are not "does it render", but the three that
the description-not-a-widget division exists to make true:

**A panel cannot break the layout.** It is a modal, so it interacts with nothing
in the main tree; it is drawn from six kinds CLV styles; and at 80x24 it stays
inside the screen and scrolls rather than clipping.

**The form round-trips, and only the parts that are a form.** Every value-
bearing control is in ``context.values`` on every callback, and buttons, labels
and static text are not — a button is an event, and a form carrying one would
hand the plugin a field nobody can set.

**A panel cannot outlive the plugin behind it.** A callback that raises, a
callback that strikes out on the budget, and a panel a plugin describes wrongly
all end the same way: the plugin is out of service, the screen is gone, and CLV
is still running. A form whose callbacks no longer run is a form that lies about
what pressing its buttons will do.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from textual.widgets import Input, Select, Switch

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import (
    Command,
    CommandContext,
    Control,
    MAX_PANEL_CONTROLS,
    Panel,
    PluginRegistry,
    panel_fault,
)
from clv.widgets.plugin_panel import PluginPanelScreen


# --- plugins under test ------------------------------------------------------


def _form() -> Panel:
    return Panel(
        title="Settings",
        controls=(
            Control("label", "heading", label="Options"),
            Control("static", "note", label="Three lines match."),
            Control("switch", "raw", label="Raw", value=False),
            Control("input", "path", label="Write to", value="/tmp/x", placeholder="…"),
            Control(
                "select",
                "mode",
                label="Mode",
                value="b",
                options=(("a", "Alpha"), ("b", "Beta")),
            ),
            Control("button", "save", label="Save"),
        ),
    )


class Former(Command):
    """Opens a form, records every callback, and closes on Save."""

    name = "former"
    command_name = "form"
    title = "A form"

    def __init__(self, answer: Optional[Panel] = None) -> None:
        self.seen: list[tuple[str, object, dict]] = []
        self._answer = answer

    def run(self, context: CommandContext) -> Panel:
        return _form()

    def on_control(self, control_id, value, context):
        self.seen.append((control_id, value, dict(context.values)))
        if control_id == "save":
            return Panel(dismiss=True)
        return self._answer


class Slow(Command):
    """Spends more than the budget on every callback."""

    name = "slow"
    command_name = "slow"
    title = "Slow"

    def run(self, context: CommandContext) -> Panel:
        return _form()

    def on_control(self, control_id, value, context):
        import time

        time.sleep(0.02)
        return None


class Broken(Command):
    name = "broken"
    command_name = "broken"
    title = "Broken"

    def __init__(self, *, in_run: bool = False) -> None:
        self._in_run = in_run

    def run(self, context: CommandContext):
        if self._in_run:
            return "not a panel"
        return _form()

    def on_control(self, control_id, value, context):
        raise RuntimeError("nope")


# --- harness -----------------------------------------------------------------


def _attach(app: LogViewerApp, *plugins) -> PluginRegistry:
    registry = PluginRegistry()
    for plugin in plugins:
        assert registry.add(
            plugin, origin=f"/tmp/{plugin.name}.py", clv_version=__version__
        ), [str(error) for error in registry.errors]
    registry.order()
    app._plugins = registry
    # Mirrors `on_mount`, which rebinds the budgets and adopts the new
    # registry's generation in the same breath. Without the last line the app
    # is left holding the generation of the registry it had *before* the swap,
    # and `_sync_plugin_generation` reads a later change as "nothing moved".
    app._panel_budget = app._new_panel_budget()
    app._plugin_generation = registry.generation
    app._install_command_bindings()
    return registry


async def _open_panel(pilot, app: LogViewerApp, command: Command) -> PluginPanelScreen:
    """Open *command*'s panel and wait for it to have laid out.

    Twice, deliberately: the first pause pushes the screen and mounts the
    controls, and the second is the one that gives them a region. A test that
    measures anything would otherwise be measuring a widget of height zero.
    """

    app._run_command(command)
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, PluginPanelScreen), type(screen).__name__
    await pilot.pause()
    return screen


# --- drawing -----------------------------------------------------------------


def test_every_control_kind_renders() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            assert screen.query_one("#panel-control-raw", Switch).value is False
            assert screen.query_one("#panel-control-path", Input).value == "/tmp/x"
            assert screen.query_one("#panel-control-mode", Select).value == "b"
            assert screen.query_one("#panel-control-save")

    asyncio.run(scenario())


def test_only_value_bearing_controls_are_in_the_form() -> None:
    """A button is an event and a label is prose. Neither is a field."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            assert set(screen.values) == {"raw", "path", "mode"}

    asyncio.run(scenario())


def test_a_control_id_cannot_collide_with_clvs_own() -> None:
    """A plugin control called `panel-close` must not become the Close button."""

    class Sneaky(Former):
        name = "sneaky"
        command_name = "sneaky"

        def run(self, context):
            return Panel("X", (Control("input", "panel-close", label="hm"),))

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Sneaky()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            # Both exist, and they are different widgets.
            assert isinstance(
                screen.query_one("#panel-control-panel-close"), Input
            )
            assert screen.query_one("#panel-close").id == "panel-close"

    asyncio.run(scenario())


def test_the_panel_fits_and_scrolls_at_eighty_by_twenty_four() -> None:
    """Requirement 11: a plugin's modal is measured against CLV's floor."""

    class Long(Former):
        name = "long"
        command_name = "long"

        def run(self, context):
            return Panel(
                "Long",
                tuple(
                    Control("input", f"f{index}", label=f"Field {index}")
                    for index in range(MAX_PANEL_CONTROLS)
                ),
            )

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            command = Long()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            dialog = screen.query_one("#panel-dialog")
            assert dialog.region.x >= 0 and dialog.region.y >= 0
            assert dialog.region.right <= 80
            assert dialog.region.bottom <= 24
            body = screen.query_one("#panel-body")
            assert body.max_scroll_y > 0, "32 fields at 24 rows must scroll"

    asyncio.run(scenario())


# --- round-tripping ----------------------------------------------------------


def test_a_switch_reaches_the_plugin_with_the_whole_form() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-raw", Switch).value = True
            await pilot.pause()

            control_id, value, values = command.seen[-1]
            assert (control_id, value) == ("raw", True)
            assert values == {"raw": True, "path": "/tmp/x", "mode": "b"}

    asyncio.run(scenario())


def test_an_input_reaches_the_plugin_as_it_is_typed() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-path", Input).value = "/tmp/y"
            await pilot.pause()

            assert command.seen[-1][:2] == ("path", "/tmp/y")
            assert command.seen[-1][2]["path"] == "/tmp/y"

    asyncio.run(scenario())


def test_a_select_reaches_the_plugin_as_its_value_not_its_label() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-mode", Select).value = "a"
            await pilot.pause()

            assert command.seen[-1][:2] == ("mode", "a")

    asyncio.run(scenario())


def test_a_button_press_carries_true_and_is_not_remembered() -> None:
    """A button is an event. The form it lives in has no field for it."""

    class Staying(Former):
        name = "staying"
        command_name = "staying"

        def on_control(self, control_id, value, context):
            # Never dismisses, so the press can be inspected with the panel
            # still open and the form still intact.
            self.seen.append((control_id, value, dict(context.values)))
            return None

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Staying()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-save").press()
            await pilot.pause()

            control_id, value, values = command.seen[-1]
            assert (control_id, value) == ("save", True)
            assert "save" not in values
            assert app.screen is screen

    asyncio.run(scenario())


def test_returning_a_panel_redraws_and_reseeds_the_form() -> None:
    class Redrawing(Former):
        name = "redrawing"
        command_name = "redrawing"

        def on_control(self, control_id, value, context):
            self.seen.append((control_id, value, dict(context.values)))
            return Panel(
                "Second",
                (Control("input", "other", label="Other", value="fresh"),),
            )

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Redrawing()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-raw", Switch).value = True
            await pilot.pause()

            assert str(screen.query_one("#panel-title").content) == "Second"
            assert screen.values == {"other": "fresh"}
            # The old controls are gone, not merely hidden.
            assert len(screen.query("#panel-control-raw").nodes) == 0

    asyncio.run(scenario())


def test_returning_none_leaves_the_screen_exactly_as_it_was() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-path", Input).value = "/tmp/y"
            await pilot.pause()

            assert app.screen is screen
            assert screen.query_one("#panel-control-path", Input).value == "/tmp/y"

    asyncio.run(scenario())


def test_a_dismissing_panel_closes_the_modal() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-save").press()
            await pilot.pause()

            assert not isinstance(app.screen, PluginPanelScreen)

    asyncio.run(scenario())


def test_escape_closes_the_panel() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            await _open_panel(pilot, app, command)

            await pilot.press("escape")
            await pilot.pause()

            assert not isinstance(app.screen, PluginPanelScreen)

    asyncio.run(scenario())


def test_a_panel_cannot_stack_twice() -> None:
    """A command reachable by a key is reachable while its own panel is open."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Former()
            _attach(app, command)
            await _open_panel(pilot, app, command)

            app._run_command(command)
            await pilot.pause()

            panels = [
                screen
                for screen in app.screen_stack
                if isinstance(screen, PluginPanelScreen)
            ]
            assert len(panels) == 1

    asyncio.run(scenario())


# --- a panel cannot outlive its plugin ---------------------------------------


def test_a_callback_that_raises_disables_the_plugin_and_closes_the_panel() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Broken()
            registry = _attach(app, command)
            screen = await _open_panel(pilot, app, command)

            screen.query_one("#panel-control-raw", Switch).value = True
            await pilot.pause()

            assert app.is_running
            assert registry.is_disabled(command)
            assert not isinstance(app.screen, PluginPanelScreen)
            assert "on_control() raised: nope" in str(registry.errors[0])

    asyncio.run(scenario())


def test_a_slow_callback_strikes_out_and_the_panel_goes_with_it() -> None:
    """The seventh budget, arriving through a keystroke rather than a sweep."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Slow()
            registry = _attach(app, command)
            # A ceiling one keystroke cannot possibly meet.
            app._panel_budget.limit_ms = 1.0
            screen = await _open_panel(pilot, app, command)

            switch = screen.query_one("#panel-control-raw", Switch)
            for value in (True, False, True):
                if isinstance(app.screen, PluginPanelScreen):
                    switch.value = value
                    await pilot.pause()

            assert registry.is_disabled(command)
            assert "panel" in (registry.disabled_reason(command) or "")
            assert not isinstance(app.screen, PluginPanelScreen)

    asyncio.run(scenario())


def test_a_panel_clv_cannot_draw_takes_the_plugin_out_of_service() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Broken(in_run=True)
            registry = _attach(app, command)

            app._run_command(command)
            await pilot.pause()

            assert not isinstance(app.screen, PluginPanelScreen)
            assert registry.is_disabled(command)
            assert "not a Panel" in str(registry.errors[0])

    asyncio.run(scenario())


# --- what a panel may be -----------------------------------------------------


@pytest.mark.parametrize(
    ("panel", "expected"),
    [
        ("nope", "not a Panel"),
        (Panel("", (Control("input", "a"),)), "no title"),
        (Panel("T", ()), "no controls"),
        (Panel("T", (Control("nope", "a"),)), "kind 'nope'"),
        (Panel("T", (Control("input", ""),)), "no id"),
        (
            Panel("T", (Control("input", "a"), Control("switch", "a"))),
            "two controls with id 'a'",
        ),
        (Panel("T", (Control("select", "a"),)), "no options"),
        (
            Panel("T", (Control("select", "a", options=(("only",),)),)),
            "(value, label) pair",
        ),
        (
            Panel(
                "T",
                tuple(
                    Control("input", f"f{i}") for i in range(MAX_PANEL_CONTROLS + 1)
                ),
            ),
            f"the limit is {MAX_PANEL_CONTROLS}",
        ),
    ],
)
def test_a_panel_is_refused_with_a_reason(panel: object, expected: str) -> None:
    """Checked when it is handed over, because that is when it first exists."""

    problem = panel_fault(panel)
    assert problem is not None and expected in problem


def test_a_dismissing_panel_is_never_refused() -> None:
    """Nothing else about it is read, so nothing else is worth refusing over."""

    assert panel_fault(Panel(dismiss=True)) is None


def test_a_well_formed_panel_passes() -> None:
    assert panel_fault(_form()) is None
