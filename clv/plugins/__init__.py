"""CLV plugin interfaces and loader.

Three extension points, matching the three things operators keep asking CLV to
do that core should not hard-code:

* :class:`LogSourceProvider` — where log lines come from.
* :class:`FilterStage` — what happens to a line on its way to the pane.
* :class:`Exporter` — where the current view can be sent.

Plugins are loaded from two places: modules dropped into ``clv/plugins/``
(``sources/``, ``filters/``, ``exporters/`` or flat), and installed
distributions advertising a ``clv.plugins`` entry point.

Loading is defensive on purpose. A plugin that raises on import, fails its
version check, or does not implement an interface is recorded in
:attr:`PluginRegistry.errors` and skipped — a broken third-party plugin must
never stop CLV from starting.

**Import from :mod:`clv.api`, not from here.** Everything a plugin needs is
re-exported there — the same objects, not copies — under a written stability
promise and its own :data:`PLUGIN_API_VERSION`. This module keeps exporting
what it always has, so nothing existing breaks, but it is the loader's own
namespace: it holds internals that will move, and only ``clv.api`` is covered
by the deprecation policy in ``clv/plugins/AGENTS.md``.
"""

from __future__ import annotations

import collections.abc
import importlib
import importlib.metadata
import inspect
import os
import pkgutil
import re
import sys
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from ..services.filtering import FilterSpec
from ..services.parsing import LogEntry
from ..services.refs import SourceRef

#: The version of the published plugin API — the surface re-exported by
#: :mod:`clv.api` — and **not** CLV's own version.
#:
#: The separation is the point. ``clv.__version__`` tracks the application and
#: moves whenever anything ships; this moves only when a published name changes
#: meaning, so a plugin declaring ``requires_api = ">=1.0,<2.0"`` stops caring
#: what release it is running on. The API is additive from here: a seam phase
#: adds names and this stays "1.0".
#:
#: Defined here rather than in :mod:`clv.api` because ``clv.api`` imports the
#: interfaces from this module, so the constant has to sit on the far side of
#: that edge for :meth:`PluginRegistry.add` to check against it without a cycle.
#: ``clv.api`` re-exports it as the name authors actually see.
PLUGIN_API_VERSION = "1.0"

#: Entry point group installed packages use to advertise CLV plugins.
ENTRY_POINT_GROUP = "clv.plugins"

#: Subdirectories of clv/plugins scanned for drop-in modules.
_LOCAL_SUBPACKAGES = ("sources", "filters", "exporters")

#: Extra plugin directories, ``os.pathsep``-separated, searched *before* the
#: user plugin directory.
#:
#: A development and test mechanism, not a way to install a plugin: it is how
#: this package's own tests get a plugin root without writing into the source
#: tree, and how an author runs a plugin they are editing in place. The
#: enable-list applies to it exactly as it does to the user directory.
PLUGIN_PATH_ENV = "CLV_PLUGIN_PATH"

#: The package name user plugins are imported *under*.
#:
#: A module in ``~/.config/clv/plugins/`` is not reachable by dotted import,
#: and putting the directory on ``sys.path`` is not the answer: a user file
#: called ``json.py`` would then shadow the standard library for the whole
#: process. Instead a synthetic namespace package is installed in
#: ``sys.modules`` whose ``__path__`` is the search roots, and user plugins are
#: imported as its submodules.
#:
#: That buys four things at once, each of which would otherwise be bespoke
#: code here: a bare ``foo.py`` and a package directory ``foo/`` load through
#: one code path; relative imports inside a package plugin work; every module
#: gets a stable ``__name__`` for :func:`_extract_plugins` to compare against
#: in its namespace scan; and the whole walk is the same shape
#: :func:`_load_local` already uses, so the per-module body is shared rather
#: than copied.
USER_PLUGIN_PACKAGE = "clv_user_plugins"


# --- interfaces -------------------------------------------------------------


