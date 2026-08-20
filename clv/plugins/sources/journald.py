"""The systemd journal, as a CLV source.

Event Viewer's entire premise is reading *the OS event log*. On Linux that is
the journal — binary, so no amount of file discovery will ever find it, and
excluded by name besides. Without this, CLV is an excellent multi-file tailer
that stops exactly where the tool it is imitating starts.

**Disabled by default, and this is the reason the journal ships as a plugin
rather than as core.** Reading the journal means running ``journalctl``, and
`clv/plugins/AGENTS.md` is explicit that a plugin must not execute a subprocess
without consent. So nothing here spawns anything until ``enable_journald`` is
true in ``settings.conf`` — checked on every ``discover()``, not once at import,
so turning it on in the Advanced drawer takes effect without a restart.

**The parser needs the records translated, which the plan did not expect.**
``journalctl -o json`` emits ``MESSAGE``/``PRIORITY``/``__REALTIME_TIMESTAMP``:
uppercase keys the JSON matcher does not look for, a priority that is a numeric
string rather than a level name, and a timestamp in microseconds since the
epoch. Fed to CLV as-is, every journal record parses as a JSON line with no
timestamp, no level, and the whole record as its message. Rather than teach
``parsing.py`` about one source's field names, the translation happens here --
the plugin chose ``-o json``, so the plugin owns what that produces -- and what
reaches the parser is a JSON line with normalised keys. Every original journal
field is kept alongside them, so `_SYSTEMD_UNIT` is still there to query.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from ...services.config import load_config
from ...services.parsing import normalize_level
from ...services.reader import TailRead
from ...services.refs import JournalRef, RemoteRef, SourceRef, parse_ref
from .. import LogSourceProvider, ProviderSource

#: The identifier scheme. Not a real path, and deliberately not one: nothing on
#: disk answers to it.
#:
#: What it *is* now is a :class:`~clv.services.refs.JournalRef`, where it used
#: to be a ``Path`` holding this prefix. The string form is unchanged, so a
#: ``session.json`` written by an older build still means what it meant; what
#: changed is that the type says so. Glob filtering and rotated-set grouping
#: still refuse one — a journal has no directory and nothing to rotate — but
#: they now refuse it *by name* rather than by it happening not to be a file.
#: Starring and merging no longer do, which is the point: a unit is exactly the
#: source an operator wants starred, and comparing one across a fleet is the
#: workflow remote sources exist for.
SCHEME = "journal:"

#: How many records the initial read asks for. The backwards-seek guarantee
#: applied to a source that has no seeking: --lines bounds it at the source.
DEFAULT_LINES = 2_000

#: Seconds a one-shot enumeration query may take. Generous because the work is
#: proportional to the journal: `--field=_SYSTEMD_UNIT` walks it, and several
#: gigabytes with a cold page cache is well past ten seconds. Timing out here
#: costs the unit list silently, so the bound is set to be reached only by
#: something genuinely wedged. Affordable because discovery runs in a worker
#: thread — on the event loop this would have been ten seconds of frozen UI.
QUERY_TIMEOUT = 45

#: Severity buckets that are worth pushing down to `journalctl --priority`.
#: `debug` and `trace` map to priority 7, which is everything, so pushing them
#: down would filter nothing while pretending to. A pushed-down priority is
#: always a *superset* of what the bucket keeps -- the client-side filter still
#: decides exactly, and this only avoids carrying what it would throw away.
PRIORITY_PUSHDOWN: dict[str, str] = {
    "error": "3",
    "warn": "4",
    "info": "6",
}


def journalctl_path() -> Optional[str]:
    return shutil.which("journalctl")


def child_environment() -> dict[str, str]:
    """The environment a *system* binary should be run with.

    A frozen build is the reason this exists. PyInstaller points
    ``LD_LIBRARY_PATH`` at its own ``_internal`` directory so the bundled
    interpreter finds the libraries shipped beside it — and child processes
    inherit it, so ``/usr/bin/journalctl`` loads the *bundle's* copies of
    libcrypto, libssl and the rest instead of the system's.

    That is fatal exactly when the bundle was built on a different distribution
    from the one running it, which for a released binary is the normal case::

        journalctl: .../_internal/libcrypto.so.3: version `OPENSSL_3.4.0' not
        found (required by /usr/lib64/systemd/libsystemd-shared-258.10.so)

    journalctl exits 1, the unit list comes back empty, and the tree shows only
    the two sources that need no subprocess. So children get the environment
    they would have had: whatever PyInstaller saved in ``*_ORIG``, or the path
    with the bundle's own directory removed. Anything the operator set
    themselves is left alone — it is theirs, and only the injected entry is
    ours to take back out.
    """

    env = dict(os.environ)
    bundle = getattr(sys, "_MEIPASS", None)
    for variable in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(f"{variable}_ORIG", None)
        if original is not None:
            env[variable] = original
            continue
        if not bundle or variable not in env:
            continue
        kept = [
            entry
            for entry in env[variable].split(os.pathsep)
            if entry and os.path.normpath(entry) != os.path.normpath(bundle)
        ]
        if kept:
            env[variable] = os.pathsep.join(kept)
        else:
            env.pop(variable, None)
    return env


def availability() -> tuple[bool, str]:
    """Whether the journal can be read here, and why not when it cannot."""

    if journalctl_path() is None:
        return False, "journalctl is not on PATH — not a systemd machine?"
    if not Path("/run/systemd/system").exists():
        return False, "systemd is not running here"
    return True, ""


def enabled() -> bool:
    """Whether the operator has opted in, read fresh every time.

    Not cached: the Advanced drawer writes ``enable_journald`` to the settings
    file, and the next rescan has to see it without a restart.
    """

    try:
        return bool(getattr(load_config(), "enable_journald", False))
    except Exception:  # noqa: BLE001 - a broken config must not break discovery
        return False


# --- translating a journal record -------------------------------------------

#: Journal field → the normalised name CLV knows it by. The journal's own keys
#: are kept as well, so a query can use either.
_FIELD_MAP: dict[str, str] = {
    "_SYSTEMD_UNIT": "unit",
    "_HOSTNAME": "host",
    "_PID": "pid",
    "SYSLOG_IDENTIFIER": "tag",
    "_BOOT_ID": "boot",
}


def translate(record: dict[str, Any]) -> str:
    """One journal record → one JSON line the parser understands."""

    payload: dict[str, Any] = {}

    stamp = _timestamp(record)
    if stamp is not None:
        payload["timestamp"] = stamp.isoformat(sep=" ", timespec="milliseconds")
    level = normalize_level(record.get("PRIORITY"))
    if level is not None:
        payload["level"] = level
    payload["message"] = _message(record)

    for journal_key, normalised in _FIELD_MAP.items():
        value = record.get(journal_key)
        if isinstance(value, str) and value:
            payload[normalised] = value

    # The originals last so they cannot overwrite a normalised key, and so a
    # query can name either `unit` or `_SYSTEMD_UNIT`.
    for key, value in record.items():
        if key not in payload and isinstance(value, (str, int, float)):
            payload[key] = value

    return json.dumps(payload, ensure_ascii=False)


def _timestamp(record: dict[str, Any]) -> Optional[datetime]:
    """``__REALTIME_TIMESTAMP`` is microseconds since the epoch, as a string."""

    raw = record.get("__REALTIME_TIMESTAMP")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1_000_000)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _message(record: dict[str, Any]) -> str:
    """The text of a record, including the binary form the journal may use."""

    message = record.get("MESSAGE")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        # A message with unprintable bytes arrives as a list of ints.
        try:
            return bytes(message).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            return ""
    return "" if message is None else str(message)


# --- the reader -------------------------------------------------------------


class JournalReader:
    """Streams ``journalctl -o json --follow`` into the same reader contract.

    The subprocess is the whole risk here, so it is handled explicitly: stdout
    is set non-blocking and drained on the poll that already runs, rather than
    read from a thread that would have to be joined; and :meth:`close` is
    called by the session on every source switch and again at shutdown, so a
    leaked ``--follow`` per switch is not possible to arrange.
    """

    RELOAD_NOTICE = "{name} restarted."

    def __init__(
        self,
        path: SourceRef,
        *,
        max_lines: int = DEFAULT_LINES,
        severity: str = "all",
        spawn=subprocess.Popen,
    ) -> None:
        #: Normalised on the way in, so exactly one shape reaches `command()`.
        #:
        #: The provider hands a `JournalRef`, but this constructor is reachable
        #: with the string form too -- `open_reader` takes whatever its caller
        #: holds, and a stored `journal:unit/sshd.service` is a legal thing to
        #: hold. Parsing here rather than branching there is what stops the
        #: failure that shape would otherwise cause: a selector silently
        #: dropped, and `journalctl` asked for the *whole journal* when one unit
        #: was meant. That reads as a working source with far too much in it,
        #: which is worse than an error.
        self.path = path if isinstance(path, JournalRef) else parse_ref(str(path))
        #: Which `journalctl` to run. Resolved through `shutil.which` locally,
        #: because a PyInstaller bundle's PATH is not the shell's. A *remote*
        #: reader overrides it with the bare name: `which` here would answer for
        #: this machine, and the other machine's layout is its own business.
        self.binary = journalctl_path() or "journalctl"
        self._max_lines = max_lines
        self._severity = severity
        self._spawn = spawn
        self._process: Any = None
        self._remainder = ""
        self._offset = 0

    # --- the command ---------------------------------------------------------

    @property
    def offset(self) -> int:
        return self._offset

    def command(self) -> list[str]:
        """The argv this reader would run."""

        argv = [
            self.binary,
            "--output=json",
            "--no-pager",
            f"--lines={self._max_lines}",
            "--follow",
        ]
        # Read off the ref rather than re-parsed out of its string form. The
        # ref already did that parsing once, at the persistence boundary, and
        # doing it again here is how the two spellings drift apart -- which is
        # exactly what a node between the scheme and the colon would have
        # exposed.
        if isinstance(self.path, JournalRef):
            if self.path.kind == "unit":
                argv.append(f"--unit={self.path.value}")
            elif self.path.kind == "boot":
                argv.append(f"--boot={self.path.value}")
        priority = PRIORITY_PUSHDOWN.get(self._severity)
        if priority is not None:
            argv.append(f"--priority={priority}")
        return argv

    def set_severity(self, severity: str) -> bool:
        """Adopt a new severity bucket, restarting if the argv would change.

        Returns whether a restart happened, so the caller knows the buffer it
        holds is stale. Only the buckets in :data:`PRIORITY_PUSHDOWN` can cause
        one — switching between two buckets that push nothing down costs
        nothing.
        """

        if severity == self._severity:
            return False
        before = PRIORITY_PUSHDOWN.get(self._severity)
        self._severity = severity
        if PRIORITY_PUSHDOWN.get(severity) == before:
            return False
        if self._process is None:
            return False
        self.close()
        return True

    # --- the contract --------------------------------------------------------

    def _start(self):
        """Spawn the follow. The **only** part of this reader that is local.

        Split out so a remote journal can reuse everything else — the argv, the
        record translation, the non-blocking drain, the severity push-down and
        the teardown are all transport-independent, and proving that is easier
        than claiming it: `tests/test_ssh_source.py` runs one captured
        `journalctl -o json` fixture through both paths and asserts one result.
        """

        try:
            return self._spawn(
                self.command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                # Without this the follow dies the same way the enumeration
                # did, and with stderr discarded it would die silently.
                env=child_environment(),
            )
        except OSError as exc:
            raise OSError(f"could not run journalctl: {exc}") from exc

    def prime(self) -> TailRead:
        self.close()
        self._process = self._start()

        self._remainder = ""
        self._offset = 0
        stdout = getattr(self._process, "stdout", None)
        if stdout is not None:
            try:
                os.set_blocking(stdout.fileno(), False)
            except (OSError, AttributeError, ValueError):
                # A fake process in a test, or a platform without it: the
                # drain below tolerates a blocking read returning everything.
                pass
        # --lines has already decided how much history there is, and it arrives
        # as fast as the journal can write it; one drain is the initial read.
        return TailRead(lines=self._drain(), offset=self._offset)

    def poll(self) -> TailRead:
        if self._process is None:
            return TailRead(lines=[], offset=self._offset)
        if self._process.poll() is not None:
            # journalctl exited: the follow is over, and re-running it every
            # tick would be a fork bomb with a nice name.
            lines = self._drain()
            self.close()
            return TailRead(lines=lines, offset=self._offset)
        return TailRead(lines=self._drain(), offset=self._offset)

    def _drain(self) -> list[str]:
        """Read whatever is buffered, translating complete records only."""

        stdout = getattr(self._process, "stdout", None)
        if stdout is None:
            return []
        try:
            chunk = stdout.read()
        except (BlockingIOError, ValueError):
            return []
        if not chunk:
            return []
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")

        return self._records(chunk)

    def _records(self, chunk: str) -> list[str]:
        """Complete JSON records in *chunk*, translated. Partial ones held back.

        Separate from :meth:`_drain` because the drain is where the transport
        lives — a local pipe here, a framed `ssh` stream for a remote host — and
        this is where the *journal* lives. A remote reader reuses this verbatim,
        which is what makes "the same fixture translates identically either way"
        a fact about the code rather than a hope.
        """

        text = self._remainder + chunk
        raw_lines = text.splitlines()
        if not text.endswith("\n"):
            # Hold the partial record: half a JSON object is not a record.
            self._remainder = raw_lines.pop() if raw_lines else text
        else:
            self._remainder = ""

        lines: list[str] = []
        for raw in raw_lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                # Not a record. Kept rather than dropped: never silently lose
                # a line, even one that arrived malformed.
                lines.append(raw)
                continue
            lines.append(translate(record) if isinstance(record, dict) else raw)
        self._offset += len(lines)
        return lines

    def close(self) -> None:
        """Stop the subprocess. Safe to call twice, and on a dead process."""

        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except Exception:  # noqa: BLE001 - the process may already be gone
            pass
        finally:
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    stdout.close()
                except Exception:  # noqa: BLE001
                    pass

    def __del__(self) -> None:  # pragma: no cover - a backstop, not the path
        # The session closes readers explicitly; this only catches a reader
        # that somehow never reached one.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# --- the provider -----------------------------------------------------------


@dataclass
class _Unit:
    name: str
    description: str = ""


class JournaldProvider(LogSourceProvider):
    """Offers the journal as a whole, per unit, and per boot."""

    name = "systemd journal"

    def __init__(self, *, runner=None, max_lines: int = DEFAULT_LINES) -> None:
        #: Injected so unit enumeration can be tested against captured output
        #: rather than against whatever the machine running the suite has.
        self._runner = runner or _run
        self._max_lines = max_lines
        self.status = "disabled"
        #: Why the last unit enumeration came back empty, when it did. Empty
        #: for "it worked", which is not the same as "it returned nothing".
        self.unit_error = ""
        #: The app's connection resolver, or ``None`` when remote journals are
        #: not wired up. **Injected, never constructed here**, and that is a
        #: correctness requirement rather than a layering preference:
        #: ``SSHConnection.socket`` hashes ``(name, host, user, port)``, so a
        #: connection this module built for itself would resolve to the *same*
        #: multiplex socket the resolver is using — and its ``close()`` would
        #: run ``ssh -O exit`` on a master the rest of CLV is actively reading
        #: through. One owner per connection, and it is not this one.
        self._resolver = None
        #: The configured hosts, in file order.
        self._hosts: tuple = ()
        #: Per host, why it offers no journal — a missing `journalctl`, an
        #: unreachable machine, a failed enumeration. Reported, never raised.
        self.host_notes: dict[str, str] = {}

    def use_remote(self, resolver, hosts) -> None:
        """Adopt the app's connection resolver and host list.

        Called after the plugin registry has loaded, because a provider is
        constructed by ``register()`` and knows nothing about the app. Passing
        ``None`` unwires it, which is what happens when ``enable_ssh`` goes off.
        """

        self._resolver = resolver
        self._hosts = tuple(hosts or ())

    # --- discovery -----------------------------------------------------------

    def discover(self) -> Iterable[ProviderSource]:
        """Every journal source, or nothing at all when not opted in.

        Checked in this order on purpose: consent first, capability second. A
        machine with no systemd should say so, but only to someone who asked
        for the journal in the first place.
        """

        self.host_notes = {}
        if not enabled():
            self.status = "disabled (set enable_journald to turn it on)"
            return []

        sources: list[ProviderSource] = []
        notes: list[str] = []

        # **Local availability no longer gates the whole provider.** A laptop
        # with no systemd reading the journals of a systemd fleet is an ordinary
        # thing to want, and returning early here would have refused it — the
        # machine CLV runs on has no bearing on what another machine offers.
        available, reason = availability()
        if available:
            sources += self._local_sources()
            notes.append(self._local_note(sources))
        else:
            notes.append(f"local: {reason}")

        self.status = "; ".join(note for note in notes if note)
        return sources

    def discover_remote(self) -> list[ProviderSource]:
        """Every configured host's journal. **Called from the host stage.**

        Deliberately not part of :meth:`discover`, and the reason is a timing
        one that a test caught rather than a design one that was foreseen.
        Discovery runs in two stages: the local tree is built first so that a
        machine which is down costs the local tree nothing, and hosts are folded
        in by a second worker afterwards. Enumerating remote journals from the
        *first* stage meant paying a connect timeout per configured host before
        anything appeared — and paying it a second time on ``Ctrl+R``, which
        resets the backoff, so a reload with one dead machine took twice as long
        as it had any reason to.

        Run from the second stage instead, every host is already in a known
        state: a live one has a probed, multiplexed connection so this costs one
        round trip, and a dead one is already marked unreachable so it costs
        nothing at all.
        """

        self.host_notes = {}
        if not enabled():
            return []
        return self._remote_sources()

    def _local_sources(self) -> list[ProviderSource]:
        """This machine's journal, its boots and its units."""

        sources = [
            ProviderSource(JournalRef("", "all"), "System journal", self.name),
            ProviderSource(JournalRef("", "boot", "0"), "This boot", self.name),
        ]
        sources += [
            ProviderSource(JournalRef("", "boot", "-1"), "Previous boot", self.name)
        ] if self._has_previous_boot() else []
        sources += [
            ProviderSource(JournalRef("", "unit", unit.name), unit.name, self.name)
            for unit in self._units()
        ]
        return sources

    def _local_note(self, sources: list[ProviderSource]) -> str:
        """Says what it found *and* what it could not.

        "Two sources and no units" is the shape of a failure and looked
        identical to success.
        """

        units = [
            source
            for source in sources
            if isinstance(source.path, JournalRef) and source.path.kind == "unit"
        ]
        if units:
            return f"{len(sources)} journal source(s), {len(units)} unit(s)"
        return (
            f"{len(sources)} journal source(s), no units listed ({self.unit_error})"
            if self.unit_error
            else f"{len(sources)} journal source(s), no units listed — journalctl reported none"
        )

    def _remote_sources(self) -> list[ProviderSource]:
        """Every configured host's journal, when both opt-ins are on.

        **Two consents, and neither implies the other.** ``enable_journald``
        says CLV may run ``journalctl``; ``enable_ssh`` says it may open a
        network connection. A remote journal needs both, and asking for one does
        not quietly grant the other.

        A host that cannot offer a journal is *noted*, never raised: no
        `journalctl` on the far side is a capability, an unreachable machine is
        a Phase 5 fact already reported elsewhere, and neither is a reason for
        the other hosts — or the local journal — to come back empty.
        """

        if self._resolver is None or not self._hosts:
            return []

        # Imported here rather than at module scope because `ssh.py` imports
        # *this* module; the dependency runs one way and a deferred import is
        # how the reader and the opt-in are reached without inverting it.
        from . import ssh

        if not ssh.enabled():
            return []

        sources: list[ProviderSource] = []
        for host in self._hosts:
            if not host.enabled:
                continue
            try:
                connection = self._connection_for(host)
                if connection is None:
                    continue
                # **A host inside its backoff is skipped, not retried**, and is
                # still reported from what is already known. Phase 5's rule, and
                # it applies with force here: `facts()` probes, and spending a
                # connect timeout per configured host on every rescan to learn
                # again what CLV was told a second ago is what makes a reload
                # with one dead machine feel broken. An *untried* host reports
                # `connected` optimistically, so a first scan still asks.
                # **Property here, method on `RemoteBackend`.** Calling the
                # backend's spelling on a connection called the frozen dataclass
                # the property returned, and 2.10.0 shipped with no remote
                # journal at all. The two shapes still differ; this is the warning.
                reach = connection.reachability
                if not reach.ok:
                    self.host_notes[host.name] = reach.reason or "unreachable"
                    continue
                if not connection.facts().journalctl:
                    self.host_notes[host.name] = "no journalctl on that host"
                    continue
                units = self._remote_units(host, connection)
            except Exception as exc:  # noqa: BLE001 - one host must not cost the rest
                # Unreachable, refused, timed out. Phase 5 owns saying so about
                # the *host*; here it costs that host's journal and nothing else.
                #
                # **Broader than `OSError` on purpose.** The comment above always
                # claimed per-host containment, but a non-`OSError` bug in this
                # loop escaped to `_discover_remote_providers` and cost *every*
                # host's journal — which is exactly how the `reachability` call
                # above shipped as a dead feature rather than one noisy host.
                first = str(exc).strip().splitlines()
                self.host_notes[host.name] = first[0] if first else "unreachable"
                continue
            sources.append(
                ProviderSource(
                    JournalRef(host.name, "all"), f"{host.name}: System journal", self.name
                )
            )
            sources += [
                ProviderSource(
                    JournalRef(host.name, "unit", unit),
                    f"{host.name}: {unit}",
                    self.name,
                )
                for unit in units
            ]
        return sources

    def _connection_for(self, host):
        """The resolver's live connection for *host*, or ``None``.

        Reached through a ref so the resolver's own caching and reconciliation
        apply — building one here is the duplicate-master hazard `use_remote`
        exists to avoid.
        """

        backend = self._resolver.for_ref(RemoteRef.build(host.name, "/"))
        return getattr(backend, "connection", None)

    def _remote_units(self, host, connection) -> list[str]:
        """`.service` units with journal entries on *host*.

        One command per host, in the worker discovery already runs on. The
        enumeration walks the remote journal, so it is the slow call here and
        `QUERY_TIMEOUT` bounds it — generous, because several gigabytes with a
        cold page cache is well past ten seconds and timing out costs the whole
        unit list.
        """

        from .ssh import quote_all

        script = quote_all("journalctl", "--no-pager", "--field=_SYSTEMD_UNIT")
        body = connection.run(f"{script} 2>/dev/null", timeout=QUERY_TIMEOUT)
        units = sorted(
            {
                line.strip()
                for line in body.splitlines()
                if line.strip().endswith(".service")
            }
        )
        return units[:200]

    def _query(self, argv: list[str]) -> tuple[str, str]:
        """Run a journalctl query as ``(stdout, why it failed)``.

        Injected runners may return a bare string — that is the whole contract
        a test needs — so both shapes are accepted.
        """

        result = self._runner(argv)
        if isinstance(result, tuple):
            return result
        return result, ""

    def _units(self) -> list[_Unit]:
        """Units with journal entries, as ``journalctl --field`` reports them."""

        output, self.unit_error = self._query(
            [journalctl_path() or "journalctl", "--no-pager", "--field=_SYSTEMD_UNIT"]
        )
        units: list[_Unit] = []
        for line in output.splitlines():
            name = line.strip()
            # Templated and scope units are real, but a machine can carry
            # thousands of them; a plain .service list is what an operator
            # recognises as "Windows Logs → Application".
            if name.endswith(".service"):
                units.append(_Unit(name))
        return sorted(units, key=lambda unit: unit.name)[:200]

    def _has_previous_boot(self) -> bool:
        output, _error = self._query(
            [journalctl_path() or "journalctl", "--no-pager", "--list-boots"]
        )
        return len([line for line in output.splitlines() if line.strip()]) > 1

    # --- opening -------------------------------------------------------------

    def open(self, path: SourceRef) -> Iterator[str]:
        """The simple contract, for completeness.

        Never called while :meth:`open_reader` returns a reader, which it
        always does — a journal follow is a stream, and an iterator cannot be
        asked to stop.
        """

        reader = self.open_reader(path, max_lines=self._max_lines)
        try:
            yield from reader.prime().lines
        finally:
            reader.close()

    def open_reader(self, path: SourceRef, *, max_lines: int, **kwargs):
        """A local follow, or a remote one when the ref names a machine.

        Dispatch is on the ref, as everywhere else in CLV: `journal:unit/x` is
        this machine and `journal@web01:unit/x` is not. The import is deferred
        for the reason `_remote_sources`'s is — `ssh.py` imports this module,
        so the dependency runs one way.
        """

        bound = min(max_lines, self._max_lines)
        node = path.node if isinstance(path, JournalRef) else ""
        if node:
            connection = self._connection_for_node(node)
            if connection is None:
                raise OSError(
                    f"No connection is configured for '{node}'. Add it with R, "
                    "or turn on Remote (SSH)."
                )
            from .ssh import RemoteJournalReader

            return RemoteJournalReader(
                path, connection=connection, max_lines=bound, **kwargs
            )
        return JournalReader(path, max_lines=bound, **kwargs)

    def _connection_for_node(self, node: str):
        """The live connection for a host *name*, or ``None`` if there is none."""

        if self._resolver is None:
            return None
        host = next((entry for entry in self._hosts if entry.name == node), None)
        if host is None:
            return None
        return self._connection_for(host)


def _run(argv: list[str]) -> tuple[str, str]:
    """Run a short, bounded journalctl query as ``(stdout, why it failed)``.

    Never raises, but no longer swallows the reason either: a query that timed
    out and one that returned nothing are different facts, and the difference
    is exactly what an operator needs when the tree is emptier than expected.
    """

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
            check=False,
            env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        return "", f"journalctl timed out after {QUERY_TIMEOUT}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"journalctl could not be run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return "", f"journalctl exited {completed.returncode}: {detail[0] if detail else 'no output'}"
    return completed.stdout or "", ""


def register() -> list[LogSourceProvider]:
    return [JournaldProvider()]
