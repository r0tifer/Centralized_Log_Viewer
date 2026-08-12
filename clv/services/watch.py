"""Watch rules — the patterns worth being told about.

Tailing means waiting for something. Until now that meant reading every line
yourself, which is exactly the job a computer should be doing. A watch rule is
a saved pattern plus what to do when a line matches it: highlight it, say
something, or both.

Three pieces, all UI-free:

* :class:`WatchRule` — a name, a pattern in Item 8's query syntax, an action,
  and whether it is on. Persisted in ``SessionState``, because a pattern is
  operator input and not log content.
* :class:`WatchIndex` — evaluates entries and **remembers the answer**, keyed
  by source and line content the same way :mod:`clv.services.marks` keys a
  bookmark. This is what lets the pane redraw a highlighted line without
  re-running every rule over every visible row: a re-render is a lookup, and
  only lines that have never been seen before cost an evaluation.
* :class:`WatchNotifier` — collects hits and hands back at most one message per
  rule per window. A rule that matches every line is the failure mode that
  gets features like this switched off, so coalescing is not a refinement here;
  it is the thing that makes the feature usable.

Time is injected rather than read, so the rate limiting is testable without a
timer, and the app drives both from the poll it already runs — no second clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from .filtering import compile_query
from .marks import mark_key
from .parsing import LogEntry
from .query import MATCH_HIT, QueryError, match_terms, parse_query

#: What a rule does when it matches.
ACTION_HIGHLIGHT = "highlight"
ACTION_NOTIFY = "notify"
ACTION_BOTH = "both"
ACTIONS: tuple[str, ...] = (ACTION_HIGHLIGHT, ACTION_NOTIFY, ACTION_BOTH)

#: Default seconds between notifications for one rule. Also the floor the app
#: clamps ``watch_rate_limit`` to; see ``config.py``.
DEFAULT_RATE_LIMIT = 60


@dataclass(frozen=True)
class WatchRule:
    """A named pattern and what to do about it."""

    name: str
    pattern: str = ""
    action: str = ACTION_BOTH
    enabled: bool = True

    @property
    def highlights(self) -> bool:
        return self.action in (ACTION_HIGHLIGHT, ACTION_BOTH)

    @property
    def notifies(self) -> bool:
        return self.action in (ACTION_NOTIFY, ACTION_BOTH)

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["WatchRule"]:
        """Build a rule from stored JSON, or ``None`` if it is not usable.

        Same contract as :class:`~clv.storage.SavedView`: one hand-edited record
        must not cost the operator the rest of their rules, and must never stop
        the app starting.
        """

        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        pattern = raw.get("pattern")
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(pattern, str) or not pattern.strip():
            # A rule with no pattern would match nothing and could never be
            # fixed from the UI without looking like a rule that was working.
            return None
        action = raw.get("action")
        enabled = raw.get("enabled")
        return cls(
            name=name.strip(),
            pattern=pattern,
            action=action if action in ACTIONS else ACTION_BOTH,
            enabled=enabled if isinstance(enabled, bool) else True,
        )


def validate_pattern(pattern: str, known_fields: Iterable[str] = ()) -> Optional[str]:
    """Why *pattern* is unusable, or ``None`` when it is fine.

    Used by the rules dialog so a bad pattern is reported where it was typed
    rather than swallowed at match time.
    """

    if not pattern.strip():
        return "Enter a pattern."
    try:
        parsed = parse_query(pattern, known_fields)
        compile_query(parsed.text)
    except QueryError as exc:
        return str(exc)
    return None


class _CompiledRule:
    """A rule with its pattern prepared, or marked unusable."""

    __slots__ = ("rule", "terms", "pattern", "broken")

    def __init__(self, rule: WatchRule, known_fields: Iterable[str]) -> None:
        self.rule = rule
        self.terms: tuple = ()
        self.pattern: Optional[re.Pattern[str]] = None
        self.broken = False
        try:
            parsed = parse_query(rule.pattern, known_fields)
            self.terms = parsed.terms
            self.pattern = compile_query(parsed.text)
        except QueryError:
            # A rule nobody can fix mid-session must not throw on every line.
            # It simply never matches; the dialog is where it gets repaired.
            self.broken = True

    def matches(self, entry: LogEntry) -> bool:
        if self.broken:
            return False
        if self.terms and match_terms(entry, self.terms) != MATCH_HIT:
            return False
        if self.pattern is not None:
            return self.pattern.search(entry.raw) is not None
        return bool(self.terms)


class WatchIndex:
    """Which rules each line hit, evaluated once per line.

    The cache is what makes the item's "rules evaluate on new lines, not on
    every re-render" true rather than aspirational: :meth:`hits` is a dict
    lookup, and :meth:`evaluate` skips any line it has already answered for.
    Keyed by source plus content digest — the key marks use — so a line that a
    filter hid and later shows again is still known, and an evicted line's
    entry is dropped by :meth:`prune`.
    """

    __slots__ = ("_rules", "_hits", "_known_fields", "evaluations")

    def __init__(
        self,
        rules: Sequence[WatchRule] = (),
        known_fields: Iterable[str] = (),
    ) -> None:
        self._known_fields = frozenset(known_fields)
        self._rules: list[_CompiledRule] = []
        self._hits: Dict[str, tuple[str, ...]] = {}
        #: Lines evaluated since the last reset. Exists so a test can assert
        #: directly that re-rendering does not re-evaluate.
        self.evaluations = 0
        self.set_rules(rules)

    @property
    def active(self) -> bool:
        """True when any enabled rule could match. The app's fast path."""

        return bool(self._rules)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def set_rules(
        self, rules: Sequence[WatchRule], known_fields: Iterable[str] | None = None
    ) -> None:
        """Replace the rule set and forget every cached answer."""

        if known_fields is not None:
            self._known_fields = frozenset(known_fields)
        self._rules = [
            _CompiledRule(rule, self._known_fields) for rule in rules if rule.enabled
        ]
        self.reset()

    def reset(self) -> None:
        self._hits.clear()
        self.evaluations = 0

    def evaluate(
        self, source: Optional[Path], entries: Iterable[LogEntry]
    ) -> list[tuple[LogEntry, tuple[str, ...]]]:
        """Answer for every entry in *entries*; return the ones that hit.

        Two different things are counted here, and keeping them apart is the
        point. **Matching** happens once per distinct line and is cached, so
        redrawing costs nothing. **Occurrences** are whatever the caller passed
        in: fifty identical "connection refused" lines are fifty events even
        though they are one question, and a notifier told otherwise would
        report one. Callers therefore hand in only what newly arrived —
        `_poll_watch` does; the silent pass over a primed buffer ignores the
        return value entirely.
        """

        if not self._rules:
            return []
        fired: list[tuple[LogEntry, tuple[str, ...]]] = []
        for entry in entries:
            key = mark_key(source, entry)
            names = self._hits.get(key)
            if names is None:
                self.evaluations += 1
                names = tuple(
                    compiled.rule.name
                    for compiled in self._rules
                    if compiled.matches(entry)
                )
                self._hits[key] = names
            if names:
                fired.append((entry, names))
        return fired

    def hits(self, source: Optional[Path], entry: LogEntry) -> tuple[str, ...]:
        """Rules this line hit. Empty for a line never evaluated."""

        return self._hits.get(mark_key(source, entry), ())

    def watched(self, source: Optional[Path], entry: LogEntry) -> bool:
        return bool(self.hits(source, entry))

    def prune(self, source: Optional[Path], entries: Iterable[LogEntry]) -> None:
        """Drop cached answers for lines no longer in the buffer."""

        self.retain({mark_key(source, entry) for entry in entries})

    def retain(self, live: set[str]) -> None:
        """Keep only the cached answers for *live* keys.

        The general form, for a pane showing several sources at once, where
        the caller has already keyed each entry against the source it actually
        came from. Unlike marks this needs no per-source scoping: an answer is
        only ever worth keeping while the line it is about is on screen.
        """

        if not self._hits:
            return
        self._hits = {key: value for key, value in self._hits.items() if key in live}


