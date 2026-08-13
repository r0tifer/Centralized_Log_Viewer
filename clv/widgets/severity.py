"""The colour each canonical severity is painted in.

Lived in ``clv/app.py`` until the timeline needed it too. A widget may not
import ``clv.app`` — that dependency runs the other way — so the palette moved
here, where the shell and any widget can both read it. It is deliberately *not*
a service: services are UI-free, and a hex colour is nothing but UI.

Kept out of CSS on purpose. These are applied to spans *within* a line, so they
are part of a renderable rather than a rule about layout; the styling rules in
``AGENTS.md`` are about who decides geometry, and this decides none.
"""

from __future__ import annotations

SEVERITY_COLORS: dict[str, str] = {
    "CRITICAL": "#fb7185",
    "ERROR": "#f87171",
    "WARN": "#facc15",
    "NOTICE": "#38bdf8",
    "INFO": "#22c55e",
    "DEBUG": "#a855f7",
    "TRACE": "#94a3b8",
}

#: What a bucket or a cluster with no detected severity is painted in. Muted,
#: so "nothing here declared a level" does not read as a level of its own.
UNKNOWN_COLOR = "#64748b"


__all__ = ["SEVERITY_COLORS", "UNKNOWN_COLOR"]
