"""A plugin that can be stopped.

Phase 13 of ``PLUGIN_TODO.md``. Every guard before this one works by *declining
to call* a plugin again — a stage that raises is disabled, a stage that is slow
strikes out and is disabled. None of them can do anything about a call CLV is
already inside, which is why ``SinkDispatcher`` says a hung sink is "abandoned,
not killed" and why ``Command``'s docstring says there is no budget that can
save you. A child process can be killed, and that is the whole of what this
phase buys.

The tests are written against real processes on purpose. A fake host would
assert that CLV sends the right message and prove nothing about the two things
that actually matter: that a plugin's work happens somewhere else, and that
somewhere else can be ended. So the plugins here are written to files and
imported by the loader's own door, the pids are compared, and the hangs are real
sleeps against a real deadline.

What is *not* claimed anywhere here: safety. The child runs as the operator with
the operator's filesystem, and ``test_plugin_docs.py`` fails the build if the
word "sandbox" ever appears next to any of this.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

from clv import __version__
from clv.app import LogViewerApp
from clv.plugins import (
    PLUGIN_PATH_ENV,
    FilterContext,
    PluginRegistry,
    load_plugins,
)
from clv.services.filtering import FilterSpec, TimeWindow
from clv.services.parsing import LogParser
from clv.widgets.export_dialog import ExportRequest

# Deliberately generous. Every deadline here is either "long enough that a
# healthy call cannot miss it" or "short enough that a sleeping plugin must",
# and nothing in between is asserted -- a tight ceiling on a loaded CI box is a
# flaky test, and a flaky test gets deleted.
SLOW_ENOUGH_MS = 300.0
PATIENT_MS = 30_000.0


# --- plugins under test ------------------------------------------------------
#
# Written to files rather than defined here: the child is a fresh interpreter
# that imports the plugin by origin, so a class defined in this module is a
# class the host cannot reach. This is the same constraint a real plugin author
# works under, reached through the same `CLV_PLUGIN_PATH` door.

PID_EXPORTER = '''
import os
from pathlib import Path
from clv.api import Exporter, ExportResult


class PidExporter(Exporter):
    name = "pid-exporter"
    isolated = True
    wants_path = True
    suggested_extension = "txt"

    def export(self, entries, context, *, destination=None):
        if destination is not None:
            Path(destination).write_text(
                "\\n".join(entry.raw for entry in entries), encoding="utf-8"
            )
        return ExportResult(
            ok=True,
            detail="pid=%d lines=%d query=%s" % (
                os.getpid(), len(entries), context.spec.query
            ),
            destination=destination,
        )
'''

ROUND_TRIP_EXPORTER = '''
from pathlib import Path
from clv.api import Exporter, ExportResult


class Echo(Exporter):
    """Writes back what it was handed, field by field."""

    name = "echo"
    isolated = True
    wants_path = True

    def export(self, entries, context, *, destination=None):
        lines = [
            "|".join([
                entry.format_name,
                "" if entry.timestamp is None else entry.timestamp.isoformat(),
                entry.level or "",
                entry.message,
                repr(sorted(entry.fields.items())),
                str(entry.continuation),
                entry.raw,
            ])
            for entry in entries
        ]
        Path(destination).write_text("\\n".join(lines), encoding="utf-8")
        return ExportResult(ok=True, detail="echoed", destination=destination)
'''

PID_COMMAND = '''
import os
from clv.api import Command, Panel, Control


class Reporter(Command):
    name = "reporter"
    isolated = True
    command_name = "report"
    title = "Report where I am"
    key = "j"

    def run(self, context):
        context.notify("ran in %d with %d entries" % (os.getpid(), len(context.entries)))
        context.request_query("level:error")
        return Panel(
            title="Report",
            controls=(Control(kind="switch", id="loud", label="Loud", value=False),),
        )

    def on_control(self, control_id, value, context):
        context.notify("%s=%r" % (control_id, value))
        return Panel(title="Done", dismiss=True)
'''

SINK = '''
import os
from pathlib import Path
from clv.api import WatchSink


class FileSink(WatchSink):
    name = "file-sink"
    isolated = True
    wants_entries = True

    def configure(self, settings):
        self._target = settings.get("target", "")

    def deliver(self, name, count, context, entries=()):
        Path(self._target).write_text(
            "%s %d %d %d" % (name, count, len(entries), os.getpid()), encoding="utf-8"
        )
'''

ANNOTATOR = '''
from datetime import datetime
from clv.api import TimelineAnnotation


class Deploys(TimelineAnnotation):
    name = "deploys"
    isolated = True

    def annotations(self, window):
        yield (datetime(2026, 8, 7, 9, 0, 1), "deploy 1.2.3", "notice")
'''

HANGING_COMMAND = '''
import time
from clv.api import Command


class Sleeper(Command):
    name = "sleeper"
    isolated = True
    command_name = "sleep"
    title = "Sleep forever"

    def run(self, context):
        time.sleep(120)
'''

HANGING_COMMAND_WITH_KEY = '''
import time
from clv.api import Command


class Sleeper(Command):
    name = "sleeper"
    isolated = True
    command_name = "sleep"
    title = "Sleep forever"
    key = "j"

    def run(self, context):
        time.sleep(120)
'''

CRASHING_COMMAND = '''
import os
from clv.api import Command


class Crasher(Command):
    name = "crasher"
    isolated = True
    command_name = "crash"
    title = "Take the child down"

    def run(self, context):
        os._exit(1)
'''

RAISING_COMMAND = '''
from clv.api import Command


class Raiser(Command):
    name = "raiser"
    isolated = True
    command_name = "raise"
    title = "Raise something"

    def run(self, context):
        raise ValueError("no thank you")
'''

PER_ENTRY_STAGE = '''
from clv.api import FilterStage


class Redact(FilterStage):
    name = "redact"
    isolated = True

    def apply(self, entry, context):
        return entry
'''

ISOLATED_PROVIDER = '''
from clv.api import LogSourceProvider


class Feed(LogSourceProvider):
    name = "feed"
    isolated = True

    def discover(self):
        return []

    def open(self, path):
        return iter(())
'''

#: Records the pid that imported it. The whole of the settings-forced door's
#: claim is that this pid is never the viewer's.
SENTINEL_COMMAND = '''
import os
from pathlib import Path

Path(os.environ["CLV_TEST_SENTINEL"]).write_text(str(os.getpid()), encoding="utf-8")

from clv.api import Command


class Marker(Command):
    name = "marker"
    command_name = "mark"
    title = "Marker"

    def run(self, context):
        context.notify("marked by %d" % os.getpid())
'''

BROKEN_IMPORT = '''
import os
from pathlib import Path

Path(os.environ["CLV_TEST_SENTINEL"]).write_text(str(os.getpid()), encoding="utf-8")
raise RuntimeError("this module is broken")
'''

ENVIRONMENT_EXPORTER = '''
import os
from clv.api import Exporter, ExportResult


class Env(Exporter):
    name = "env"
    isolated = True

    def export(self, entries, context, *, destination=None):
        return ExportResult(
            ok=True, detail=repr(os.environ.get("LD_LIBRARY_PATH"))
        )
'''

LIFECYCLE_COMMAND = '''
import os
from pathlib import Path
from clv.api import Command


class Lifecycle(Command):
    name = "lifecycle"
    isolated = True
    command_name = "lifecycle"
    title = "Lifecycle"

    def configure(self, settings):
        self._log = settings.get("log", "")

    def _note(self, what):
        with open(self._log, "a", encoding="utf-8") as handle:
            handle.write("%s %d\\n" % (what, os.getpid()))

    def setup(self):
        self._note("setup")

    def teardown(self):
        self._note("teardown")

    def run(self, context):
        context.notify(self._log)
'''

HANGING_TEARDOWN = '''
import time
from clv.api import Command


class Clingy(Command):
    name = "clingy"
    isolated = True
    command_name = "cling"
    title = "Refuse to leave"

    def run(self, context):
        context.notify("here")

    def teardown(self):
        time.sleep(120)
'''

SETTINGS_COMMAND = '''
from clv.api import Command


class Echoes(Command):
    name = "echoes"
    isolated = True
    command_name = "echo-setting"
    title = "Echo a setting"

    def configure(self, settings):
        self._settings = settings

    def run(self, context):
        context.notify(self._settings.get("greeting", "(unset)"))
'''

SINK_AND_COMMAND = '''
from pathlib import Path
from clv.api import Command, WatchSink


class Sink(WatchSink):
    name = "both-sink"
    isolated = True

    def configure(self, settings):
        self._target = settings.get("target", "")

    def deliver(self, name, count, context, entries=()):
        with open(self._target, "a", encoding="utf-8") as handle:
            handle.write("%s %d\\n" % (name, count))


class Runner(Command):
    name = "both-command"
    isolated = True
    command_name = "both"
    title = "Run"

    def run(self, context):
        context.notify("ran")
'''

HALF_BROKEN_MODULE = '''
from clv.api import Command


class Broken(Command):
    name = "broken"
    command_name = "broken"
    title = "Cannot be built"

    def __init__(self):
        raise RuntimeError("no")

    def run(self, context):
        return None


class Works(Command):
    name = "works"
    command_name = "works"
    title = "Builds fine"

    def run(self, context):
        context.notify("fine")
'''

PICKY_COMMAND = '''
from clv.api import Command


class Picky(Command):
    name = "picky"
    isolated = True
    command_name = "picky"
    title = "Refuse my settings"

    def configure(self, settings):
        if "target" not in settings:
            raise ValueError("needs a target")

    def run(self, context):
        context.notify("configured")
'''

PLAIN_STAGE = '''
from clv.api import FilterStage


class Plain(FilterStage):
    name = "plain"

    def apply(self, entry, context):
        return entry
'''

MIXED_MODULE = '''
from clv.api import Exporter, ExportResult, FilterStage


class Contained(Exporter):
    name = "contained"
    isolated = True

    def export(self, entries, context, *, destination=None):
        return ExportResult(ok=True, detail="fine")


class Here(FilterStage):
    name = "here"

    def apply(self, entry, context):
        return entry
'''

NAMELESS_COMMAND = '''
from clv.api import Command


class Nameless(Command):
    name = "nameless"
    isolated = True
    title = "I forgot my command_name"

    def run(self, context):
        return None
'''


# --- harness -----------------------------------------------------------------


@pytest.fixture
def user_root(tmp_path, monkeypatch):
    """A plugin directory on ``CLV_PLUGIN_PATH``, the documented door.

    The same fixture ``tests/test_plugins.py`` uses, for the same reason: the
    tests and a plugin author should reach the loader the same way. ``PATH`` is
    set rather than the real user directory written, and ``conftest.py`` has
    already pointed ``XDG_CONFIG_HOME`` somewhere disposable.
    """

    known = set(sys.modules)
    root = tmp_path / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(root))
    importlib.invalidate_caches()
    yield root
    for name in [n for n in sys.modules if n not in known]:
        del sys.modules[name]


def _write(root: Path, module: str, body: str) -> Path:
    path = root / f"{module}.py"
    path.write_text(body, encoding="utf-8")
    importlib.invalidate_caches()
    return path


def _load(
    *names: str,
    settings: dict | None = None,
    timeout_ms: float = PATIENT_MS,
) -> PluginRegistry:
    """Load only what these tests wrote, with a host ceiling they control."""

    return load_plugins(
        clv_version=__version__,
        include_local=False,
        include_entry_points=False,
        enabled=list(names),
        settings=settings or {},
        host_timeout_ms=timeout_ms,
    )


def _entries(*lines: str):
    return list(LogParser().feed(list(lines)))


def _context(query: str = "") -> FilterContext:
    return FilterContext(spec=FilterSpec(query=query), source=Path("/var/log/syslog"))


def _errors(registry: PluginRegistry) -> list[str]:
    return [f"{error.origin}: {error.message}" for error in registry.errors]


def _log(tmp_path: Path) -> Path:
    path = tmp_path / "app.log"
    path.write_text(
        "2026-08-07 09:00:00 INFO first\n"
        "2026-08-07 09:00:01 ERROR second\n",
        encoding="utf-8",
    )
    return path


def _adopt(app: LogViewerApp, registry: PluginRegistry) -> None:
    """Give a running app a registry, exactly as ``on_mount`` does."""

    app._plugins = registry
    app._panel_budget = app._new_panel_budget()
    app._plugin_generation = registry.generation
    app._install_command_bindings()


def _host_of(registry: PluginRegistry, origin_name: str):
    for origin, host in registry._hosts.items():
        if Path(origin).name == origin_name:
            return host
    return None


def _warmed(registry: PluginRegistry, origin_name: str, *, deadline: float):
    """Start the host patiently, then hold it to a deadline it must miss.

    Starting a child means spawning an interpreter and importing CLV in it,
    which on a loaded machine is well over any ceiling short enough to make a
    sleeping plugin fail quickly. Timing the two together would be a test that
    passes for the wrong reason on a fast box and flakes on a slow one — so the
    handshake is given `PATIENT_MS` and only the call under test is tightened.
    """

    host = _host_of(registry, origin_name)
    assert host is not None, "the proxy should have built its host at load"
    host.ensure_started()
    registry.host_timeout_ms = deadline
    host.timeout_ms = deadline
    return host


# --- the four kinds, through their own call sites ----------------------------


def test_an_isolated_exporter_runs_in_another_process(user_root, tmp_path) -> None:
    """The claim, at its plainest: the plugin's work happened somewhere else."""

    _write(user_root, "pidexp", PID_EXPORTER)
    registry = _load("pidexp")
    assert not _errors(registry)
    registry.start()

    exporter = registry.exporters[0]
    # Read off the manifest rather than by asking the child: an export dialog
    # that had to make a round trip per keystroke to know what extension to
    # suggest would be unusable.
    assert exporter.wants_path is True
    assert exporter.suggested_extension == "txt"
    assert exporter.name == "pid-exporter"

    destination = tmp_path / "out.txt"
    result = exporter.export(
        _entries("2026-08-07 09:00:00 ERROR boom", "plain"),
        _context("boom"),
        destination=destination,
    )

    assert result.ok
    assert result.destination == destination
    assert destination.read_text(encoding="utf-8").splitlines() == [
        "2026-08-07 09:00:00 ERROR boom",
        "plain",
    ]
    pid = int(result.detail.split("pid=")[1].split()[0])
    assert pid != os.getpid()
    assert "lines=2" in result.detail
    assert "query=boom" in result.detail
    registry.shutdown()


