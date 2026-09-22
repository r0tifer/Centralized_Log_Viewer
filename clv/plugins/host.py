"""The isolation host: the first place in CLV a plugin can be *stopped*.

Every plugin CLV loads runs in this process, at this process's privilege, on
this process's event loop. Phase 1 made a plugin that *raises* survivable;
nothing made a plugin that *hangs* survivable, because the six render budgets
work by measuring a pass and declining to start the next one and CLV cannot
interrupt a call it is inside. ``SinkDispatcher`` says the same thing about a
thread: abandoned, not killed.

A child process can be killed. That is the whole of what this module buys, and
the honest sentence is worth putting at the top of the file that implements it:

    **Isolation contains crashes, hangs and leaks; it does not make an
    untrusted plugin safe.**

The child runs as the operator, with the operator's filesystem, holding the
operator's credentials. It starts in a scrubbed environment and a restricted
working directory and that is the extent of the reduction — no namespaces, no
seccomp, no capability dropping, and no claim that any of those are happening.

**Why ``multiprocessing`` with the spawn context.** Fork in a running Textual
app, with open readers and a terminal in raw mode, is a footgun; spawn gives a
clean interpreter. Under PyInstaller a spawned child would re-run the whole
application, which is why :mod:`clv.__main__` calls ``freeze_support()`` before
it imports anything of CLV's.

**Why nothing is pickled that CLV did not encode itself.** ``LogEntry`` cannot
be pickled at all — ``fields`` is a ``mappingproxy``, including the shared empty
one, so ``pickle.dumps`` raises on every entry CLV produces. Phase 2 published
:func:`~clv.services.parsing.entry_to_wire` for exactly this boundary, and every
payload here is built from JSON scalars so that what crosses is inspectable
rather than whatever a plugin's ``__reduce__`` happened to do.

**One child per origin, not per plugin object.** A module's plugins share the
module's imported state; giving them a host each would import it once per
plugin and give two halves of one plugin two different globals.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..services.filtering import FilterSpec, TimeWindow
from ..services.parsing import LogEntry, entry_from_wire, entry_to_wire
from ..services.refs import SourceRef, format_ref, parse_ref

#: Bumped when a payload below changes shape. Checked on the handshake, so a
#: child from a different CLV — which a frozen build plus a source checkout on
#: one machine can produce — is refused with a message rather than
#: misunderstood field by field.
HOST_WIRE_VERSION = 1

#: How long a host may take to start and answer its handshake, and how long any
#: one call may take, when the caller does not say. The app passes
#: ``plugin_host_timeout_ms`` from the operator's settings.
DEFAULT_HOST_TIMEOUT_MS = 5_000.0

#: How long a child gets to leave after being asked politely, before it is
#: terminated, and again before it is killed. The same two seconds
#: ``JournalReader.close`` has always given ``journalctl``.
_STOP_GRACE = 0.5
_TERMINATE_GRACE = 2.0


class HostError(Exception):
    """Base for every way a host can fail its side of a call."""


class HostTimeout(HostError):
    """A call did not answer inside the deadline. The child has been killed."""


class HostDead(HostError):
    """The child is gone — it crashed, exited, or never started."""


class PluginCallError(Exception):
    """The plugin itself raised, in the child.

    Deliberately not a :class:`HostError`: the host did its job. This is the
    ordinary "third-party code raised" that every call site in CLV already
    handles, carried across a process boundary with its message intact.
    """


# --- the wire ---------------------------------------------------------------
#
# Everything below is JSON scalars, lists and dicts. Nothing here reaches for
# pickle's ability to send an arbitrary object: a payload that cannot be read
# by eye is a payload nobody can debug, and an object graph a plugin controls
# is not something to unpickle on either side of this boundary.


def _moment(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _moment_from(value: Any) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _scalar(value: Any) -> Any:
    """Reduce a control's value to something JSON can carry.

    ``CONTROL_KINDS`` produces strings and booleans and nothing else, so this is
    a guard rather than a conversion — but a plugin builds the ``Control`` and a
    plugin can put anything in ``value``, and the failure mode of letting that
    through is an unpicklable object taking down a call instead of a control
    rendering oddly.
    """

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def ref_to_wire(ref: Optional[SourceRef]) -> Optional[str]:
    """A source's identity as text.

    :func:`~clv.services.refs.format_ref` and ``parse_ref`` are exact inverses
    that never touch the filesystem — which is what makes them the right pair
    here, where the two ends have different working directories on purpose.
    """

    return None if ref is None else format_ref(ref)


def ref_from_wire(value: Any) -> Optional[SourceRef]:
    return None if not value else parse_ref(str(value))


def window_to_wire(window: TimeWindow) -> dict[str, Any]:
    return {"start": _moment(window.start), "end": _moment(window.end)}


def window_from_wire(payload: Mapping[str, Any]) -> TimeWindow:
    return TimeWindow(
        start=_moment_from(payload.get("start")),
        end=_moment_from(payload.get("end")),
    )


def spec_to_wire(spec: Optional[FilterSpec]) -> Optional[dict[str, Any]]:
    if spec is None:
        return None
    return {
        "query": spec.query,
        "severity": spec.severity,
        "window": window_to_wire(spec.window),
        "case_sensitive": spec.case_sensitive,
        "regex": spec.regex,
        "invert": spec.invert,
        "known_fields": sorted(spec.known_fields),
    }


def spec_from_wire(payload: Optional[Mapping[str, Any]]) -> Optional[FilterSpec]:
    if payload is None:
        return None
    return FilterSpec(
        query=str(payload.get("query", "")),
        severity=str(payload.get("severity", "all")),
        window=window_from_wire(payload.get("window") or {}),
        case_sensitive=payload.get("case_sensitive"),
        regex=bool(payload.get("regex", True)),
        invert=bool(payload.get("invert", False)),
        known_fields=frozenset(payload.get("known_fields") or ()),
    )


def entries_to_wire(entries: Sequence[LogEntry]) -> list[dict[str, Any]]:
    return [entry_to_wire(entry) for entry in entries]


def entries_from_wire(payload: Sequence[Mapping[str, Any]]) -> list[LogEntry]:
    return [entry_from_wire(item) for item in payload]


def filter_context_to_wire(context: Any) -> dict[str, Any]:
    return {
        "spec": spec_to_wire(getattr(context, "spec", None)),
        "source": ref_to_wire(getattr(context, "source", None)),
    }


def filter_context_from_wire(payload: Mapping[str, Any]) -> Any:
    from . import FilterContext

    return FilterContext(
        spec=spec_from_wire(payload.get("spec")) or FilterSpec(),
        source=ref_from_wire(payload.get("source")),
    )


def command_context_to_wire(context: Any) -> dict[str, Any]:
    return {
        "entry": entry_to_wire(context.entry) if context.entry is not None else None,
        "entries": entries_to_wire(context.entries),
        "spec": spec_to_wire(context.spec),
        "source": ref_to_wire(context.source),
        "values": {str(key): _scalar(value) for key, value in context.values.items()},
    }


def command_context_from_wire(payload: Mapping[str, Any]) -> Any:
    from . import CommandContext

    entry = payload.get("entry")
    return CommandContext(
        entry=entry_from_wire(entry) if entry else None,
        entries=tuple(entries_from_wire(payload.get("entries") or ())),
        spec=spec_from_wire(payload.get("spec")),
        source=ref_from_wire(payload.get("source")),
        values=dict(payload.get("values") or {}),
    )


def requests_to_wire(requests: Sequence[tuple[str, Any]]) -> list[list[Any]]:
    """A command's outbox, on its way home.

    The queue is filled in the child — ``context.notify()`` appends there — and
    performed in the parent, which is the one place this boundary reverses
    direction. It carries nothing a request did not already carry: the whole
    point of ``CommandContext`` holding data rather than a callable was that a
    command could not reach the app, and a command in another process reaches it
    exactly as far.
    """

    encoded: list[list[Any]] = []
    for kind, payload in requests:
        if kind == "source":
            encoded.append([kind, ref_to_wire(payload)])
        elif kind == "notify":
            text, severity = payload
            encoded.append([kind, [str(text), str(severity)]])
        else:
            encoded.append([kind, str(payload)])
    return encoded


def requests_from_wire(payload: Sequence[Sequence[Any]]) -> list[tuple[str, Any]]:
    decoded: list[tuple[str, Any]] = []
    for item in payload:
        kind = str(item[0])
        value = item[1]
        if kind == "source":
            decoded.append((kind, ref_from_wire(value)))
        elif kind == "notify":
            text, severity = value
            decoded.append((kind, (str(text), str(severity))))
        else:
            decoded.append((kind, str(value)))
    return decoded


def control_to_wire(control: Any) -> dict[str, Any]:
    return {
        "kind": str(control.kind),
        "id": str(control.id),
        "label": str(control.label),
        "value": _scalar(control.value),
        "options": [[str(value), str(label)] for value, label in control.options],
        "placeholder": str(control.placeholder),
    }


def panel_to_wire(panel: Any) -> Optional[dict[str, Any]]:
    if panel is None:
        return None
    return {
        "title": str(panel.title),
        "controls": [control_to_wire(control) for control in panel.controls],
        "dismiss": bool(panel.dismiss),
    }


def panel_from_wire(payload: Optional[Mapping[str, Any]]) -> Any:
    if payload is None:
        return None
    from . import Control, Panel

    controls = tuple(
        Control(
            kind=str(item.get("kind", "")),
            id=str(item.get("id", "")),
            label=str(item.get("label", "")),
            value=item.get("value", ""),
            options=tuple((str(a), str(b)) for a, b in (item.get("options") or ())),
            placeholder=str(item.get("placeholder", "")),
        )
        for item in payload.get("controls") or ()
    )
    return Panel(
        title=str(payload.get("title", "")),
        controls=controls,
        dismiss=bool(payload.get("dismiss", False)),
    )


def export_result_to_wire(result: Any) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    destination = getattr(result, "destination", None)
    return {
        "ok": bool(getattr(result, "ok", False)),
        "detail": str(getattr(result, "detail", "")),
        "destination": None if destination is None else str(destination),
    }


def export_result_from_wire(payload: Optional[Mapping[str, Any]]) -> Any:
    if payload is None:
        return None
    from . import ExportResult

    destination = payload.get("destination")
    return ExportResult(
        ok=bool(payload.get("ok", False)),
        detail=str(payload.get("detail", "")),
        destination=None if destination is None else Path(str(destination)),
    )


def annotations_to_wire(produced: Any) -> list[list[Any]]:
    """Whatever the plugin yielded, without judging it.

    A malformed annotation must reach the parent *as* a malformed annotation:
    ``TimelineStack`` is where the shape is validated and where the plugin is
    taken out of service with a message naming what it returned, and a second
    validator here would either disagree with that one or make it unreachable.
    """

    encoded: list[list[Any]] = []
    for item in produced:
        try:
            moment, label, level = item
        except (TypeError, ValueError):
            encoded.append(["?", repr(item)])
            continue
        encoded.append(
            [
                _moment(moment) if isinstance(moment, datetime) else repr(moment),
                str(label),
                None if level is None else str(level),
            ]
        )
    return encoded


def annotations_from_wire(payload: Sequence[Sequence[Any]]) -> list[Any]:
    decoded: list[Any] = []
    for item in payload:
        if len(item) != 3:
            decoded.append(tuple(item))
            continue
        moment, label, level = item
        try:
            when: Any = _moment_from(moment)
        except ValueError:
            when = moment
        decoded.append((when, label, level))
    return decoded


# --- the parent side --------------------------------------------------------


@dataclass
class HostPlugin:
    """One plugin the child holds, as the parent knows it.

    The declarative half of a plugin — everything CLV reads off the class rather
    than calling — travels once, at the handshake, and is what the proxy answers
    from. Built by ``manifest_for`` in both processes, so the class-declared
    door and the settings-forced door cannot describe the same plugin
    differently.
    """

    index: int
    qualname: str
    name: str
    kinds: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    requires_clv: Optional[str] = None
    requires_api: Optional[str] = None

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "HostPlugin":
        return cls(
            index=int(payload.get("index", 0)),
            qualname=str(payload.get("qualname", "")),
            name=str(payload.get("name", "")),
            kinds=tuple(str(kind) for kind in payload.get("kinds") or ()),
            attributes=dict(payload.get("attributes") or {}),
            priority=int(payload.get("priority", 100)),
            requires_clv=payload.get("requires_clv"),
            requires_api=payload.get("requires_api"),
        )


class PluginHost:
    """One child process, and the plugins one origin put in it.

    Started lazily, because a plugin whose command is never pressed should not
    cost a subprocess — with one exception, and it is structural rather than an
    oversight: a plugin the operator isolated from ``settings.conf`` is never
    imported here, so the only way to find out what it *is* is to ask the child.
    That door pays for its handshake at load.
    """

    def __init__(
        self,
        origin: str,
        *,
        timeout_ms: float = DEFAULT_HOST_TIMEOUT_MS,
        settings: Optional[Mapping[str, str]] = None,
        start_method: str = "spawn",
    ) -> None:
        self.origin = origin
        self.timeout_ms = timeout_ms
        self.plugins: list[HostPlugin] = []
        self.errors: list[str] = []
        self._settings = dict(settings or {})
        self._settings_dirty = False
        self._start_method = start_method
        self._process: Any = None
        self._conn: Any = None
        self._workdir: Optional[str] = None
        self._dead: Optional[str] = None
        self._started = False
        #: One pipe, and callers on two different threads. A `WatchSink` is
        #: delivered on `SinkDispatcher`'s thread while a `Command` from the
        #: same module runs on the event loop -- one origin, one host, one
        #: connection, and two interleaved requests on it would be a corrupted
        #: stream rather than a slow one. Re-entrant because `call` starts the
        #: host through `ensure_started`.
        self._lock = threading.RLock()

    # --- lifecycle ----------------------------------------------------------

    @property
    def alive(self) -> bool:
        process = self._process
        return bool(process is not None and process.is_alive())

    @property
    def dead_reason(self) -> Optional[str]:
        return self._dead

    def revive(self) -> None:
        """Forget that this host died, so the next call starts a fresh one.

        The way back from a kill, reached when an operator presses **Re-enable**
        in ``P``. A host that was killed for hanging is not evidence that the
        next call will hang, and a plugin the operator has deliberately put back
        into service must not be answered with the corpse of its last attempt.
        """

        self._dead = None

    def ensure_started(self, *, setup: bool = False) -> None:
        """Start the child and complete its handshake, if it is not up.

        Raises :class:`HostDead` if it cannot be started, or if it died earlier
        and has not been revived — a plugin whose host failed is disabled and
        reported, never quietly run in this process.
        """

        with self._lock:
            if self._dead is not None:
                raise HostDead(self._dead)
            if self.alive and self._started:
                return
            self._spawn()
            if setup:
                self.setup([plugin.index for plugin in self.plugins])

    def _spawn(self) -> None:
        # Imported here rather than at module scope so that a build with no
        # isolated plugins never touches multiprocessing at all -- Requirement
        # 10, and the semaphore tracker spawn starts is not free.
        import multiprocessing

        self._workdir = tempfile.mkdtemp(prefix="clv-plugin-")
        try:
            context = multiprocessing.get_context(self._start_method)
        except ValueError as exc:  # pragma: no cover - platform dependent
            self._fail(f"no {self._start_method} start method: {exc}")
        parent_conn, child_conn = context.Pipe(duplex=True)
        spec = {
            "version": HOST_WIRE_VERSION,
            "origin": self.origin,
            "workdir": self._workdir,
            "settings": dict(self._settings),
        }
        try:
            process = context.Process(
                target=_child_main,
                args=(child_conn, spec),
                name=f"clv-plugin-{_short(self.origin)}",
                daemon=True,
            )
            with _without_main_module(), _spawnable_stderr():
                process.start()
        except Exception as exc:  # noqa: BLE001 - OS refusal, not plugin code
            parent_conn.close()
            child_conn.close()
            self._fail(f"could not start an isolation host: {exc}")
        # The child's copy is closed here on purpose: while the parent holds an
        # open handle to it, a child that exits never shows up as EOF and every
        # read waits for the full deadline instead of failing at once.
        child_conn.close()
        self._process = process
        self._conn = parent_conn
        self._settings_dirty = False
        self._handshake()

    def _handshake(self) -> None:
        payload = self._receive(self.timeout_ms)
        if not payload.get("ok"):
            self._fail(str(payload.get("error") or "the isolation host failed to load"))
        version = payload.get("version")
        if version != HOST_WIRE_VERSION:
            self._fail(
                f"isolation host speaks wire version {version!r}, expected "
                f"{HOST_WIRE_VERSION}"
            )
        self.plugins = [
            HostPlugin.from_wire(item) for item in payload.get("plugins") or ()
        ]
        #: What went wrong in the child that did not stop it loading -- a
        #: candidate that could not be constructed, say. Read by the loader and
        #: filed against the origin, so a fault in a module CLV never imported
        #: still reaches the operator's plugin list.
        self.errors = [str(message) for message in payload.get("errors") or ()]
        self._started = True

    def index_of(self, qualname: str) -> Optional[int]:
        for plugin in self.plugins:
            if plugin.qualname == qualname:
                return plugin.index
        return None

    def settings_changed(self, settings: Mapping[str, str]) -> None:
        """Note a re-read of ``settings.conf``, without a round trip now.

        An in-process plugin holds a *live* view of its section and sees a
        reload without being called; a plugin in another process cannot, so it
        holds a snapshot and is sent a new one before its next call. Pushing it
        the moment the operator pressed ``Ctrl+R`` would put a round trip on the
        reload path for a plugin that may never be called again.
        """

        updated = dict(settings)
        if updated == self._settings:
            # Not a change, so not a round trip. `configure()` is called at
            # `add()` time with the section the host was built from, and a
            # reload that changed nothing is the common case.
            return
        self._settings = updated
        self._settings_dirty = True

    def stop(self) -> None:
        """Ask, then terminate, then kill. Once.

        The body of ``JournalReader.close``, which has been closing
        ``journalctl`` this way since the journal provider shipped. A host
        mid-call is exactly the case the escalation exists for: it will not
        answer the polite request, and waiting for it is the hang this whole
        module exists to end.
        """

        # Tried for, never waited on. The thread this might be racing is one
        # inside a call that has not returned -- which is the case stopping
        # exists for, so blocking on it would be the hang wearing a lock. The
        # kill below ends that call anyway: its `recv` comes back EOF.
        held = self._lock.acquire(timeout=0.2)
        try:
            self._stop_locked()
        finally:
            if held:
                self._lock.release()

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        conn, self._conn = self._conn, None
        self._started = False
        try:
            if process is not None and process.is_alive():
                try:
                    if conn is not None:
                        conn.send({"op": "stop"})
                except Exception:  # noqa: BLE001 - a dead pipe is the normal case
                    pass
                process.join(_STOP_GRACE)
                if process.is_alive():
                    process.terminate()
                    process.join(_TERMINATE_GRACE)
                if process.is_alive():  # pragma: no cover - a child ignoring SIGTERM
                    process.kill()
                    process.join(_TERMINATE_GRACE)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if process is not None:
                try:
                    process.close()
                except Exception:  # noqa: BLE001 - already reaped
                    pass
            self._cleanup_workdir()

    def _cleanup_workdir(self) -> None:
        workdir, self._workdir = self._workdir, None
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- calling ------------------------------------------------------------

    def call(
        self,
        index: int,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        timeout_ms: Optional[float] = None,
    ) -> Any:
        """Run one method in the child and bring its answer back.

        Three failures, kept apart because they need three different words in
        the ``P`` dialog: the plugin raised (:class:`PluginCallError`, which is
        the ordinary third-party failure every call site already handles), the
        child did not answer in time (:class:`HostTimeout`, and it has been
        killed), or the child is gone (:class:`HostDead`).
        """

        with self._lock:
            self.ensure_started()
            if self._settings_dirty:
                self._settings_dirty = False
                self._exchange(
                    {"op": "configure", "settings": dict(self._settings)},
                    self.timeout_ms,
                )
            answer = self._exchange(
                {
                    "op": "call",
                    "index": index,
                    "method": method,
                    "payload": dict(payload or {}),
                },
                self.timeout_ms if timeout_ms is None else timeout_ms,
            )
        if not answer.get("ok"):
            raise PluginCallError(str(answer.get("error") or "raised"))
        return answer.get("value")

    def setup(self, indices: Sequence[int]) -> list[tuple[int, str]]:
        return self._hook("setup", indices)

    def teardown(self, indices: Sequence[int]) -> list[tuple[int, str]]:
        return self._hook("teardown", indices)

    def _hook(self, op: str, indices: Sequence[int]) -> list[tuple[int, str]]:
        if not self.alive or not self._started:
            return []
        with self._lock:
            answer = self._exchange(
                {"op": op, "indices": list(indices)}, self.timeout_ms
            )
        return [
            (int(index), str(message)) for index, message in answer.get("failed") or ()
        ]

    def _exchange(self, message: Mapping[str, Any], timeout_ms: float) -> Mapping[str, Any]:
        conn = self._conn
        if conn is None or not self.alive:
            self._fail("the isolation host is not running")
        try:
            conn.send(dict(message))
        except Exception as exc:  # noqa: BLE001 - a closed pipe
            self._fail(f"the isolation host stopped answering: {exc}")
        return self._receive(timeout_ms)

    def _receive(self, timeout_ms: float) -> Mapping[str, Any]:
        conn = self._conn
        if conn is None:
            self._fail("the isolation host is not running")
        # A zero or negative ceiling is the documented way to turn the guard
        # off, exactly as it is for the render budgets -- and it means what it
        # says: CLV will wait, and a plugin that never answers hangs the viewer
        # the way it would have done with no host at all.
        #
        # What the ceiling bounds is the answer *starting to arrive*, not its
        # size: once the first bytes are there, `recv` reads the whole message.
        # A plugin returning something enormous is therefore slow rather than
        # killed. That is the right trade for a plugin that is merely
        # thoughtless, and it is not a defence against one that is hostile --
        # which is the line the trust model draws, not this one.
        timeout = None if timeout_ms <= 0 else timeout_ms / 1000.0
        try:
            if timeout is not None and not conn.poll(timeout):
                self._kill(
                    f"did not answer within {timeout_ms:.0f} ms and was killed"
                )
            payload = conn.recv()
        except (EOFError, OSError) as exc:
            self._fail(f"the isolation host exited: {exc}")
        except HostError:
            raise
        except Exception as exc:  # noqa: BLE001 - unpicklable answer, broken pipe
            self._fail(f"the isolation host sent something unreadable: {exc}")
        if not isinstance(payload, Mapping):  # pragma: no cover - defensive
            self._fail("the isolation host sent something unreadable")
        return payload

    def _kill(self, reason: str) -> None:
        """Stop the child now, and remember why, then raise."""

        self._dead = reason
        process = self._process
        if process is not None:
            try:
                process.kill()
                process.join(_TERMINATE_GRACE)
            except Exception:  # noqa: BLE001
                pass
        self._teardown_state()
        raise HostTimeout(reason)

    def _fail(self, reason: str) -> None:
        self._dead = reason
        self._teardown_state()
        raise HostDead(reason)

    def _teardown_state(self) -> None:
        conn, self._conn = self._conn, None
        process, self._process = self._process, None
        self._started = False
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if process is not None:
            try:
                if process.is_alive():
                    process.terminate()
                    process.join(_TERMINATE_GRACE)
            except Exception:  # noqa: BLE001
                pass
        self._cleanup_workdir()


def _has_fileno(stream: Any) -> bool:
    try:
        return stream is not None and stream.fileno() >= 0
    except Exception:  # noqa: BLE001 - a redirect that does not pretend to be a file
        return False


@contextlib.contextmanager
def _spawnable_stderr():
    """Give ``multiprocessing`` a real stderr to hand its tracker.

    ``resource_tracker`` starts its own process the first time anything is
    spawned, and passes it ``sys.stderr.fileno()``. Inside a running Textual app
    ``sys.stderr`` is Textual's redirect, whose ``fileno()`` does not answer with
    a file descriptor — so the very first isolated call in a live viewer failed
    with ``bad value(s) in fds_to_keep``, a message about neither plugins nor
    isolation, while the same call from a script worked perfectly.

    ``os.devnull`` rather than ``sys.__stderr__``, and that is the deliberate
    half: the real stderr is the terminal the viewer is drawing on, and the
    tracker writes to it uninvited (``leaked semaphore objects``, on exit). A
    stray line there does not look like a warning, it looks like CLV is broken.
    Anything that has a genuine fd — a test run, a redirected shell — is left
    exactly as it is.
    """

    if _has_fileno(sys.stderr):
        yield
        return
    original = sys.stderr
    sink = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = sink
    try:
        yield
    finally:
        sys.stderr = original
        sink.close()


@contextlib.contextmanager
def _without_main_module():
    """Keep the child from re-running whatever started this process.

    Spawn's default is to re-import the parent's ``__main__`` in the child, so
    that a target defined there can be found. CLV's target is
    :func:`_child_main`, which the child imports from :mod:`clv.plugins.host` by
    name, so it needs none of that — and paying for it is worse than useless in
    all three of the places CLV actually runs:

    * ``python -m clv`` re-imports ``clv/__main__.py``, which imports the whole
      application into a process that exists to run one exporter.
    * The test suite's ``__main__`` is the ``pytest`` console script, so every
      host would re-import pytest before doing anything.
    * Anything embedding CLV in a script without an ``if __name__`` guard gets
      the classic bootstrapping ``RuntimeError`` — from *our* subprocess, about
      *their* file, which is an unanswerable bug report.

    Presenting a ``__main__`` with no spec and no file is how ``multiprocessing``
    already handles an interactive interpreter; it is read once, inside
    ``Process.start()``, which is the whole of what this brackets.

    ``__file__`` is left alone on Windows, where ``get_preparation_data`` reads
    it before it checks whether it is there.
    """

    import __main__ as main_module

    missing = object()
    spec = getattr(main_module, "__spec__", missing)
    file = getattr(main_module, "__file__", missing)
    main_module.__spec__ = None
    drop_file = file is not missing and sys.platform != "win32"
    if drop_file:
        del main_module.__file__
    try:
        yield
    finally:
        if spec is missing:  # pragma: no cover - a __main__ without a spec attr
            with contextlib.suppress(AttributeError):
                del main_module.__spec__
        else:
            main_module.__spec__ = spec
        if drop_file:
            main_module.__file__ = file


def _short(origin: str) -> str:
    """A readable name for the child, out of any origin shape.

    This is ``multiprocessing``'s own name for the process — what
    ``active_children()`` and its log records say — and **not** what ``ps``
    shows, which stays the interpreter's argv. Setting the process title needs
    a third-party module, and Requirement 7 is not being spent on a nicer
    ``ps`` line.
    """

    tail = origin.rsplit(":", 1)[-1]
    return Path(tail).name or "plugin"


# --- the child side ---------------------------------------------------------


def _child_main(conn: Any, spec: Mapping[str, Any]) -> None:  # pragma: no cover - child
    """Entry point of the isolation host. Runs in a fresh interpreter.

    Marked no-cover because coverage is measured in the parent; the tests
    exercise this through a real process and assert on what comes back, which is
    the only way it could be tested honestly anyway.
    """

    _child_silence()
    try:
        _child_serve(conn, spec)
    except BaseException:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise


def _child_serve(conn: Any, spec: Mapping[str, Any]) -> None:  # pragma: no cover - child
    _child_environment()
    workdir = spec.get("workdir")
    if workdir:
        try:
            os.chdir(str(workdir))
        except OSError:
            pass

    plugins: list[Any] = []
    try:
        plugins, manifest, faults, errors = _child_load(
            str(spec.get("origin", "")), spec.get("settings") or {}
        )
    except Exception as exc:  # noqa: BLE001 - third-party import
        conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return
    conn.send(
        {
            "ok": True,
            "version": HOST_WIRE_VERSION,
            "plugins": manifest,
            "errors": errors,
        }
    )

    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        op = message.get("op")
        if op == "stop":
            return
        if op == "configure":
            faults = _child_configure(plugins, message.get("settings") or {})
            conn.send({"ok": True})
            continue
        if op in ("setup", "teardown"):
            conn.send({"ok": True, "failed": _child_hook(plugins, op, message.get("indices") or [])})
            continue
        if op == "call":
            conn.send(_child_call(plugins, message, faults))
            continue
        conn.send({"ok": False, "error": f"unknown operation {op!r}"})


def _child_silence() -> None:  # pragma: no cover - child
    """Point the child's own stdout and stderr at ``/dev/null``.

    The child inherits the parent's file descriptors, which in the ordinary case
    means it inherits **the terminal the viewer is drawing on**. A plugin that
    prints, a library that warns, or an unraisable exception on the way out
    would then write over the pane — and what an operator sees is not a stray
    line of output, it is CLV appearing to corrupt its own screen.

    At the descriptor level rather than by rebinding ``sys.stdout``, because a
    plugin that writes to fd 1 directly, or a C library that does, is exactly
    the case this is protecting against. ``clv/plugins/AGENTS.md`` says plainly
    that an isolated plugin's ``print()`` goes nowhere and that ``notify()`` is
    how it talks to the operator.
    """

    try:
        null = os.open(os.devnull, os.O_RDWR)
    except OSError:
        return
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
    except OSError:
        pass
    finally:
        if null > 2:
            os.close(null)


def _child_environment() -> None:  # pragma: no cover - child
    """Scrub the bundle's library path out of this process's environment.

    ``multiprocessing`` has no ``env=``, so the child does for itself what
    ``Popen(env=child_environment())`` does for ``journalctl``: PyInstaller puts
    ``_internal`` on ``LD_LIBRARY_PATH``, and a plugin that then execs a system
    binary loads the bundle's libcrypto instead of the system's. Reused rather
    than reimplemented, because there is only one correct version of this and it
    already exists.
    """

    try:
        from .sources.journald import child_environment
    except Exception:  # noqa: BLE001 - never worth failing a host over
        return
    try:
        scrubbed = child_environment()
    except Exception:  # noqa: BLE001
        return
    os.environ.clear()
    os.environ.update(scrubbed)


def _child_load(
    origin: str, settings: Mapping[str, str]
) -> tuple[
    list[Any], list[dict[str, Any]], dict[int, str], list[str]
]:  # pragma: no cover - child
    """Import *origin* here, instantiate what it exports, and describe it."""

    from . import (
        _entry_point_candidates,
        _extract_plugins,
        _install_user_package,
        ENTRY_POINT_GROUP,
        USER_PLUGIN_PACKAGE,
        manifest_for,
    )

    candidates: list[Any] = []
    if origin.startswith(f"{ENTRY_POINT_GROUP}:"):
        import importlib.metadata

        wanted = origin.split(":", 1)[1]
        for entry_point in importlib.metadata.entry_points().select(
            group=ENTRY_POINT_GROUP
        ):
            if entry_point.name == wanted:
                candidates, _ = _entry_point_candidates(entry_point.load())
                break
    elif origin.startswith(f"{ENTRY_POINT_GROUP}.") or "/" not in origin:
        import importlib

        candidates, _ = _extract_plugins(importlib.import_module(origin))
    else:
        import importlib

        path = Path(origin)
        _install_user_package([path.parent])
        candidates, _ = _extract_plugins(
            importlib.import_module(f"{USER_PLUGIN_PACKAGE}.{path.name}")
        )

    plugins: list[Any] = []
    manifest: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in candidates:
        try:
            plugin = candidate() if isinstance(candidate, type) else candidate
        except Exception as exc:  # noqa: BLE001 - third-party constructor
            # Per candidate, exactly as `PluginRegistry.add` does it in-process:
            # one plugin that cannot be built costs itself, not the two working
            # ones its module exported beside it. Reported by name to the
            # parent, which files it against the origin like any other.
            name = getattr(candidate, "__name__", repr(candidate))
            errors.append(f"{name} could not be instantiated: {exc}")
            continue
        entry = manifest_for(plugin)
        if not entry["kinds"]:
            # Not an interface CLV knows. The parent says so in its own words
            # when it refuses the row; the child has nothing to add and nothing
            # to gain from holding an object nobody can call.
            continue
        entry["index"] = len(plugins)
        plugins.append(plugin)
        manifest.append(entry)
    return plugins, manifest, _child_configure(plugins, settings), errors


def _child_configure(
    plugins: Sequence[Any], settings: Mapping[str, str]
) -> dict[int, str]:  # pragma: no cover - child
    """Hand each plugin its section, and remember which ones refused it.

    In-process, a ``configure()`` that raises disables the plugin through
    ``PluginRegistry.disable`` — it is filed and then skipped. There is nothing
    to disable from in here, and a handshake that failed for one plugin must not
    take down a host holding three, so the fault is kept and returned from every
    call into that plugin instead. The parent then sees it at the call site,
    with the plugin's own message, and takes the same action it takes for any
    other plugin failure.
    """

    import types as _types

    view = _types.MappingProxyType(dict(settings))
    faults: dict[int, str] = {}
    for index, plugin in enumerate(plugins):
        hook = getattr(plugin, "configure", None)
        if hook is None:
            continue
        try:
            hook(view)
        except Exception as exc:  # noqa: BLE001 - third-party code
            faults[index] = f"configure() raised: {exc}"
    return faults


def _child_hook(
    plugins: Sequence[Any], hook: str, indices: Sequence[int]
) -> list[list[Any]]:  # pragma: no cover - child
    failed: list[list[Any]] = []
    for index in indices:
        if not 0 <= index < len(plugins):
            continue
        method = getattr(plugins[index], hook, None)
        if method is None:
            continue
        try:
            method()
        except Exception as exc:  # noqa: BLE001 - third-party code
            failed.append([index, f"{hook}() raised: {exc}"])
    return failed


def _child_call(
    plugins: Sequence[Any],
    message: Mapping[str, Any],
    faults: Mapping[int, str],
) -> dict[str, Any]:  # pragma: no cover - child
    index = int(message.get("index", -1))
    method = str(message.get("method", ""))
    payload = message.get("payload") or {}
    if not 0 <= index < len(plugins):
        return {"ok": False, "error": f"no plugin at index {index}"}
    if index in faults:
        return {"ok": False, "error": faults[index]}
    plugin = plugins[index]
    try:
        return {"ok": True, "value": _child_dispatch(plugin, method, payload)}
    except Exception as exc:  # noqa: BLE001 - third-party code
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _child_dispatch(
    plugin: Any, method: str, payload: Mapping[str, Any]
) -> Any:  # pragma: no cover - child
    if method == "export":
        destination = payload.get("destination")
        entries = entries_from_wire(payload.get("entries") or ())
        context = filter_context_from_wire(payload.get("context") or {})
        if payload.get("wants_path"):
            result = plugin.export(
                entries,
                context,
                destination=None if destination is None else Path(str(destination)),
            )
        else:
            result = plugin.export(entries, context)
        return export_result_to_wire(result)

    if method == "deliver":
        plugin.deliver(
            str(payload.get("name", "")),
            int(payload.get("count", 0)),
            filter_context_from_wire(payload.get("context") or {}),
            tuple(entries_from_wire(payload.get("entries") or ())),
        )
        return None

    if method == "annotations":
        produced = plugin.annotations(window_from_wire(payload.get("window") or {}))
        return annotations_to_wire(list(produced))

    if method in ("run", "on_control"):
        context = command_context_from_wire(payload.get("context") or {})
        if method == "run":
            panel = plugin.run(context)
        else:
            panel = plugin.on_control(
                str(payload.get("control_id", "")), payload.get("value"), context
            )
        return {
            "panel": panel_to_wire(panel),
            "requests": requests_to_wire(context.requests),
        }

    raise PluginCallError(f"unknown method {method!r}")