class Plugin(ABC):
    """Common metadata for every plugin type."""

    #: Human-readable name shown in the UI.
    name: str = "unnamed plugin"

    #: Optional CLV version constraint, e.g. ``">=2.0,<3.0"``. When set and
    #: unsatisfied, the plugin is rejected with a recorded error.
    requires_clv: Optional[str] = None

    #: Optional constraint on the *published API*, e.g. ``">=1.0,<2.0"``, in
    #: the same grammar as :attr:`requires_clv`. This is the one to reach for:
    #: what a plugin actually depends on is the shape of :mod:`clv.api`, and
    #: pinning CLV's own version instead means re-releasing on every CLV
    #: release that changed nothing a plugin can see.
    requires_api: Optional[str] = None

    def describe(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """One source a provider offers, and who offered it.

    A provider's sources are **not** filesystem paths, even though they look
    like one: nothing on disk answers to ``journal:unit/sshd.service``. That is
    still true and still matters — it is what keeps them out of include/exclude
    globs (which describe a directory walk) and out of rotated-set grouping
    (which is name arithmetic over files that rotate).

    **What that exclusion used to cover, and no longer does.** Starring and
    merging were on the list, and they were on it for a reason that did not
    survive examination: a provider source was not a ``SourceRef``, so
    ``refs.is_source_ref`` could not see it, so it could not be starred. That
    was cheap rather than right. A journal unit is the clearest case of a source
    someone wants starred, and comparing one unit across a fleet is the workflow
    remote sources exist for. :class:`~clv.services.refs.JournalRef` is now a
    ref, so both work.

    **This record is still not a ref, and that is what keeps the rest honest.**
    It is the *tree node's* payload: :attr:`path` carries the identity and this
    carries the label and the provider name that error attribution needs. A
    provider offering something that genuinely is not a source identity still
    cannot reach the starred set, because the union is over concrete ref types
    rather than over "anything source-shaped" — which is why it is a union of
    types and not a duck test.
    """

    #: The source's identity. A ``JournalRef`` for the journal; a provider that
    #: has no ref type of its own may still hand back a ``Path``, which is what
    #: ``LogSourceProvider.discover``'s bare-identifier contract allows.
    path: SourceRef
    label: str
    #: The provider's own name, for the tree group and for error attribution.
    provider: str = ""

    @property
    def name(self) -> str:
        return self.label or self.path.name


class LogSourceProvider(Plugin):
    """Supplies log sources that the filesystem walker would not find."""

    @abstractmethod
    def discover(self) -> Iterable[Path | ProviderSource]:
        """Return the sources this provider offers.

        Either bare identifiers or :class:`ProviderSource` records, when the
        provider has a better label than the identifier's last component.
        """

    @abstractmethod
    def open(self, path: Path) -> Iterator[str]:
        """Yield the lines of *path*.

        The simple contract, and still the whole of it for a provider that
        produces a finite list of lines. A provider that tails something should
        implement :meth:`open_reader` instead; this is then never called.
        """

    def open_reader(self, path: Path, *, max_lines: int) -> Optional[Any]:
        """Return a ``prime``/``poll`` reader for *path*, or None.

        Optional. Returning None — the default — means "use :meth:`open`", and
        core wraps that iterator in a reader itself, so a provider written
        against the original interface keeps working untouched.

        Implement this when the source is a live stream rather than a list of
        lines: an iterator cannot express tailing, cannot be asked to stop, and
        has nowhere to put the cleanup a subprocess needs. The returned object
        must expose ``path``, ``prime()``, ``poll()`` and ``RELOAD_NOTICE``,
        and should expose ``close()`` when it holds anything.
        """

        return None


@dataclass(frozen=True, slots=True)
class FilterContext:
    """Read-only view of viewer state handed to each filter stage."""

    spec: FilterSpec
    source: Optional[Path] = None


class FilterStage(Plugin):
    """Transforms or drops entries before they reach the pane.

    Return the entry (optionally modified via :func:`dataclasses.replace`) to
    keep it, or ``None`` to drop it. Redaction is the common case::

        def apply(self, entry, context):
            if "password" not in entry.raw:
                return entry
            return replace(entry, raw=entry.raw.replace("password", "******"))
    """

    @abstractmethod
    def apply(self, entry: LogEntry, context: FilterContext) -> Optional[LogEntry]:
        """Return the entry to keep, or None to drop it."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """What an exporter did, so the UI can report it."""

    ok: bool
    detail: str = ""
    destination: Optional[Path] = None


class Exporter(Plugin):
    """Sends the currently visible entries somewhere.

    **Two kinds, and the default is the self-routing one.** An exporter that
    knows where its output goes — a syslog forwarder, an HTTP endpoint, a fixed
    report path — implements ``export(entries, context)`` and reports what it
    did as :class:`ExportResult`. Nothing about that changed.

    An exporter that writes a *file* wants the destination the operator just
    typed, and until now could not have it: the export dialog hardcoded every
    plugin choice as supplying no path, disabled its path input, and said so.
    Setting :attr:`wants_path` re-enables the input and hands the chosen path to
    :meth:`export` as ``destination``.
    """

    #: Whether the export dialog should ask the operator for a destination and
    #: pass it to :meth:`export`. False — the default, and what every exporter
    #: written before this attribute existed gets — means the dialog's path
    #: input stays disabled and ``destination`` is never passed, so an
    #: ``export(self, entries, context)`` written against the original
    #: interface keeps working untouched.
    wants_path: bool = False

    #: Suffix the dialog's suggested filename gets when :attr:`wants_path` is
    #: set, without a leading dot (``"ndjson"``). Empty falls back to ``log``,
    #: which is what the built-in formats do.
    suggested_extension: str = ""

    @abstractmethod
    def export(
        self,
        entries: Sequence[LogEntry],
        context: FilterContext,
        *,
        destination: Optional[Path] = None,
    ) -> ExportResult:
        """Write or transmit *entries*.

        *destination* is the path the operator chose, and is passed **only**
        when the exporter set :attr:`wants_path`. Keyword-only so that adding it
        could not change what an existing positional call means.
        """


# --- adapting the simple contract -------------------------------------------


class IteratorReader:
    """Turns a provider's ``open()`` iterator into a reader.

    So that the older, simpler half of :class:`LogSourceProvider` keeps working
    now that core expects ``prime``/``poll``. The iterator is drained up to the
    line budget on prime and then drained further on each poll, which is enough
    for a provider that yields a finite list and honest for one that does not:
    a generator that blocks would block the poll, which is why a provider that
    tails should implement ``open_reader`` instead.
    """

    RELOAD_NOTICE = "{name} was reloaded."

    def __init__(self, path: Path, lines: Iterator[str], *, max_lines: int) -> None:
        self.path = path
        self._lines = lines
        self._max_lines = max_lines
        self._offset = 0
        self._exhausted = False

    @property
    def offset(self) -> int:
        return self._offset

    def _drain(self, limit: int) -> list[str]:
        collected: list[str] = []
        if self._exhausted:
            return collected
        for line in self._lines:
            collected.append(str(line).rstrip("\n"))
            if len(collected) >= limit:
                return collected
        self._exhausted = True
        return collected

    def prime(self):
        from ..services.reader import TailRead

        lines = self._drain(self._max_lines)
        self._offset = len(lines)
        return TailRead(lines=lines, offset=self._offset)

    def poll(self):
        from ..services.reader import TailRead

        lines = self._drain(self._max_lines)
        self._offset += len(lines)
        return TailRead(lines=lines, offset=self._offset)

    def close(self) -> None:
        closer = getattr(self._lines, "close", None)
        if closer is not None:
            closer()


# --- version constraints ----------------------------------------------------
#
# A PEP 440 subset, hand-rolled. ``packaging`` is not a dependency and will not
# become one — the minimal-dependency policy is not relaxed for the plugin work.
#
# The comparator this replaces stripped non-digits per segment, so "2.6.0rc1"
# became (2, 6, 1) and a release candidate compared as *newer* than its own
# release. It also rejected `~=` and `^` outright, which is not "unsatisfied" but
# "unparseable" — and it returned False for both, silently disabling a plugin
# whose author wrote the most idiomatic constraint in the ecosystem. Both of
# those are failures a plugin author cannot diagnose from the outside, which is
# why the replacement is a real grammar rather than a wider regex.
#
# **One deliberate divergence from PEP 440: prereleases are always considered.**
# Strict PEP 440 excludes a prerelease from a range unless the range itself names
# one, so `requires_clv=">=2.6"` would be *unsatisfied* on a running 2.7.0rc1 and
# every plugin would vanish on any release-candidate build. CLV compares versions
# in plain order instead. Documented in clv/plugins/AGENTS.md.

_VERSION_RE = re.compile(
    r"""^\s*
    v?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_letter>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_num>\d+)?)?
    (?:[-_.]?post[-_.]?(?P<post_num>\d+)?|-(?P<post_bare>\d+))?
    (?:[-_.]?dev[-_.]?(?P<dev_num>\d+)?)?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