def test_an_isolated_sink_delivers_through_its_spec(user_root, tmp_path) -> None:
    """Through ``WatchStack``'s guarded spec — what ``SinkDispatcher`` calls."""

    target = tmp_path / "delivered.txt"
    _write(user_root, "filesink", SINK)
    registry = _load("filesink", settings={"filesink": {"target": str(target)}})
    assert not _errors(registry)
    registry.start()

    stack = registry.watch_stack()
    spec = stack.sinks[0]
    assert spec.wants_entries is True
    spec.deliver("errors", 500, _context(), _entries("a", "b"))

    name, count, sampled, pid = target.read_text(encoding="utf-8").split()
    assert (name, count, sampled) == ("errors", "500", "2")
    assert int(pid) != os.getpid()
    registry.shutdown()


def test_an_isolated_annotation_reaches_the_timeline(user_root) -> None:
    from clv.services.timeline import build_timeline, install_timeline_plugins

    _write(user_root, "deploys", ANNOTATOR)
    registry = _load("deploys")
    assert not _errors(registry)
    registry.start()

    stack = registry.timeline_stack()
    install_timeline_plugins(stack.annotators, stack.metric)
    timeline = build_timeline(
        _entries("2026-08-07 09:00:00 INFO one", "2026-08-07 09:00:02 INFO two"),
        width=20,
    )

    assert [mark.label for mark in timeline.annotations] == ["deploy 1.2.3"]
    # Normalised by CLV, exactly as an in-process annotation's level is: the
    # plugin says "notice" and the axis draws NOTICE.
    assert [mark.level for mark in timeline.annotations] == ["NOTICE"]
    registry.shutdown()


