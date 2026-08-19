"""Where a source's bytes actually come from, and what that costs.

``refs.py`` answered *what a source is*. This module answers *who reads it*.
Discovery and reading used to call ``os.walk``, ``os.access``, ``os.scandir``,
``path.stat()`` and ``path.open("rb")`` directly, which is eight years of code
quietly asserting that a source is a file on this machine. A
:class:`SourceBackend` sits between them and those calls; ``LocalBackend`` is
the only implementation today and its behaviour is exactly what was there
before.

**The blocking contract is the reason this is a type and not a helper module.**

``SourceReader.poll()`` is driven from a ``set_interval`` timer at ``refresh_hz``
— twice a second, per merged source, on the event loop. Locally every call below
is a ``stat`` or a bounded read and costs microseconds, which is the whole
reason nothing has needed this until now. Backed by a network round trip the
same code is a frozen UI, and the natural implementation of every remote
operation is the blocking one. So a backend does not merely *have* costs, it
**declares** them:

* :func:`cheap` and :func:`blocking` mark every protocol method on every
  implementation. The mark is not documentation — :func:`blocking` wraps the
  method with a guard.
* :func:`blocking_methods` derives ``BackendCapabilities.blocking`` from those
  marks, so the declaration cannot drift from the code, and refuses a class that
  leaves a method unmarked or marks a :data:`GUARANTEED_CHEAP` one as blocking.
* :func:`cheap_only` is entered by ``SourceBuffer.poll``. Inside it, calling a
  declared-blocking method raises :class:`BlockingCallError` instead of stalling
  the UI for a round trip. The flag is thread-local, so a worker thread — which
  is where ``prime()``, probes and connection setup belong — is unaffected by a
  guard the event loop is holding.

Two members deserve their reasoning stated here rather than at the call site.

**``identity`` returns an opaque comparable, never ``(st_dev, st_ino)``.** It is
compared for equality and nothing else. ``None`` means *this backend cannot
produce a stable identity*, which is a real case rather than a hypothetical: an
SFTP client's ``SFTPAttributes`` carries no inode, and a BusyBox ``stat`` may not
offer one either. ``SourceReader`` degrades to a shrink-only rotation test when
``capabilities.stable_identity`` is false, and says so, rather than pretending.

**``stat`` is cheap and ``open`` is not.** That split is the load-bearing one: it
is what lets ``poll()`` ask "did this file grow?" on the event loop while the
read that answers it is driven from somewhere else.

**``reachability`` is cheap for a different reason.** It is not an operation at
all: it reports the *last known* state of whatever the backend talks to, and it
must never probe. The status line reads it on every render and the log pane
reads it on every empty result, so a version of it that connected would be the
frozen UI in a new place. A backend that cannot reach its source is required to
say so through it, because ``SourceBuffer.poll`` deliberately swallows
``OSError`` — a source that vanished mid-session is not worth taking the pane
down for — and that is exactly right for a rotated local file and exactly wrong
for a dropped connection, which would otherwise render as a log that had merely
gone quiet.

Constraints a future backend inherits, recorded here because they are invisible
until something breaks:

* :func:`SourceBackend.open` must return a **seekable** binary handle.
  ``read_last_lines`` seeks to the end and steps backwards, and
  ``zipfile.ZipFile`` refuses a non-seekable stream outright.
* :func:`SourceBackend.walk` yields **files only**, and swallows what it cannot
  list. :func:`SourceBackend.list_dir` is the opposite: one level, no recursion,
  and it *raises*, because ``check_access`` exists to report exactly that error.
"""

from __future__ import annotations

import functools
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Literal, Protocol, Sequence

from .refs import SourceRef

