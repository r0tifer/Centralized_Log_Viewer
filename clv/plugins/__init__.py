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
"""

from __future__ import annotations

import collections.abc
import importlib
import importlib.metadata
import inspect
import pkgutil
import re
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from ..services.filtering import FilterSpec
from ..services.parsing import LogEntry

#: Entry point group installed packages use to advertise CLV plugins.
ENTRY_POINT_GROUP = "clv.plugins"

#: Subdirectories of clv/plugins scanned for drop-in modules.
_LOCAL_SUBPACKAGES = ("sources", "filters", "exporters")


# --- interfaces -------------------------------------------------------------


class Plugin(ABC):
    """Common metadata for every plugin type."""

    #: Human-readable name shown in the UI.
    name: str = "unnamed plugin"

    #: Optional CLV version constraint, e.g. ``">=2.0,<3.0"``. When set and
    #: unsatisfied, the plugin is rejected with a recorded error.
    requires_clv: Optional[str] = None

    def describe(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """One source a provider offers, and who offered it.

    A provider's sources are **not** filesystem paths, even though they are
    identified by one: nothing on disk answers to ``journal:unit/sshd.service``.
    That is deliberate and is what keeps them out of every operation that
    assumes a real file — starring (which persists a path), include/exclude
    globs (which describe a directory walk), and the rotated-set grouping — all
    of which test ``isinstance(data, Path)`` and so cannot see one of these.
    Those operations were generalised nowhere: a provider source is a different
    kind of thing, and pretending otherwise is how it would end up in someone's
    ``session.json`` as a path that does not exist.
    """

    path: Path
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
    """Sends the currently visible entries somewhere."""

    @abstractmethod
    def export(self, entries: Sequence[LogEntry], context: FilterContext) -> ExportResult:
        """Write or transmit *entries*."""


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


@dataclass
class PluginRegistry:
    """Everything successfully loaded, plus everything that failed to load."""

    sources: list[LogSourceProvider] = field(default_factory=list)
    filters: list[FilterStage] = field(default_factory=list)
    exporters: list[Exporter] = field(default_factory=list)
    errors: PluginErrors = field(default_factory=PluginErrors)
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

    def add(self, plugin: Any, *, origin: str, clv_version: str) -> bool:
        """Classify and store *plugin*, recording why it was rejected if so."""

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

        requirement = getattr(plugin, "requires_clv", None)
        try:
            compatible = satisfies(clv_version, requirement)
        except ValueError as exc:
            # A constraint CLV cannot read is an error naming the constraint,
            # never a silent False: a typo and a genuine incompatibility used to
            # look identical from the outside, and both simply vanished.
            self.errors.append(PluginError(origin, f"bad requires_clv: {exc}"))
            return False
        if not compatible:
            self.errors.append(
                PluginError(
                    origin,
                    f"requires CLV {requirement}, running {clv_version}",
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


def _load_local(registry: PluginRegistry, clv_version: str) -> None:
    """Import drop-in modules under clv/plugins/ (flat and in subpackages).

    Where each subpackage *lives* is asked of the import system rather than of
    the filesystem. In a PyInstaller bundle the modules are inside the archive
    and ``clv/plugins/sources/`` is not a directory on disk, so testing
    ``is_dir()`` skipped every drop-in — silently, since finding no plugins is
    not an error. That is the whole of why the shipped binary offered no
    journal: not packaging, not the opt-in, just a filesystem check standing in
    for a question only the loader can answer.
    """

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
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.errors.append(PluginError(module_name, f"import failed: {exc}"))
                continue
            try:
                candidates, diagnosis = _extract_plugins(module)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.errors.append(PluginError(module_name, f"register() failed: {exc}"))
                continue
            if diagnosis:
                registry.errors.append(PluginError(module_name, diagnosis))
            for candidate in candidates:
                registry.add(candidate, origin=module_name, clv_version=clv_version)


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


def _load_entry_points(registry: PluginRegistry, clv_version: str) -> None:
    """Load plugins advertised by installed distributions."""

    try:
        entry_points = importlib.metadata.entry_points()
        selected = entry_points.select(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 - environment dependent
        registry.errors.append(PluginError(ENTRY_POINT_GROUP, f"lookup failed: {exc}"))
        return

    for entry_point in selected:
        origin = f"{ENTRY_POINT_GROUP}:{entry_point.name}"
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
) -> PluginRegistry:
    """Discover and load all available plugins.

    Never raises: any failure is captured in :attr:`PluginRegistry.errors`.
    """

    if clv_version is None:
        from .. import __version__ as clv_version  # local import avoids a cycle

    registry = PluginRegistry()
    if include_local:
        _load_local(registry, clv_version)
    if include_entry_points:
        _load_entry_points(registry, clv_version)
    return registry


__all__ = [
    "ENTRY_POINT_GROUP",
    "MAX_PLUGIN_ERRORS",
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
    "satisfies",
]