def test_an_isolated_command_runs_from_its_key(user_root, tmp_path) -> None:
    """The app's own dispatch, unchanged, with a plugin in another process."""

    _write(user_root, "reporter", PID_COMMAND)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            registry = _load("reporter")
            assert not _errors(registry)
            _adopt(app, registry)

            await pilot.press("j")
            await pilot.pause()

            # It ran, it was handed the filtered set, and what it asked CLV to
            # do was performed by the path that already owns it.
            assert not registry.is_disabled(registry.commands[0]), registry.disabled_reason(registry.commands[0])
            assert app.state.query == "level:error"
            assert app.query_bar.get_query_value() == "level:error"
            registry.shutdown()

    asyncio.run(scenario())


# --- the kind gate -----------------------------------------------------------


def test_a_per_entry_kind_is_refused_with_its_reason(user_root) -> None:
    _write(user_root, "redact", PER_ENTRY_STAGE)
    registry = _load("redact")

    assert registry.filters == []
    assert registry._hosts == {}, "a refused plugin must not cost a subprocess"
    message = "\n".join(_errors(registry))
    assert "filter" in message
    assert "once per entry" in message
    assert "exporter, command" in message.replace("timeline, sink, ", "")


def test_a_source_provider_is_refused_for_its_own_reason(user_root) -> None:
    """Not "per entry" — a provider is refused for *when* CLV calls it."""

    _write(user_root, "feed", ISOLATED_PROVIDER)
    registry = _load("feed")

    assert registry.sources == []
    message = "\n".join(_errors(registry))
    assert "live reader" in message
    assert "event loop" in message


