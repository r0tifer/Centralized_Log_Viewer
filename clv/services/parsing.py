"""Multi-format log line parsing.

CLV points at whatever files an operator names, so the parser cannot assume a
single layout. Every line is turned into a :class:`LogEntry`; a line that no
format recognises still becomes an entry with ``timestamp``/``level`` unset and
its full text preserved in :attr:`LogEntry.raw`. Filtering always has something
to work with, so searching never silently drops lines it failed to parse.

Continuation lines (stack traces, wrapped payloads) inherit the timestamp and
level of the entry above them, which keeps a traceback attached to the ERROR
that produced it when time or severity filters are active.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

#: How far past "now" an inferred syslog year may land before we roll it back.
_FUTURE_TOLERANCE = timedelta(days=1)


# --- canonical severity vocabulary -----------------------------------------

#: Canonical level names. Everything a format produces is normalised into one
#: of these so the severity control has a stable vocabulary to filter on.
LEVEL_TRACE = "TRACE"
LEVEL_DEBUG = "DEBUG"
LEVEL_INFO = "INFO"
LEVEL_NOTICE = "NOTICE"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"
LEVEL_CRITICAL = "CRITICAL"

_LEVEL_ALIASES: dict[str, str] = {
    "TRACE": LEVEL_TRACE,
    "TRC": LEVEL_TRACE,
    "VERBOSE": LEVEL_TRACE,
    "DEBUG": LEVEL_DEBUG,
    "DBG": LEVEL_DEBUG,
    "INFO": LEVEL_INFO,
    "INF": LEVEL_INFO,
    "INFORMATION": LEVEL_INFO,
    "INFORMATIONAL": LEVEL_INFO,
    "NOTICE": LEVEL_NOTICE,
    "WARN": LEVEL_WARN,
    "WARNING": LEVEL_WARN,
    "WRN": LEVEL_WARN,
    "ERROR": LEVEL_ERROR,
    "ERR": LEVEL_ERROR,
    "EROR": LEVEL_ERROR,
    "SEVERE": LEVEL_ERROR,
    "FAIL": LEVEL_ERROR,
    "FAILURE": LEVEL_ERROR,
    "CRIT": LEVEL_CRITICAL,
    "CRITICAL": LEVEL_CRITICAL,
    "FATAL": LEVEL_CRITICAL,
    "ALERT": LEVEL_CRITICAL,
    "EMERG": LEVEL_CRITICAL,
    "EMERGENCY": LEVEL_CRITICAL,
    "PANIC": LEVEL_CRITICAL,
}

#: Severity buckets exposed by the UI, mapped to the canonical levels they
#: accept. ``error`` deliberately includes CRITICAL so a fatal line is never
#: hidden from someone filtering for errors.
SEVERITY_BUCKETS: dict[str, frozenset[str]] = {
    "all": frozenset(),
    "trace": frozenset({LEVEL_TRACE}),
    "debug": frozenset({LEVEL_DEBUG, LEVEL_TRACE}),
    "info": frozenset({LEVEL_INFO, LEVEL_NOTICE}),
    "warn": frozenset({LEVEL_WARN}),
    "error": frozenset({LEVEL_ERROR, LEVEL_CRITICAL}),
}

#: Syslog numeric priorities (RFC 5424 severity part) to canonical levels.
_SYSLOG_SEVERITY: dict[int, str] = {
    0: LEVEL_CRITICAL,
    1: LEVEL_CRITICAL,
    2: LEVEL_CRITICAL,
    3: LEVEL_ERROR,
    4: LEVEL_WARN,
    5: LEVEL_NOTICE,
    6: LEVEL_INFO,
    7: LEVEL_DEBUG,
}


def normalize_level(raw: object) -> Optional[str]:
    """Map a format-specific level token onto the canonical vocabulary."""

    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return _SYSLOG_SEVERITY.get(raw)
    text = str(raw).strip().strip("[]()<>:").upper()
    if not text:
        return None
    if text.isdigit():
        return _SYSLOG_SEVERITY.get(int(text))
    return _LEVEL_ALIASES.get(text)


def level_matches(level: Optional[str], bucket: str) -> bool:
    """Return True when *level* belongs to the named severity *bucket*."""

    accepted = SEVERITY_BUCKETS.get(bucket.lower())
    if not accepted:  # "all", or an unknown bucket: do not filter
        return True
    if level is None:
        return False
    return level in accepted


# --- entries ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One physical line of a log file, plus whatever structure we recovered."""

    raw: str
    timestamp: Optional[datetime] = None
    level: Optional[str] = None
    message: str = ""
    format_name: str = "raw"
    #: True when timestamp/level were inherited from the preceding entry
    #: because this line is a continuation (stack trace, wrapped payload).
    continuation: bool = False

    @property
    def structured(self) -> bool:
        """True when a format matched this line outright."""
        return self.format_name != "raw"