__all__ = [
    "CONNECTED",
    "GUARANTEED_CHEAP",
    "LOCAL",
    "LOCAL_ACCESS_HINT",
    "MAY_BLOCK",
    "PROTOCOL_METHODS",
    "RECONNECT_ATTEMPTS",
    "RECONNECT_BACKOFF",
    "BackendCapabilities",
    "BackendResolver",
    "BackendStat",
    "BlockingCallError",
    "ClassifyRequest",
    "ClassifyResult",
    "LocalBackend",
    "Reachability",
    "ReachabilityState",
    "RefKind",
    "SourceBackend",
    "WalkEntry",
    "backoff_for",
    "blocking",
    "blocking_methods",
    "cheap",
    "cheap_only",
    "in_cheap_only",
]


#: What a ref turns out to be. ``denied`` is kept apart from ``missing`` because
#: they are different instructions to the operator: one is a permission to fix,
#: the other is a path to correct. ``other`` is a socket, a fifo, a device — and
#: also whatever a backend could not classify.
RefKind = Literal["file", "dir", "other", "missing", "denied"]


# ---------------------------------------------------------------------------
# The blocking contract
# ---------------------------------------------------------------------------


class BlockingCallError(RuntimeError):
    """A declared-blocking backend call was made from a cheap-only context.

    Raised rather than tolerated. The call would have been a round trip on the
    event loop, and the symptom of tolerating it is a UI that freezes twice a
    second — a bug that is miserable to diagnose from the outside and trivial to
    catch here.
    """


_COST_ATTR = "__backend_cost__"

#: Per-thread, so the guard the event loop holds does not reach into a worker.
#: That is not an optimisation: it is what makes "drive it from a thread" the
#: actual escape hatch rather than a comment recommending one.
_GUARD = threading.local()


def cheap(func: Callable) -> Callable:
    """Mark *func* as safe to call from ``poll()``.

    No wrapper: a cheap call is always allowed, and paying a function call to
    say so on every ``stat`` would be its own small version of this module's
    problem.
    """

    setattr(func, _COST_ATTR, "cheap")
    return func


def blocking(func: Callable) -> Callable:
    """Mark *func* as one that may block, and stop it running under the guard."""

    @functools.wraps(func)
    def guarded(self, *args: Any, **kwargs: Any):
        if getattr(_GUARD, "cheap_only", False):
            raise BlockingCallError(
                f"{type(self).__name__}.{func.__name__}() may block and was "
                "called from a cheap-only context (a poll on the event loop). "
                "Drive it from a worker thread instead."
            )
        return func(self, *args, **kwargs)

    setattr(guarded, _COST_ATTR, "blocking")
    return guarded


def in_cheap_only() -> bool:
    """Whether this thread is currently inside :func:`cheap_only`.

    Exists for the one legitimate reason a backend has to ask: a member that is
    :data:`GUARANTEED_CHEAP` but whose honest implementation is a round trip has
    to answer *something* cheap here, and it cannot know it is being asked from
    the event loop any other way.

    ``RemoteBackend.stat`` is that member. It serves its cache under the guard
    and refreshes off the wire outside it, which is what lets ``poll()`` cost
    nothing while a worker — and the backend contract suite — still see a true
    answer. Reading the flag is *not* a licence to block under it; that is still
    what :func:`blocking` exists to stop.
    """

    return bool(getattr(_GUARD, "cheap_only", False))


@contextmanager
def cheap_only() -> Iterator[None]:
    """Inside this block, a declared-blocking backend call raises.

    Nests: the previous value is restored rather than cleared, so a caller that
    is already guarded is not un-guarded by an inner block finishing.
    """

    previous = getattr(_GUARD, "cheap_only", False)
    _GUARD.cheap_only = True
    try:
        yield
    finally:
        _GUARD.cheap_only = previous


# ---------------------------------------------------------------------------
# What a backend answers with
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendStat:
    """Everything ``poll()`` needs about a source, from one cheap call.

    Carries :attr:`identity` so tailing costs **one** call rather than a
    separate size lookup and identity lookup. That mattered locally as two
    ``stat`` syscalls; it matters over a network as two round trips.
    """

    size: int
    mtime_ns: int
    #: Opaque and compared only for equality. ``None`` when the backend has no
    #: stable answer — see the module docstring.
    identity: object | None = None