def test_an_isolated_command_is_still_held_to_its_own_faults(user_root) -> None:
    """The stand-in goes through ``add()``, so every check still applies."""

    _write(user_root, "nameless", NAMELESS_COMMAND)
    registry = _load("nameless")

    assert registry.commands == []
    assert any("command_name" in message for message in _errors(registry))


# --- hangs, crashes and raises -----------------------------------------------


def test_a_hanging_plugin_is_killed_at_the_deadline(user_root) -> None:
    """The capability that does not exist at any price without a child."""

    from clv.plugins.host import HostTimeout

    from clv.plugins import CommandContext

    _write(user_root, "sleeper", HANGING_COMMAND)
    registry = _load("sleeper")
    registry.start()
    command = registry.commands[0]
    host = _warmed(registry, "sleeper", deadline=SLOW_ENOUGH_MS)

    with pytest.raises(HostTimeout):
        command.run(CommandContext())

    assert not host.alive, "the child is still running after its deadline"
    assert registry.is_disabled(command)
    assert "killed" in (registry.disabled_reason(command) or "")
    registry.shutdown()


def test_a_crashing_host_takes_its_plugin_out_of_service(user_root) -> None:
    from clv.plugins import CommandContext
    from clv.plugins.host import HostDead

    _write(user_root, "crasher", CRASHING_COMMAND)
    registry = _load("crasher")
    registry.start()
    command = registry.commands[0]

    with pytest.raises(HostDead):
        command.run(CommandContext())

    assert registry.is_disabled(command)
    assert "exited" in (registry.disabled_reason(command) or "")
    registry.shutdown()


def test_a_plugin_that_raises_is_a_plugin_failure_not_a_host_failure(
    user_root,
) -> None:
    """The distinction the ``P`` dialog needs two different sentences for.

    A raising plugin is the ordinary third-party failure every call site in CLV
    already guards; the host did its job and is still up. Disabling belongs to
    the call site, exactly as it does in-process, so nothing here pre-empts it.
    """

    from clv.plugins import CommandContext
    from clv.plugins.host import HostError

    _write(user_root, "raiser", RAISING_COMMAND)
    registry = _load("raiser")
    registry.start()
    command = registry.commands[0]

    with pytest.raises(Exception) as caught:
        command.run(CommandContext())

    assert not isinstance(caught.value, HostError)
    assert "no thank you" in str(caught.value)
    assert not registry.is_disabled(command)
    assert _host_of(registry, "raiser").alive
    registry.shutdown()


