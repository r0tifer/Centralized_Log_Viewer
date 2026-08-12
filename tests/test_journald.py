"""Provider wiring and the journald plugin (Item 12).

Fixture-based throughout: the suite must pass in a container with no systemd,
so nothing here runs `journalctl`. The one thing that *is* asserted about the
real subprocess is that it never starts — see the opt-in tests, which patch the
spawn point and check it was not reached.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from textual.widgets import Static, Switch

from clv.app import LogTree, LogViewerApp
from clv.plugins import (
    IteratorReader,
    LogSourceProvider,
    PluginRegistry,
    ProviderSource,
    load_plugins,
)
from clv.plugins.sources import journald
from clv.services import SourceManager, persist_setting
from clv.services.config import load_config, user_config_path
from clv.services.parsing import parse_line
from clv.services.session import SourceSession

# One captured `journalctl -o json` record per line, trimmed to the fields that
# matter. Real output has ~30 keys; the shape is what is being tested.
JOURNAL_RECORDS = [
    {
        "__REALTIME_TIMESTAMP": "1754913600123456",
        "PRIORITY": "3",
        "_HOSTNAME": "web01",
        "_SYSTEMD_UNIT": "sshd.service",
        "_PID": "991",
        "SYSLOG_IDENTIFIER": "sshd",
        "MESSAGE": "Failed password for root from 10.0.0.9",
    },
    {
        "__REALTIME_TIMESTAMP": "1754913601500000",
        "PRIORITY": "6",
        "_HOSTNAME": "web01",
        "_SYSTEMD_UNIT": "nginx.service",
        "_PID": "1042",
        "SYSLOG_IDENTIFIER": "nginx",
        "MESSAGE": "GET /health 200",
    },
]

FIXTURE = "".join(json.dumps(record) + "\n" for record in JOURNAL_RECORDS)


class FakeProcess:
    """A journalctl that has already said everything it is going to say."""

    def __init__(self, payload: str = FIXTURE, *, exits: bool = False) -> None:
        self.stdout = _Pipe(payload)
        self.terminated = False
        self.killed = False
        self._exits = exits

    def poll(self):
        return 0 if self._exits else None

    def terminate(self) -> None:
        self.terminated = True
        self._exits = True

    def kill(self) -> None:  # pragma: no cover - only on a wedged process
        self.killed = True

    def wait(self, timeout=None) -> int:
        return 0


class _Pipe:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")
        self.closed = False

    def read(self, size=-1):
        chunk, self._payload = self._payload, b""
        return chunk

    def fileno(self) -> int:
        raise OSError("not a real pipe")

    def close(self) -> None:
        self.closed = True


def _spawner(process):
    def spawn(argv, **kwargs):
        process.argv = argv
        return process

    return spawn


# --- translating records ----------------------------------------------------


def test_a_journal_record_becomes_a_line_the_parser_understands() -> None:
    """The item assumed no translation was needed. It is."""

    raw = journald.translate(JOURNAL_RECORDS[0])
    entry = parse_line(raw)

    assert entry.format_name == "json"
    assert entry.level == "ERROR"  # PRIORITY 3, a numeric string
    assert entry.timestamp is not None  # __REALTIME_TIMESTAMP, microseconds
    assert entry.message == "Failed password for root from 10.0.0.9"


def test_untranslated_journal_output_would_not_have_parsed() -> None:
    """Kept as the reason the translation exists, not as trivia."""

    entry = parse_line(json.dumps(JOURNAL_RECORDS[0]))

    assert entry.timestamp is None
    assert entry.level is None


def test_journal_fields_arrive_under_names_a_query_can_use() -> None:
    entry = parse_line(journald.translate(JOURNAL_RECORDS[0]))

    assert entry.fields["unit"] == "sshd.service"
    assert entry.fields["host"] == "web01"
    assert entry.fields["pid"] == "991"
    assert entry.fields["tag"] == "sshd"
    # The journal's own names survive too, so either spelling works.
    assert entry.fields["_SYSTEMD_UNIT"] == "sshd.service"


def test_a_binary_message_is_decoded_rather_than_dropped() -> None:
    record = dict(JOURNAL_RECORDS[0], MESSAGE=[104, 105])

    assert parse_line(journald.translate(record)).message == "hi"


# --- the reader -------------------------------------------------------------


def test_priming_reads_the_captured_records() -> None:
    process = FakeProcess()
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=100, spawn=_spawner(process)
    )

    result = reader.prime()

    assert len(result.lines) == 2
    assert parse_line(result.lines[0]).message == "Failed password for root from 10.0.0.9"


def test_a_partial_record_is_held_until_the_rest_arrives() -> None:
    """Half a JSON object is not a record."""

    head, tail = FIXTURE[:60], FIXTURE[60:]
    process = FakeProcess(head)
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=100, spawn=_spawner(process)
    )

    first = reader.prime()
    process.stdout = _Pipe(tail)
    second = reader.poll()

    assert len(first.lines) + len(second.lines) == 2


def test_a_line_that_is_not_json_is_kept_rather_than_dropped() -> None:
    process = FakeProcess("-- No entries --\n")
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=100, spawn=_spawner(process)
    )

    assert reader.prime().lines == ["-- No entries --"]


def test_the_command_carries_the_unit_and_the_line_bound() -> None:
    reader = journald.JournalReader(Path("journal:unit/sshd.service"), max_lines=500)

    argv = reader.command()

    assert "--unit=sshd.service" in argv
    assert "--lines=500" in argv
    assert "--follow" in argv
    assert "--output=json" in argv


def test_the_command_carries_a_boot_selector() -> None:
    assert "--boot=-1" in journald.JournalReader(Path("journal:boot/-1")).command()


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [("error", "--priority=3"), ("warn", "--priority=4"), ("info", "--priority=6")],
)
def test_severity_pushes_down_to_journalctl(bucket, expected) -> None:
    reader = journald.JournalReader(Path("journal:all"), severity=bucket)

    assert expected in reader.command()


@pytest.mark.parametrize("bucket", ["all", "debug", "trace"])
def test_buckets_that_would_filter_nothing_push_nothing_down(bucket) -> None:
    """--priority=7 is everything; pretending to filter would be a lie."""

    argv = journald.JournalReader(Path("journal:all"), severity=bucket).command()

    assert not [arg for arg in argv if arg.startswith("--priority")]


def test_changing_severity_restarts_the_follow() -> None:
    process = FakeProcess()
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=100, spawn=_spawner(process)
    )
    reader.prime()

    assert reader.set_severity("error") is True
    assert process.terminated is True
    # Two buckets that push the same thing down cost no restart.
    reader.prime()
    assert reader.set_severity("error") is False


def test_closing_terminates_the_subprocess() -> None:
    process = FakeProcess()
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=100, spawn=_spawner(process)
    )
    reader.prime()

    reader.close()

    assert process.terminated is True
    assert process.stdout.closed is True
    reader.close()  # twice is safe


def test_an_exited_journalctl_is_not_respawned_every_tick() -> None:
    """Re-running it on every poll would be a fork bomb with a nice name."""

    process = FakeProcess(exits=True)
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=100, spawn=_spawner(process)
    )
    reader.prime()

    reader.poll()
    spawned: list[list[str]] = []

    def counting_spawn(argv, **kwargs):  # pragma: no cover - must not be called
        spawned.append(argv)
        return FakeProcess()

    reader._spawn = counting_spawn
    reader.poll()
    reader.poll()

    assert spawned == []


# --- the opt-in -------------------------------------------------------------


def test_nothing_is_spawned_without_the_opt_in(monkeypatch) -> None:
    """The consent guarantee, asserted at the spawn point itself."""

    spawned: list[object] = []
    monkeypatch.setattr(
        journald.subprocess, "Popen", lambda *a, **k: spawned.append(a) or FakeProcess()
    )
    monkeypatch.setattr(journald.subprocess, "run", lambda *a, **k: spawned.append(a))

    provider = journald.JournaldProvider()

    assert list(provider.discover()) == []
    assert spawned == []
    assert "disabled" in provider.status


def test_the_opt_in_is_read_fresh_so_the_switch_needs_no_restart(monkeypatch) -> None:
    settings = user_config_path()
    assert journald.enabled() is False

    persist_setting(settings, "enable_journald", "true")

    assert journald.enabled() is True
    assert load_config().enable_journald is True


def test_a_machine_without_journalctl_reports_rather_than_raises(monkeypatch) -> None:
    monkeypatch.setattr(journald.shutil, "which", lambda _name: None)
    persist_setting(user_config_path(), "enable_journald", "true")

    provider = journald.JournaldProvider()

    assert list(provider.discover()) == []
    assert "journalctl" in provider.status
    available, reason = journald.availability()
    assert available is False and reason


def test_units_and_boots_are_enumerated_from_captured_output(monkeypatch) -> None:
    monkeypatch.setattr(journald.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(journald, "availability", lambda: (True, ""))
    persist_setting(user_config_path(), "enable_journald", "true")

    def runner(argv):
        if "--list-boots" in argv:
            return "-1 abc Fri...\n 0 def Sat...\n"
        return "sshd.service\nnginx.service\nsession-3.scope\n"

    sources = list(journald.JournaldProvider(runner=runner).discover())
    labels = [source.label for source in sources]

    assert "System journal" in labels
    assert "This boot" in labels
    assert "Previous boot" in labels
    assert "sshd.service" in labels
    assert "nginx.service" in labels
    # A scope is a real unit, but a machine carries thousands; the tree lists
    # what an operator recognises.
    assert "session-3.scope" not in labels


# --- registry wiring --------------------------------------------------------


class _Fake(LogSourceProvider):
    name = "fake provider"

    def __init__(self, *, raise_discover=False, raise_open=False) -> None:
        self.raise_discover = raise_discover
        self.raise_open = raise_open

    def discover(self):
        if self.raise_discover:
            raise RuntimeError("provider is broken")
        return [ProviderSource(Path("fake:one"), "One"), Path("fake:two")]

    def open(self, path):
        if self.raise_open:
            raise RuntimeError("cannot open")
        return iter(["2026-08-11 10:00:00 - INFO - from a provider"])


def test_provider_sources_are_collected_with_their_labels() -> None:
    registry = PluginRegistry(sources=[_Fake()])

    found = registry.discover_sources()

    # A provider that supplied a label keeps it; a bare identifier is labelled
    # with itself, which is why a provider with better names should say so.
    assert [source.label for source in found] == ["One", "fake:two"]
    assert {source.provider for source in found} == {"fake provider"}


def test_a_provider_that_raises_in_discover_is_recorded_and_skipped() -> None:
    registry = PluginRegistry(sources=[_Fake(raise_discover=True), _Fake()])

    found = registry.discover_sources()

    assert len(found) == 2  # the working provider's sources survive
    assert any("discover() raised" in str(error) for error in registry.errors)


def test_a_provider_that_raises_in_open_is_recorded_and_skipped() -> None:
    registry = PluginRegistry(sources=[_Fake(raise_open=True)])
    source = registry.discover_sources()[0]

    assert registry.open_source(source, max_lines=10) is None
    assert any("open() raised" in str(error) for error in registry.errors)


def test_the_simple_open_contract_still_works() -> None:
    """A provider written before open_reader existed must not need changing."""

    registry = PluginRegistry(sources=[_Fake()])
    source = registry.discover_sources()[0]

    reader = registry.open_source(source, max_lines=10)

    assert isinstance(reader, IteratorReader)
    assert reader.prime().lines == ["2026-08-11 10:00:00 - INFO - from a provider"]
    assert reader.poll().lines == []


def test_a_provider_reader_wins_when_one_is_offered() -> None:
    class Richer(_Fake):
        def open_reader(self, path, *, max_lines):
            return journald.JournalReader(
                path, max_lines=max_lines, spawn=_spawner(FakeProcess())
            )

    registry = PluginRegistry(sources=[Richer()])
    source = registry.discover_sources()[0]

    assert isinstance(registry.open_source(source, max_lines=10), journald.JournalReader)


def test_a_session_reads_a_provider_source_like_any_other() -> None:
    registry = PluginRegistry(sources=[_Fake()])
    source = registry.discover_sources()[0]
    reader = registry.open_source(source, max_lines=10)

    session = SourceSession(max_lines=10)
    session.adopt(source.path, reader)

    assert [entry.message for entry in session.entries] == ["from a provider"]


def test_the_session_closes_a_provider_reader_on_switch(tmp_path: Path) -> None:
    """A leaked --follow per source switch is the obvious failure here."""

    process = FakeProcess()
    reader = journald.JournalReader(
        Path("journal:all"), max_lines=10, spawn=_spawner(process)
    )
    session = SourceSession(max_lines=10)
    session.adopt(Path("journal:all"), reader)

    log = tmp_path / "app.log"
    log.write_text("2026-08-11 10:00:00 - INFO - a file\n", encoding="utf-8")
    session.open_single(log)

    assert process.terminated is True


# --- the app ----------------------------------------------------------------


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_provider_sources_appear_in_their_own_group_and_open(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._plugins = PluginRegistry(sources=[_Fake()])
            app._source_manager = SourceManager([], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            provider_nodes = [
                node for node in _walk(tree.root) if isinstance(node.data, ProviderSource)
            ]
            assert len(provider_nodes) == 2

            group = provider_nodes[0].parent
            assert "Providers" in str(group.label)

            app._select_provider_source(provider_nodes[0].data)
            await pilot.pause()

            assert [entry.message for entry in app._entries] == ["from a provider"]

    asyncio.run(scenario())


def test_a_broken_provider_does_not_break_discovery(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "app.log").write_text("2026-08-11 10:00:00 - INFO - real\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._plugins = PluginRegistry(sources=[_Fake(raise_discover=True)])
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            files = [
                node
                for node in _walk(tree.root)
                if isinstance(node.data, Path) and node.data.name == "app.log"
            ]
            assert files, "a broken provider cost the operator their real sources"
            assert app._plugins.errors

    asyncio.run(scenario())


def test_the_drawer_switch_writes_the_opt_in_back_to_settings() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._set_journald(True)
            await pilot.pause()

            contents = app._settings_path.read_text(encoding="utf-8")
            assert "enable_journald = true" in contents
            # Consent given once, not once per launch.
            assert load_config(app._settings_path).enable_journald is True

            app._set_journald(False)
            await pilot.pause()
            assert "enable_journald = false" in app._settings_path.read_text(
                encoding="utf-8"
            )

    asyncio.run(scenario())


# --- the loader, where the shipped binary differs from a checkout -----------


def test_drop_ins_are_found_without_the_folder_existing_on_disk(monkeypatch) -> None:
    """A PyInstaller bundle has the modules but not the directories.

    `_load_local` used to gate the subpackage search on `sub_dir.is_dir()`,
    which is False inside a bundle even though the modules are present and
    importable. Every drop-in was skipped, and silently — finding no plugins
    is a valid state, so nothing was reported. The shipped binary therefore
    offered no journal however the opt-in was set.

    Where a subpackage lives is now asked of the import system, which is the
    only thing that knows when the answer is "inside an archive". Simulated
    here by making the filesystem deny the directory exists.
    """

    from pathlib import Path as RealPath

    monkeypatch.setattr(RealPath, "is_dir", lambda self: False)

    registry = load_plugins(include_entry_points=False)

    assert any(isinstance(plugin, journald.JournaldProvider) for plugin in registry.sources), (
        "no drop-in was loaded when the plugin folder was not a real directory"
    )
    assert not registry.errors


def test_the_drawer_says_when_the_plugin_is_missing_from_the_build() -> None:
    """A switch that writes an opt-in nothing reads is worse than a dead one."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._plugins = PluginRegistry()  # a build that loaded no plugins
            app._sync_journald_status()
            await pilot.pause()

            drawer = app.advanced_drawer
            status = drawer.query_one("#journald-status", Static).render().plain
            assert "not loaded" in status
            assert drawer.query_one("#drawer-journald", Switch).disabled is True

    asyncio.run(scenario())


