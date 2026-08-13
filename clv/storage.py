"""JSON-backed session state.

Everything the operator set up is persisted, not just the toggles: the README
has always promised restarts "pick up exactly where you left off", and filters
are the part people most want back.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

from .services.watch import WatchRule


@dataclass(frozen=True)
class SavedView:
    """A named bundle of filters, and the log they were built against.

    Event Viewer calls these Custom Views. The shape is the one starred logs
    already established: an explicit, named choice by the operator, recorded
    because they asked for it — not a trace of what they happened to be doing.

    What a view holds is **settings and one path**. Never a log line, never a
    match count, never a result. ``source`` is the log the view was saved on so
    that applying it puts you back where the filters make sense; if that path
    has since gone, the filters are applied anyway and the miss is reported,
    because a rotated-away file is not a reason to refuse.
    """

    name: str
    query: str = ""
    severity: str = "all"
    time_window: str = "all"
    custom_start: str = ""
    custom_end: str = ""
    case_sensitive: bool = False
    use_regex: bool = True
    invert_match: bool = False
    include_globs: str = ""
    exclude_globs: str = ""
    #: Absolute path of the log this view was saved against. Empty means the
    #: view is about filters alone and applying it leaves the source as it is.
    source: str = ""
    #: The merged set, when the view was saved on one. Paths only, like every
    #: other field here — a view records what you were looking at, never what
    #: was in it.
    merged: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["SavedView"]:
        """Build a view from stored JSON, or ``None`` if it is not usable.

        Returning None rather than raising is the point: one hand-edited record
        must not cost the operator every other view, nor stop the app starting.
        """

        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None

        types = {field.name: field.type for field in fields(cls)}
        values: Dict[str, Any] = {"name": name.strip()}
        for key, expected in types.items():
            if key == "name" or key not in raw:
                continue
            value = raw[key]
            if expected == "bool" and not isinstance(value, bool):
                continue
            if expected == "str" and not isinstance(value, str):
                continue
            if expected == "tuple[str, ...]":
                # Same rule as SessionState's: one bad element must not cost
                # the whole list, and JSON has no tuples.
                if not isinstance(value, (list, tuple)):
                    continue
                value = tuple(item for item in value if isinstance(item, str))
            values[key] = value
        return cls(**values)

    def summary(self) -> str:
        """One line describing what this view filters to, for the picker."""

        parts: list[str] = []
        if self.query:
            parts.append(f"query {self.query}")
        if self.severity != "all":
            parts.append(self.severity)
        if self.time_window == "range" and self.custom_start:
            parts.append(f"{self.custom_start} → {self.custom_end}")
        elif self.time_window not in {"", "all"}:
            parts.append(self.time_window)
        if self.invert_match:
            parts.append("inverted")
        if not self.use_regex:
            parts.append("literal")
        if self.case_sensitive:
            parts.append("case-sensitive")
        if self.include_globs:
            parts.append(f"include {self.include_globs}")
        if self.merged:
            parts.append(f"{len(self.merged)} merged sources")
        elif self.source:
            parts.append(Path(self.source).name)
        return " · ".join(parts) if parts else "no filters"


@dataclass
class SessionState:
    """Persisted options that should survive restarts."""

    query: str = ""
    severity: str = "all"
    time_window: str = "all"
    custom_start: str = ""
    custom_end: str = ""
    auto_scroll: bool = True
    pretty_rendering: bool = False
    #: Whether `y` may emit an OSC 52 clipboard sequence. A property of the
    #: terminal the operator is on, not of any log: some multiplexers and
    #: hardened terminals render the sequence as garbage, and turning it off
    #: leaves `Ctrl+L` copy mode as the fallback.
    clipboard_osc52: bool = True
    #: Whether the event detail pane is open. A layout preference, so it
    #: survives a restart; the *selected line* does not, because that would
    #: record which log content someone was reading.
    detail_pane: bool = False
    #: Whether the severity timeline is shown. Same argument as the detail
    #: pane: which panes are open is a preference, what was in them is not —
    #: the *selected bucket* is therefore not persisted either.
    timeline: bool = False
    #: Whether repeated lines are collapsed into clusters. A reading
    #: preference; *which* clusters were expanded is not persisted, for the
    #: reason marks are not — a cluster key is derived from log content.
    clustering: bool = False
    # Advanced drawer state
    include_globs: str = ""
    exclude_globs: str = ""
    follow_symlinks: bool = False
    skip_binary: bool = True
    #: Present a rotated log's members as one source. Persisted beside the
    #: other discovery switches, and like them it takes precedence over
    #: `settings.conf` once the operator has touched it.
    group_rotated: bool = True
    case_sensitive: bool = False
    use_regex: bool = True
    invert_match: bool = False
    tree_width: int = 38
    #: Absolute paths the operator starred, as a sorted tuple. Unlike the
    #: source that merely happened to be open, these are chosen explicitly, so
    #: recording them is something the operator asked for.
    starred: tuple[str, ...] = ()
    #: Named filter bundles, sorted by name. Same argument as `starred`: an
    #: explicit choice, and settings only — see :class:`SavedView`.
    views: tuple[SavedView, ...] = ()
    #: Absolute paths in the merged set. Chosen one `x` at a time, so keeping
    #: them is recording a decision rather than a trace of what was read.
    merged: tuple[str, ...] = ()
    #: Watch rules. A pattern is something the operator typed, like a query, so
    #: keeping it is recording their setup and not their reading — which is
    #: exactly the line marks fall on the other side of.
    watch_rules: tuple[WatchRule, ...] = ()

    #: Fields written to disk. Every field on this class — the previous build
    #: persisted only three and dropped every filter on exit.
    #:
    #: The selected source is deliberately not among them. The viewer opens on
    #: the discovery summary rather than resuming and tailing whatever was last
    #: open, so storing the path would record where someone had been reading
    #: without ever being used.
    PERSISTED_FIELDS: ClassVar[tuple[str, ...]] = (
        "query",
        "severity",
        "time_window",
        "custom_start",
        "custom_end",
        "auto_scroll",
        "pretty_rendering",
        "clipboard_osc52",
        "detail_pane",
        "timeline",
        "clustering",
        "include_globs",
        "exclude_globs",
        "follow_symlinks",
        "skip_binary",
        "group_rotated",
        "case_sensitive",
        "use_regex",
        "invert_match",
        "tree_width",
        "starred",
        "views",
        "merged",
        "watch_rules",
    )

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SessionState":
        """Build state from stored JSON, ignoring unknown or mistyped values."""

        types = {field.name: field.type for field in fields(cls)}
        known: Dict[str, Any] = {}
        for name in cls.PERSISTED_FIELDS:
            if name not in raw:
                continue
            value = raw[name]
            expected = types.get(name)
            # Guard against a hand-edited or older state file.
            if expected == "bool" and not isinstance(value, bool):
                continue
            if expected == "int" and not isinstance(value, int):
                continue
            if expected == "str" and not isinstance(value, str):
                continue
            if expected == "tuple[str, ...]":
                # JSON has no tuples; keep only the string entries so one bad
                # element cannot discard the whole list.
                if not isinstance(value, (list, tuple)):
                    continue
                value = tuple(item for item in value if isinstance(item, str))
            if expected in ("tuple[SavedView, ...]", "tuple[WatchRule, ...]"):
                # Same rule one level down: a malformed record is dropped and
                # the rest of the operator's views (or rules) survive it.
                if not isinstance(value, (list, tuple)):
                    continue
                build = (
                    SavedView.from_dict
                    if expected == "tuple[SavedView, ...]"
                    else WatchRule.from_dict
                )
                restored = (build(item) for item in value)
                value = tuple(record for record in restored if record is not None)
            known[name] = value
        return cls(**known)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return {name: data[name] for name in self.PERSISTED_FIELDS}


class StateStore:
    """Tiny JSON backed storage for session preferences."""

    def __init__(self, app_name: str = "clv", root: Path | None = None) -> None:
        cache_root = root or (Path.home() / ".cache" / app_name)
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._state_file = cache_root / "session.json"

    @property
    def path(self) -> Path:
        return self._state_file

    def load(self) -> SessionState:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return SessionState()
        if not isinstance(data, dict):
            return SessionState()
        return SessionState.from_dict(data)

    def save(self, state: SessionState) -> None:
        """Write atomically so a crash mid-write cannot corrupt the file."""

        payload = json.dumps(state.to_dict(), indent=2)
        temp = self._state_file.with_suffix(".json.tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self._state_file)
        except OSError:
            # Losing session state is not worth interrupting the session.
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