#: Prerelease spellings that mean the same thing, and their order.
_PRE_STAGES = {
    "a": 0, "alpha": 0,
    "b": 1, "beta": 1,
    "c": 2, "rc": 2, "pre": 2, "preview": 2,
}

#: Sort-key components. A dev release with no prerelease sorts *before* every
#: prerelease of the same version; a version with no prerelease at all sorts
#: after all of them; "no dev segment" sorts after any dev segment.
_NO_PRE_BUT_DEV = (-1, 0)
_FINAL = (99, 0)
_NO_DEV = 1 << 62

#: Longest first, so ">=" is never read as ">" with a stray "=".
_OPERATORS = (">=", "<=", "==", "!=", "~=", "^", ">", "<")


@dataclass(frozen=True, slots=True)
class _Version:
    """A parsed version, in the shape the sort key needs."""

    release: tuple[int, ...]
    pre: Optional[tuple[int, int]]
    post: Optional[int]
    dev: Optional[int]

    def key(self, width: int) -> tuple:
        """Order-preserving key, padded so 2.0 and 2.0.0 compare equal.

        Orders 1.0.dev1 < 1.0a1.dev1 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 < 1.0.post1.
        """

        release = self.release + (0,) * (width - len(self.release))
        if self.pre is not None:
            pre = self.pre
        elif self.dev is not None and self.post is None:
            pre = _NO_PRE_BUT_DEV
        else:
            pre = _FINAL
        return (
            release,
            pre,
            -1 if self.post is None else self.post,
            _NO_DEV if self.dev is None else self.dev,
        )


def _parse_version(text: str) -> Optional[_Version]:
    """Parse a version, or None if it is not one. Never raises."""

    match = _VERSION_RE.match(text)
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))

    letter = match.group("pre_letter")
    pre = (
        (_PRE_STAGES[letter.lower()], int(match.group("pre_num") or 0))
        if letter
        else None
    )

    if match.group("post_num") is not None:
        post: Optional[int] = int(match.group("post_num"))
    elif match.group("post_bare") is not None:
        post = int(match.group("post_bare"))
    elif re.search(r"post", text, re.IGNORECASE):
        post = 0  # a bare ".post" means post 0
    else:
        post = None

    if match.group("dev_num") is not None:
        dev: Optional[int] = int(match.group("dev_num"))
    elif re.search(r"dev", text, re.IGNORECASE):
        dev = 0  # a bare ".dev" means dev 0
    else:
        dev = None

    return _Version(release, pre, post, dev)


def _compare(left: _Version, right: _Version) -> int:
    """-1, 0 or 1, comparing on equal release width."""

    width = max(len(left.release), len(right.release))
    a, b = left.key(width), right.key(width)
    return (a > b) - (a < b)


def _release_prefix_match(current: _Version, target: tuple[int, ...]) -> bool:
    """Whether *current*'s release starts with *target* — the ``==2.6.*`` test."""

    padded = current.release + (0,) * (len(target) - len(current.release))
    return padded[: len(target)] == target


def _split_operator(piece: str) -> tuple[str, str]:
    """Split a constraint piece into its operator and operand.

    A bare version means ``==``, which is what the previous comparator did and
    what an author writing ``requires_clv = "2.6.0"`` means.
    """

    stripped = piece.strip()
    for operator in _OPERATORS:
        if stripped.startswith(operator):
            return operator, stripped[len(operator):].strip()
    return "==", stripped


def _expand(operator: str, operand: str, constraint: str) -> list[tuple[str, str]]:
    """Rewrite ``~=`` and ``^`` into the plain comparisons they stand for.

    ``~=X.Y`` is ``>=X.Y, ==X.*``; ``~=X.Y.Z`` is ``>=X.Y.Z, ==X.Y.*``. Poetry's
    ``^`` is accepted as a documented alias and expands to the next significant
    release: ``^2.0.0`` is ``>=2.0.0,<3.0.0``, ``^0.2.3`` is ``>=0.2.3,<0.3.0``,
    and ``^0.0.3`` is ``>=0.0.3,<0.0.4``.
    """

    if operator == "~=":
        if operand.endswith(".*"):
            raise ValueError(
                f"unparseable constraint {constraint!r}: ~= cannot take a wildcard"
            )
        parsed = _parse_version(operand)
        if parsed is None or len(parsed.release) < 2:
            raise ValueError(
                f"unparseable constraint {constraint!r}: ~= needs at least two "
                f"release segments, got {operand!r}"
            )
        prefix = ".".join(str(part) for part in parsed.release[:-1])
        return [(">=", operand), ("==", f"{prefix}.*")]

    if operator == "^":
        parsed = _parse_version(operand)
        if parsed is None:
            raise ValueError(f"unparseable constraint {constraint!r}")
        release = parsed.release + (0,) * (3 - len(parsed.release))
        upper = [1]
        for index, part in enumerate(release):
            if part != 0:
                upper = list(release[: index + 1])
                upper[index] += 1
                break
        return [(">=", operand), ("<", ".".join(str(part) for part in upper))]

    return [(operator, operand)]


def satisfies(version: str, constraint: Optional[str]) -> bool:
    """Check *version* against a comma-separated constraint like ``>=2.0,<3.0``.

    Supported: ``>=``, ``<=``, ``>``, ``<``, ``==``, ``!=``, the ``==X.Y.*``
    wildcard, ``~=`` compatible-release, and ``^`` as a documented Poetry alias.
    Pieces are ANDed. An empty or absent constraint means "any version".

    Raises :class:`ValueError` naming the constraint when it cannot be parsed.
    That is deliberate and is the point of the rewrite: the previous comparator
    returned a silent ``False`` for anything it did not recognise, so a typo and
    a genuinely incompatible plugin were indistinguishable — both simply vanished.
    Callers record the error against the plugin; see :meth:`PluginRegistry.add`.
    """

    if not constraint or not constraint.strip():
        return True

    current = _parse_version(version)
    if current is None:
        raise ValueError(f"unparseable version {version!r}")

    for piece in constraint.split(","):
        if not piece.strip():
            continue
        operator, operand = _split_operator(piece)
        if not operand:
            raise ValueError(f"unparseable constraint {constraint!r}")

        for op, value in _expand(operator, operand, constraint):
            if value.endswith(".*"):
                if op not in ("==", "!="):
                    raise ValueError(
                        f"unparseable constraint {constraint!r}: "
                        f"{op} cannot take a wildcard"
                    )
                body = value[:-2]
                if not re.fullmatch(r"\d+(?:\.\d+)*", body):
                    raise ValueError(f"unparseable constraint {constraint!r}")
                matched = _release_prefix_match(
                    current, tuple(int(part) for part in body.split("."))
                )
                if (op == "==" and not matched) or (op == "!=" and matched):
                    return False
                continue

            target = _parse_version(value)
            if target is None:
                raise ValueError(f"unparseable constraint {constraint!r}")
            order = _compare(current, target)
            if not {
                ">=": order >= 0,
                "<=": order <= 0,
                ">": order > 0,
                "<": order < 0,
                "==": order == 0,
                "!=": order != 0,
            }[op]:
                return False

    return True


