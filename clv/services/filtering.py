"""Filtering of parsed log entries.

Two rules drive the design, both reactions to the previous implementation:

1. The query is matched against the **whole raw line**, never just a parsed
   message field, so searching for a hostname, timestamp or status code works.
2. A line the parser could not structure is never dropped by the query alone.
   It only disappears when the operator asks for something it demonstrably
   lacks (a severity, or a time window), and when that happens the count is
   reported back so the UI can explain the empty pane instead of just showing
   one.

Since Item 8 the query may also carry **field terms** (``host:web01``,
``status>=500``). The grammar lives in :mod:`clv.services.query`; this module
splits the query once, applies the terms, and hands whatever is left to
``compile_query`` exactly as the whole string used to be. Rule 2 extends to
them: an entry that lacks a referenced field is hidden and counted in
``hidden_missing_field``, never dropped without a reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .parsing import LogEntry, level_matches
from .query import (
    MATCH_HIT,
    MATCH_MISSING_FIELD,
    FieldTerm,
    QueryError,
    entry_matches,
    match_terms,
    parse_query,
)


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
    #: When False, a plain-substring query is used instead of a regex. Field
    #: terms are unaffected: they are never a regex, so there is nothing to
    #: escape. Only the free-text remainder is taken literally.
    regex: bool = True
    #: Invert the **free-text** match (show lines whose text does not match).
    #: Field terms are always positive — `!=`, `<` and `>` are the per-term
    #: negation, and there is no honest way to invert "this entry has no such
    #: field": the entry would have to count as both hidden and shown.
    invert: bool = False
    #: Field names that may appear as query terms: the parser's normalised
    #: vocabulary plus whatever the buffer actually carries. Anything else in
    #: the query stays free text, which is what keeps `sshd:` a regex. Passed
    #: in rather than derived from the entries so the same spec always parses
    #: the same way, whatever list it is applied to.
    known_fields: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return bool(self.query) or self.severity != "all" or self.window.bounded

    def parse(self):
        """Split :attr:`query` into field terms and free text.

        Raises:
            QueryError: if a term is malformed.
        """

        return parse_query(self.query, self.known_fields)


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
    #: Hidden because a field the query asked about is not on the entry at all.
    #: Kept apart from hidden_by_query, which means "has the field, does not
    #: match": one is a filter doing its job, the other is a question this
    #: source cannot answer, and only the second needs explaining.
    hidden_missing_field: int = 0


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


def count_matches(
    entries: Sequence[LogEntry],
    pattern: Optional[re.Pattern[str]],
    terms: Sequence[FieldTerm] = (),
) -> int:
    """Count entries matching *pattern* and every term in *terms*.

    With neither, every entry counts. The term half goes through the same
    predicate :func:`filter_entries` uses, so the hit counter in the query bar
    cannot report a number the pane disagrees with.
    """

    if pattern is None and not terms:
        return len(entries)
    return sum(1 for entry in entries if entry_matches(entry, terms, pattern))


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


def sortable_moment(moment: datetime, *, naive: bool) -> datetime:
    """One comparable form for a timestamp, for ordering or bucketing a *set*.

    :func:`align_moments` answers this pairwise, which is all a comparison
    needs. A k-way merge and a timeline both need a single key across every
    entry at once, so the decision is made once for the set: when any member is
    naive the offsets are dropped — the same rule, applied in bulk — and when
    every member is aware they are kept, which orders two time zones correctly
    rather than pretending both are local.
    """

    if naive and moment.tzinfo is not None:
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

    parsed = spec.parse()
    terms = parsed.terms
    pattern = compile_query(
        parsed.text,
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
    missing_field = 0

    for entry in entries:
        # Terms first: an entry that cannot answer the question is a different
        # kind of absent from one that answers it wrongly, and the counters
        # have to keep them apart for describe_empty_result to be useful.
        if terms:
            outcome = match_terms(entry, terms)
            if outcome == MATCH_MISSING_FIELD:
                missing_field += 1
                continue
            if outcome != MATCH_HIT:
                hidden_by_query += 1
                continue

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
        hidden_missing_field=missing_field,
    )
    return FilterResult(entries=kept, stats=stats)


def describe_empty_result(
    stats: FilterStats, spec: FilterSpec, *, unreachable: str = ""
) -> str:
    """Explain an empty pane in terms of the filter that emptied it.

    *unreachable* is the reason the source cannot be reached, when it cannot.
    It takes precedence over every filter explanation below, because a filter
    that hid nothing is not why the pane is empty — and "no entries in the
    selected source" is a claim about the source that CLV is in no position to
    make when it cannot see it. An unreachable source reported as an empty one
    is the failure this parameter exists to prevent.
    """

    if unreachable:
        return unreachable

    if stats.total == 0:
        return "No log entries in the selected source."

    reasons: list[str] = []
    if stats.hidden_by_query:
        reasons.append(f"{stats.hidden_by_query} filtered out by the query")
    if stats.hidden_missing_field:
        reasons.append(
            f"{stats.hidden_missing_field} carry no {_field_list(spec)} "
            f"(this source's format does not report it)"
        )
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


def _field_list(spec: FilterSpec) -> str:
    """Name the fields the query asked about, for the missing-field reason.

    Re-parsed rather than carried on ``FilterStats``: the spec already knows
    everything needed, and a stats object that held query fragments would be a
    second place for the two to drift apart.
    """

    try:
        keys = spec.parse().field_keys
    except QueryError:  # pragma: no cover - the caller had a result to describe
        keys = ()
    if not keys:
        return "field the query asked for"
    if len(keys) == 1:
        return f"'{keys[0]}' field"
    return "'" + "', '".join(keys) + "' fields"