def test_the_status_says_what_was_found_not_just_that_it_looked() -> None:
    """"Two sources and no units" is a failure that looked like success."""

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(journald.shutil, "which", lambda _n: "/usr/bin/journalctl")
        monkey.setattr(journald, "availability", lambda: (True, ""))
        persist_setting(user_config_path(), "enable_journald", "true")

        healthy = journald.JournaldProvider(
            runner=lambda argv: (
                "-1 a\n 0 b\n" if "--list-boots" in argv else "sshd.service\nnginx.service\n"
            )
        )
        list(healthy.discover())
        assert "2 unit(s)" in healthy.status

        # journalctl ran and said nothing — a real answer, not a failure.
        quiet = journald.JournaldProvider(runner=lambda argv: "")
        list(quiet.discover())
        assert "no units listed" in quiet.status
        assert "journalctl reported none" in quiet.status

        # journalctl could not be run — a different fact, and the actionable one.
        broken = journald.JournaldProvider(
            runner=lambda argv: ("", "journalctl timed out after 10s")
        )
        list(broken.discover())
        assert "timed out" in broken.status
    finally:
        monkey.undo()


def test_a_failing_journalctl_reports_why_rather_than_returning_empty() -> None:
    assert _run_reports("/nonexistent/journalctl") != ""