# --- registry ---------------------------------------------------------------


#: How many *distinct* problems the registry keeps. Anything past this is
#: counted, not stored: the collection exists to tell an operator what is wrong,
#: and a list long enough to scroll has stopped doing that.
MAX_PLUGIN_ERRORS = 50


@dataclass
class PluginError:
    origin: str
    message: str
    #: How many times this exact problem happened. Filled by
    #: :meth:`PluginErrors.append`; a plugin that fails per render used to
    #: append a fresh identical error every pass.
    count: int = 1

    def __str__(self) -> str:  # pragma: no cover - trivial
        repeats = f" (×{self.count})" if self.count > 1 else ""
        return f"{self.origin}: {self.message}{repeats}"


class PluginErrors(Sequence[PluginError]):
    """Everything that went wrong, deduplicated and bounded.

    A plain list was wrong in both directions. It grew without limit — and
    :meth:`PluginRegistry.apply_filters` runs per render, so a single raising
    stage produced one error per pass and 200 passes produced 200 identical
    entries — and every one of them was printed into the log panel, where a wall
    of repeats buries the discovery summary the operator actually opened CLV to
    read.

    Identical ``(origin, message)`` pairs collapse into one entry with a count.
    Past :data:`MAX_PLUGIN_ERRORS` distinct problems the rest are counted in
    :attr:`dropped` and reported by :attr:`overflow_note`, so the collection
    never lies about how much it is not showing.

    Deliberately list-like: it is appended to from ``app.py`` as well as from
    here, indexed, sliced and truth-tested, and none of those call sites should
    have to care that it is no longer a list.
    """

    __slots__ = ("_errors", "_index", "_dropped")

    def __init__(self, errors: Optional[Iterable[PluginError]] = None) -> None:
        self._errors: list[PluginError] = []
        self._index: dict[tuple[str, str], PluginError] = {}
        self._dropped = 0
        for error in errors or ():
            self.append(error)

    def append(self, error: PluginError) -> None:
        """Record *error*, collapsing a repeat into the entry already held."""

        key = (error.origin, error.message)
        existing = self._index.get(key)
        if existing is not None:
            existing.count += error.count
            return
        if len(self._errors) >= MAX_PLUGIN_ERRORS:
            self._dropped += 1
            return
        self._index[key] = error
        self._errors.append(error)

    @property
    def dropped(self) -> int:
        """Distinct problems the cap refused to store."""

        return self._dropped

    @property
    def overflow_note(self) -> str:
        """``"and N more"`` when the cap dropped something, else ``""``."""

        if not self._dropped:
            return ""
        return f"and {self._dropped} more"

    def clear(self) -> None:
        self._errors.clear()
        self._index.clear()
        self._dropped = 0

    def __getitem__(self, index):  # type: ignore[override]
        return self._errors[index]

    def __len__(self) -> int:
        return len(self._errors)

    def __iter__(self) -> Iterator[PluginError]:
        return iter(self._errors)

    def __eq__(self, other: Any) -> Any:
        """Compare equal to a plain list, so ``errors == []`` still reads right."""

        if isinstance(other, PluginErrors):
            return self._errors == other._errors
        if isinstance(other, (list, tuple)):
            return self._errors == list(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # mutable, like the list it replaces

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PluginErrors({self._errors!r}, dropped={self._dropped})"


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    """A plugin CLV found in a user root, whether or not it was loaded.

    New state, and deliberately not a :class:`PluginError`. Being installed but
    not enabled is the *designed* resting state of a user plugin -- it is what
    "installing a plugin is not consent to run it" looks like from the
    registry's side -- and ``app.py`` prints every error into the log panel in
    amber as a problem. A plugin waiting to be named is not a problem.

    A shadowed entry keeps :attr:`shadowed_by` so the report can say which
    origin won the name rather than only that something did.
    """

    #: The module name as found on disk, without ``.py``.
    name: str
    #: The search root it was found in.
    root: Path
    #: Whether it was named in the enable-list and therefore imported.
    enabled: bool
    #: A directory with an ``__init__.py`` rather than a single file.
    is_package: bool = False
    #: The origin that claimed this name first, if this one lost it.
    shadowed_by: Optional[str] = None


@dataclass
class PluginRegistry:
    """Everything successfully loaded, plus everything that failed to load."""

    sources: list[LogSourceProvider] = field(default_factory=list)
    filters: list[FilterStage] = field(default_factory=list)
    exporters: list[Exporter] = field(default_factory=list)
    errors: PluginErrors = field(default_factory=PluginErrors)
    #: Every plugin found in a user root, loaded or not, in search order.
    #: Empty on a build with no user plugin directory, which is the common case
    #: and stays free: nothing is imported to fill this in.
    discovered: list[DiscoveredPlugin] = field(default_factory=list)
    #: Which provider offered which source, filled by `discover_sources`. Keyed
    #: on **(provider name, path)**, because a source identifier means nothing
    #: without the provider that coined it: keyed on the path alone, the second
    #: provider to offer an identifier silently replaced the first and selecting
    #: provider A's row in the tree opened provider B's lines.
    _owners: dict[tuple[str, Path], LogSourceProvider] = field(
        default_factory=dict, repr=False
    )
    #: Plugins taken out of service, ``id(plugin) -> reason``. Keyed on identity
    #: because a plugin is third-party code that may define ``__eq__`` without
    #: ``__hash__``; the registry's own lists hold every plugin alive for the
    #: session, so an id cannot be recycled underneath this map.
    _disabled: dict[int, str] = field(default_factory=dict, repr=False)

    @property
    def total(self) -> int:
        return len(self.sources) + len(self.filters) + len(self.exporters)

    def available(self) -> list[DiscoveredPlugin]:
        """Plugins installed in a user root but not enabled.

        What an operator who copied a file in and has not yet named it in
        ``settings.conf`` needs to be told. A shadowed plugin is not here: it
        was not merely left unenabled, it lost its name to something else and
        is reported on its own account.
        """

        return [
            entry
            for entry in self.discovered
            if not entry.enabled and entry.shadowed_by is None
        ]

    # --- taking a plugin out of service -------------------------------------

    def disable(self, plugin: Any, reason: str, *, origin: Optional[str] = None) -> None:
        """Take *plugin* out of service for the rest of the session.

        Idempotent: the second and later calls record nothing, which is the
        whole point. A raising filter stage used to be disabled *for the current
        pass only*, so it was retried on the next render and appended a fresh
        identical error every time — 200 render passes, 200 errors.

        General rather than filter-specific on purpose. Later work needs to
        disable a plugin for its own reasons — a time budget, a failed
        lifecycle hook, a killed isolation host — and none of it should invent a
        second mechanism with second semantics.

        Nothing is removed from :attr:`sources`, :attr:`filters` or
        :attr:`exporters`. Those lists are addressed positionally elsewhere
        (``app.py`` builds export choices keyed ``plugin:<index>``), so removal
        would silently re-target an in-flight export. Disabling is a marking.
        """

        key = id(plugin)
        if key in self._disabled:
            return
        self._disabled[key] = reason
        self.errors.append(PluginError(origin or _plugin_name(plugin), reason))

    def enable(self, plugin: Any) -> bool:
        """Put a disabled plugin back into service. True if it was disabled."""

        return self._disabled.pop(id(plugin), None) is not None

    def is_disabled(self, plugin: Any) -> bool:
        return id(plugin) in self._disabled

    def disabled_reason(self, plugin: Any) -> Optional[str]:
        return self._disabled.get(id(plugin))

    # --- loading -------------------------------------------------------------

    def add(
        self,
        plugin: Any,
        *,
        origin: str,
        clv_version: str,
        api_version: str = PLUGIN_API_VERSION,
    ) -> bool:
        """Classify and store *plugin*, recording why it was rejected if so.

        *api_version* defaults to the running :data:`PLUGIN_API_VERSION` and is
        a parameter only so a test can pin it; no loader passes it.
        """

        if isinstance(plugin, type):
            try:
                plugin = plugin()
            except Exception as exc:  # noqa: BLE001 - third-party code
                self.errors.append(PluginError(origin, f"could not be instantiated: {exc}"))
                return False

        if not isinstance(plugin, (LogSourceProvider, FilterStage, Exporter)):
            self.errors.append(
                PluginError(origin, "does not implement a CLV plugin interface")
            )
            return False

        # Both constraints, in the same grammar, with the same two failure
        # shapes: an unreadable constraint is an error naming it, and an
        # unsatisfied one names both versions. A plugin may declare either or
        # both, and each is checked on its own account -- `requires_api` is the
        # one the documentation tells authors to use.
        for label, requirement, running in (
            ("requires_clv", getattr(plugin, "requires_clv", None), clv_version),
            ("requires_api", getattr(plugin, "requires_api", None), api_version),
        ):
            try:
                compatible = satisfies(running, requirement)
            except ValueError as exc:
                # A constraint CLV cannot read is an error naming the
                # constraint, never a silent False: a typo and a genuine
                # incompatibility used to look identical from the outside, and
                # both simply vanished.
                self.errors.append(PluginError(origin, f"bad {label}: {exc}"))
                return False
            if not compatible:
                subject = "CLV" if label == "requires_clv" else "plugin API"
                self.errors.append(
                    PluginError(
                        origin,
                        f"requires {subject} {requirement}, running {running}",
                    )
                )
                return False

        if isinstance(plugin, LogSourceProvider):
            self.sources.append(plugin)
        elif isinstance(plugin, FilterStage):
            self.filters.append(plugin)
        else:
            self.exporters.append(plugin)
        return True

    def discover_sources(self) -> list[ProviderSource]:
        """Ask every provider what it offers, skipping the ones that raise.

        Same contract as a ``FilterStage`` that throws: recorded, surfaced in
        the drawer, and survivable. A broken provider must not be able to stop
        discovery, which is the one thing standing between the operator and
        every source they have.
        """

        found: list[ProviderSource] = []
        self._owners = {}
        offered_by: dict[Path, list[str]] = {}
        for provider in self.sources:
            if self.is_disabled(provider):
                continue
            name = _plugin_name(provider)
            try:
                offered = list(provider.discover())
            except Exception as exc:  # noqa: BLE001 - third-party code
                self.errors.append(PluginError(name, f"discover() raised: {exc}"))
                continue
            for item in offered:
                source = (
                    item
                    if isinstance(item, ProviderSource)
                    else ProviderSource(Path(item), Path(item).name, name)
                )
                if not source.provider:
                    source = ProviderSource(source.path, source.label, name)
                key = (source.provider, source.path)
                if key in self._owners:
                    # The same provider offering the same identifier twice: a
                    # genuine shadow, and only one of them can ever be opened.
                    self.errors.append(
                        PluginError(
                            source.provider,
                            f"offers {source.path} more than once; keeping the first",
                        )
                    )
                    continue
                self._owners[key] = provider
                offered_by.setdefault(source.path, []).append(source.provider)
                found.append(source)

        # Two *different* providers offering one identifier is no longer a bug —
        # each row now opens its own provider's lines — but it is worth saying,
        # because the operator sees two rows that may well be labelled the same.
        for path, providers in offered_by.items():
            if len(providers) > 1:
                self.errors.append(
                    PluginError(
                        ", ".join(sorted(providers)),
                        f"all offer {path}; each opens its own source",
                    )
                )
        return found

    def open_source(self, source: ProviderSource, *, max_lines: int) -> Optional[Any]:
        """Build a reader for a provider source, or None if it failed.

        Prefers the provider's own ``open_reader``; falls back to wrapping
        ``open()``, so both halves of the interface reach the same pane.
        """

        provider = self._resolve_owner(source)
        if provider is None:
            return None
        if self.is_disabled(provider):
            self.errors.append(
                PluginError(
                    _plugin_name(provider),
                    f"is disabled: {self.disabled_reason(provider)}",
                )
            )
            return None
        name = _plugin_name(provider)
        try:
            reader = provider.open_reader(source.path, max_lines=max_lines)
            if reader is not None:
                return reader
            return IteratorReader(
                source.path, iter(provider.open(source.path)), max_lines=max_lines
            )
        except Exception as exc:  # noqa: BLE001 - third-party code
            self.errors.append(PluginError(name, f"open() raised: {exc}"))
            return None

    def reader_for_ref(self, ref: SourceRef, *, max_lines: int) -> Optional[Any]:
        """Build a reader for a **ref** a provider offers, or ``None``.

        :meth:`open_source` takes the tree node's record, which is what the
        operator clicked. This takes the identity alone, which is what a
        *restored* source is: ``session.json`` stores ``journal:unit/sshd.service``
        and nothing else, so opening a starred unit at launch, or merging one
        with a file, has no ``ProviderSource`` to hand.

        Resolution is by ref rather than by ``(provider, path)`` because a
        stored ref names no provider — the same fallback ``_resolve_owner``
        already makes for a record built by hand, and it refuses an ambiguous
        answer for the same reason: resolving by luck is the defect that key was
        widened to fix.

        ``None`` when nothing offers it, which is the ordinary case rather than
        an error: the journal is off, or the unit no longer exists.
        ``sources.check_access`` is where that becomes a message.
        """

        candidates = {
            owner: holder
            for (owner, path), holder in self._owners.items()
            if path == ref
        }
        if len(candidates) != 1:
            return None
        provider = next(iter(candidates.values()))
        if self.is_disabled(provider):
            return None
        name = _plugin_name(provider)
        try:
            reader = provider.open_reader(ref, max_lines=max_lines)
            if reader is not None:
                return reader
            return IteratorReader(ref, iter(provider.open(ref)), max_lines=max_lines)
        except Exception as exc:  # noqa: BLE001 - third-party code
            self.errors.append(PluginError(name, f"open() raised: {exc}"))
            return None

    def offers(self, ref: SourceRef) -> bool:
        """Whether any loaded provider currently offers *ref*.

        Cheap and side-effect free, so the tree and ``check_access`` can ask on
        every render without opening anything.
        """

        return any(path == ref for _owner, path in self._owners)

    def _resolve_owner(self, source: ProviderSource) -> Optional[LogSourceProvider]:
        """Find the provider that offered *source*.

        The record carries the provider's own name, so the usual case is an
        exact ``(provider, path)`` hit. A ``ProviderSource`` built by hand or
        restored from older state may have no provider name; that falls back to
        matching on the path alone, and only when exactly one provider offers it
        — resolving an ambiguous one by luck is the defect this key was widened
        to fix.
        """

        provider = self._owners.get((source.provider, source.path))
        if provider is not None:
            return provider

        candidates = {
            owner: holder
            for (owner, path), holder in self._owners.items()
            if path == source.path
        }
        if source.provider or not candidates:
            self.errors.append(
                PluginError(source.provider or "provider", "no longer offers this source")
            )
            return None
        if len(candidates) > 1:
            self.errors.append(
                PluginError(
                    ", ".join(sorted(candidates)),
                    f"all offer {source.path} and the source names no provider; "
                    "refusing to guess",
                )
            )
            return None
        return next(iter(candidates.values()))

    def apply_filters(self, entries: Sequence[LogEntry], context: FilterContext) -> list[LogEntry]:
        """Run every filter stage over *entries*, skipping stages that raise.

        A stage that raises is disabled for the **session**, not for the pass.
        Disabling it per pass meant retrying it on the next render and recording
        the same failure again, so a broken stage cost one error per render
        rather than one error.
        """

        if not self.filters:
            return list(entries)

        result: list[LogEntry] = []
        for entry in entries:
            current: Optional[LogEntry] = entry
            for stage in self.filters:
                if current is None:
                    break
                if self.is_disabled(stage):
                    continue
                try:
                    current = stage.apply(current, context)
                except Exception as exc:  # noqa: BLE001 - third-party code
                    # The pane keeps working with the remaining stages; the
                    # operator re-enables the stage once they have fixed it.
                    self.disable(stage, f"raised: {exc}", origin=_plugin_name(stage))
            if current is not None:
                result.append(current)
        return result


def _plugin_name(plugin: Any) -> str:
    """The name to attribute a problem to. Never raises, never empty."""

    try:
        name = getattr(plugin, "name", "")
    except Exception:  # noqa: BLE001 - a property on third-party code
        name = ""
    return name or type(plugin).__name__


def _as_list(produced: Any) -> list[Any]:
    """Normalise whatever ``register()`` handed back into a list."""

    if produced is None:
        return []
    if isinstance(produced, (list, tuple, set, frozenset)):
        return list(produced)
    # A generator is an obvious way to write ``register()`` and used to be
    # collected as one unusable object. Plugins are not iterable, so this
    # cannot swallow a plugin that happens to be returned on its own.
    if isinstance(produced, collections.abc.Iterator):
        return list(produced)
    return [produced]


def _extract_plugins(module: Any) -> tuple[list[Any], Optional[str]]:
    """Pull plugin objects out of a loaded module, and say so when there are none.

    Three strategies, in order, plus a diagnosis — because the failure this
    returns a message for is the single most likely first-run experience for a
    new plugin author, and it used to be completely silent: a module with no
    ``register()`` and no ``__all__`` loaded as zero plugins and zero errors.

    1. ``register()``, returning one plugin or any iterable of them. Returning
       ``None`` or an empty list is **deliberate** — it is how a plugin declines
       to register itself on this machine — and is never diagnosed.
    2. ``__all__``, listing plugin classes or instances.
    3. A scan of the module's own namespace for concrete :class:`Plugin`
       subclasses **defined in that module**. The ``__module__`` test keeps an
       imported base class out, and the abstractness test keeps out a subclass
       that forgot to implement its interface method — which would otherwise be
       instantiated into a confusing ``TypeError`` at ``add()``.

    Returns ``(candidates, diagnosis)``; the diagnosis is None unless the module
    genuinely says nothing about what it exports.
    """

    register = getattr(module, "register", None)
    if callable(register):
        return _as_list(register()), None

    exported = getattr(module, "__all__", None)
    if exported:
        return [getattr(module, name) for name in exported if hasattr(module, name)], None

    module_name = getattr(module, "__name__", None)
    found = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, Plugin)
        and value.__module__ == module_name
        and not inspect.isabstract(value)
    ]
    if found:
        return found, None

    return [], "defines no plugin — add register() or __all__"