@dataclass(frozen=True, slots=True)
class WalkEntry:
    """One file found beneath a root, measured as it was found.

    :attr:`size` and :attr:`identity` come back with the entry rather than being
    asked for afterwards. Locally that is one ``DirEntry.stat()`` instead of an
    ``is_file()`` plus a ``stat()``; remotely it is the difference between one
    ``find`` and one round trip per file, which is Requirement 4.
    """

    ref: SourceRef
    size: int
    identity: object | None = None
    #: The entry exists by name but could not be measured — a file whose
    #: directory became unreadable between listing and stat, most often. Yielded
    #: rather than dropped, because discovery counts it: a file that vanishes
    #: into neither the tree nor the skip tally is the thing Requirement 2 of
    #: ``AGENTS.md`` forbids.
    unreadable: bool = False


@dataclass(frozen=True, slots=True)
class ClassifyRequest:
    """One file discovery wants a verdict on, and how much of it that needs.

    *head_bytes* is set by the caller rather than by the backend because the
    **rule** lives with the caller: ``reader.looks_binary`` wants a sniff block,
    ``compressed.probe`` wants enough of an archive to parse its header, and a
    backend that decided between them would have to import both — which is the
    dependency cycle this parameter exists to avoid.
    """

    ref: SourceRef
    head_bytes: int


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    """What a batch verdict carries back. Measurement only, never judgement.

    :attr:`head` is the file's leading bytes, so the decision about what they
    *mean* stays in ``reader.looks_binary`` and ``compressed.probe`` and is made
    identically whichever backend supplied them. That split is the whole reason
    this member exists: a remote backend that answered "binary: yes" would have
    reimplemented the UTF-16 rule in ``sh``, and got it wrong.
    """

    #: ``os.R_OK``, as :meth:`SourceBackend.access` would answer it.
    readable: bool
    #: Empty when unreadable, or when *head_bytes* was zero.
    head: bytes = b""
    #: True when :attr:`head` is the **entire** file. The difference matters to
    #: ``compressed.probe``: a decompressor running out of input part way
    #: through a large member means the member is fine and the sample was
    #: short, while the same error on a complete file means the file is
    #: truncated. Without this the two are indistinguishable.
    complete: bool = False


#: What a backend's connection to its source is doing. ``connected`` is not a
#: claim that the last read worked — it is a claim that nothing has said
#: otherwise, which is the honest thing a cheap answer can mean.
ReachabilityState = Literal["connected", "connecting", "unreachable", "disabled"]


#: How long to wait before each successive reconnection attempt, in seconds.
#:
#: Bounded and finite. A host that has been gone for an hour is not coming back
#: because CLV asked a sixtieth time, and an open-ended retry loop is a stream of
#: ``ssh`` processes against a machine the operator may have deliberately taken
#: down. When the schedule is spent the pane says so and names ``Ctrl+R``, which
#: puts the decision where it belongs.
RECONNECT_BACKOFF: tuple[int, ...] = (1, 2, 5, 15, 30, 60)

#: Automatic attempts before CLV stops trying and says it has.
RECONNECT_ATTEMPTS = len(RECONNECT_BACKOFF)


def backoff_for(attempt: int) -> int:
    """Seconds to wait before *attempt* (1-based). Clamped to the last step."""

    index = min(max(attempt, 1), len(RECONNECT_BACKOFF)) - 1
    return RECONNECT_BACKOFF[index]


