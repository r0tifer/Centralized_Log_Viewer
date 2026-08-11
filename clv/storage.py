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
from typing import Any, ClassVar, Dict


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
    # Advanced drawer state
    include_globs: str = ""
    exclude_globs: str = ""
    follow_symlinks: bool = False
    skip_binary: bool = True
    case_sensitive: bool = False
    use_regex: bool = True
    invert_match: bool = False
    tree_width: int = 38
    #: Absolute paths the operator starred, as a sorted tuple. Unlike the
    #: source that merely happened to be open, these are chosen explicitly, so
    #: recording them is something the operator asked for.
    starred: tuple[str, ...] = ()

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
        "include_globs",
        "exclude_globs",
        "follow_symlinks",
        "skip_binary",
        "case_sensitive",
        "use_regex",
        "invert_match",
        "tree_width",
        "starred",
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
