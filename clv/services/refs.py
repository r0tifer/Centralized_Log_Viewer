"""What CLV requires of a log source, and how one survives a restart.

A source is a **SourceRef**. ``pathlib.Path`` is one implementation and
:class:`RemoteRef` is the other — neither is the assumed one. The reason a
plugin alone could not deliver remote sources is that eight years of code said
*a source is a path*.

**Why the remote ref type lives here and not in the SSH plugin.** ``parse_ref``
decodes ``session.json`` before any plugin is imported, which is the same reason
:data:`KNOWN_SCHEMES` is static and closed: a type registered at plugin-import
time would decode the same file differently depending on load order, and a
starred ``ssh:`` ref would be a remote source on one launch and a relative path
on the next. :class:`RemoteRef` is pure identity — no IO, no connection, no
subprocess — so nothing about it needs the transport that
``clv/plugins/sources/ssh.py`` owns.

This module answers *what a source is*. :mod:`clv.services.backend` answers
*who reads it*, and owns every filesystem call that used to be made against a
ref directly.

Two boundaries live here, and keeping them apart is the whole point.

``parse_ref`` / ``format_ref`` are the **persistence boundary**. They are exact
inverses on every string ``format_ref`` can produce, and they never touch the
filesystem, expand ``~``, or consult the working directory. What was written to
``session.json`` is what comes back out of it.

``normalize_ref`` is the **user-input boundary** — ``log_dirs`` in
``settings.conf``, the add-source dialog. It expands and absolutises what a
person typed, which is a convenience when typing and a corruption when applied
to something already stored: it is exactly what turns ``journal:all`` into
``/current/dir/journal:all``.

The twelve ``Path(entry)`` sites over ``state.starred``, ``state.merged``,
``SavedView.source`` and ``ORIGIN_FIELD`` were reconstruction, not input. They
go through ``parse_ref``, and a guard test in ``tests/test_refs.py`` keeps them
there — because the seam is one function call wide and can rot back in one line.

**Why the string form is load-bearing.** ``str(ref)`` is three things at once:
what lands in ``session.json``, the value of ``ORIGIN_FIELD`` on every entry of
a merged set (so it is what the operator types after ``source:`` in a query),
and half of a ``marks.mark_key``. Two hosts with the same path must therefore
produce different strings, or ``source:/var/log/syslog`` matches every machine
at once. It also may contain neither ``,`` (``log_dirs`` is comma-separated,
``config.parse_log_dirs``) nor NUL (``mark_key`` separates on it).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, NoReturn, Protocol

__all__ = [
    "KNOWN_SCHEMES",
    "PROTOCOL_MEMBERS",
    "SOURCE_REF_TYPES",
    "SSH_SCHEME",
    "RemoteRef",
    "RemoteRefIOError",
    "SourceRef",
    "format_ref",
    "identity",
    "is_local",
    "is_source_ref",
    "normalize_ref",
    "parse_ref",
    "ref_key",
    "scheme_of",
    "split_scheme",
]


class SourceRef(Protocol):
    """Everything CLV asks of a log source, and nothing it does not.

    Derived from every source-facing call in ``clv/``, counted rather than
    guessed. Members CLV never uses *of a source* are absent even where ``Path``
    offers them — ``stem``, ``glob``, ``iterdir``, ``samefile``, ``joinpath`` —
    because a protocol that lists them is a promise a remote implementation
    would have to keep for no caller.

    Four members are absent for a stronger reason than disuse.

    ``resolve``, ``expanduser`` and ``is_absolute`` are the operations that
    destroy a scheme ref, and every source-facing caller of them is now inside
    this module (:func:`identity`, :func:`normalize_ref`). Putting them on the
    protocol would re-open the seam this module closes.

    ``__fspath__`` is absent too, and deliberately: nothing in ``clv/`` calls
    ``os.fspath`` or annotates ``PathLike``, and declaring it would promise that
    every ref is usable with ``open()`` and ``os.scandir``. A remote ref cannot
    honestly keep that promise, and the failure would surface as a
    ``FileNotFoundError`` from deep inside the standard library rather than as a
    clean protocol error.

    There is no ``__lt__``, and that is a rule rather than an omission:
    **ordering is by ``str(ref)``, never native.** Every source sort in ``clv/``
    already passes an explicit ``key=``, and the two that do not are sorting the
    stored strings. Declaring an ordering would let a mixed local/remote list be
    sorted natively, which is where a ``TypeError`` would first appear.

    This project runs no type checker, so nothing mechanically enforces the
    protocol. :data:`PROTOCOL_MEMBERS` exists so a test can.
    """

    # --- naming and structure ------------------------------------------------
    @property
    def name(self) -> str: ...

    @property
    def parent(self) -> "SourceRef": ...

    @property
    def parts(self) -> tuple[str, ...]: ...

    @property
    def suffix(self) -> str: ...

    def with_name(self, name: str) -> "SourceRef": ...

    def with_suffix(self, suffix: str) -> "SourceRef": ...

    def relative_to(self, other: Any) -> "SourceRef": ...

    def __truediv__(self, other: str) -> "SourceRef": ...

    # --- identity -------------------------------------------------------------
    #: The stored form, the ``ORIGIN_FIELD`` value, and half of a mark key.
    def __str__(self) -> str: ...

    #: Membership is set- and dict-based throughout ``app.py`` — the starred
    #: set, the merged set, the tree's node index — so a ref must hash and
    #: compare by identity rather than by object address.
    def __hash__(self) -> int: ...

    def __eq__(self, other: object) -> bool: ...

    # --- IO -------------------------------------------------------------------
    # Phase 2 moved these behind :mod:`clv.services.backend`. They stay listed
    # because ``LocalBackend`` is implemented *in terms of them* — it calls
    # ``ref.stat()``, ``ref.open()``, ``ref.is_dir()`` — so they remain part of
    # what a **local** ref must provide, and removing them would make that
    # dependency undeclared rather than absent.
    #
    # What changed is who may call them: nothing outside ``backend.py`` does,
    # and a guard test in ``tests/test_backend.py`` keeps it that way. A remote
    # ref is not expected to implement them at all; its backend answers instead,
    # which is the whole point of the seam.
    def exists(self) -> bool: ...

    def is_file(self) -> bool: ...

    def is_dir(self) -> bool: ...

    def stat(self) -> os.stat_result: ...

    def open(self, mode: str = "r", **kwargs: Any) -> IO[Any]: ...


#: The protocol's members, as data, so a test can assert an implementation
#: covers them without depending on ``typing`` internals that moved between
#: 3.11 and 3.14.
PROTOCOL_MEMBERS: tuple[str, ...] = (
    "__eq__",
    "__hash__",
    "__str__",
    "__truediv__",
    "exists",
    "is_dir",
    "is_file",
    "name",
    "open",
    "parent",
    "parts",
    "relative_to",
    "stat",
    "suffix",
    "with_name",
    "with_suffix",
)


#: Ref schemes whose string form is an identifier, not a filesystem location.
#:
#: Static and closed on purpose. ``session.json`` is read before any plugin is
#: imported, so a registry that plugins populated at import time would decode
#: the same file differently depending on load order — a starred ``journal:``
#: ref would be a scheme on one launch and a relative path on the next.
#:
#: ``ssh`` is listed before it has a backend, which is not aspiration: it means
#: an ``ssh:`` ref hand-written into ``settings.conf`` today is reported under
#: its own name instead of being turned into ``$CWD/ssh:web01/var/log``. A
#: missing source is a legible failure; an invented local path is not.
KNOWN_SCHEMES: frozenset[str] = frozenset({"journal", "ssh"})

#: A scheme is a lowercase token, one colon, and something that is not a slash.
#:
#: ``ssh://web01/...`` is deliberately **not** matched. ``Path`` collapses the
#: double slash to ``ssh:/web01/...``, so that shape cannot round trip and must
#: never be accepted — CLV should not take in a string it would then corrupt.
#: The journald scheme (``journal:unit/sshd.service``) is the shape that can.
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*):(?!/)")


def scheme_of(ref: "SourceRef | str") -> str | None:
    """The registered scheme *ref* carries, or ``None`` if it names a file.

    The closed :data:`KNOWN_SCHEMES` check is what makes this safe rather than
    merely likely-safe: without it a real relative directory named ``backup:``
    or ``logs:2026`` would be mistaken for an identifier and never absolutised.

    What is left ambiguous, stated rather than hidden: a **relative** local path
    whose first component begins ``journal:`` or ``ssh:`` reads as a scheme.
    ``./journal:all`` does not disambiguate it — ``Path`` normalises the ``./``
    away before this ever sees it — and the escape is to name it absolutely,
    which every path CLV stores already is.

    Such a path is not unreachable: ``Path`` operations on it still resolve
    against the working directory, so it opens and reads. What it loses is
    **absolutisation**. It is the one entry :func:`normalize_ref` hands back
    unpinned, so unlike every other relative one it is re-interpreted against
    whatever directory CLV is launched from — it works, until someone starts the
    viewer somewhere else.

    **That refusal now exists, and it is not here.** ``config.parse_log_dirs``
    refuses such an entry and says to name it absolutely, because refusing is a
    *reporting* decision and this module has nowhere to report to: it is called
    from the persistence path as well as the input one, and a function that
    raised or dropped would break the round trip. The validation channel is
    ``config.ConfigIssue``, and the config layer is where it is spent.
    """

    match = _SCHEME_RE.match(ref if isinstance(ref, str) else str(ref))
    if match is None:
        return None
    scheme = match.group(1)
    return scheme if scheme in KNOWN_SCHEMES else None


def split_scheme(ref: "SourceRef | str") -> tuple[str | None, str]:
    """``("journal", "unit/sshd.service")`` — the scheme, and what follows it."""

    text = ref if isinstance(ref, str) else str(ref)
    scheme = scheme_of(text)
    return (None, text) if scheme is None else (scheme, text[len(scheme) + 1 :])


def is_local(ref: "SourceRef") -> bool:
    """Whether the local filesystem can answer for *ref*.

    An absolute path short-circuits before the regex, which is the
    overwhelmingly common case and keeps :func:`identity` cheap on every tree
    row. Anything that is not a ``Path`` at all is not local by definition —
    that is the branch a remote ref will take.
    """

    return isinstance(ref, Path) and (ref.is_absolute() or scheme_of(ref) is None)


# ---------------------------------------------------------------------------
# The remote implementation
# ---------------------------------------------------------------------------


#: The scheme prefix, spelled once. Single-colon, like ``journal:`` — see
#: :data:`_SCHEME_RE` for why ``ssh://`` is refused rather than supported.
SSH_SCHEME = "ssh:"


class RemoteRefIOError(TypeError):
    """A remote ref was asked to perform IO of its own.

    A ``TypeError`` rather than an ``OSError`` on purpose: this is not a read
    that failed, it is a call that should never have been made, and the
    ``except OSError`` guards scattered through discovery and the session would
    otherwise swallow it into a plausible-looking "source unavailable".
    """


@dataclass(frozen=True, slots=True)
class RemoteRef:
    """A log source on another machine: ``ssh:web01/var/log/syslog``.

    Identity only. Holding one has connected to nothing and spawned nothing —
    every byte comes from the ``RemoteBackend`` a resolver hands back, which is
    the same split :class:`SourceRef` and :mod:`clv.services.backend` already
    make for local sources.

    **Not a ``pathlib.Path`` subclass**, deliberately. ``pyproject.toml``
    requires ``>=3.11`` and practical ``Path`` subclassing only arrived in 3.12,
    so this implements the surface CLV actually uses instead of inheriting one
    it would have to keep fighting.

    **No ``__fspath__``.** Declaring it would promise that ``open()`` and
    ``os.scandir`` work on one of these, and the failure would surface as a
    ``FileNotFoundError`` from inside the standard library rather than as the
    plain error the IO members below raise. The protocol omits it for the same
    reason.

    Two hosts with the same path are two identities, which is a correctness
    requirement rather than tidiness: ``str(ref)`` is what lands in
    ``ORIGIN_FIELD`` on every entry of a merged set, so an unqualified
    ``/var/log/syslog`` would make ``source:/var/log/syslog`` match every
    machine at once.
    """

    #: CLV's name for the machine — the ``[ssh:<name>]`` section suffix, not
    #: necessarily the address. ``RemoteHost.host`` is what is connected to.
    node: str
    #: Always absolute, always POSIX. A remote path is not this machine's.
    path: PurePosixPath

    # --- construction --------------------------------------------------------

    @classmethod
    def build(cls, node: str, path: "str | PurePosixPath") -> "RemoteRef":
        """A ref for *path* on *node*, normalised the way ``Path`` would.

        The single construction site. ``//`` collapses and a ``.`` component
        drops, so one pass reaches a fixed point and the round trip through
        :func:`format_ref` is exact — the same guarantee, and the same
        exceptions, that ``Path`` gives locally.
        """

        posix = PurePosixPath(path)
        if not posix.is_absolute():
            posix = PurePosixPath("/") / posix
        return cls(node=node, path=posix)

    @classmethod
    def parse(cls, text: str) -> "RemoteRef | None":
        """``"ssh:web01/var/log"`` → a ref, or ``None`` if it is not one.

        ``None`` rather than an exception because the caller is
        :func:`parse_ref`, which sits on the persistence path and has nowhere to
        report to: a malformed entry degrades to a visibly-wrong scheme ref
        instead of taking a session restore down with it.
        """

        if not text.startswith(SSH_SCHEME):
            return None
        node, separator, remainder = text[len(SSH_SCHEME) :].partition("/")
        if not node or not separator:
            # `ssh:` alone, or `ssh:web01` with no path. There is no sensible
            # default here -- "the whole filesystem" is not what anyone meant.
            return None
        if ".." in PurePosixPath(f"/{remainder}").parts:
            # A stored ref that walks upwards cannot round trip and could name
            # a place outside the root it was discovered under.
            return None
        return cls.build(node, f"/{remainder}")

    # --- identity ------------------------------------------------------------

    def __str__(self) -> str:
        return f"{SSH_SCHEME}{self.node}{self.path}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RemoteRef({str(self)!r})"

    # --- naming and structure ------------------------------------------------

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parent(self) -> "RemoteRef":
        return RemoteRef(node=self.node, path=self.path.parent)

    @property
    def parts(self) -> tuple[str, ...]:
        """As ``Path`` reports them, leading ``"/"`` included.

        The host is deliberately absent: these are the path's components, and a
        caller that wants the machine asks for :attr:`node`.
        """

        return self.path.parts

    @property
    def suffix(self) -> str:
        return self.path.suffix

    def with_name(self, name: str) -> "RemoteRef":
        return RemoteRef(node=self.node, path=self.path.with_name(name))

    def with_suffix(self, suffix: str) -> "RemoteRef":
        return RemoteRef(node=self.node, path=self.path.with_suffix(suffix))

    def relative_to(self, other: Any) -> PurePosixPath:
        """The path below *other*, as a **relative** path rather than a ref.

        A relative path has no machine, so wrapping one back into a
        :class:`RemoteRef` would produce ``ssh:web01nested/b.log`` — a string
        that is not a ref and does not round trip. ``Path.relative_to`` returns
        a relative ``Path`` for exactly the same reason, and both callers want
        the components rather than the identity: ``discovery.matched_glob``
        takes ``str()`` of it, and ``app._by_folder`` takes ``.parts``.

        Raises ``ValueError``, as ``Path`` does, when *other* is not a prefix —
        including when it names a different machine.
        """

        if isinstance(other, RemoteRef):
            if other.node != self.node:
                raise ValueError(f"{self!s} is not on the same node as {other!s}")
            return self.path.relative_to(other.path)
        return self.path.relative_to(PurePosixPath(str(other)))

    def __truediv__(self, other: str) -> "RemoteRef":
        return RemoteRef(node=self.node, path=self.path / other)

    # --- IO: never here ------------------------------------------------------
    #
    # Present because they are on :data:`PROTOCOL_MEMBERS` and absent-and-
    # raising is louder than absent. A caller reaching one of these is a seam
    # that rotted: the answer lives on the backend, which is what a
    # ``BackendResolver`` exists to hand back.

    def _no_io(self, operation: str) -> NoReturn:
        raise RemoteRefIOError(
            f"{operation}() is not answerable by a remote ref ({self!s}); "
            "ask its backend instead."
        )

    def exists(self) -> bool:
        self._no_io("exists")

    def is_file(self) -> bool:
        self._no_io("is_file")

    def is_dir(self) -> bool:
        self._no_io("is_dir")

    def stat(self) -> os.stat_result:
        self._no_io("stat")

    def open(self, mode: str = "r", **kwargs: Any) -> IO[Any]:
        self._no_io("open")


#: Every concrete :class:`SourceRef`. One tuple, so the union has a single
#: definition rather than one per call site.
SOURCE_REF_TYPES: tuple[type, ...] = (Path, RemoteRef)


def is_source_ref(value: object) -> bool:
    """Whether *value* is a log source rather than something else in the tree.

    The tree is typed on ``object`` and carries four different things:
    ``SourceRef``s, ``SavedView`` records, ``ProviderSource`` records, and the
    merged-view sentinel. Everything that walks it looking for a *source* used
    to spell that ``isinstance(data, Path)``, which was correct for exactly as
    long as ``Path`` was the only implementation.

    **A provider source is still excluded, and by the same mechanism.** A
    journal node carries a ``ProviderSource``, not a ref — it is a different
    kind of thing, with no directory to walk and no file to persist — so the
    union being over *ref types* rather than over "anything source-shaped" is
    what keeps ``journal:unit/sshd.service`` out of the starred set. That was
    the point of the original narrowing and it survives this widening intact.
    """

    return isinstance(value, SOURCE_REF_TYPES)


def parse_ref(text: str) -> SourceRef:
    """The stored form of a source, back as a source.

    The inverse of :func:`format_ref`. Nothing here expands ``~``, prepends the
    working directory or resolves — so a scheme ref cannot be damaged by being
    read back. That is the contract, not an accident of the implementation: see
    :func:`normalize_ref` for where expansion legitimately happens.

    The round trip is exact on every string :func:`format_ref` produces. It is
    *not* exact on arbitrary input, because ``Path`` normalises a few shapes
    (``./x`` to ``x``, ``x//y`` to ``x/y``, ``x/`` to ``x``, ``""`` to ``"."``).
    One pass through ``parse_ref`` reaches a fixed point, which is all that
    persistence needs. :class:`RemoteRef` normalises the same shapes and gives
    the same guarantee.

    ``journal:`` is **not** given a type of its own and still comes back as a
    ``Path``. The journald plugin builds its identifiers as ``Path`` and reads
    them back through :func:`split_scheme`, and there is no backend behind one —
    a provider answers for it instead, which is the distinction
    ``ProviderSource`` exists to draw.

    A malformed ``ssh:`` string falls through to ``Path``. There is nowhere to
    report from here: this function is on the restore path, and one bad entry in
    ``session.json`` must not cost the session. It degrades to a scheme ref that
    resolves to nothing and is reported as missing — which is legible, where an
    invented local path is not.
    """

    if text.startswith(SSH_SCHEME) and scheme_of(text) == "ssh":
        remote = RemoteRef.parse(text)
        if remote is not None:
            return remote
    return Path(text)


def format_ref(ref: SourceRef) -> str:
    """The form of *ref* that goes on disk. Exactly ``str(ref)``.

    **Never resolved.** Resolution is a separate decision made by
    :func:`identity`, and every caller that wants both says so — spelled
    :func:`ref_key`. Folding resolution in here would also change what
    ``session.tag_origins`` writes into ``ORIGIN_FIELD``, which would move every
    ``source:`` query and every mark key with it.
    """

    return str(ref)


def identity(ref: SourceRef) -> SourceRef:
    """The form in which two refs are compared.

    Local: ``resolve()``, so a symlink and its target are one log — the rule
    starring and the merged set have always used. A scheme ref is its own
    identity, because resolving one would invent a working-directory path for
    something that has no location at all.

    Replaces ``app._resolve``, and with :func:`ref_key` also ``sources._marker``
    — two functions that were the same idea with different return types.
    """

    if not is_local(ref):
        return ref
    try:
        return ref.resolve()
    except OSError:
        return ref


def ref_key(ref: SourceRef) -> str:
    """:func:`identity`, in the form that goes in a dict, a set, or on disk."""

    return format_ref(identity(ref))


def normalize_ref(raw: str | SourceRef) -> SourceRef:
    """Expand and absolutise something a person typed.

    The body of the old ``sources.normalize_path``, plus one guard: a scheme ref
    is returned untouched. ``expanduser()``, ``Path.cwd() / ...`` and
    ``resolve()`` would each on their own turn ``journal:unit/sshd.service`` into
    a path under the working directory.

    Deliberately does **not** strip whitespace. Both callers already strip (and
    also strip quotes), and a trailing space is a legal component of a filename
    — stripping here would make such a file unreachable while looking like
    tidiness.
    """

    ref = parse_ref(raw) if isinstance(raw, str) else raw
    if not is_local(ref):
        return ref
    path = ref.expanduser()
    try:
        if path.is_absolute():
            return path.resolve(strict=False)
        return (Path.cwd() / path).resolve(strict=False)
    except OSError:
        return path
