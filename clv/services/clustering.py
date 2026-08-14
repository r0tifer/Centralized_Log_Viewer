"""Repeat clustering: the same event, said a hundred times, shown once.

A tail is mostly repetition. Five hundred `connection refused` lines differing
only in a port number are one fact and five hundred rows, and scrolling past
them is how the line that mattered gets missed. Clustering normalises the
volatile tokens out of a line, groups the lines that then look identical, and
lets the pane draw them as one row with a count.

**Collapsing is a display transform, never a filter.** Every line is still
there, still selectable, still exportable; a collapsed cluster expands in place
and gives back exactly the lines that went into it, in order and byte-identical.
That is the "never silently lose a line" rule applied to rendering, and this
module is useless without it — hence
``test_expanding_a_cluster_gives_back_every_original_line``.

The shape
---------

A line's *shape* is its message with volatile tokens replaced by placeholders,
plus two things that are not part of the text:

* its **level**, so a WARN and an ERROR that read alike stay apart, and
* its **source**, so a merged view does not fold two logs' lines together and
  leave the source column with nothing honest to show.

Field *values* are deliberately not in the shape. A request ID differing between
two lines is exactly what must not split a cluster; that is the whole point.

The rules are applied in this order, and the order matters — each one runs on
what the previous left behind, and the placeholders they write contain no digits
so a later numeric rule cannot chew them up:

1. quoted strings  2. timestamps  3. UUIDs  4. IPv6  5. IPv4 (and `:port`)
6. hex (``0x…`` and long bare runs)  7. paths  8. floats  9. integers

Not configurable from ``settings.conf``, and there is no rules DSL for the
operator: that is a stated non-goal and it stands.

**Reversed 2026-08-14, narrowly.** ``PLUGIN_TODO.md`` Phase 10 adds a
``ClusterRule`` plugin interface, so the rule list above stops being closed.
Recorded here rather than quietly outgrown, because the original objection was
right and survives intact: it was to *the operator* hand-writing regex rules in
a config file, where a typo is a silently mis-clustered pane and there is no
review, no test and no way to tell a bad rule from a bad log. A plugin author
writing Python against a reviewed interface is a different party making a
different promise. The rules stay unconfigurable from ``settings.conf``, and
nothing here becomes a text format.

The lookback
------------

Lines join the most recent cluster with their shape **only when that cluster's
last member is within ``lookback`` entries**. Two consequences, both wanted: a
cluster cannot silently span an entire session and swallow an event from an hour
ago, and cost stays linear in the buffer. A cluster renders at its *first*
member's position, so reading order is preserved rather than rearranged.

Cost
----

Clustering runs on the filtered set, and the filtered set is rebuilt on every
keystroke in the query box. Shaping five thousand lines costs about 115 ms,
which is far too much to pay per character — so :func:`normalise` is memoised
and every render after the first is dictionary lookups, at about 6 ms. A tailed
line costs one :meth:`ClusterStream.add`, not a recompute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from typing import Iterable, Optional, Sequence, Union

from .parsing import LogEntry, level_rank
from .session import ORIGIN_FIELD

#: Default entries of lookback. Overridable through ``cluster_lookback`` in
#: ``settings.conf``; see :mod:`clv.services.config`.
DEFAULT_LOOKBACK = 200

#: Placeholder each rule writes. Kept digit-free on purpose: rules 8 and 9 run
#: last and would otherwise rewrite what the earlier ones produced.
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "quoted strings",
        re.compile(r"\"[^\"]*\"|'[^']*'"),
        "<str>",
    ),
    (
        "timestamps",
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
            r"|\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}(?:\s[+-]\d{4})?"
            r"|\b\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
            r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
        ),
        "<ts>",
    ),
    (
        "UUIDs",
        re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        "<uuid>",
    ),
    (
        "IPv6 addresses",
        # Deliberately loose: anything with two or more colons among hex groups.
        # A false positive costs a slightly coarser cluster; a false negative
        # costs a cluster per address, which is the failure that matters.
        re.compile(r"\b(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\b"),
        "<ipv6>",
    ),
    (
        "IPv4 addresses",
        re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
        "<ip>",
    ),
    (
        "hex",
        re.compile(r"\b0[xX][0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b"),
        "<hex>",
    ),
    (
        "paths",
        # Request targets count: an access log's varying part is its path, and
        # `/orders/8821` and `/orders/8822` are the same event.
        re.compile(r"(?<![\w/])/[\w.\-+@%/]+"),
        "<path>",
    ),
    # No trailing \b on either: a number is just as volatile with a unit stuck
    # to it, and `1.25s` / `125ms` are the two commonest ways a duration is
    # written. A *leading* \b is still required, so `sha256` and `utf8` keep
    # their digits and stay one word.
    (
        "floats",
        re.compile(r"\b\d+\.\d+"),
        "<float>",
    ),
    (
        "integers",
        re.compile(r"\b\d+"),
        "<int>",
    ),
)

#: The rule names, in order, for documentation and for the tests that walk them.
RULE_NAMES: tuple[str, ...] = tuple(name for name, _pattern, _placeholder in _RULES)

_WHITESPACE = re.compile(r"\s+")

#: Distinct messages whose shape is remembered.
#:
#: Clustering re-runs on every render, and a render happens on every keystroke
#: in the query box — so the same five thousand lines get normalised again for
#: every character typed. Measured at ~115 ms per five thousand lines, that is
#: the difference between a filter that keeps up with typing and one that does
#: not. The cache turns every render after the first into dictionary lookups.
#:
#: In memory only, like `WatchIndex` and `MarkSet`: nothing here is ever
#: written anywhere. The bound is what keeps a long tail of distinct lines from
#: growing it without limit, and it holds *shapes*, which are shorter than the
#: lines already in the buffer.
_SHAPE_CACHE_SIZE = 8_192


@lru_cache(maxsize=_SHAPE_CACHE_SIZE)
def normalise(text: str) -> str:
    """Replace every volatile token in *text*, in the documented order."""

    for _name, pattern, placeholder in _RULES:
        text = pattern.sub(placeholder, text)
    return _WHITESPACE.sub(" ", text).strip()


def shape_of(entry: LogEntry) -> str:
    """The key two entries must share to cluster together."""

    body = entry.message or entry.raw
    origin = entry.fields.get(ORIGIN_FIELD, "")
    return f"{origin}\0{entry.level or ''}\0{normalise(body)}"


class Cluster:
    """Several entries that read as the same event.

    Mutable, unlike most of what the services hand around: a cluster grows as
    a log tails, and rebuilding it per arrival would make the tail path cost
    the cluster's size rather than the arrival's.
    """

    __slots__ = ("shape", "entries")

    def __init__(self, shape: str, entries: list[LogEntry]) -> None:
        self.shape = shape
        self.entries = entries

    def add(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def representative(self) -> LogEntry:
        """The first line of the cluster — what the collapsed row shows.

        The first rather than the newest: it is the one already on screen in
        reading order, and a row whose text changed as the count rose would be
        unreadable.
        """

        return self.entries[0]

    @property
    def first(self) -> Optional[datetime]:
        for entry in self.entries:
            if entry.timestamp is not None:
                return entry.timestamp
        return None

    @property
    def last(self) -> Optional[datetime]:
        for entry in reversed(self.entries):
            if entry.timestamp is not None:
                return entry.timestamp
        return None

    @property
    def level(self) -> Optional[str]:
        """The worst level in the cluster.

        The shape already carries the level, so in practice every member shares
        one — but a cluster reports what it holds rather than what its key
        implies, so this stays correct if the key ever widens.
        """

        best: Optional[LogEntry] = None
        for entry in self.entries:
            if best is None or level_rank(entry.level) > level_rank(best.level):
                best = entry
        return best.level if best is not None else None

    def key(self) -> str:
        """Stable identity across re-renders, for remembering what is expanded.

        Content-keyed, exactly as ``marks.mark_key`` is and for the same reason:
        the buffer is a bounded deque, so anything positional starts pointing at
        a different cluster as lines are evicted.
        """

        return f"{self.shape}\0{self.representative.raw}"

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Cluster ×{self.count} {self.representative.raw[:40]!r}>"


#: What a clustered pane renders: bare entries and collapsed groups, in order.
Row = Union[LogEntry, Cluster]


@dataclass(frozen=True, slots=True)
class Growth:
    """What one entry did to the row list."""

    index: int
    #: True when the entry started a new row rather than joining one.
    appended: bool
    row: Row


class ClusterStream:
    """Clustering as a stream, so the tail path and the rebuild share one code path.

    ``cluster_entries`` is this class fed everything at once; the app keeps a
    stream alive across polls and feeds it arrivals. There is therefore no
    second implementation for "incremental" to disagree with — which is what
    ``test_incremental_clustering_matches_a_full_recompute`` is checking has not
    been undone.
    """

    __slots__ = ("lookback", "_rows", "_open", "_last_seen", "_position")

    def __init__(self, *, lookback: int = DEFAULT_LOOKBACK) -> None:
        self.lookback = max(1, lookback)
        self._rows: list[Row] = []
        #: shape -> index into _rows of the cluster still accepting members.
        self._open: dict[str, int] = {}
        #: shape -> position of its most recent member, for the lookback test.
        self._last_seen: dict[str, int] = {}
        self._position = 0

    @property
    def rows(self) -> list[Row]:
        return self._rows

    @property
    def clustered(self) -> int:
        """How many rows are collapsed groups rather than single lines."""

        return sum(1 for row in self._rows if isinstance(row, Cluster))

    @property
    def entries(self) -> int:
        """Total lines behind the rows. Equals what was fed in, always."""

        return sum(len(row) if isinstance(row, Cluster) else 1 for row in self._rows)

    def add(self, entry: LogEntry) -> Growth:
        """Fold one entry in, and say what that did to the rows."""

        position = self._position
        self._position += 1
        shape = shape_of(entry)

        index = self._open.get(shape)
        if index is not None and position - self._last_seen[shape] <= self.lookback:
            self._last_seen[shape] = position
            row = self._rows[index]
            if isinstance(row, Cluster):
                row.add(entry)
            else:
                # The second member is what turns a line into a cluster. Until
                # then a row is the entry itself, so an unclustered buffer costs
                # no wrapper objects at all.
                row = Cluster(shape, [row, entry])
                self._rows[index] = row
            return Growth(index, False, row)

        self._rows.append(entry)
        self._open[shape] = len(self._rows) - 1
        self._last_seen[shape] = position
        return Growth(len(self._rows) - 1, True, entry)

    def extend(self, entries: Iterable[LogEntry]) -> list[Growth]:
        return [self.add(entry) for entry in entries]


def cluster_entries(
    entries: Sequence[LogEntry], *, lookback: int = DEFAULT_LOOKBACK
) -> ClusterStream:
    """Cluster *entries* in one pass. The same code the tail path uses."""

    stream = ClusterStream(lookback=lookback)
    stream.extend(entries)
    return stream


def expand(rows: Iterable[Row]) -> list[LogEntry]:
    """Every line behind *rows* — the no-loss guarantee, as a function.

    Order is the *rows'* order: a cluster hands back its own members in the
    order they were read, at the position of its first one. That is not the
    order the lines arrived in, and cannot be — gathering a run into one row is
    the whole feature, and a run with something else interleaved is exactly the
    case it is for. Nothing is dropped, nothing is invented, and no line's text
    changes; where a collapsed group sits relative to its neighbours does.
    """

    lines: list[LogEntry] = []
    for row in rows:
        if isinstance(row, Cluster):
            lines.extend(row.entries)
        else:
            lines.append(row)
    return lines


#: Prefix on a collapsed row. A glyph plus a number, so the count reads without
#: colour and a cluster row cannot be mistaken for an ordinary line.
COUNT_PREFIX = "×"


def summarise(cluster: Cluster) -> LogEntry:
    """One entry standing for a whole cluster, for a clustered export.

    An ordinary :class:`LogEntry`, so every exporter writes it without knowing
    clusters exist — which is why ``export.py`` did not change for this item.
    The count and the span go in ``fields``, where JSON Lines and CSV pick them
    up for free.
    """

    representative = cluster.representative
    fields = dict(representative.fields)
    fields["cluster.count"] = str(cluster.count)
    first, last = cluster.first, cluster.last
    if first is not None:
        fields["cluster.first"] = first.isoformat(sep=" ")
    if last is not None:
        fields["cluster.last"] = last.isoformat(sep=" ")
    return replace(
        representative,
        raw=f"{COUNT_PREFIX}{cluster.count}  {representative.raw}",
        level=cluster.level,
        fields=fields,
    )


def describe(stream: ClusterStream) -> str:
    """The status line's version: what was collapsed into what."""

    collapsed = stream.clustered
    if not collapsed:
        return ""
    lines = sum(len(row) for row in stream.rows if isinstance(row, Cluster))
    return f"{lines} lines in {collapsed} clusters"


__all__ = [
    "COUNT_PREFIX",
    "DEFAULT_LOOKBACK",
    "RULE_NAMES",
    "Cluster",
    "ClusterStream",
    "Growth",
    "Row",
    "cluster_entries",
    "describe",
    "expand",
    "normalise",
    "shape_of",
    "summarise",
]
