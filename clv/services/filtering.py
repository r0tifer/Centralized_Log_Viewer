"""Filtering of parsed log entries.

Two rules drive the design, both reactions to the previous implementation:

1. The query is matched against the **whole raw line**, never just a parsed
   message field, so searching for a hostname, timestamp or status code works.
2. A line the parser could not structure is never dropped by the query alone.
   It only disappears when the operator asks for something it demonstrably
   lacks (a severity, or a time window), and when that happens the count is
   reported back so the UI can explain the empty pane instead of just showing
   one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .parsing import LogEntry, level_matches


class QueryError(ValueError):
    """Raised when a query string is not a usable regular expression."""


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """An absolute time range. Either bound may be open."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None

    @property
    def bounded(self) -> bool:
        return self.start is not None or self.end is not None

    def contains(self, moment: datetime) -> bool:
        if self.start is not None and moment < self.start:
            return False
        if self.end is not None and moment > self.end:
            return False
        return True


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """Everything the viewer filters on, in one value."""

    query: str = ""
    severity: str = "all"
    window: TimeWindow = field(default_factory=TimeWindow)
    #: None selects "smart case": case-insensitive unless the query has an
    #: uppercase character, matching the convention operators expect from
    #: ripgrep and friends.
    case_sensitive: Optional[bool] = None
    #: When False, a plain-substring query is used instead of a regex.
    regex: bool = True
    #: Invert the query match (show lines that do *not* match).
    invert: bool = False

    @property
    def active(self) -> bool:
        return bool(self.query) or self.severity != "all" or self.window.bounded


@dataclass(frozen=True, slots=True)
class FilterStats:
    """Why lines are missing, so the UI can say something useful."""

    total: int = 0
    matched: int = 0
    hidden_by_query: int = 0
    hidden_by_severity: int = 0
    hidden_by_time: int = 0
    #: Subset of hidden_by_severity whose level was never detected.
    hidden_missing_level: int = 0
    #: Subset of hidden_by_time whose timestamp was never detected.
    hidden_missing_timestamp: int = 0


@dataclass(frozen=True, slots=True)
class FilterResult:
    entries: list[LogEntry]
    stats: FilterStats

    def __len__(self) -> int:
        return len(self.entries)


# --- query compilation ------------------------------------------------------


def compile_query(
    query: str,
    *,
    case_sensitive: Optional[bool] = None,
    regex: bool = True,
) -> Optional[re.Pattern[str]]:
    """Compile *query* into a pattern, applying smart-case when unset.

    Raises:
        QueryError: if *query* is not a valid regular expression.
    """

    if not query:
        return None
    if case_sensitive is None:
        case_sensitive = any(char.isupper() for char in query)
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = query if regex else re.escape(query)
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise QueryError(str(exc)) from exc


def count_matches(entries: Sequence[LogEntry], pattern: Optional[re.Pattern[str]]) -> int:
    """Count entries whose raw text matches *pattern* (all of them if None)."""

    if pattern is None:
        return len(entries)
    return sum(1 for entry in entries if pattern.search(entry.raw))


# --- time window construction ----------------------------------------------

_RELATIVE_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>[smhdw])$")

_UNIT_TO_DELTA = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_relative_window(shortcut: str, *, now: Optional[datetime] = None) -> TimeWindow:
    """Turn ``15m``/``6h``/``7d`` (or ``all``) into an absolute window."""

    reference = now or datetime.now()
    token = shortcut.lower().strip()
    if not token or token == "all":
        return TimeWindow()
    match = _RELATIVE_RE.match(token)
    if not match:
        raise ValueError(f"Unsupported time shortcut {shortcut!r}. Use forms like '15m', '6h', '7d'.")
    amount = int(match.group("amount"))
    delta = timedelta(**{_UNIT_TO_DELTA[match.group("unit")]: amount})
    return TimeWindow(start=reference - delta, end=reference)


def parse_absolute_window(start: str, end: str) -> Optional[TimeWindow]:
    """Build a window from two ISO-ish datetime strings."""

    if not start or not end:
        return None
    try:
        parsed_start = datetime.fromisoformat(start.strip())
        parsed_end = datetime.fromisoformat(end.strip())
    except ValueError:
        return None
    if parsed_end < parsed_start:
        parsed_start, parsed_end = parsed_end, parsed_start
    return TimeWindow(start=parsed_start, end=parsed_end)


