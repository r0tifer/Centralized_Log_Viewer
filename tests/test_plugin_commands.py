"""A plugin is invoked, and the layout is still CLV's.

Phase 12 of ``PLUGIN_TODO.md``, the whole of Stage D. Every seam before this one
lets a plugin *answer* — a format when a line arrives, a metric when a bucket is
drawn, a matcher when a rule is tested. None of them lets a plugin be asked.

Three claims are under test here, and they are different in kind.

**Requirement 9, equal terms.** A plugin command runs from a key it was given
and from the ``C`` dialog by name, it appears in the help overlay in its own
section built by the same code that lists CLV's own bindings, and what it asks
CLV to do — set a query, open a source, apply a saved view — goes through the
same paths that `/`, a tree click and `v` go through.

**Requirement 11, the layout is not negotiable.** `show=True` is refused rather
than honoured, a key that collides with a built-in is refused rather than
stolen, and the footer at 80 columns is asserted byte-identical with five
plugin commands installed. That last one is the test the whole "constrained
vocabulary, never a widget" doctrine exists to be able to pass.

**The context cannot reach the app, and it is asserted rather than intended.**
``test_the_context_has_no_route_to_the_app`` walks every attribute, every bound
method's ``__self__`` and every closure cell reachable from a live
``CommandContext``, because the obvious implementation — hand the plugin
``self._notify`` — passes a shallower test and fails this one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Footer

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import Command, CommandContext, PluginRegistry
from clv.storage import SavedView
from clv.widgets.commands_dialog import CommandRow, CommandsDialog
from clv.widgets.help_overlay import HelpOverlay


# --- plugins under test ------------------------------------------------------


class Recorder(Command):
    """Records that it ran, and how much it was shown."""

    name = "recorder"
    command_name = "record"
    title = "Record this view"

    def __init__(self, key: str = "", *, priority: int = 100) -> None:
        self.key = key
        self.priority = priority
        self.calls: list[int] = []

    def run(self, context: CommandContext) -> None:
        self.calls.append(len(context.entries))
        context.notify("recorded")


class Raiser(Command):
    name = "raiser"
    command_name = "boom"
    title = "Explode"
    key = "z"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, context: CommandContext) -> None:
        self.calls += 1
        raise RuntimeError("no")


class Greedy(Command):
    """Asks for the footer, which is the one thing it may not have."""

    name = "greedy"
    command_name = "greedy"
    title = "Greedy"
    key = "k"
    show = True

    def run(self, context: CommandContext) -> None:
        return None


class Asker(Command):
    """Queues one request and nothing else."""

    name = "asker"
    command_name = "ask"
    title = "Ask for something"

    def __init__(self, *requests) -> None:
        self._requests = requests

    def run(self, context: CommandContext) -> None:
        for kind, payload in self._requests:
            getattr(context, f"request_{kind}")(payload)


def _named(name: str, key: str = "", title: str = "") -> Command:
    """A minimal command, for the tests that only need one to exist.

    `run` is defined in the class body rather than attached afterwards:
    `ABCMeta` computes `__abstractmethods__` when the class is created, so a
    method bolted on later leaves the class abstract and uninstantiable.
    """

    class _One(Command):
        def run(self, context: CommandContext) -> None:
            return None

    _One.name = name
    _One.command_name = name
    _One.title = title or name.title()
    _One.key = key
    return _One()


# --- harness -----------------------------------------------------------------


def _attach(app: LogViewerApp, *plugins) -> PluginRegistry:
    """Give a running app a registry holding *plugins*, wired as at mount."""

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


def _log_file(tmp_path: Path) -> Path:
    path = tmp_path / "app.log"
    path.write_text(
        "2026-08-07 09:00:00 INFO first\n"
        "2026-08-07 09:00:01 ERROR second\n"
        "2026-08-07 09:00:02 INFO third\n",
        encoding="utf-8",
    )
    return path


async def _open(pilot, app: LogViewerApp, tmp_path: Path) -> None:
    await pilot.pause()
    app._select_source(_log_file(tmp_path), announce=False)
    app.set_focus(app.log_panel)
    await pilot.pause()


def _conflicts(registry: PluginRegistry) -> list[str]:
    return [error.message for error in registry.errors if error.category == "conflict"]


# --- running -----------------------------------------------------------------


def test_a_command_runs_from_its_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            command = Recorder(key="j")
            _attach(app, command)

            await pilot.press("j")
            await pilot.pause()

            # Handed the *filtered* set, which is the three lines in the file.
            assert command.calls == [3]

    asyncio.run(scenario())


def test_a_command_runs_by_name_from_the_dialog(tmp_path: Path) -> None:
    """The surface that makes a refused key survivable."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            command = Recorder()  # no key at all
            _attach(app, command)

            app.action_commands()
            await pilot.pause()
            assert isinstance(app.screen, CommandsDialog)

            await pilot.press("enter")
            await pilot.pause()

            assert command.calls == [3]
            assert not isinstance(app.screen, CommandsDialog)

    asyncio.run(scenario())