@dataclass(frozen=True, slots=True)
class Reachability:
    """Whether a backend can currently reach its source, and why not.

    Reported by a **cheap** member, so this is always a record of what was last
    observed rather than the result of asking now. That is the only shape an
    answer can take when the caller is the event loop.

    :attr:`reason` is written for the operator and already names the host: it is
    what the pane shows in place of an empty-source message and what the
    discovery summary prints beside an unreadable root. Requirement 7 — an
    unreachable host, a dropped connection and a host with no matching files are
    three different facts and must read as three different messages.
    """

    state: ReachabilityState = "connected"
    #: Actionable, and already naming the host. Empty when connected.
    reason: str = ""
    #: Consecutive failures. Drives which :data:`RECONNECT_BACKOFF` step applies.
    attempts: int = 0
    #: ``time.monotonic()`` deadline before which a retry is pointless. Zero
    #: means "now", which is also what a fresh connection reports.
    retry_at: float = 0.0
    #: The backoff schedule is spent. Nothing further is attempted automatically
    #: — only an explicit reload — and the pane says so rather than looking as
    #: though it were still trying.
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "connected"

    def may_attempt(self, now: float) -> bool:
        """Whether a reconnection is worth making at *now*.

        False while the backoff deadline stands, once the schedule is spent, and
        for a host the operator switched off — three different reasons not to
        spawn ``ssh``, all of which end in not spawning it.
        """

        if self.state == "disabled" or self.exhausted:
            return False
        return now >= self.retry_at


#: The answer for a backend with nothing between it and its files. A constant
#: rather than a fresh instance: ``LocalBackend`` reports it on every render.
CONNECTED = Reachability()


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """What this backend can do, and what it costs to ask."""

    name: str
    #: Method names that may block. Derived from the marks by
    #: :func:`blocking_methods`, never written by hand.
    blocking: frozenset[str]
    #: False when :meth:`SourceBackend.identity` cannot answer stably, so
    #: ``SourceReader`` must fall back to a shrink-only rotation test.
    stable_identity: bool
    #: What to tell an operator whose read was refused. A backend-level answer
    #: because the local one — "re-launch with sudo" — is not merely unhelpful
    #: for a file on another machine, it recommends something CLV refuses to do.
    access_hint: str


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


class SourceBackend(Protocol):
    """The IO surface CLV requires of a source, with its costs declared.

    An implementation marks every method below with :func:`cheap` or
    :func:`blocking`; :func:`blocking_methods` refuses one that does not. The
    marks on *this* class say what a **caller** must assume — the members in
    :data:`MAY_BLOCK` may be round trips and belong in a worker — while the
    marks on an implementation say what it actually does. ``LocalBackend`` marks
    everything cheap, which is exactly why none of this was needed before.
    """

    @property
    def capabilities(self) -> BackendCapabilities: ...

    # --- guaranteed cheap: safe from poll() on any backend -------------------

    @cheap
    def stat(self, ref: SourceRef) -> BackendStat | None:
        """Size, mtime and identity, or ``None`` if it could not be read."""

    @cheap
    def identity(self, ref: SourceRef) -> object | None:
        """An opaque comparable, or ``None`` when the backend has no stable one."""

    @cheap
    def reachability(self) -> Reachability:
        """Whether this backend can currently reach its source. **Never probes.**

        Cheap because the status line asks on every render and the log pane asks
        on every empty result. It reports what was last observed; a backend that
        connected here would be the frozen UI in a new place.
        """

    # --- may block: drive these from a worker --------------------------------

    @blocking
    def walk(
        self,
        root: SourceRef,
        *,
        follow_symlinks: bool = False,
        seen: set[object] | None = None,
    ) -> Iterator[WalkEntry]:
        """Every file beneath *root*, lazily, files only.

        *seen* is a caller-owned set of directory identities, so several walks
        can share cycle state — two configured roots that overlap must not walk
        the shared subtree twice. Only consulted when *follow_symlinks* is true,
        which is the only way a cycle can arise.

        Laziness is part of the contract: a consumer that stops at
        ``max_files`` must not have paid for the rest of the tree.
        """

    @blocking
    def list_dir(self, ref: SourceRef) -> Iterator[SourceRef]:
        """One directory level, no recursion. **Raises** ``OSError``.

        Unlike :meth:`walk`, which skips what it cannot list. This exists for
        ``sources.check_access``, whose entire job is to report that error to
        the operator rather than to survive it.
        """

    @blocking
    def kind(self, ref: SourceRef) -> RefKind:
        """What *ref* is, in one call.

        One call rather than an ``exists``/``is_file``/``is_dir`` triple,
        because remotely that triple is three round trips to answer one
        question.
        """

    @blocking
    def access(self, ref: SourceRef, mode: int) -> bool:
        """``os.access`` semantics: can this be read, listed, traversed?"""

    @blocking
    def open(self, ref: SourceRef, mode: str = "rb") -> IO[bytes]:
        """A **seekable** binary handle. Raises ``OSError``."""

    @blocking
    def classify(
        self, requests: Sequence[ClassifyRequest]
    ) -> dict[SourceRef, ClassifyResult]:
        """Readability and leading bytes for a **batch** of candidates.

        The one member that takes a list, and the reason is Requirement 4 of
        ``SSH_TODO.md``: discovery has to decide "readable? binary? a valid
        archive?" about every file it found, and asking that one file at a time
        is a round trip per file — the specific failure that makes a remote
        ``/var/log`` unusable at 400 files, and the same thing that makes an
        ``sshfs`` mount slow.

        Locally this is exactly what ``skip_reason`` used to do inline and costs
        the same; remotely it is one command over the whole batch. A backend may
        split a batch it considers too large, but the count must stay
        proportional to the *batch* rather than to the files in it.

        A ref that could not be measured at all is absent from the result rather
        than present-and-empty; the caller reports that as unreadable, which is
        the same conclusion ``access`` returning False reached before.
        """


