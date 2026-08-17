"""What CLV requires of a log source, and how one survives a restart.

A source is a **SourceRef**. ``pathlib.Path`` is one implementation and, as of
this module, the only one — but it is no longer the assumed one. Remote sources
over SSH are in scope (see ``SSH_TODO.md``), and the reason a plugin alone could
not deliver them is that eight years of code said *a source is a path*.

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
from pathlib import Path
from typing import IO, Any, Protocol

__all__ = [
    "KNOWN_SCHEMES",
    "PROTOCOL_MEMBERS",
    "SourceRef",
    "format_ref",
    "identity",
    "is_local",
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
    # Phase 2 moves these behind a ``SourceBackend`` with a declared blocking
    # contract. Until it does, they are here because discovery and reading call
    # them directly, and a protocol that omitted them would be a lie about what
    # CLV requires today.
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
    whose first component begins ``journal:`` or ``ssh:`` reads as a scheme and
    cannot be pointed at. ``./journal:all`` does not disambiguate it —
    ``Path`` normalises the ``./`` away before this ever sees it — so the
    escape is to name it absolutely, which every path CLV stores already is.
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
    persistence needs.
    """

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