# --- timestamp helpers ------------------------------------------------------

_ISO_TS_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S,%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S,%f",
    "%Y-%m-%dT%H:%M:%S",
)

_SYSLOG_TS_FORMAT = "%Y %b %d %H:%M:%S"
_CLF_TS_FORMAT = "%d/%b/%Y:%H:%M:%S"


def _parse_iso_timestamp(text: str) -> Optional[datetime]:
    """Parse an ISO-8601-ish timestamp, tolerating commas and trailing zones."""

    candidate = text.strip()
    if not candidate:
        return None
    try:
        # fromisoformat handles offsets and fractional seconds on 3.11+.
        return datetime.fromisoformat(candidate.replace(",", ".").replace("Z", "+00:00"))
    except ValueError:
        pass
    stripped = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", candidate).strip()
    for fmt in _ISO_TS_FORMATS:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return None


def _parse_syslog_timestamp(text: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """RFC 3164 stamps omit the year; infer it without jumping into the future."""

    reference = now or datetime.now()
    collapsed = " ".join(text.split())
    # Bind the year explicitly rather than letting strptime default it: an
    # unqualified "%b %d" is ambiguous, cannot express Feb 29, and is slated to
    # change behaviour in Python 3.15.
    try:
        candidate = datetime.strptime(f"{reference.year} {collapsed}", _SYSLOG_TS_FORMAT)
    except ValueError:
        # Feb 29 in a non-leap reference year: retry against the prior year.
        try:
            candidate = datetime.strptime(
                f"{reference.year - 1} {collapsed}", _SYSLOG_TS_FORMAT
            )
        except ValueError:
            return None
        return candidate
    if candidate - reference > _FUTURE_TOLERANCE:
        # A December stamp read in January belongs to the previous year.
        try:
            candidate = candidate.replace(year=reference.year - 1)
        except ValueError:  # Feb 29 -> prior year is not a leap year
            return candidate
    return candidate


def _parse_clf_timestamp(text: str) -> Optional[datetime]:
    """Common Log Format: ``07/Aug/2026:09:25:01 +0000``."""

    candidate = text.strip()
    zone = ""
    match = re.search(r"\s([+-]\d{4})$", candidate)
    if match:
        zone = match.group(1)
        candidate = candidate[: match.start()].strip()
    try:
        parsed = datetime.strptime(candidate, _CLF_TS_FORMAT)
    except ValueError:
        return None
    if zone:
        try:
            return datetime.strptime(f"{candidate} {zone}", f"{_CLF_TS_FORMAT} %z")
        except ValueError:
            return parsed
    return parsed


# --- format matchers --------------------------------------------------------

_LEVEL_TOKEN = "|".join(sorted(_LEVEL_ALIASES, key=len, reverse=True))

# "2026-08-07 09:25:01 - WARNING - disk almost full"  (Python logging default)
_RE_PYTHON_LOGGING = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    r"\s+-\s+(?P<level>\w+)\s+-\s+(?P<msg>.*)$"
)

# "2026-08-07T09:25:01Z ERROR msg" / "[2026-08-07 09:25:01] [error] msg"
_RE_ISO_LEVEL = re.compile(
    r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?"
    r"[\s:|-]+\[?(?P<level>" + _LEVEL_TOKEN + r")\]?[\s:|-]*(?P<msg>.*)$",
    re.IGNORECASE,
)

# ISO timestamp with no recognisable level token.
_RE_ISO_PLAIN = re.compile(
    r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?"
    r"\s*(?P<msg>.*)$"
)