def test_re_enabling_after_a_kill_starts_a_fresh_host(user_root, tmp_path) -> None:
    """Re-enable means what it says, even for a plugin CLV killed."""

    from clv.plugins import CommandContext
    from clv.plugins.host import HostTimeout

    _write(user_root, "sleeper", HANGING_COMMAND)
    registry = _load("sleeper")
    registry.start()
    command = registry.commands[0]
    host = _warmed(registry, "sleeper", deadline=SLOW_ENOUGH_MS)
    with pytest.raises(HostTimeout):
        command.run(CommandContext())
    assert registry.is_disabled(command)

    assert registry.enable(command) is True
    assert host.dead_reason is None, "a revived host must be startable again"

    # And it really does start: the same plugin, asked again, gets a new child
    # — which it then hangs in exactly as it did the first time.
    registry.host_timeout_ms = PATIENT_MS
    with pytest.raises(HostTimeout):
        registry.host_timeout_ms = SLOW_ENOUGH_MS
        command.run(CommandContext())
    registry.shutdown()


# --- the settings-forced door ------------------------------------------------


def test_isolating_from_settings_never_imports_the_module_here(
    user_root, tmp_path, monkeypatch
) -> None:
    """The half a class attribute cannot buy: the import is contained too."""

    sentinel = tmp_path / "importer.pid"
    monkeypatch.setenv("CLV_TEST_SENTINEL", str(sentinel))
    _write(user_root, "marker", SENTINEL_COMMAND)

    registry = _load("marker", settings={"marker": {"isolated": "true"}})
    assert not _errors(registry)

    assert registry.commands, "the plugin should have loaded, in a child"
    assert "clv_user_plugins.marker" not in sys.modules
    assert sentinel.exists(), "the child should have imported it"
    assert int(sentinel.read_text(encoding="utf-8")) != os.getpid()

    # And it is a working command, addressable by the name it declared.
    from clv.plugins import CommandContext

    context = CommandContext()
    registry.commands[0].run(context)
    assert context.requests[0][0] == "notify"
    registry.shutdown()


def test_a_module_that_cannot_be_imported_is_reported_not_run_here(
    user_root, tmp_path, monkeypatch
) -> None:
    """A failed start disables; it never degrades to running in-process."""

    sentinel = tmp_path / "importer.pid"
    monkeypatch.setenv("CLV_TEST_SENTINEL", str(sentinel))
    _write(user_root, "broken", BROKEN_IMPORT)

    registry = _load("broken", settings={"broken": {"isolated": "true"}})

    assert registry.total == 0
    assert any("this module is broken" in message for message in _errors(registry))
    assert "clv_user_plugins.broken" not in sys.modules
    assert registry._hosts == {}, "a host that produced nothing must not linger"
    assert int(sentinel.read_text(encoding="utf-8")) != os.getpid()


def test_the_enable_list_still_gates_an_isolated_plugin(user_root) -> None:
    """Isolation is not a way around consent. Requirement 2."""

    _write(user_root, "marker", SENTINEL_COMMAND)
    registry = _load(settings={"marker": {"isolated": "true"}})

    assert registry.total == 0
    assert registry._hosts == {}


def test_a_settings_isolated_per_entry_kind_is_refused_too(user_root) -> None:
    _write(user_root, "plain", PLAIN_STAGE)
    registry = _load("plain", settings={"plain": {"isolated": "true"}})

    assert registry.filters == []
    assert any("once per entry" in message for message in _errors(registry))
    assert registry._hosts == {}


# --- what crosses the wire ---------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "2026-08-07 09:00:00 ERROR plain text",
        '{"ts": "2026-08-07T09:00:00Z", "level": "warn", "msg": "json line"}',
        "Aug  7 09:00:00 web01 sshd[123]: syslog line",
        'ts=2026-08-07T09:00:00Z level=info msg="logfmt line" host=web01',
        "10.0.0.1 - - [07/Aug/2026:09:00:00 +0000] \"GET / HTTP/1.1\" 500 12",
        "  continuation of the line above",
    ],
)
def test_every_built_in_shape_survives_the_boundary(
    user_root, tmp_path, line
) -> None:
    """A real process boundary, for the formats a plugin will actually meet.

    ``entry_to_wire`` is unit-tested against JSON in ``test_api_surface.py``;
    this asserts the same thing where it is actually used, which is the only
    place the shared empty ``mappingproxy`` and a naive-versus-aware timestamp
    can go wrong for real.
    """

    _write(user_root, "echo", ROUND_TRIP_EXPORTER)
    registry = _load("echo")
    registry.start()

    entries = _entries(line)
    destination = tmp_path / "echoed.txt"
    registry.exporters[0].export(entries, _context(), destination=destination)

    expected = "|".join(
        [
            entries[0].format_name,
            "" if entries[0].timestamp is None else entries[0].timestamp.isoformat(),
            entries[0].level or "",
            entries[0].message,
            repr(sorted(entries[0].fields.items())),
            str(entries[0].continuation),
            entries[0].raw,
        ]
    )
    assert destination.read_text(encoding="utf-8") == expected
    registry.shutdown()