def test_a_raising_command_is_disabled_and_the_app_survives(tmp_path: Path) -> None:
    """Phase 1's contract, one seam later: recorded once, and still running."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            command = Raiser()
            registry = _attach(app, command)

            for _ in range(5):
                await pilot.press("z")
                await pilot.pause()

            assert app.is_running
            assert registry.is_disabled(command)
            # Once, however many times the key was pressed -- and the key stops
            # reaching it at all after the first, which is what the rebuild in
            # `_install_command_bindings` is for.
            assert command.calls == 1
            runtime = [
                error for error in registry.errors if error.category == "runtime"
            ]
            assert len(runtime) == 1
            assert "run() raised: no" in runtime[0].message

    asyncio.run(scenario())


def test_disabling_a_command_takes_its_key_away(tmp_path: Path) -> None:
    """And re-enabling gives it back. The install that removes something."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            command = Recorder(key="j")
            registry = _attach(app, command)

            await pilot.press("j")
            await pilot.pause()
            assert len(command.calls) == 1

            registry.disable(command, "switched off")
            app._sync_plugin_generation()
            await pilot.press("j")
            await pilot.pause()
            assert len(command.calls) == 1, "a disabled command kept its key"
            assert app._command_keys == {}

            registry.enable(command)
            app._sync_plugin_generation()
            await pilot.press("j")
            await pilot.pause()
            assert len(command.calls) == 2

    asyncio.run(scenario())


# --- bindings ----------------------------------------------------------------


def test_a_key_that_collides_with_a_builtin_is_refused(tmp_path: Path) -> None:
    """The built-in keeps working, and the command is still reachable by name."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            # `t` is `cycle_time`, and has been since long before plugins.
            command = Recorder(key="t")
            registry = _attach(app, command)

            assert "t" not in app._command_keys
            message = "\n".join(_conflicts(registry))
            assert "'t'" in message and "which is CLV's" in message

            before = app.state.time_window
            await pilot.press("t")
            await pilot.pause()
            assert app.state.time_window != before, "the built-in lost its key"
            assert command.calls == []

            # And it is still invocable, which is the half that makes the
            # refusal acceptable rather than merely safe. On reachability only:
            # the `t` above moved the time window, so what the command is shown
            # is whatever that window now matches, and asserting a count here
            # would be asserting something about `cycle_time`.
            app.action_plugin_command("record")
            await pilot.pause()
            assert len(command.calls) == 1

    asyncio.run(scenario())


def test_the_palette_binding_is_reserved_too() -> None:
    """Bound by `App`, so it is in no class attribute this file could gather."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            registry = _attach(app, Recorder(key=app.COMMAND_PALETTE_BINDING))

            assert app._command_keys == {}
            assert any("which is CLV's" in m for m in _conflicts(registry))

    asyncio.run(scenario())


def test_two_commands_claiming_one_key_are_settled_by_priority() -> None:
    """Deterministic, and not a function of which module loaded first."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            winner = _named("early", key="j")
            winner.priority = 10
            loser = _named("late", key="j")
            loser.priority = 900

            # Added in the order that would give the wrong answer without the
            # sort, so the assertion is about `plugin_sort_key` and not luck.
            registry = _attach(app, loser, winner)

            assert app._command_keys == {"j": "early"}
            assert any("claimed first" in m for m in _conflicts(registry))

    asyncio.run(scenario())


def test_the_winner_is_the_same_whichever_order_they_load_in() -> None:
    """The shuffled-load assertion Phase 5 established for every ordered registry."""

    async def scenario() -> None:
        for order in (("a", "b"), ("b", "a")):
            app = LogViewerApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                plugins = {}
                for name in order:
                    command = _named(name, key="j")
                    command.priority = 100  # tie, broken by name
                    plugins[name] = command
                _attach(app, *(plugins[name] for name in order))

                assert app._command_keys == {"j": "a"}, order

    asyncio.run(scenario())


def test_asking_for_the_footer_is_refused_with_a_reason() -> None:
    """Requirement 11. The plugin is told why, and is not disabled for asking."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command = Greedy()
            registry = _attach(app, command)

            message = "\n".join(_conflicts(registry))
            assert "footer" in message and "80 columns" in message
            # Refused, not punished: it still got its key and still runs.
            assert app._command_keys == {"k": "greedy"}
            assert not registry.is_disabled(command)
            binding = next(b for b in app._command_bindings if b.key == "k")
            assert binding.show is False

    asyncio.run(scenario())


