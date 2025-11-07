from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict


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
    selected_source: str = ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SessionState":
        known: Dict[str, Any] = {}
        for field in (
            "query",
            "severity",
            "time_window",
            "custom_start",
            "custom_end",
            "auto_scroll",
            "pretty_rendering",
            "selected_source",
        ):
            if field in raw:
                known[field] = raw[field]
        return cls(**known)


class StateStore:
    """Tiny JSON backed storage for session preferences."""

    def __init__(self, app_name: str = "clv") -> None:
        cache_root = Path.home() / ".cache" / app_name
        cache_root.mkdir(parents=True, exist_ok=True)
        self._state_file = cache_root / "session.json"

    def load(self) -> SessionState:
        if not self._state_file.exists():
            return SessionState()
        try:
            data = json.loads(self._state_file.read_text())
        except json.JSONDecodeError:
            return SessionState()
        return SessionState.from_dict(data)

    def save(self, state: SessionState) -> None:
        payload = json.dumps(asdict(state), indent=2)
        self._state_file.write_text(payload)