# "Aug  7 09:25:01 myhost CRON[12345]: (root) CMD (...)"
_RE_SYSLOG_BSD = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s{1,2}\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<tag>[^\s:\[]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$"
)

# "<134>1 2026-08-07T09:25:01Z host app 123 - - msg"
_RE_SYSLOG_5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<ver>\d)\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+"
    r"(?P<app>\S+)\s+(?P<pid>\S+)\s+(?P<msgid>\S+)\s+(?P<sd>\S+|\[.*?\])\s*(?P<msg>.*)$"
)

# '10.0.0.1 - - [07/Aug/2026:09:25:01 +0000] "GET / HTTP/1.1" 500 123'
_RE_CLF = re.compile(
    r"^(?P<host>\S+)\s+(?P<ident>\S+)\s+(?P<user>\S+)\s+"
    r"\[(?P<ts>[^\]]+)\]\s+"
    r'"(?P<request>[^"]*)"\s+(?P<status>\d{3})\s+(?P<size>\S+)'
)

_JSON_TS_KEYS = ("timestamp", "@timestamp", "time", "ts", "asctime", "eventTime", "date")
_JSON_LEVEL_KEYS = ("level", "levelname", "severity", "lvl", "loglevel", "log_level", "priority")
_JSON_MSG_KEYS = ("message", "msg", "event", "text", "log")

# Cheap last-resort scan for a bracketed/uppercase level near the start of a line.
_RE_BARE_LEVEL = re.compile(r"[\[\(<|\s]?\b(" + _LEVEL_TOKEN + r")\b[\]\)>|:\s]")


def _status_to_level(status: str) -> Optional[str]:
    """Access logs carry no level; derive one from the HTTP status class."""

    if not status.isdigit():
        return None
    code = int(status)
    if code >= 500:
        return LEVEL_ERROR
    if code >= 400:
        return LEVEL_WARN
    return LEVEL_INFO


def _parse_json(line: str) -> Optional[LogEntry]:
    try:
        payload = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None

    timestamp = None
    for key in _JSON_TS_KEYS:
        if key in payload:
            value = payload[key]
            if isinstance(value, (int, float)):
                try:
                    timestamp = datetime.fromtimestamp(value)
                except (OverflowError, OSError, ValueError):
                    timestamp = None
            else:
                timestamp = _parse_iso_timestamp(str(value))
            if timestamp is not None:
                break

    level = None
    for key in _JSON_LEVEL_KEYS:
        if key in payload:
            level = normalize_level(payload[key])
            if level is not None:
                break

    message = ""
    for key in _JSON_MSG_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            message = value
            break
    if not message:
        message = line

    return LogEntry(
        raw=line,
        timestamp=timestamp,
        level=level,
        message=message,
        format_name="json",
    )


def _parse_structured(line: str, *, now: Optional[datetime] = None) -> Optional[LogEntry]:
    """Try each known format, dispatching on cheap prefix checks first."""

    stripped = line.lstrip()
    if not stripped:
        return None

    first = stripped[0]

    if first == "{":
        entry = _parse_json(stripped)
        if entry is not None:
            return replace(entry, raw=line)

    if first == "<" and len(stripped) > 1 and stripped[1].isdigit():
        match = _RE_SYSLOG_5424.match(stripped)
        if match:
            priority = int(match.group("pri"))
            return LogEntry(
                raw=line,
                timestamp=_parse_iso_timestamp(match.group("ts")),
                level=_SYSLOG_SEVERITY.get(priority % 8),
                message=match.group("msg"),
                format_name="syslog-5424",
            )

    if first.isdigit() and len(stripped) > 10 and stripped[4:5] == "-":
        match = _RE_PYTHON_LOGGING.match(stripped)
        if match:
            level = normalize_level(match.group("level"))
            if level is not None:
                return LogEntry(
                    raw=line,
                    timestamp=_parse_iso_timestamp(match.group("ts")),
                    level=level,
                    message=match.group("msg"),
                    format_name="python-logging",
                )

    if first == "[" or (first.isdigit() and stripped[4:5] == "-"):
        match = _RE_ISO_LEVEL.match(stripped)
        if match:
            return LogEntry(
                raw=line,
                timestamp=_parse_iso_timestamp(match.group("ts")),
                level=normalize_level(match.group("level")),
                message=match.group("msg"),
                format_name="iso-level",
            )
        match = _RE_ISO_PLAIN.match(stripped)
        if match:
            timestamp = _parse_iso_timestamp(match.group("ts"))
            if timestamp is not None:
                message = match.group("msg")
                return LogEntry(
                    raw=line,
                    timestamp=timestamp,
                    level=_scan_level(message),
                    message=message,
                    format_name="iso",
                )

    if first.isalpha():
        match = _RE_SYSLOG_BSD.match(stripped)
        if match:
            message = match.group("msg")
            return LogEntry(
                raw=line,
                timestamp=_parse_syslog_timestamp(match.group("ts"), now=now),
                level=_scan_level(message),
                message=message,
                format_name="syslog",
            )

    match = _RE_CLF.match(stripped)
    if match:
        return LogEntry(
            raw=line,
            timestamp=_parse_clf_timestamp(match.group("ts")),
            level=_status_to_level(match.group("status")),
            message=match.group("request"),
            format_name="access-log",
        )

    return None