def test_every_plugin_binding_is_hidden() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            _attach(app, _named("one", key="j"), _named("two", key="k"))

            assert app._command_bindings
            assert all(not b.show for b in app._command_bindings)

    asyncio.run(scenario())


# --- the help overlay --------------------------------------------------------


def test_plugin_bindings_appear_in_the_overlay_under_plugins() -> None:
    """The guarantee that no key can go missing from help, extended to plugins."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            _attach(app, _named("redact", key="j", title="Redact this view"))

            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()

            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            plugins = next(s for s in overlay._sections if s.title == "Plugins")
            assert ("j", "Redact this view") in plugins.rows

    asyncio.run(scenario())


def test_the_footer_at_eighty_columns_is_unchanged_by_five_commands(
    tmp_path: Path,
) -> None:
    """Requirement 11's test, and the reason plugins never get a widget."""

    async def footer_keys(*plugins) -> list[str]:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await _open(pilot, app, tmp_path)
            if plugins:
                _attach(app, *plugins)
                await pilot.pause()
            footer = app.query_one(Footer)
            return [
                child.key
                for child in footer.children
                if child.region.width and child.region.right <= 80
            ]

    async def scenario() -> None:
        bare = await footer_keys()
        loaded = await footer_keys(
            *(
                _named(f"cmd{index}", key=key, title=f"Command {index}")
                for index, key in enumerate("jklhi")
            )
        )
        assert loaded == bare

    asyncio.run(scenario())


# --- the context -------------------------------------------------------------