def _load_module(
    registry: PluginRegistry,
    module_name: str,
    *,
    origin: str,
    clv_version: str,
) -> None:
    """Import one module and file whatever it exports into *registry*.

    Shared by the bundled walk and the user-root walk. The two differ in where
    they find a name and in whether they are allowed to import it at all; once
    a name has been cleared for import the handling is identical, and it should
    stay identical -- a plugin must not be diagnosed differently for having been
    installed in a different directory.
    """

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - third-party code
        registry.errors.append(PluginError(origin, f"import failed: {exc}"))
        return
    try:
        candidates, diagnosis = _extract_plugins(module)
    except Exception as exc:  # noqa: BLE001 - third-party code
        registry.errors.append(PluginError(origin, f"register() failed: {exc}"))
        return
    if diagnosis:
        registry.errors.append(PluginError(origin, diagnosis))
    for candidate in candidates:
        registry.add(candidate, origin=origin, clv_version=clv_version)


def plugin_search_roots() -> list[Path]:
    """The user plugin roots, in search order, first-wins.

    ``CLV_PLUGIN_PATH`` first, then ``~/.config/clv/plugins/``. The env var is
    ahead so an author can run a plugin they are editing without moving it, and
    so the tests get a root without writing into the source tree.

    De-duplicated by resolved path with order preserved: naming the user
    directory in ``CLV_PLUGIN_PATH`` should not make every plugin in it report
    itself as shadowing itself.
    """

    roots: list[Path] = []
    raw = os.environ.get(PLUGIN_PATH_ENV, "")
    roots.extend(
        Path(piece).expanduser()
        for piece in raw.split(os.pathsep)
        if piece.strip()
    )
    # Local import for the same reason `load_plugins` imports `clv.__version__`
    # locally: `clv.api` re-exports this module, and the loader has no business
    # dragging the settings parser into a plugin author's unit tests.
    from ..services.config import user_plugin_dir

    try:
        roots.append(user_plugin_dir())
    except Exception:  # noqa: BLE001 - a $HOME-less environment is not fatal
        pass

    ordered: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:  # pragma: no cover - unresolvable path
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(root)
    return ordered