def test_a_command_can_still_ask_clv_for_things(user_root) -> None:
    """The outbox reverses direction across the boundary, intact and in order."""

    from clv.plugins import CommandContext

    _write(user_root, "reporter", PID_COMMAND)
    registry = _load("reporter")
    registry.start()

    context = CommandContext(entries=tuple(_entries("one", "two")))
    panel = registry.commands[0].run(context)

    assert [kind for kind, _ in context.requests] == ["notify", "query"]
    assert context.requests[1][1] == "level:error"
    assert panel is not None and panel.title == "Report"
    assert [control.id for control in panel.controls] == ["loud"]
    assert panel.controls[0].value is False

    # And a panel callback round-trips the same way, including its dismissal.
    answer = registry.commands[0].on_control("loud", True, context)
    assert answer is not None and answer.dismiss is True
    assert context.requests[-1][1][0] == "loud=True"
    registry.shutdown()


def test_the_time_window_crosses_with_the_call(user_root) -> None:
    """``TimeWindow`` is the one payload with two open-ended halves."""

    from clv.plugins.host import window_from_wire, window_to_wire

    window = TimeWindow(start=None, end=None)
    assert window_from_wire(window_to_wire(window)) == window

    from datetime import datetime, timezone

    aware = TimeWindow(
        start=datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 8, 7, 10, 0),
    )
    assert window_from_wire(window_to_wire(aware)) == aware


# --- lifecycle ---------------------------------------------------------------


def test_setup_and_teardown_run_in_the_child_exactly_once(
    user_root, tmp_path
) -> None:
    log = tmp_path / "lifecycle.txt"
    _write(user_root, "lifecycle", LIFECYCLE_COMMAND)
    registry = _load("lifecycle", settings={"lifecycle": {"log": str(log)}})

    # Nothing yet: a host starts when it is needed, not when CLV mounts.
    registry.start()
    assert not log.exists()

    from clv.plugins import CommandContext

    registry.commands[0].run(CommandContext())
    assert [line.split()[0] for line in log.read_text().splitlines()] == ["setup"]

    registry.shutdown()
    registry.shutdown()  # `on_unmount` is not guaranteed to fire exactly once
    kinds = [line.split()[0] for line in log.read_text().splitlines()]
    assert kinds == ["setup", "teardown"]
    assert registry._hosts == {}


def test_shutdown_survives_a_teardown_that_never_returns(user_root) -> None:
    """A hang on the way out is the case ``stop()``'s escalation exists for."""

    from clv.plugins import CommandContext

    _write(user_root, "clingy", HANGING_TEARDOWN)
    registry = _load("clingy")
    registry.start()
    registry.commands[0].run(CommandContext())
    host = _host_of(registry, "clingy")
    assert host.alive
    # Tightened only now, and through the registry — which is also the assertion
    # that `shutdown()` reads the ceiling as it stands rather than as it stood
    # when the host was built.
    registry.host_timeout_ms = SLOW_ENOUGH_MS

    registry.shutdown()

    assert not host.alive, "a child that will not leave must be made to"
    assert registry._hosts == {}


def test_a_reload_carries_the_new_section_to_the_child(user_root) -> None:
    """An isolated plugin holds a snapshot, and CLV replaces it before the call.

    The documented cost of the boundary: an in-process plugin reads through a
    live view and sees an edit without being called.
    """

    from clv.plugins import CommandContext

    _write(user_root, "echoes", SETTINGS_COMMAND)
    registry = _load("echoes", settings={"echoes": {"greeting": "first"}})
    registry.start()

    context = CommandContext()
    registry.commands[0].run(context)
    assert context.requests[0][1][0] == "first"

    registry.refresh_settings({"echoes": {"greeting": "second"}})
    context = CommandContext()
    registry.commands[0].run(context)
    assert context.requests[0][1][0] == "second"
    registry.shutdown()


def test_the_child_has_no_bundle_library_path(user_root, monkeypatch) -> None:
    """``child_environment`` is reused, not reimplemented. One correct version."""

    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/clv/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib64")
    _write(user_root, "env", ENVIRONMENT_EXPORTER)
    registry = _load("env")
    registry.start()

    result = registry.exporters[0].export((), _context())

    assert result.detail == repr("/usr/lib64")
    registry.shutdown()


# --- a build with no isolated plugins ----------------------------------------


def test_no_isolated_plugin_starts_no_process(user_root, monkeypatch) -> None:
    """Requirement 10, asserted where it would be easiest to break."""

    import multiprocessing

    def refuse(*args, **kwargs):  # pragma: no cover - the point is it is not hit
        raise AssertionError("a build with no isolated plugins spawned something")

    monkeypatch.setattr(multiprocessing, "get_context", refuse)

    _write(user_root, "plain", PLAIN_STAGE)
    registry = _load("plain")
    registry.start()

    assert registry.filters and not _errors(registry)
    assert registry._hosts == {}
    registry.shutdown()


# --- what the operator is shown ----------------------------------------------


def test_an_isolated_plugin_is_reported_as_isolated(user_root) -> None:
    """The state Phase 4 reserved and never produced."""

    _write(user_root, "pidexp", PID_EXPORTER)
    registry = _load("pidexp")

    row = registry.status()[0]
    assert row.state == "isolated"
    assert row.kinds == ("exporter",)
    assert "subprocess" in row.detail
    assert "runs as you" in row.detail, "the honest half of the sentence"