def test_the_context_has_no_route_to_the_app(tmp_path: Path) -> None:
    """Walked, not eyeballed. The obvious implementation passes a shallower test.

    Handing a plugin ``self._notify`` would satisfy "the context has no ``app``
    attribute" while carrying the whole application on ``__self__``. So this
    walks bound methods and closure cells as well as attributes, which is what
    makes the queued-request design load-bearing rather than stylistic.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            context = app._command_context()

            seen: set[int] = set()
            queue = [context]
            while queue:
                current = queue.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                assert not isinstance(current, LogViewerApp), "reached the app"
                bound = getattr(current, "__self__", None)
                if bound is not None:
                    queue.append(bound)
                for cell in getattr(current, "__closure__", None) or ():
                    try:
                        queue.append(cell.cell_contents)
                    except ValueError:  # pragma: no cover - empty cell
                        pass
                for attribute in getattr(current, "__slots__", ()):
                    queue.append(getattr(current, attribute, None))
                for value in getattr(current, "__dict__", {}).values():
                    queue.append(value)

    asyncio.run(scenario())


def test_the_context_carries_the_filtered_set_not_the_buffer(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            app._update_state(query="ERROR")
            app._render_log()
            await pilot.pause()

            context = app._command_context()
            assert [entry.message for entry in context.entries] == ["second"]
            assert context.spec.query == "ERROR"
            assert context.source == _log_file(tmp_path)

    asyncio.run(scenario())


def test_an_unparseable_query_box_hands_over_an_empty_set(tmp_path: Path) -> None:
    """A command invoked mid-edit is being asked about a view that does not exist."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            app._update_state(query="[")

            assert app._command_context().entries == ()

    asyncio.run(scenario())


# --- mediated requests -------------------------------------------------------


def test_a_command_can_set_the_query(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            _attach(app, Asker(("query", "level>=error")))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert app.state.query == "level>=error"
            assert app.query_bar.get_query_value() == "level>=error"

    asyncio.run(scenario())


def test_a_query_that_does_not_parse_is_refused_and_reported(tmp_path: Path) -> None:
    """With the parser's own words, so it reads as any bad query does."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            registry = _attach(app, Asker(("query", "[")))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert app.state.query == ""
            assert any("does not parse" in m for m in _conflicts(registry))
            # Refused, not disabled: being wrong about a query is not being
            # broken, and the next invocation may well be fine.
            assert not registry.is_disabled(registry.commands[0])

    asyncio.run(scenario())


def test_a_command_can_open_a_source_clv_already_offers(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            other = tmp_path / "other.log"
            other.write_text("2026-08-07 09:00:00 INFO elsewhere\n", encoding="utf-8")
            app._file_refs = {_log_file(tmp_path), other}
            _attach(app, Asker(("source", other)))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert app._selected_source == other

    asyncio.run(scenario())


def test_a_source_clv_is_not_offering_is_refused(tmp_path: Path) -> None:
    """A command can move to a source; it cannot conjure one."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            registry = _attach(app, Asker(("source", tmp_path / "nowhere.log")))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert app._selected_source == _log_file(tmp_path)
            assert any("is not offering" in m for m in _conflicts(registry))

    asyncio.run(scenario())


def test_a_command_can_apply_a_saved_view(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            app._update_state(
                views=(SavedView(name="errors", query="level>=error"),)
            )
            _attach(app, Asker(("view", "errors")))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert app.state.query == "level>=error"

    asyncio.run(scenario())


def test_a_view_that_does_not_exist_is_refused_by_name(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            registry = _attach(app, Asker(("view", "gone")))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert any("'gone'" in m for m in _conflicts(registry))

    asyncio.run(scenario())


def test_a_view_whose_plugin_is_missing_still_degrades_by_explaining(
    tmp_path: Path,
) -> None:
    """Requirement 12, reached through `_apply_view` rather than reimplemented."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            app._update_state(
                views=(
                    SavedView(
                        name="needs-plugin",
                        query="svc~web",
                        requires=("operator:~",),
                    ),
                )
            )
            _attach(app, Asker(("view", "needs-plugin")))

            app.action_plugin_command("ask")
            await pilot.pause()

            # Not applied, and the view is untouched on disk -- the same answer
            # pressing `v` on it gives.
            assert app.state.query == ""
            assert app.state.views[0].query == "svc~web"

    asyncio.run(scenario())


def test_the_last_request_of_each_kind_wins(tmp_path: Path) -> None:
    """Two queries meant the second one, and applying both filters twice."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            _attach(app, Asker(("query", "first"), ("query", "level>=error")))

            app.action_plugin_command("ask")
            await pilot.pause()

            assert app.state.query == "level>=error"

    asyncio.run(scenario())


def test_notifications_arrive_in_the_order_they_were_queued(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            said: list[str] = []
            app._notify = lambda text, severity="info": said.append(text)

            class Chatty(Command):
                name = "chatty"
                command_name = "chatty"
                title = "Chatty"

                def run(self, context):
                    context.notify("one")
                    context.notify("two")
                    context.notify("three")

            _attach(app, Chatty())
            app.action_plugin_command("chatty")
            await pilot.pause()

            assert said == ["one", "two", "three"]

    asyncio.run(scenario())


# --- the dialog --------------------------------------------------------------


def test_the_dialog_is_not_opened_when_nothing_is_installed() -> None:
    """Requirement 10 at the keyboard: a sentence beats an empty modal."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            said: list[str] = []
            app._notify = lambda text, severity="info": said.append(text)

            app.action_commands()
            await pilot.pause()

            assert not isinstance(app.screen, CommandsDialog)
            assert said == ["No plugin commands installed."]

    asyncio.run(scenario())


def test_the_dialog_shows_the_key_a_command_actually_got() -> None:
    """Not the one it asked for. A refused key must not be advertised."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            _attach(app, Recorder(key="t"))  # refused: `t` is CLV's

            app.action_commands()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, CommandsDialog)
            assert dialog._rows[0].key == ""

    asyncio.run(scenario())


def test_escape_closes_the_dialog_and_runs_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            command = Recorder()
            _attach(app, command)

            app.action_commands()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert not isinstance(app.screen, CommandsDialog)
            assert command.calls == []

    asyncio.run(scenario())


def test_a_disabled_command_is_not_listed(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)
            live = _named("live", title="Live")
            dead = _named("dead", title="Dead")
            registry = _attach(app, live, dead)
            registry.disable(dead, "switched off")

            app.action_commands()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, CommandsDialog)
            assert [row.command_name for row in dialog._rows] == ["live"]

    asyncio.run(scenario())


def test_the_dialog_renders_a_row_without_a_key() -> None:
    """The `–` placeholder, so the column still lines up."""

    dialog = CommandsDialog([CommandRow("x", "Do x", "", "plug")])
    assert "–" in dialog._row(CommandRow("x", "Do x", "", "plug")).plain


# --- load-time refusals ------------------------------------------------------


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"command_name": "", "title": "T"}, "declares no command_name"),
        ({"command_name": "a b", "title": "T"}, "contains whitespace"),
        ({"command_name": "ok", "title": ""}, "declares no title"),
        ({"command_name": "ok", "title": "T", "key": 7}, "key must be a string"),
        ({"command_name": "ok", "title": "T", "key": "a b"}, "key 'a b' contains"),
    ],
)
def test_a_command_that_cannot_be_addressed_is_refused_at_load(
    attributes: dict, expected: str
) -> None:
    """At load, never at the first invocation -- the other four faults' argument."""

    class _Bad(Command):
        name = "bad"

        def run(self, context):
            return None

    for key, value in attributes.items():
        setattr(_Bad, key, value)

    registry = PluginRegistry()
    assert not registry.add(_Bad(), origin="/tmp/bad.py", clv_version=__version__)
    assert expected in str(registry.errors[0])