def _scan_level(text: str, *, window: int = 80) -> Optional[str]:
    """Best-effort level detection for formats that don't declare one.

    Only the head of the line is scanned, and only delimited tokens count, to
    keep an ``ERROR`` mentioned mid-sentence from mislabelling the entry.
    """

    match = _RE_BARE_LEVEL.search(text[:window])
    if not match:
        return None
    return normalize_level(match.group(1))


def parse_line(line: str, *, now: Optional[datetime] = None) -> LogEntry:
    """Parse one line, always returning an entry (``raw`` format on no match)."""

    entry = _parse_structured(line, now=now)
    if entry is not None:
        return entry
    return LogEntry(raw=line, message=line, format_name="raw", level=_scan_level(line))


def parse_lines(
    lines: Iterable[str],
    *,
    now: Optional[datetime] = None,
    carry_forward: bool = True,
) -> list[LogEntry]:
    """Parse a block of lines, letting continuations inherit their parent entry.

    A line no format recognised, following a line that one did, is treated as a
    continuation: it borrows the timestamp and level above it so a stack trace
    stays with the ERROR it belongs to under time and severity filters.
    """

    entries: list[LogEntry] = []
    last_timestamp: Optional[datetime] = None
    last_level: Optional[str] = None

    for line in lines:
        entry = parse_line(line, now=now)
        if entry.structured:
            if entry.timestamp is not None:
                last_timestamp = entry.timestamp
            if entry.level is not None:
                last_level = entry.level
        elif carry_forward and (last_timestamp is not None or last_level is not None):
            entry = replace(
                entry,
                timestamp=last_timestamp,
                level=entry.level if entry.level is not None else last_level,
                continuation=True,
            )
        entries.append(entry)

    return entries


class LogParser:
    """Stateful parser that preserves carry-forward across streamed appends.

    The app parses a file once up front and then only parses newly tailed
    lines; keeping the trailing timestamp/level here means a traceback split
    across two polls still inherits correctly.
    """

    def __init__(self, *, carry_forward: bool = True) -> None:
        self._carry_forward = carry_forward
        self._last_timestamp: Optional[datetime] = None
        self._last_level: Optional[str] = None

    def reset(self) -> None:
        """Forget the trailing entry; call when switching source or reloading."""
        self._last_timestamp = None
        self._last_level = None

    def feed(self, lines: Sequence[str], *, now: Optional[datetime] = None) -> list[LogEntry]:
        """Parse *lines*, carrying structure forward from previous calls."""
        entries: list[LogEntry] = []
        for line in lines:
            entry = parse_line(line, now=now)
            if entry.structured:
                if entry.timestamp is not None:
                    self._last_timestamp = entry.timestamp
                if entry.level is not None:
                    self._last_level = entry.level
            elif self._carry_forward and (
                self._last_timestamp is not None or self._last_level is not None
            ):
                entry = replace(
                    entry,
                    timestamp=self._last_timestamp,
                    level=entry.level if entry.level is not None else self._last_level,
                    continuation=True,
                )
            entries.append(entry)
        return entries
