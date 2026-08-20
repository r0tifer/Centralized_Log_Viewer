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
from typing import IO, Any, NoReturn, Protocol, Sequence

__all__ = [
    "JOURNAL_SCHEME",
    "KNOWN_SCHEMES",
    "PROTOCOL_MEMBERS",
    "SOURCE_REF_TYPES",
    "SSH_SCHEME",
    "JournalRef",
    "JournalRefIOError",
    "RemoteRef",
    "RemoteRefIOError",
    "SourceRef",
    "format_ref",
    "identity",
    "is_local",
    "is_source_ref",
    "node_of_scheme",
    "normalize_ref",
    "parse_ref",
    "ref_key",
    "scheme_of",
    "split_scheme",
    "stem_of",
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

#: A scheme is a lowercase token, an optional ``@machine``, one colon, and
#: something that is not a slash.
#:
#: ``ssh://web01/...`` is deliberately **not** matched. ``Path`` collapses the
#: double slash to ``ssh:/web01/...``, so that shape cannot round trip and must
#: never be accepted — CLV should not take in a string it would then corrupt.
#: The journald scheme (``journal:unit/sshd.service``) is the shape that can.
#:
#: **The node goes before the colon, and that is load-bearing.** A journal
#: source on another machine has to say which machine, and the obvious place
#: for it — inside the selector — cannot work: templated unit names contain
#: ``@`` (``getty@tty1.service``, ``user@1000.service``), so splitting
#: ``unit/getty@tty1.service`` on its first ``@`` yields a machine called
#: ``unit/getty``. A scheme token can contain neither ``@`` nor ``:``, so
#: ``journal@web01:unit/getty@tty1.service`` splits exactly once, in the only
#: place it could. Every un-suffixed string keeps its existing meaning.
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*)(?:@([a-z0-9][a-z0-9._\-]*))?:(?!/)")


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
    """``("journal", "unit/sshd.service")`` — the scheme, and what follows it.

    The remainder is taken from where the match actually **ended**, not from the
    scheme's own length: ``journal@web01:unit/x`` has a node between the two, and
    slicing by ``len(scheme) + 1`` would hand back ``web01:unit/x``. Callers use
    this to reach the selector, so that arithmetic being wrong is a selector
    silently containing a machine name.
    """

    text = ref if isinstance(ref, str) else str(ref)
    match = _SCHEME_RE.match(text)
    if match is None or match.group(1) not in KNOWN_SCHEMES:
        return None, text
    return match.group(1), text[match.end() :]


def node_of_scheme(ref: "SourceRef | str") -> str:
    """The machine a scheme ref names, or ``""`` for one on this one.

    ``journal@web01:unit/sshd.service`` → ``"web01"``; ``journal:all`` → ``""``.
    Separate from :func:`node_of`, which answers the same question for a ref
    object that already knows; this is for the string form, before parsing.
    """

    match = _SCHEME_RE.match(ref if isinstance(ref, str) else str(ref))
    if match is None or match.group(1) not in KNOWN_SCHEMES:
        return ""
    return match.group(2) or ""


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


# ---------------------------------------------------------------------------
# The journal implementation
# ---------------------------------------------------------------------------


#: The scheme prefix, spelled once. Single-colon, like ``ssh:``.
JOURNAL_SCHEME = "journal:"

#: The selector kinds a journal source can have. Closed, because these are the
#: three things ``journalctl`` is asked for and an unrecognised fourth would be
#: a ref that parses and then cannot be opened.
JOURNAL_KINDS: frozenset[str] = frozenset({"all", "unit", "boot"})

#: What to call the whole-journal source. Not derived from the ref, because
#: ``journal:all``'s last component is ``all``, which names nothing.
SYSTEM_JOURNAL_LABEL = "System journal"


class JournalRefIOError(TypeError):
    """A journal ref was asked to perform IO, or to have a hierarchy.

    A ``TypeError`` for the same reason :class:`RemoteRefIOError` is: this is
    not a read that failed but a call that should never have been made, and the
    ``except OSError`` guards through discovery and the session would otherwise
    swallow it into a plausible-looking "source unavailable".
    """