def test_a_half_isolated_module_is_not_reported_as_contained(user_root) -> None:
    """One module, two plugins, one of them in this process. Not "isolated"."""

    _write(user_root, "mixed", MIXED_MODULE)
    registry = _load("mixed")

    row = registry.status()[0]
    assert row.state == "loaded"
    assert "1 of 2 isolated" in row.detail


def test_a_killed_plugin_is_reported_as_failed_and_can_be_reinstated(
    user_root,
) -> None:
    from clv.plugins import CommandContext
    from clv.plugins.host import HostTimeout

    _write(user_root, "sleeper", HANGING_COMMAND)
    registry = _load("sleeper")
    registry.start()
    _warmed(registry, "sleeper", deadline=SLOW_ENOUGH_MS)
    with pytest.raises(HostTimeout):
        registry.commands[0].run(CommandContext())

    row = registry.status()[0]
    assert row.state == "failed"
    # `runtime` is what the dialog gates **Re-enable** on, and a killed host is
    # exactly the case an operator should be able to put back.
    assert row.category == "runtime"
    assert "killed" in row.detail
    registry.shutdown()


# --- frozen builds -----------------------------------------------------------


def test_the_spawn_handshake_precedes_the_app_import() -> None:
    """Asserted against the source, because no test can build a binary.

    Spawn starts its child by re-running ``sys.executable``, which in a frozen
    build is the CLV binary. Unless that argv is handled before anything else,
    an isolated plugin's first call re-launches the whole viewer.
    """

    source = (Path(__file__).resolve().parents[1] / "clv" / "__main__.py").read_text(
        encoding="utf-8"
    )
    # Everything after the module docstring, comments and blank lines dropped.
    code = [
        line.strip()
        for line in source.split('"""', 2)[-1].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    handshake = code.index("freeze_support()")
    imports_app = next(
        index for index, line in enumerate(code) if "from clv.app import run" in line
    )
    assert handshake < imports_app, "CLV is imported before the spawn handshake"


def test_the_handshake_is_the_one_that_works_on_the_release_floor() -> None:
    """3.11's ``multiprocessing.freeze_support`` does nothing on Linux.

    It checks ``sys.platform == 'win32'`` — so the documented spelling is a
    no-op in exactly the build this exists for, the 3.11 Linux bundle the
    release workflow produces. 3.14 widened the check, which is what makes this
    the kind of divergence a green local suite cannot show you.

    The implementation underneath is the same on both and detects a spawned
    child from argv, so CLV calls that one. This test is here so that
    "simplifying" it back to the documented call has to argue with 3.11 first.
    """

    import multiprocessing.spawn

    source = (Path(__file__).resolve().parents[1] / "clv" / "__main__.py").read_text(
        encoding="utf-8"
    )
    assert "from multiprocessing.spawn import freeze_support" in source
    assert hasattr(multiprocessing.spawn, "freeze_support")

    # And the real reason, pinned against the running interpreter rather than
    # asserted from memory: on 3.11 the documented one is platform-gated.
    import inspect
    import multiprocessing.context

    documented = inspect.getsource(multiprocessing.context.BaseContext.freeze_support)
    if sys.version_info < (3, 12):
        assert "win32" in documented, (
            "3.11 stopped platform-gating freeze_support; the comment in "
            "clv/__main__.py needs rereading"
        )


# --- what the operator's switch does -----------------------------------------


def test_switching_an_isolated_plugin_off_stops_its_process(user_root) -> None:
    """"Stops now" has to mean the process too.

    The `P` dialog tells an operator that disabling a user plugin stops it now.
    For an isolated one that has to include the child: nothing can call into it
    again, and a host left running for a plugin nobody can reach is exactly the
    state this phase exists to be able to end.
    """

    from clv.plugins import OPERATOR_DISABLE_REASON, CommandContext

    _write(user_root, "reporter", PID_COMMAND)
    registry = _load("reporter")
    registry.start()
    command = registry.commands[0]
    command.run(CommandContext())
    host = _host_of(registry, "reporter")
    assert host.alive

    registry.disable(command, OPERATOR_DISABLE_REASON, record=False)

    assert not host.alive
    # And switching it back on is not answered with the corpse: the next call
    # starts a fresh child.
    assert registry.enable(command) is True
    context = CommandContext()
    command.run(context)
    assert _host_of(registry, "reporter").alive
    assert context.requests, "the plugin ran again"
    registry.shutdown()


# --- one plugin's fault is one plugin's --------------------------------------


def test_a_configure_that_raises_reaches_the_call_site(user_root) -> None:
    """In-process it disables the plugin; here it has to survive the handshake.

    A host holds every plugin one module exported, so a `configure()` that
    raises cannot be allowed to fail the handshake — that would take down two
    working plugins because a third had a bad section. The fault is kept and
    answered on every call into *that* plugin, which is what lets the call site
    disable it exactly as it disables an in-process one.
    """

    from clv.plugins import CommandContext

    _write(user_root, "picky", PICKY_COMMAND)
    registry = _load("picky")
    registry.start()

    with pytest.raises(Exception) as caught:
        registry.commands[0].run(CommandContext())

    assert "configure() raised" in str(caught.value)
    assert "needs a target" in str(caught.value)
    # Still a plugin failure rather than a host failure: the child is fine.
    assert _host_of(registry, "picky").alive
    registry.shutdown()


# --- one module, one child, two threads --------------------------------------


def test_one_host_serves_two_threads_without_interleaving(user_root, tmp_path) -> None:
    """A sink on its own thread and a command on the event loop, one pipe.

    One module can export both, and a module gets one host — so two requests
    can be in flight on one connection from two threads. Interleaved, that is a
    corrupted stream rather than a slow one, and the failure would look like the
    plugin returning somebody else's answer.
    """

    import threading

    from clv.plugins import CommandContext

    target = tmp_path / "delivered.txt"
    _write(user_root, "both", SINK_AND_COMMAND)
    registry = _load("both", settings={"both": {"target": str(target)}})
    registry.start()

    host = _host_of(registry, "both")
    assert host is not None
    spec = registry.watch_stack().sinks[0]
    command = registry.commands[0]
    assert len(registry._hosts) == 1, "one module, one child"

    answers: list[str] = []
    errors: list[BaseException] = []

    def deliver(index: int) -> None:
        try:
            spec.deliver(f"rule-{index}", index, _context(), ())
        except BaseException as exc:  # noqa: BLE001 - reported, not raised here
            errors.append(exc)

    def invoke(index: int) -> None:
        try:
            context = CommandContext()
            command.run(context)
            answers.append(context.requests[0][1][0])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=deliver if index % 2 else invoke, args=(index,))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads)
    # Every command call got the command's answer, not a sink's.
    assert answers == ["ran"] * 4
    registry.shutdown()


