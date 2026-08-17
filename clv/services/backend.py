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
from typing import IO, Any, Callable, Iterator, Literal, Protocol

from .refs import SourceRef

__all__ = [
    "GUARANTEED_CHEAP",
    "LOCAL",
    "LOCAL_ACCESS_HINT",
    "MAY_BLOCK",
    "PROTOCOL_METHODS",
    "BackendCapabilities",
    "BackendResolver",
    "BackendStat",
    "BlockingCallError",
    "LocalBackend",
    "RefKind",
    "SourceBackend",
    "WalkEntry",
    "blocking",
    "blocking_methods",
    "cheap",
    "cheap_only",
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


#: Every method an implementation must mark. Data rather than introspection so
#: the check does not depend on ``typing`` internals, which moved between 3.11
#: and 3.14 — the same reason ``refs.PROTOCOL_MEMBERS`` exists.
PROTOCOL_METHODS: tuple[str, ...] = (
    "access",
    "identity",
    "kind",
    "list_dir",
    "open",
    "stat",
    "walk",
)

#: Callable from ``poll()`` on **any** backend. A backend that cannot honour
#: this is not a backend; it is a reason to change the reader.
GUARANTEED_CHEAP: frozenset[str] = frozenset({"identity", "stat"})

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