@dataclass(frozen=True, slots=True)
class JournalRef:
    """The systemd journal, or one unit or boot of it: ``journal:unit/sshd.service``.

    Identity only, like :class:`RemoteRef`. Holding one has spawned nothing;
    every line comes from the journald provider, which is where the ``journalctl``
    subprocess — and therefore the operator's consent — lives.

    **Why this is a ref at all, when it was deliberately not one.**
    ``ProviderSource`` exists to keep a journal source out of starring, glob
    filtering and rotated-set grouping, and it did that by *not being a ref*.
    Two of those three exclusions are still right and are now enforced directly:
    a journal has no directory to walk and nothing to rotate. The third was
    never right, only cheap — a unit is exactly the kind of source someone wants
    starred and merged across a fleet, and "compare this unit on five machines"
    is the same workflow ``ssh:`` refs exist to serve.

    **The node goes before the colon.** ``journal@web01:unit/sshd.service``. See
    :data:`_SCHEME_RE` for why the obvious alternative cannot work: templated
    unit names contain ``@``.

    **A local journal ref's string form is byte-identical to what the provider
    used to build with ``Path``**, which is what lets an existing
    ``session.json`` and every journald assertion survive this unedited.
    """

    #: CLV's name for the machine, or ``""`` for this one — the convention
    #: :func:`node_of` already returns for a local path.
    node: str
    #: One of :data:`JOURNAL_KINDS`.
    kind: str
    #: The unit name or boot offset; empty for ``all``.
    value: str = ""

    # --- construction --------------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "JournalRef | None":
        """``"journal@web01:unit/sshd.service"`` → a ref, or ``None``.

        ``None`` rather than an exception because the caller is
        :func:`parse_ref`, which sits on the persistence path: a malformed entry
        degrades to a visibly-wrong scheme ref instead of taking a session
        restore down with it.
        """

        match = _SCHEME_RE.match(text)
        if match is None or match.group(1) != "journal":
            return None
        selector = text[match.end() :]
        kind, _, value = selector.partition("/")
        if kind not in JOURNAL_KINDS:
            return None
        # `all` takes no value and the other two require one. Enforced so the
        # round trip is exact: `journal:all/x` would otherwise format back as
        # `journal:all` and quietly become a different source.
        if (kind == "all") != (not value):
            return None
        return cls(node=match.group(2) or "", kind=kind, value=value)

    # --- identity ------------------------------------------------------------

    def __str__(self) -> str:
        machine = f"@{self.node}" if self.node else ""
        selector = self.kind if self.kind == "all" else f"{self.kind}/{self.value}"
        return f"journal{machine}:{selector}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JournalRef({str(self)!r})"

    # --- naming --------------------------------------------------------------

    @property
    def name(self) -> str:
        """What to call this in a tree row or a status line.

        The unit name, the boot offset, or :data:`SYSTEM_JOURNAL_LABEL` — never
        the raw selector, because ``all`` is not a name anyone would recognise.
        """

        if self.kind == "all":
            return SYSTEM_JOURNAL_LABEL
        if self.kind == "boot":
            return "This boot" if self.value == "0" else f"Boot {self.value}"
        return self.value

    @property
    def parts(self) -> tuple[str, ...]:
        """The selector's components. The machine is deliberately absent.

        Same rule as :attr:`RemoteRef.parts`: these describe the source within
        its machine, and a caller that wants the machine asks :attr:`node`.
        """

        return (self.kind,) if self.kind == "all" else (self.kind, self.value)

    @property
    def suffix(self) -> str:
        """Always empty.

        ``sshd.service`` ends in something that looks like one, and returning
        ``.service`` would make ``exclude_globs = *.service`` hide a unit — a
        glob written about filenames silently filtering something that is not a
        file. Rotated-set grouping strips suffixes too. Neither should see one.
        """

        return ""

    def with_name(self, name: str) -> "JournalRef":
        """The same kind of source, named differently. Only ``unit`` has a name."""

        if self.kind != "unit":
            self._no_hierarchy("with_name")
        return JournalRef(node=self.node, kind="unit", value=name)

    # --- no hierarchy, and no IO ---------------------------------------------
    #
    # Present and raising rather than absent, the rule ``RemoteRef`` sets: a
    # caller reaching one of these is a seam that rotted, and an ``AttributeError``
    # from deep in a call chain says far less than this does.

    def _no_hierarchy(self, operation: str) -> NoReturn:
        raise JournalRefIOError(
            f"{operation}() has no meaning for a journal source ({self!s}): "
            "the journal is not a filesystem and has no enclosing directory."
        )

    def _no_io(self, operation: str) -> NoReturn:
        raise JournalRefIOError(
            f"{operation}() is not answerable by a journal ref ({self!s}); "
            "the journald provider reads it."
        )

    @property
    def parent(self) -> NoReturn:
        self._no_hierarchy("parent")

    def relative_to(self, other: Any) -> NoReturn:
        self._no_hierarchy("relative_to")

    def with_suffix(self, suffix: str) -> NoReturn:
        self._no_hierarchy("with_suffix")

    def __truediv__(self, other: str) -> NoReturn:
        self._no_hierarchy("__truediv__")

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
SOURCE_REF_TYPES: tuple[type, ...] = (Path, RemoteRef, JournalRef)


