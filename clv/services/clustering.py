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

Plugins
-------

Two seams, installed by the app through :func:`install_cluster_plugins` and
neither reachable from here by import: this module never learns that
``clv.plugins`` exists, and what it is handed are :class:`ClusterRuleSpec` and
:class:`ShapeSpec` records whose callables already carry their guard.

* A :class:`~clv.plugins.ClusterRule` supplies one more volatile token to
  normalise out. Plugin rules run **after every built-in**, in
  ``plugin_sort_key`` order, and before the whitespace collapse. Appending is
  the only position that cannot break the order above, in which each rule runs
  on what the previous left behind.

  The digit-free placeholder rule is **validated at load**, not assumed: a
  placeholder carrying a digit would be chewed up by a later plugin rule
  matching numbers, and the mistake is invisible at runtime — a shape that is
  subtly wrong reads exactly like clustering working. A backslash is refused on
  the same argument, because the placeholder is a :func:`re.sub` *replacement
  template*, so a group reference written in it would splice in whatever the
  pattern captured.

* A :class:`~clv.plugins.ShapeContributor` widens the key itself. Contributions
  are appended to the shape in the same order, so a plugin can keep two clusters
  apart — by ``unit``, by ``node`` — without changing what any existing
  component of a shape means. One returning the same string for everything is a
  no-op, which is what makes adding one safe.

**Only *enabled* plugins are installed.** Unlike a ``QueryOperator``'s token or
a ``WatchMatcher``'s rule kind, a cluster rule reserves nothing: no saved view,
watch rule or session names one, so there is nothing that could be silently
reinterpreted while the plugin is switched off. A rule out of service is simply
absent and the shapes go back to being what they were.

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

That cache is why :func:`install_cluster_plugins` clears it, always: a shape
computed under the old rule set is not stale, it is *wrong*, and an entry folded
into the wrong cluster looks exactly like the feature working. It also decides
the two seams' cost models, which are not the same. A plugin rule is one
substitution per **distinct** line, paid once and then memoised; a contributor
runs per **entry** per render and is memoised by nothing, because its answer
depends on the whole entry rather than on the text alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from typing import Callable, Iterable, Optional, Sequence, Union

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


# --- the plugin registry ----------------------------------------------------
#
# Module-level and installed by the app, the shape `query.install_query_plugins`
# and `watch.install_watch_plugins` already have, and for the same reason: this
# module may not import `clv.plugins`, so what it receives are records whose
# callables already carry their guard and their budget.


@dataclass(frozen=True, slots=True)
class ClusterRuleSpec:
    """One installed normalisation rule.

    :attr:`apply` is the whole of it: a callable that takes the line as it
    stands and returns it with this rule's token replaced. The substitution
    itself is CLV's — a :class:`~clv.plugins.ClusterRule` declares a compiled
    pattern and a placeholder and nothing else — so what the guard around it
    catches is not the plugin raising, which it cannot, but a pathological input
    making :meth:`re.Pattern.sub` fail on a line nobody will ever see again.

    Unlike :class:`~clv.services.query.OperatorSpec` there is no null state: an
    out-of-service rule is not installed at all. See the module docstring.
    """

    plugin: str
    apply: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ShapeSpec:
    """One extra component of the key two entries must share.

    :attr:`contribute` returns the string appended to the shape for an entry.
    An empty string is a legitimate answer and the no-op one — it says "this
    entry is not one I distinguish" — which is why a contributor that is
    disabled mid-render falls back to it rather than to anything louder.
    """

    plugin: str
    contribute: Callable[[LogEntry], str]


#: Installed rules, in ``plugin_sort_key`` order, applied after every built-in.
#: Empty on every build with no clustering plugins, which is what keeps this
#: seam free: :func:`normalise` iterates a tuple with nothing in it.
_PLUGIN_RULES: tuple[ClusterRuleSpec, ...] = ()
#: Installed shape contributors, in the same order. Empty is the common case and
#: :func:`shape_of` returns the exact string it returned before they existed.
_CONTRIBUTORS: tuple[ShapeSpec, ...] = ()


def install_cluster_plugins(
    rules: Sequence[ClusterRuleSpec] = (),
    contributors: Sequence[ShapeSpec] = (),
) -> None:
    """Register what the enabled clustering plugins declared. Replaces, not merges.

    Called once at mount and again whenever the plugin registry's generation
    moves, so a rule switched off in the ``P`` dialog stops folding lines and a
    re-enabled one starts again. Both arguments default to empty, which is how a
    test — and ``on_unmount`` — puts the module back.

    **This clears the shape cache, and that is not the caller's job.** Every
    shape :func:`normalise` has memoised was computed under the rule set being
    replaced; left alone it would go on answering with shapes the installed
    rules no longer produce, which is a wrong answer rather than a stale one and
    is indistinguishable from clustering working. Owning the invalidation here
    means there is no way to install a rule set and forget.
    """

    global _PLUGIN_RULES, _CONTRIBUTORS

    _PLUGIN_RULES = tuple(rules)
    _CONTRIBUTORS = tuple(contributors)
    normalise.cache_clear()


def installed_rules() -> tuple[ClusterRuleSpec, ...]:
    """Every installed normalisation rule, in the order they run."""

    return _PLUGIN_RULES


def installed_contributors() -> tuple[ShapeSpec, ...]:
    """Every installed shape contributor, in the order they are appended."""

    return _CONTRIBUTORS

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
    """Replace every volatile token in *text*, in the documented order.

    Plugin rules run after the built-ins and before the whitespace collapse, so
    a placeholder one of them writes is still tidied the same way and a rule
    cannot land in the middle of the fixed order it was documented to follow.
    """

    for _name, pattern, placeholder in _RULES:
        text = pattern.sub(placeholder, text)
    for spec in _PLUGIN_RULES:
        text = spec.apply(text)
    return _WHITESPACE.sub(" ", text).strip()


def shape_of(entry: LogEntry) -> str:
    """The key two entries must share to cluster together.

    With no contributors installed this is the string it has always been, byte
    for byte — the property ``tests/test_clustering.py`` pins by never having
    been changed for this seam.
    """

    body = entry.message or entry.raw
    origin = entry.fields.get(ORIGIN_FIELD, "")
    shape = f"{origin}\0{entry.level or ''}\0{normalise(body)}"
    if not _CONTRIBUTORS:
        return shape
    # An empty contribution adds *nothing*, not an empty component. That is
    # what makes "a contributor that is disabled, or that raised, leaves the
    # shape exactly as it was" true byte for byte rather than approximately --
    # the guard returns "" in both cases, and a trailing separator would be a
    # shape no build without the plugin could ever produce.
    return shape + "".join(
        f"\0{contribution}"
        for contribution in (spec.contribute(entry) for spec in _CONTRIBUTORS)
        if contribution
    )


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
    "ClusterRuleSpec",
    "ClusterStream",
    "Growth",
    "Row",
    "ShapeSpec",
    "cluster_entries",
    "describe",
    "expand",
    "install_cluster_plugins",
    "installed_contributors",
    "installed_rules",
    "normalise",
    "shape_of",
    "summarise",
]