def _install_user_package(roots: Sequence[Path]) -> None:
    """Put the synthetic user-plugin package into ``sys.modules``.

    Rebuilt on every load rather than cached, because the roots can differ
    between one call and the next -- which in practice means between one test
    and the next.
    """

    package = types.ModuleType(USER_PLUGIN_PACKAGE)
    package.__path__ = [str(root) for root in roots]  # type: ignore[attr-defined]
    package.__doc__ = "Synthetic package: CLV user plugins, imported by path."
    sys.modules[USER_PLUGIN_PACKAGE] = package


def _load_user_roots(
    registry: PluginRegistry,
    clv_version: str,
    roots: Sequence[Path],
    enabled: Sequence[str],
    claimed: dict[str, str],
) -> None:
    """Walk the user roots, importing only what the enable-list names.

    The order of the two checks in the loop is the phase's whole point:
    ``pkgutil.iter_modules`` yields a name **without importing it**, and
    ``importlib`` is reached only for a name the operator wrote in
    ``settings.conf``. An unlisted module is recorded and left alone, so
    dropping a file into the plugin directory cannot execute anything.
    """

    # Each root paired with what is in it, resolved before anything is
    # installed: on the overwhelmingly common machine with no user plugins at
    # all there is nothing to search, and nothing should be put into
    # `sys.modules` on its behalf.
    listings: list[tuple[Path, list]] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            listings.append((root, list(pkgutil.iter_modules([str(root)]))))
        except OSError:
            # An unreadable root is a non-event, not a failure. The operator
            # who chmod'd their own plugin directory does not need CLV to stop.
            continue

    wanted = set(enabled)
    found: set[str] = set()

    # Not an early return even when there is nothing to search: a plugin named
    # in settings.conf that is nowhere on disk still has to be reported, and
    # "the directory does not exist" is the most likely reason for it.
    if listings:
        _install_user_package([root for root, _ in listings])

    for root, entries in listings:
        for info in entries:
            if info.name.startswith("_"):
                continue
            key = info.name.casefold()
            found.add(key)
            origin = f"{root / info.name}"

            if key in claimed:
                registry.discovered.append(
                    DiscoveredPlugin(
                        name=info.name,
                        root=root,
                        enabled=False,
                        is_package=info.ispkg,
                        shadowed_by=claimed[key],
                    )
                )
                registry.errors.append(
                    PluginError(origin, f"shadowed by {claimed[key]}, which was found first")
                )
                continue

            if key not in wanted:
                registry.discovered.append(
                    DiscoveredPlugin(
                        name=info.name,
                        root=root,
                        enabled=False,
                        is_package=info.ispkg,
                    )
                )
                continue

            claimed[key] = origin
            registry.discovered.append(
                DiscoveredPlugin(
                    name=info.name,
                    root=root,
                    enabled=True,
                    is_package=info.ispkg,
                )
            )
            _load_module(
                registry,
                f"{USER_PLUGIN_PACKAGE}.{info.name}",
                origin=origin,
                clv_version=clv_version,
            )

    # A typo in settings.conf says so. Naming a plugin that is not there used
    # to be indistinguishable from naming nothing at all.
    where = ", ".join(str(root) for root in roots)
    for name in enabled:
        if name not in found:
            registry.errors.append(
                PluginError(
                    "plugins",
                    f"{name!r} is named in settings.conf but was not found"
                    + (f" in {where}" if where else " -- no plugin directory exists"),
                )
            )