def is_source_ref(value: object) -> bool:
    """Whether *value* is a log source rather than something else in the tree.

    The tree is typed on ``object`` and carries four different things:
    ``SourceRef``s, ``SavedView`` records, ``ProviderSource`` records, and the
    merged-view sentinel. Everything that walks it looking for a *source* used
    to spell that ``isinstance(data, Path)``, which was correct for exactly as
    long as ``Path`` was the only implementation.

    **A journal source is no longer excluded, and that is deliberate.** It used
    to be, by this very mechanism: a journal node carried a ``ProviderSource``
    and nothing else, so starring and grouping could not see it. Two thirds of
    that exclusion were right and are now enforced where they belong —
    ``rotation.group_rotated`` and discovery's glob filtering reject a
    :class:`JournalRef` by name, because a journal has no directory to walk and
    nothing to rotate. The third, starring and merging, was never right: a unit
    is the clearest case of a source someone wants starred, and comparing one
    unit across a fleet is the workflow remote sources exist for.

    ``ProviderSource`` still exists and is still not a ref. It is the *tree
    node's* payload, carrying the label and the provider name that error
    attribution needs; its ``path`` is the ref. So a provider that offers
    something which genuinely is not a source identity still cannot reach the
    starred set, which is the part of the original guarantee worth keeping.
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

    ``journal:`` **is** given a type of its own, and used not to be. The reason
    it did not is recorded here rather than deleted, because the reason it now
    does is the same argument turned around: a provider answers for a journal
    source, so there is no backend behind one — but "no backend" was taken to
    mean "not a source identity", and that was the mistake. A unit is exactly
    what an operator wants starred, and comparing one unit across a fleet is the
    workflow the whole remote feature exists for. What a journal ref still is
    not is a *file*, and :class:`JournalRef` enforces that far more directly
    than being a ``Path`` that happens not to exist ever did.

    The string form is unchanged for a local journal, which is what lets a
    ``session.json`` written by an older build come back meaning the same thing.

    A malformed ``ssh:`` or ``journal:`` string falls through to ``Path``. There
    is nowhere to report from here: this function is on the restore path, and one
    bad entry in ``session.json`` must not cost the session. It degrades to a
    scheme ref that resolves to nothing and is reported as missing — which is
    legible, where an invented local path is not.
    """

    if text.startswith(SSH_SCHEME) and scheme_of(text) == "ssh":
        remote = RemoteRef.parse(text)
        if remote is not None:
            return remote
    if scheme_of(text) == "journal":
        journal = JournalRef.parse(text)
        if journal is not None:
            return journal
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