def _run_reports(binary: str) -> str:
    _out, error = journald._run([binary, "--list-boots"])
    return error


def test_the_drawer_reports_what_the_provider_found() -> None:
    """The provider knew all along; nothing displayed it."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()

            provider = journald.JournaldProvider()
            provider.status = "3 journal source(s), no units listed (journalctl timed out)"
            app._plugins = PluginRegistry(sources=[provider])
            app._config = replace(app._config, enable_journald=True)
            monkey = pytest.MonkeyPatch()
            monkey.setattr(
                "clv.plugins.sources.journald.availability", lambda: (True, "")
            )
            try:
                app._sync_journald_status()
                await pilot.pause()
                status = app.advanced_drawer.query_one(
                    "#journald-status", Static
                ).render().plain
            finally:
                monkey.undo()

            assert "no units listed" in status
            assert "timed out" in status

    asyncio.run(scenario())


def test_providers_are_asked_off_the_event_loop(tmp_path: Path) -> None:
    """A provider shells out; on the UI thread that is a frozen app.

    Enumerating units on a multi-gigabyte journal is seconds of work, and the
    query has a timeout measured in tens of seconds. Discovery already runs in
    a worker thread for the filesystem walk; this asserts the providers went
    with it, because the symptom of getting that wrong is a UI that stops
    responding and a tree that quietly comes back short.
    """

    import threading

    root = tmp_path / "logs"
    root.mkdir()
    (root / "app.log").write_text("2026-08-12 10:00:00 - INFO - x\n", encoding="utf-8")

    class Slow(LogSourceProvider):
        name = "slow provider"

        def __init__(self) -> None:
            self.thread: str | None = None

        def discover(self):
            self.thread = threading.current_thread().name
            return [ProviderSource(Path("slow:one"), "One")]

        def open(self, path):
            return iter(())

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            provider = Slow()
            app._plugins = PluginRegistry(sources=[provider])
            app._source_manager = SourceManager([root], [])
            main = threading.current_thread().name

            await app._rescan()
            await pilot.pause()

            assert provider.thread is not None, "the provider was never asked"
            assert provider.thread != main, (
                f"discover() ran on {provider.thread}, the event loop's thread"
            )
            assert len(app._provider_sources) == 1

    asyncio.run(scenario())