def _load_local(
    registry: PluginRegistry,
    clv_version: str,
    claimed: Optional[dict[str, str]] = None,
    claimed_by_user: Optional[dict[str, str]] = None,
) -> None:
    """Import drop-in modules under clv/plugins/ (flat and in subpackages).

    Where each subpackage *lives* is asked of the import system rather than of
    the filesystem. In a PyInstaller bundle the modules are inside the archive
    and ``clv/plugins/sources/`` is not a directory on disk, so testing
    ``is_dir()`` skipped every drop-in — silently, since finding no plugins is
    not an error. That is the whole of why the shipped binary offered no
    journal: not packaging, not the opt-in, just a filesystem check standing in
    for a question only the loader can answer.
    """

    claimed = {} if claimed is None else claimed
    claimed_by_user = {} if claimed_by_user is None else claimed_by_user

    package_dir = Path(__file__).resolve().parent
    search: list[tuple[str, str]] = [(str(package_dir), __name__)]
    for sub in _LOCAL_SUBPACKAGES:
        package_name = f"{__name__}.{sub}"
        try:
            subpackage = importlib.import_module(package_name)
        except ImportError:
            # A build that dropped the subpackage entirely. Not an error worth
            # reporting: an absent drop-in folder is a valid state.
            continue
        except Exception as exc:  # noqa: BLE001 - a broken __init__ is on them
            registry.errors.append(PluginError(package_name, f"import failed: {exc}"))
            continue
        search.extend((str(entry), package_name) for entry in getattr(subpackage, "__path__", ()))

    for directory, package_name in search:
        for info in pkgutil.iter_modules([directory]):
            if info.name.startswith("_") or info.name in _LOCAL_SUBPACKAGES:
                continue
            module_name = f"{package_name}.{info.name}"
            key = info.name.casefold()
            # Only a *user* root can shadow a bundled drop-in, and only one the
            # operator enabled -- an unlisted file is never imported, so it
            # cannot displace anything. Two bundled subpackages sharing a
            # basename are untouched by this: `claimed_by_user` holds user
            # names only, and nothing here writes into it.
            if key in claimed_by_user:
                registry.errors.append(
                    PluginError(
                        module_name,
                        f"shadowed by {claimed_by_user[key]}, which was found first",
                    )
                )
                continue
            claimed.setdefault(key, module_name)
            _load_module(
                registry, module_name, origin=module_name, clv_version=clv_version
            )