class WatchNotifier:
    """Coalesces hits into at most one message per rule per window.

    A rule matching every line must not produce a notification storm — that is
    the behaviour that makes people turn a feature like this off. The first hit
    for a rule is reported immediately, because the first one is the news;
    everything inside the window after it is accumulated and reported as a
    count when the window closes.
    """

    __slots__ = ("window", "_last_sent", "_pending")

    def __init__(self, window: float = DEFAULT_RATE_LIMIT) -> None:
        self.window = window
        self._last_sent: Dict[str, float] = {}
        self._pending: Dict[str, int] = {}

    def reset(self) -> None:
        self._last_sent.clear()
        self._pending.clear()

    def record(self, names: Iterable[str], *, count: int = 1) -> None:
        for name in names:
            self._pending[name] = self._pending.get(name, 0) + count

    def due(self, now: float) -> list[str]:
        """Messages to show at *now*, emptying whatever they report."""

        messages: list[str] = []
        for name in sorted(self._pending):
            count = self._pending[name]
            if not count:
                continue
            last = self._last_sent.get(name)
            if last is not None and now - last < self.window:
                continue
            self._last_sent[name] = now
            self._pending[name] = 0
            messages.append(self.describe(name, count))
        # Rules that reported are left at zero rather than deleted, so their
        # window keeps being honoured for as long as the rule exists.
        return messages

    def describe(self, name: str, count: int) -> str:
        if count == 1:
            return f"Watch '{name}' matched a line."
        return f"Watch '{name}' matched {count} lines."


def notifying(names: Sequence[str], rules: Sequence[WatchRule]) -> tuple[str, ...]:
    """Of *names*, the rules whose action includes notifying.

    A highlight-only rule still marks its lines; it just says nothing.
    """

    wanted = {rule.name for rule in rules if rule.notifies}
    return tuple(name for name in names if name in wanted)


def describe_rules(rules: Sequence[WatchRule]) -> str:
    """Status line for the Advanced drawer."""

    if not rules:
        return "Watch rules: none"
    enabled = sum(1 for rule in rules if rule.enabled)
    return f"Watch rules: {enabled} active of {len(rules)}"


def toggled(rules: Sequence[WatchRule], name: str, enabled: bool) -> tuple[WatchRule, ...]:
    """*rules* with the named one switched on or off."""

    return tuple(
        replace(rule, enabled=enabled) if rule.name == name else rule for rule in rules
    )


__all__ = [
    "ACTIONS",
    "ACTION_BOTH",
    "ACTION_HIGHLIGHT",
    "ACTION_NOTIFY",
    "DEFAULT_RATE_LIMIT",
    "WatchIndex",
    "WatchNotifier",
    "WatchRule",
    "describe_rules",
    "notifying",
    "toggled",
    "validate_pattern",
]
