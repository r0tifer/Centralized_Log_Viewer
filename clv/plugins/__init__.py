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

import importlib
import importlib.metadata
import importlib.util
import pkgutil
import re
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


class LogSourceProvider(Plugin):
    """Supplies log sources that the filesystem walker would not find."""

    @abstractmethod
    def discover(self) -> Iterable[Path]:
        """Return the paths (or path-like identifiers) this provider offers."""

    @abstractmethod
    def open(self, path: Path) -> Iterator[str]:
        """Yield the lines of *path*."""


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


# --- version constraints ----------------------------------------------------

_CONSTRAINT_RE = re.compile(r"^\s*(?P<op>>=|<=|==|!=|>|<)?\s*(?P<version>[\w.]+)\s*$")


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def satisfies(version: str, constraint: Optional[str]) -> bool:
    """Check *version* against a comma-separated constraint like ``>=2.0,<3.0``."""

    if not constraint:
        return True
    current = _version_tuple(version)
    for piece in constraint.split(","):
        match = _CONSTRAINT_RE.match(piece)
        if not match:
            return False
        operator = match.group("op") or "=="
        target = _version_tuple(match.group("version"))
        # Compare on equal length so 2.0 and 2.0.0 are the same version.
        width = max(len(current), len(target))
        left = current + (0,) * (width - len(current))
        right = target + (0,) * (width - len(target))
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and left != right:
            return False
        if operator == "!=" and left == right:
            return False
    return True


# --- registry ---------------------------------------------------------------


@dataclass
class PluginError:
    origin: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.origin}: {self.message}"


@dataclass
class PluginRegistry:
    """Everything successfully loaded, plus everything that failed to load."""

    sources: list[LogSourceProvider] = field(default_factory=list)
    filters: list[FilterStage] = field(default_factory=list)
    exporters: list[Exporter] = field(default_factory=list)
    errors: list[PluginError] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sources) + len(self.filters) + len(self.exporters)

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
        if not satisfies(clv_version, requirement):
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

    def apply_filters(self, entries: Sequence[LogEntry], context: FilterContext) -> list[LogEntry]:
        """Run every filter stage over *entries*, skipping stages that raise."""

        if not self.filters:
            return list(entries)

        result: list[LogEntry] = []
        broken: list[FilterStage] = []
        for entry in entries:
            current: Optional[LogEntry] = entry
            for stage in self.filters:
                if stage in broken or current is None:
                    continue
                try:
                    current = stage.apply(current, context)
                except Exception as exc:  # noqa: BLE001 - third-party code
                    # Disable the stage for this pass rather than failing the
                    # render; the pane keeps working with the remaining stages.
                    self.errors.append(
                        PluginError(getattr(stage, "name", "filter"), f"raised: {exc}")
                    )
                    broken.append(stage)
            if current is not None:
                result.append(current)
        return result


def _extract_plugins(module: Any) -> list[Any]:
    """Pull plugin objects out of a loaded module.

    Supports ``register()`` returning one plugin or an iterable, and ``__all__``
    listing plugin classes.
    """

    register = getattr(module, "register", None)
    if callable(register):
        produced = register()
        if produced is None:
            return []
        if isinstance(produced, (list, tuple, set)):
            return list(produced)
        return [produced]

    exported = getattr(module, "__all__", None)
    if exported:
        return [getattr(module, name) for name in exported if hasattr(module, name)]

    return []


def _load_local(registry: PluginRegistry, clv_version: str) -> None:
    """Import drop-in modules under clv/plugins/ (flat and in subpackages)."""

    package_dir = Path(__file__).resolve().parent
    search: list[tuple[Path, str]] = [(package_dir, __name__)]
    for sub in _LOCAL_SUBPACKAGES:
        sub_dir = package_dir / sub
        if sub_dir.is_dir():
            search.append((sub_dir, f"{__name__}.{sub}"))

    for directory, package_name in search:
        for info in pkgutil.iter_modules([str(directory)]):
            if info.name.startswith("_") or info.name in _LOCAL_SUBPACKAGES:
                continue
            module_name = f"{package_name}.{info.name}"
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.errors.append(PluginError(module_name, f"import failed: {exc}"))
                continue
            try:
                candidates = _extract_plugins(module)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.errors.append(PluginError(module_name, f"register() failed: {exc}"))
                continue
            for candidate in candidates:
                registry.add(candidate, origin=module_name, clv_version=clv_version)


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
        candidates = _extract_plugins(loaded) if hasattr(loaded, "__name__") and not isinstance(
            loaded, type
        ) else [loaded]
        if not candidates:
            candidates = [loaded]
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
    "Exporter",
    "ExportResult",
    "FilterContext",
    "FilterStage",
    "LogSourceProvider",
    "Plugin",
    "PluginError",
    "PluginRegistry",
    "load_plugins",
    "satisfies",
]