def _entry_point_candidates(loaded: Any) -> tuple[list[Any], Optional[str]]:
    """Resolve whatever an entry point pointed at into plugin candidates.

    Four legal target shapes, each handled and each documented, because the
    previous test — ``hasattr(loaded, "__name__") and not isinstance(loaded, type)``
    — is true for a **function** as well as a module. A perfectly correct
    ``clv.plugins = ["x = mypkg:make_plugin"]`` was therefore sent through
    :func:`_extract_plugins`, which found no ``register`` and no ``__all__`` on a
    function object, and the fallthrough handed the function itself to ``add()``
    to be rejected as "does not implement a CLV plugin interface" — a message
    about the wrong problem entirely.

    A ``Plugin`` instance is tested for before ``callable``: an instance is
    callable too if its class defines ``__call__``.
    """

    if isinstance(loaded, types.ModuleType):
        return _extract_plugins(loaded)
    if isinstance(loaded, Plugin):
        return [loaded], None
    if isinstance(loaded, type):
        return [loaded], None
    if callable(loaded):
        try:
            signature = inspect.signature(loaded)
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            signature = None
        if signature is not None and any(
            parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            for parameter in signature.parameters.values()
        ):
            return [], (
                "points at a callable that requires arguments; an entry point "
                "must name a module, a plugin class, or a zero-argument factory"
            )
        return _as_list(loaded()), None
    return [loaded], None


def _load_entry_points(
    registry: PluginRegistry,
    clv_version: str,
    claimed: Optional[dict[str, str]] = None,
) -> None:
    """Load plugins advertised by installed distributions.

    Last in the search order, so an entry point whose name a user root or a
    bundled drop-in already took loses it -- and is told so, rather than being
    resolved by whichever happened to run first.
    """

    claimed = {} if claimed is None else claimed

    try:
        entry_points = importlib.metadata.entry_points()
        selected = entry_points.select(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 - environment dependent
        registry.errors.append(PluginError(ENTRY_POINT_GROUP, f"lookup failed: {exc}"))
        return

    for entry_point in selected:
        origin = f"{ENTRY_POINT_GROUP}:{entry_point.name}"
        key = entry_point.name.casefold()
        if key in claimed:
            registry.errors.append(
                PluginError(origin, f"shadowed by {claimed[key]}, which was found first")
            )
            continue
        claimed.setdefault(key, origin)
        try:
            loaded = entry_point.load()
        except Exception as exc:  # noqa: BLE001 - third-party code
            registry.errors.append(PluginError(origin, f"load failed: {exc}"))
            continue
        try:
            candidates, diagnosis = _entry_point_candidates(loaded)
        except Exception as exc:  # noqa: BLE001 - third-party code
            # Previously unguarded, so a module entry point whose register()
            # raised propagated straight out of load_plugins() — contradicting
            # its own "Never raises" contract.
            registry.errors.append(PluginError(origin, f"register() failed: {exc}"))
            continue
        if diagnosis:
            registry.errors.append(PluginError(origin, diagnosis))
        for candidate in candidates:
            registry.add(candidate, origin=origin, clv_version=clv_version)


def load_plugins(
    *,
    clv_version: Optional[str] = None,
    include_local: bool = True,
    include_entry_points: bool = True,
    include_user: bool = True,
    roots: Optional[Sequence[Path]] = None,
    enabled: Iterable[str] = (),
) -> PluginRegistry:
    """Discover and load all available plugins.

    Search order, first name wins, and a loser is **reported** rather than
    silently dropped: ``CLV_PLUGIN_PATH``, then ``~/.config/clv/plugins/``,
    then the bundled ``clv/plugins/`` drop-ins, then ``clv.plugins`` entry
    points.

    *enabled* names the user-root modules the operator consented to run. It has
    to be passed in rather than applied afterwards, because it decides whether
    a module is **imported at all** -- filtering an already-loaded registry
    would be a consent check that runs after the code it was guarding.

    Bundled drop-ins ignore *enabled* entirely. They shipped with CLV, and the
    operator's trust in them is the trust they placed in CLV.

    Never raises: any failure is captured in :attr:`PluginRegistry.errors`.
    """

    if clv_version is None:
        from .. import __version__ as clv_version  # local import avoids a cycle

    registry = PluginRegistry()
    #: Names taken by an enabled *user* module. Only these may displace a
    #: bundled drop-in; two bundled subpackages sharing a basename are two
    #: different modules, as they have always been.
    claimed_by_user: dict[str, str] = {}
    #: The above plus bundled module names -- what an entry point competes with.
    claimed: dict[str, str] = {}

    if include_user:
        search_roots = list(roots) if roots is not None else plugin_search_roots()
        _load_user_roots(
            registry,
            clv_version,
            search_roots,
            [name.casefold() for name in enabled],
            claimed,
        )
        claimed_by_user.update(claimed)
    if include_local:
        _load_local(registry, clv_version, claimed, claimed_by_user)
    if include_entry_points:
        _load_entry_points(registry, clv_version, claimed)
    return registry


__all__ = [
    "ENTRY_POINT_GROUP",
    "MAX_PLUGIN_ERRORS",
    "PLUGIN_API_VERSION",
    "PLUGIN_PATH_ENV",
    "USER_PLUGIN_PACKAGE",
    "DiscoveredPlugin",
    "Exporter",
    "ExportResult",
    "FilterContext",
    "FilterStage",
    "IteratorReader",
    "LogSourceProvider",
    "ProviderSource",
    "Plugin",
    "PluginError",
    "PluginErrors",
    "PluginRegistry",
    "load_plugins",
    "plugin_search_roots",
    "satisfies",
]