def stem_of(ref: SourceRef) -> str:
    """What to call *ref* in a filename — the machine included when there is one.

    ``syslog`` locally, ``web01-syslog`` for the same path on a remote host.
    Not an identity and never persisted: :func:`format_ref` remains the form
    that goes on disk, and this is only ever read by a human off an export.

    It exists because ``ref.name`` is the bare basename on both types, so an
    export of ``/var/log/syslog`` on ``web01`` was indistinguishable from an
    export of the local file of the same name — and a merged set built from one
    path across two machines named itself ``syslog+syslog``.

    The union is spelled out here rather than in ``export`` for the reason
    :func:`is_source_ref` is: one place in CLV knows that a source is a ``Path``
    or a :class:`RemoteRef`, and a second copy is how the two drift apart.
    """

    if isinstance(ref, RemoteRef):
        return f"{ref.node}-{ref.path.name}"
    if isinstance(ref, JournalRef):
        # A unit keeps exactly the name it exported under before this type
        # existed -- `Path("journal:unit/sshd.service").name` was already
        # `sshd.service`, and it is the name an operator recognises.
        #
        # The other two are *changed*, because what they produced was not a
        # usable filename: the whole journal exported as `journal:all`, colon
        # included, and a boot exported as the bare offset -- `0`, or `-1`,
        # which a shell reads as an option rather than a file. `.name` cannot
        # be reused for them either, since "System journal" and "This boot"
        # both carry a space.
        stem = {"all": "journal", "boot": f"boot{ref.value}"}.get(ref.kind, ref.value)
        return f"{ref.node}-{stem}" if ref.node else stem
    return ref.name


#: What the merged pane's source column calls a local member when the set also
#: holds remote ones. Not this machine's hostname: ``host`` already means "what
#: the log says about itself", and borrowing it here would answer a different
#: question than the column is asking.
LOCAL_NODE_LABEL = "local"


def compact_of(ref: SourceRef) -> str:
    """``deeper/a.log``, with the machine in front of it when there is one.

    ``web01:nginx/access.log``. The local form is exactly what ``app._compact_path``
    produced before this existed, so nothing local moved — but two machines with
    the same log rendered the same two words, which is the one thing a label whose
    entire job is telling identically named logs apart may not do.

    Lives here for the reason :func:`stem_of` does: one place in CLV knows a
    source is a ``Path`` or a :class:`RemoteRef`, and a second copy of that union
    is how the two drift apart.
    """

    if isinstance(ref, RemoteRef):
        parent = ref.path.parent.name
        tail = f"{parent}/{ref.path.name}" if parent else ref.path.name
        return f"{ref.node}:{tail}"
    if isinstance(ref, JournalRef):
        # No parent to borrow, so the label stands alone -- qualified by the
        # machine when there is one, exactly as a remote path is.
        return f"{ref.node}:{ref.name}" if ref.node else ref.name
    parent = ref.parent.name
    return f"{parent}/{ref.name}" if parent else ref.name


def node_of(ref: SourceRef) -> str:
    """CLV's name for the machine *ref* was read from; ``""`` for this one."""

    return ref.node if isinstance(ref, (RemoteRef, JournalRef)) else ""


def column_labels(origins: Sequence[SourceRef]) -> dict[SourceRef, str]:
    """What to call each member of a merged set in the pane's source column.

    The column exists to answer "which of these did this line come from", and the
    canonical cross-host merge — one path across a fleet — is exactly the case
    where the basename cannot answer it. Five hosts' ``access.log`` rendered
    ``access.log`` five times, and at the ``-compact`` width of 8 the existing
    left-ellipsis turned ``web01-access.log`` into ``…ess.log``, which is the same
    string for every machine. So the *distinguishing* part is chosen per set:

    * one machine (including an all-local set) → the basename, byte-for-byte what
      this has always rendered;
    * one basename across several machines → the machine, which is short enough to
      survive the narrowest column;
    * otherwise → ``node/name``, and the caller's truncation does the rest.
    """

    unique = list(dict.fromkeys(origins))
    nodes = {node_of(ref) for ref in unique}
    if len(nodes) <= 1:
        return {ref: ref.name for ref in unique}
    if len({ref.name for ref in unique}) == 1:
        return {ref: node_of(ref) or LOCAL_NODE_LABEL for ref in unique}
    return {
        ref: (f"{node}/{ref.name}" if (node := node_of(ref)) else ref.name)
        for ref in unique
    }


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