def parse_moment(text: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Resolve a single point in time from what an operator typed.

    Two forms, both already understood elsewhere in this module: an ISO-ish
    absolute (``datetime.fromisoformat``, as :func:`parse_absolute_window` uses)
    and a signed relative offset built from the same ``_RELATIVE_RE`` table as
    :func:`parse_relative_window`. ``-15m`` is a quarter hour ago and ``+2h`` is
    two hours ahead; a bare ``15m`` means the past, because "go to 15m" in a log
    is never a request to look forward.

    Returns ``None`` rather than raising: this is prompt input, and the caller
    reports a bad value to whoever typed it.
    """

    token = text.strip()
    if not token:
        return None

    sign = -1
    body = token
    if body[0] in "+-":
        sign = 1 if body[0] == "+" else -1
        body = body[1:]

    match = _RELATIVE_RE.match(body.lower())
    if match:
        amount = int(match.group("amount"))
        delta = timedelta(**{_UNIT_TO_DELTA[match.group("unit")]: amount})
        return (now or datetime.now()) + sign * delta

    try:
        return datetime.fromisoformat(token)
    except ValueError:
        return None


# --- the filter itself ------------------------------------------------------


def _comparable(moment: datetime, reference: Optional[datetime]) -> datetime:
    """Align tz-awareness so aware and naive stamps can be compared.

    Log files mix the two constantly (JSON with offsets beside syslog without).
    Rather than refuse to compare, drop the offset from the aware side.
    """

    if reference is None:
        return moment
    if (moment.tzinfo is None) == (reference.tzinfo is None):
        return moment
    if moment.tzinfo is not None:
        return moment.replace(tzinfo=None)
    return moment


def align_moments(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    """Make two timestamps comparable, whatever their tz-awareness.

    The public form of :func:`_comparable`, for callers that need to *order*
    two moments rather than test one against a window — jumping the cursor to a
    timestamp, for instance. Same rule: drop the offset from the aware side
    rather than refuse to compare.
    """

    return _comparable(left, right), _comparable(right, left)


def filter_entries(entries: Sequence[LogEntry], spec: FilterSpec) -> FilterResult:
    """Apply *spec* to *entries*, reporting why anything was withheld."""

    pattern = compile_query(
        spec.query,
        case_sensitive=spec.case_sensitive,
        regex=spec.regex,
    )
    severity_active = spec.severity != "all"
    window = spec.window

    kept: list[LogEntry] = []
    hidden_by_query = 0
    hidden_by_severity = 0
    hidden_by_time = 0
    missing_level = 0
    missing_timestamp = 0

    for entry in entries:
        if pattern is not None:
            hit = pattern.search(entry.raw) is not None
            if hit == spec.invert:
                hidden_by_query += 1
                continue

        if severity_active and not level_matches(entry.level, spec.severity):
            hidden_by_severity += 1
            if entry.level is None:
                missing_level += 1
            continue

        if window.bounded:
            if entry.timestamp is None:
                hidden_by_time += 1
                missing_timestamp += 1
                continue
            moment = _comparable(entry.timestamp, window.start or window.end)
            if not window.contains(moment):
                hidden_by_time += 1
                continue

        kept.append(entry)

    stats = FilterStats(
        total=len(entries),
        matched=len(kept),
        hidden_by_query=hidden_by_query,
        hidden_by_severity=hidden_by_severity,
        hidden_by_time=hidden_by_time,
        hidden_missing_level=missing_level,
        hidden_missing_timestamp=missing_timestamp,
    )
    return FilterResult(entries=kept, stats=stats)


def describe_empty_result(stats: FilterStats, spec: FilterSpec) -> str:
    """Explain an empty pane in terms of the filter that emptied it."""

    if stats.total == 0:
        return "No log entries in the selected source."

    reasons: list[str] = []
    if stats.hidden_by_query:
        reasons.append(f"{stats.hidden_by_query} filtered out by the query")
    if stats.hidden_by_severity:
        if stats.hidden_missing_level == stats.hidden_by_severity:
            reasons.append(
                f"{stats.hidden_by_severity} have no detected severity "
                f"(nothing in this source declares a level)"
            )
        else:
            reasons.append(f"{stats.hidden_by_severity} outside severity '{spec.severity}'")
    if stats.hidden_by_time:
        if stats.hidden_missing_timestamp == stats.hidden_by_time:
            reasons.append(
                f"{stats.hidden_by_time} have no detected timestamp "
                f"(this source's format carries no date)"
            )
        else:
            reasons.append(f"{stats.hidden_by_time} outside the time window")

    if not reasons:
        return "No log lines match the current filters."
    return "No matches — " + "; ".join(reasons) + "."