#: Every method an implementation must mark. Data rather than introspection so
#: the check does not depend on ``typing`` internals, which moved between 3.11
#: and 3.14 — the same reason ``refs.PROTOCOL_MEMBERS`` exists.
PROTOCOL_METHODS: tuple[str, ...] = (
    "access",
    "classify",
    "identity",
    "kind",
    "list_dir",
    "open",
    "reachability",
    "stat",
    "walk",
)

#: Callable from ``poll()`` on **any** backend. A backend that cannot honour
#: this is not a backend; it is a reason to change the reader.
GUARANTEED_CHEAP: frozenset[str] = frozenset({"identity", "reachability", "stat"})

#: The rest — a caller must assume a round trip and use a worker.
MAY_BLOCK: frozenset[str] = frozenset(PROTOCOL_METHODS) - GUARANTEED_CHEAP


def blocking_methods(cls: type) -> frozenset[str]:
    """The declared-blocking members of *cls*, refusing anything undeclared.

    The point of deriving this rather than declaring it twice: a method that
    grows a round trip and gets re-marked updates its capabilities in the same
    edit, and a method that is added without a mark fails loudly at import.
    """

    names: set[str] = set()
    for name in PROTOCOL_METHODS:
        member = getattr(cls, name, None)
        if member is None:
            raise TypeError(f"{cls.__name__} does not implement {name}()")
        cost = getattr(member, _COST_ATTR, None)
        if cost is None:
            raise TypeError(
                f"{cls.__name__}.{name}() is marked neither @cheap nor @blocking. "
                "A backend declares its costs; an unmarked method is a caller "
                "guessing."
            )
        if cost == "blocking":
            if name in GUARANTEED_CHEAP:
                raise TypeError(
                    f"{cls.__name__}.{name}() is declared blocking, but {name}() "
                    "is called from poll() on the event loop and must be cheap "
                    "on every backend."
                )
            names.add(name)
    return frozenset(names)


class BackendResolver(Protocol):
    """Picks the backend for a ref.

    Injected rather than looked up in module state. ``refs.KNOWN_SCHEMES`` is
    static and closed for a reason — a registry plugins populated at import time
    would behave differently depending on load order — and a mutable backend
    registry would reintroduce exactly that, with a live connection in it.

    A resolver is what ``discover`` takes, because in Phase 4 it is handed a
    mixed list of local and remote roots and one backend cannot answer for all
    of them.
    """

    def for_ref(self, ref: SourceRef) -> SourceBackend: ...