def test_a_duplicate_command_name_is_refused_however_it_is_capitalised() -> None:
    """Folded, because dispatch folds: the second would load and never be reached."""

    registry = PluginRegistry()
    assert registry.add(_named("redact"), origin="/tmp/a.py", clv_version=__version__)
    assert not registry.add(
        _named("Redact"), origin="/tmp/b.py", clv_version=__version__
    )
    assert "already registered by" in str(registry.errors[0])


def test_a_command_is_filed_under_its_kind() -> None:
    registry = PluginRegistry()
    command = _named("x")
    registry.add(command, origin="/tmp/x.py", clv_version=__version__)

    assert registry.commands == [command]
    assert registry.loaded[0].kinds == ("command",)
    assert registry.total == 1


# --- requirement 10 ----------------------------------------------------------


def test_zero_commands_installs_nothing_at_all(tmp_path: Path) -> None:
    """A build with no plugins pays nothing and shows nothing new."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app, tmp_path)

            assert app._command_bindings == []
            assert app._command_keys == {}
            app.set_focus(app.log_panel)
            await pilot.press("question_mark")
            await pilot.pause()
            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            plugins = next(s for s in overlay._sections if s.title == "Plugins")
            # `P` and `C` -- CLV's own, and nothing else.
            assert [key for key, _ in plugins.rows] == ["P", "C"]

    asyncio.run(scenario())


def test_a_refusal_is_re_decided_rather_than_accumulated() -> None:
    """The install re-runs on every generation change; the note must not pile up.

    And it is a statement about the *current* set, not a fault on the record: a
    command that lost `j` to a higher-priority one wins it the moment that one
    is switched off, and the note has to go when it does.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            winner = _named("early", key="j")
            winner.priority = 10
            loser = _named("late", key="j")
            loser.priority = 900
            registry = _attach(app, winner, loser)

            assert len(_conflicts(registry)) == 1

            for _ in range(5):
                app._install_command_bindings()
            assert len(_conflicts(registry)) == 1, "the refusal accumulated"

            # Switch the winner off and the loser takes the key -- and the note
            # about it goes with the decision that produced it.
            registry.disable(winner, "switched off")
            app._sync_plugin_generation()

            assert app._command_keys == {"j": "late"}
            assert _conflicts(registry) == []

    asyncio.run(scenario())


def test_installing_commands_does_not_take_away_textuals_own_bindings() -> None:
    """The regression that would have cost every session `Ctrl+C`.

    `DOMNode` merges bindings down the whole MRO, so `App`'s `ctrl+c`, `ctrl+q`
    and `ctrl+p` are live on this app and declared in no class attribute in
    `clv/app.py`. The first version of `_install_command_bindings` rebuilt the
    map from `LogViewerApp.BINDINGS` and dropped all three — and since that
    install runs at mount whether or not a plugin is installed, it would have
    done so to everyone.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            before = set(app._bindings.key_to_bindings)
            assert {"ctrl+c", "ctrl+q"} <= before, "Textual stopped binding these"

            _attach(app, _named("one", key="j"))
            after = set(app._bindings.key_to_bindings)

            assert before - after == set(), f"lost {before - after}"
            assert "j" in after

    asyncio.run(scenario())


def test_a_plugin_cannot_claim_a_key_textual_bound() -> None:
    """`ctrl+c` quits. It is reserved even though no CLV class declares it."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            registry = _attach(app, _named("greedy", key="ctrl+c"))

            assert app._command_keys == {}
            assert any("which is CLV's" in m for m in _conflicts(registry))
            # And the quit binding is still the quit binding.
            actions = [
                binding.action
                for binding in app._bindings.key_to_bindings["ctrl+c"]
            ]
            assert not any("plugin_command" in action for action in actions)

    asyncio.run(scenario())


def test_restoring_the_map_does_not_mutate_the_pristine_copy() -> None:
    """Many installs must not accumulate anything in the base map."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            base = len(app._base_bindings.key_to_bindings)

            _attach(app, _named("one", key="j"), _named("two", key="k"))
            for _ in range(5):
                app._install_command_bindings()

            assert len(app._base_bindings.key_to_bindings) == base
            assert len(app._command_bindings) == 2

    asyncio.run(scenario())