# --- through the app ---------------------------------------------------------


def test_an_isolated_exporter_reaches_the_ui_through_the_app(
    user_root, tmp_path
) -> None:
    """The app's own export path, with the plugin in another process.

    `_export_via_plugin` is unchanged by this phase — it resolves the exporter
    positionally, reads `wants_path` off it, calls `export` and reports what
    came back. That it needs no knowledge of any of this is the claim, so the
    test drives the app rather than the registry.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log(tmp_path), announce=False)
            await pilot.pause()

            _write(user_root, "pidexp", PID_EXPORTER)
            registry = _load("pidexp")
            _adopt(app, registry)
            registry.start()

            notes: list[tuple[str, str]] = []
            app._notify = lambda text, severity="information": notes.append(
                (text, severity)
            )

            destination = tmp_path / "exported.txt"
            app._run_export(
                ExportRequest(
                    key="plugin:0",
                    path=destination,
                    marked_only=False,
                    clustered=False,
                ),
                list(app._entries),
            )
            await pilot.pause()

            assert destination.exists(), notes
            assert notes and "pid=" in notes[0][0]
            assert str(destination) in notes[0][0], "the destination reaches the toast"
            assert str(os.getpid()) not in notes[0][0].split("lines=")[0]
            registry.shutdown()

    asyncio.run(scenario())


def test_the_app_comes_back_after_a_hanging_isolated_command(
    user_root, tmp_path
) -> None:
    """What a host actually buys a command, stated as the test asserts it.

    Not "the UI stays responsive": a command is called *from* the event loop and
    CLV waits for the answer, so a hanging one parks the pane for the length of
    the deadline. What changes is that the pane comes back at all — in-process
    it never did — and that the plugin is out of service rather than waiting to
    do it again on the next keypress.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._select_source(_log(tmp_path), announce=False)
            app.set_focus(app.log_panel)
            await pilot.pause()

            _write(user_root, "sleeper", HANGING_COMMAND_WITH_KEY)
            registry = _load("sleeper")
            _adopt(app, registry)
            registry.start()
            _warmed(registry, "sleeper", deadline=SLOW_ENOUGH_MS)

            await pilot.press("j")
            await pilot.pause()

            # Back, and the plugin is out of service with its key released.
            command = registry.commands[0]
            assert registry.is_disabled(command)
            assert "killed" in (registry.disabled_reason(command) or "")
            assert not _host_of(registry, "sleeper").alive

            # And the app is usable: an ordinary binding still works.
            await pilot.press("t")
            await pilot.pause()
            assert app.is_running

    asyncio.run(scenario())


def test_one_unbuildable_plugin_does_not_cost_its_module(user_root) -> None:
    """`add()`'s contract, on the far side of a process boundary.

    In-process, a class whose ``__init__`` raises is recorded and skipped and
    the module's other plugins load. The child has to do the same: the module
    is imported once there, so failing the handshake over one bad constructor
    would take down every plugin that module exported.
    """

    from clv.plugins import CommandContext

    _write(user_root, "halfbroken", HALF_BROKEN_MODULE)
    registry = _load("halfbroken", settings={"halfbroken": {"isolated": "true"}})

    assert [command.command_name for command in registry.commands] == ["works"]
    assert any(
        "could not be instantiated" in message and "Broken" in message
        for message in _errors(registry)
    ), _errors(registry)

    context = CommandContext()
    registry.commands[0].run(context)
    assert context.requests[0][1][0] == "fine"
    registry.shutdown()
