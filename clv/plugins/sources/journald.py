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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from ...services.config import load_config
from ...services.parsing import normalize_level
from ...services.reader import TailRead
from .. import LogSourceProvider, ProviderSource

#: The identifier scheme. Not a real path, and deliberately not one: nothing on
#: disk answers to it, which is what keeps these out of starring, glob
#: filtering and anything else that assumes a file.
SCHEME = "journal:"

#: How many records the initial read asks for. The backwards-seek guarantee
#: applied to a source that has no seeking: --lines bounds it at the source.
DEFAULT_LINES = 2_000

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
        path: Path,
        *,
        max_lines: int = DEFAULT_LINES,
        severity: str = "all",
        spawn=subprocess.Popen,
    ) -> None:
        self.path = path
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
            journalctl_path() or "journalctl",
            "--output=json",
            "--no-pager",
            f"--lines={self._max_lines}",
            "--follow",
        ]
        selector = str(self.path)[len(SCHEME) :] if str(self.path).startswith(SCHEME) else ""
        kind, _, value = selector.partition("/")
        if kind == "unit" and value:
            argv.append(f"--unit={value}")
        elif kind == "boot" and value:
            argv.append(f"--boot={value}")
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

    def prime(self) -> TailRead:
        self.close()
        try:
            self._process = self._spawn(
                self.command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
            )
        except OSError as exc:
            raise OSError(f"could not run journalctl: {exc}") from exc

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

    # --- discovery -----------------------------------------------------------

    def discover(self) -> Iterable[ProviderSource]:
        """Every journal source, or nothing at all when not opted in.

        Checked in this order on purpose: consent first, capability second. A
        machine with no systemd should say so, but only to someone who asked
        for the journal in the first place.
        """

        if not enabled():
            self.status = "disabled (set enable_journald to turn it on)"
            return []
        available, reason = availability()
        if not available:
            self.status = reason
            return []

        sources = [
            ProviderSource(Path(f"{SCHEME}all"), "System journal", self.name),
            ProviderSource(Path(f"{SCHEME}boot/0"), "This boot", self.name),
        ]
        sources += [
            ProviderSource(Path(f"{SCHEME}boot/-1"), "Previous boot", self.name)
        ] if self._has_previous_boot() else []
        sources += [
            ProviderSource(Path(f"{SCHEME}unit/{unit.name}"), unit.name, self.name)
            for unit in self._units()
        ]
        self.status = f"{len(sources)} journal source(s)"
        return sources

    def _units(self) -> list[_Unit]:
        """Units with journal entries, as ``journalctl --field`` reports them."""

        output = self._runner(
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
        output = self._runner(
            [journalctl_path() or "journalctl", "--no-pager", "--list-boots"]
        )
        return len([line for line in output.splitlines() if line.strip()]) > 1

    # --- opening -------------------------------------------------------------

    def open(self, path: Path) -> Iterator[str]:
        """The simple contract, for completeness.

        Never called while :meth:`open_reader` returns a reader, which it
        always does — a journal follow is a stream, and an iterator cannot be
        asked to stop.
        """

        reader = JournalReader(path, max_lines=self._max_lines)
        try:
            yield from reader.prime().lines
        finally:
            reader.close()

    def open_reader(self, path: Path, *, max_lines: int, **kwargs):
        return JournalReader(path, max_lines=min(max_lines, self._max_lines), **kwargs)


def _run(argv: list[str]) -> str:
    """Run a short, bounded journalctl query. Never raises."""

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout or ""


def register() -> list[LogSourceProvider]:
    return [JournaldProvider()]