# ---------------------------------------------------------------------------
# The local backend
# ---------------------------------------------------------------------------


LOCAL_ACCESS_HINT = (
    "Re-launch Centralized Log Viewer with elevated permissions (for example `sudo`) "
    "to include this source."
)


class LocalBackend:
    """This machine's filesystem — today's behaviour, moved behind the seam.

    Every method is marked :func:`cheap`, which is a claim about *this* backend
    rather than about the protocol: a local ``stat`` or bounded read is
    microseconds, and that is precisely why the event loop has been allowed to
    do them all along.

    Also its own :class:`BackendResolver`, so ``LOCAL`` can be passed wherever
    either is wanted and the local default costs no extra object.

    Requires **more** than :class:`SourceRef` declares, and says so rather than
    letting it be discovered: the ``os`` functions below need ``__fspath__``,
    which the protocol deliberately omits because a remote ref cannot honestly
    keep that promise (see ``refs.SourceRef``). Every ref that reaches here is a
    ``pathlib.Path`` — that is what ``refs.is_local`` decides and what a
    resolver routes on.
    """

    def for_ref(self, ref: SourceRef) -> "LocalBackend":
        return self

    @property
    def capabilities(self) -> BackendCapabilities:
        return LOCAL_CAPABILITIES

    # --- cheap ---------------------------------------------------------------

    @cheap
    def stat(self, ref: SourceRef) -> BackendStat | None:
        try:
            info = ref.stat()
        except OSError:
            return None
        return BackendStat(
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            identity=(info.st_dev, info.st_ino),
        )

    @cheap
    def identity(self, ref: SourceRef) -> object | None:
        info = self.stat(ref)
        return None if info is None else info.identity

    @cheap
    def reachability(self) -> Reachability:
        """Always connected. There is nothing between this backend and its files.

        A local file that is gone is *missing*, which ``kind`` already reports
        and which is a different fact from a source CLV cannot get to. Conflating
        them is precisely what this member exists to stop.
        """

        return CONNECTED

    # --- also cheap here, though the protocol lets them block ----------------

    @cheap
    def walk(
        self,
        root: SourceRef,
        *,
        follow_symlinks: bool = False,
        seen: set[object] | None = None,
    ) -> Iterator[WalkEntry]:
        """Pre-order depth-first, directories and files each sorted by name.

        Built on ``os.scandir`` rather than ``os.walk`` for one reason worth
        stating: a ``DirEntry`` answers ``is_file()`` from the directory read
        itself and caches its ``stat()``, so an entry costs **one** syscall
        where ``os.walk`` plus ``Path.is_file()`` plus ``Path.stat()`` cost two.
        The traversal order is reproduced exactly, because ``max_files``
        truncation is defined by it.

        An explicit stack rather than recursion: a deep tree must not be a
        ``RecursionError``, and a chain of ``yield from`` frames would make
        every entry cost the depth it was found at.
        """

        stack: list[Path] = [root if isinstance(root, Path) else Path(os.fspath(root))]
        while stack:
            current = stack.pop()

            if follow_symlinks and seen is not None:
                # Cycle guard. Only reachable when symlinks are followed, which
                # is the only way a directory can contain itself.
                try:
                    info = current.stat()
                except OSError:
                    continue
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    continue
                seen.add(key)

            try:
                with os.scandir(current) as scan:
                    entries = list(scan)
            except OSError:
                # A directory that will not list is skipped silently, exactly as
                # `os.walk(onerror=...)` did. The *root* not listing is a
                # different fact and is reported by the caller.
                continue

            children: list[Path] = []
            files: list[os.DirEntry] = []
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                    # Symlinked directories are classified as directories even
                    # when they are not descended into -- `os.walk` does the
                    # same, which is what keeps them out of the file list.
                    descend = is_dir and (follow_symlinks or not entry.is_symlink())
                except OSError:
                    # Either probe can fail on a racing unlink. Treated as a
                    # file so the loop below reports it once, rather than
                    # silently as neither.
                    is_dir = descend = False
                if is_dir:
                    if descend:
                        children.append(Path(entry.path))
                    continue
                files.append(entry)

            files.sort(key=lambda item: item.name.lower())
            for entry in files:
                ref = Path(entry.path)
                try:
                    if not entry.is_file():
                        continue
                    info = entry.stat()
                except OSError:
                    yield WalkEntry(ref=ref, size=0, unreadable=True)
                    continue
                yield WalkEntry(
                    ref=ref,
                    size=info.st_size,
                    identity=(info.st_dev, info.st_ino),
                )

            # Reversed, because the stack pops from the end: this is what makes
            # the descent alphabetical rather than backwards.
            children.sort(key=lambda item: item.name.lower(), reverse=True)
            stack.extend(children)

    @cheap
    def list_dir(self, ref: SourceRef) -> Iterator[SourceRef]:
        with os.scandir(os.fspath(ref)) as scan:
            for entry in scan:
                yield Path(entry.path)

    @cheap
    def kind(self, ref: SourceRef) -> RefKind:
        try:
            if ref.is_dir():
                return "dir"
            if ref.is_file():
                return "file"
            return "other" if ref.exists() else "missing"
        except PermissionError:
            return "denied"
        except OSError:
            return "other"

    @cheap
    def access(self, ref: SourceRef, mode: int) -> bool:
        # `ref` is passed straight through rather than via `os.fspath`: this
        # runs once per candidate during discovery, and letting the C layer do
        # the conversion saves a Python-level call on every file in the tree.
        return os.access(ref, mode)

    @cheap
    def open(self, ref: SourceRef, mode: str = "rb") -> IO[bytes]:
        return ref.open(mode)

    @cheap
    def classify(
        self, requests: Sequence[ClassifyRequest]
    ) -> dict[SourceRef, ClassifyResult]:
        """One open and one bounded read per file — what discovery already did.

        The batch is a *remote* optimisation and there is nothing here to batch:
        a local open is a syscall, so looping is both the simplest
        implementation and the fastest one. What matters is that the loop is
        here rather than in ``discovery``, so the two backends are asked the
        same question.

        One read of ``head_bytes + 1``: the extra byte is what distinguishes "we
        sampled the start of a longer file" from "that was all of it", which is
        :attr:`ClassifyResult.complete` and which ``compressed.probe`` needs.
        """

        results: dict[SourceRef, ClassifyResult] = {}
        for request in requests:
            if not os.access(request.ref, os.R_OK):
                results[request.ref] = ClassifyResult(readable=False)
                continue
            if request.head_bytes <= 0:
                # Readability was the whole question -- a document, or
                # `skip_binary` turned off. Opening the file to read nothing is
                # what this member exists to avoid doing per file.
                results[request.ref] = ClassifyResult(readable=True)
                continue
            try:
                with request.ref.open("rb") as handle:
                    head = handle.read(request.head_bytes + 1)
            except OSError:
                results[request.ref] = ClassifyResult(readable=False)
                continue
            complete = len(head) <= request.head_bytes
            results[request.ref] = ClassifyResult(
                readable=True,
                head=head[: request.head_bytes],
                complete=complete,
            )
        return results


LOCAL_CAPABILITIES = BackendCapabilities(
    name="local",
    blocking=blocking_methods(LocalBackend),
    stable_identity=True,
    access_hint=LOCAL_ACCESS_HINT,
)

#: The default everywhere a backend or a resolver is optional. One instance:
#: it holds no state, and a per-call one would be a small allocation on a path
#: that runs per file.
LOCAL = LocalBackend()
