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

Two of those pieces are extensible, and they are extensible in very different
ways. A :class:`MatcherSpec` is a plugin-supplied *rule kind* — a way of
deciding that a line is worth telling someone about that is not "this pattern
matched" — and it is per entry, in this process, under a budget. A
:class:`SinkSpec` is a plugin-supplied *destination*, and it is the first thing
in CLV that genuinely wants the network: it runs on :class:`SinkDispatcher`'s
threads, never on the event loop, and it is fed what :meth:`WatchNotifier.due_hits`
already coalesced rather than the raw stream of hits.

Both arrive by injection. This module never imports ``clv.plugins``; the app
installs already-guarded callables through :func:`install_watch_plugins`, the
same shape ``query.install_query_plugins`` uses and for the same reason.
"""

from __future__ import annotations

import queue
import re
import threading
from collections import deque
from time import monotonic
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from .filtering import compile_query
from .marks import mark_key
from .parsing import LogEntry
from .query import (
    MATCH_HIT,
    QueryError,
    match_terms,
    parse_query,
    requirements,
    unsatisfied,
)
from .refs import SourceRef

#: What a rule does when it matches.
ACTION_HIGHLIGHT = "highlight"
ACTION_NOTIFY = "notify"
ACTION_BOTH = "both"
ACTIONS: tuple[str, ...] = (ACTION_HIGHLIGHT, ACTION_NOTIFY, ACTION_BOTH)

#: Default seconds between notifications for one rule. Also the floor the app
#: clamps ``watch_rate_limit`` to; see ``config.py``.
DEFAULT_RATE_LIMIT = 60

#: The built-in rule kind: :attr:`WatchRule.pattern` is a query in Item 8's
#: syntax. Reserved, so a plugin cannot redefine what every rule ever saved
#: already means — ``PluginRegistry._watch_fault`` refuses a matcher claiming it.
KIND_PATTERN = "pattern"

#: How many matching entries a window keeps for a sink that asked for content.
#: A cap rather than the whole window because the count is already exact: what
#: a sink gains from the lines themselves is a *sample* of what the burst looked
#: like, and 500 of them is the same sample as 50 at fifty times the memory —
#: memory holding log content, which is the thing this whole path is careful
#: about.
SINK_SAMPLE_LIMIT = 50


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    """One installed rule kind, and the predicate behind it.

    :attr:`matches` is ``None`` when the plugin is **loaded but out of service**
    — disabled by a fault, by the time budget, or by the operator in the ``P``
    dialog. The kind stays registered in that state on purpose, for the reason
    :class:`~clv.services.query.OperatorSpec` keeps its token: a rule whose kind
    quietly stopped existing would fall back to the pattern path and start
    matching its parameter string as a *query*, which is a different rule that
    happens to parse. It is reported by name instead.
    """

    kind: str
    plugin: str
    matches: Optional[Callable[[LogEntry, "WatchRule"], bool]] = None
    #: The matcher's own check on a rule's parameter string, so the rules dialog
    #: can report a bad one where it was typed. ``None`` when the plugin is out
    #: of service, or when it did not implement the optional hook.
    validate: Optional[Callable[[str], Optional[str]]] = None


@dataclass(frozen=True, slots=True)
class SinkSpec:
    """One destination a coalesced hit is delivered to.

    Two lanes, and the lane is a property of the sink rather than a branch in
    the caller. :attr:`inline` marks CLV's own toast, which *must* run on the
    event loop because it paints; everything else is third-party code and runs
    on :class:`SinkDispatcher`'s threads because it might do IO.

    :attr:`disable` is the spec's own kill switch, closed over the plugin
    registry by ``PluginRegistry.watch_stack``. It is here so that a sink which
    *hangs* can be taken out of service by this module, which may not import
    ``clv.plugins`` — the same reasoning that puts the guard inside
    :attr:`deliver` rather than beside it.
    """

    plugin: str
    deliver: Callable[..., None]
    #: Whether this sink asked to be handed the lines themselves. False means it
    #: receives a rule name and a count and nothing else, which is the default
    #: and the state the ``P`` dialog reports as such.
    wants_entries: bool = False
    inline: bool = False
    disable: Optional[Callable[[str], None]] = None


#: Installed rule kinds, keyed casefolded — a stored ``kind`` is operator-facing
#: text and ``Burst`` naming the ``burst`` matcher is a typo class, not an
#: intent. Empty on every build with no watch plugins, which is what keeps this
#: seam free: :class:`_CompiledRule` tests it only for a rule that declares a
#: kind, and no rule written before this existed declares one.
_MATCHERS: Dict[str, MatcherSpec] = {}
#: Installed destinations, in ``plugin_sort_key`` order. Empty is the common
#: case and costs nothing: the app skips the whole delivery path.
_SINKS: list[SinkSpec] = []


def install_watch_plugins(
    matchers: Sequence[MatcherSpec] = (),
    sinks: Sequence[SinkSpec] = (),
) -> None:
    """Register what the loaded watch plugins declared. Replaces, not merges.

    Called once at mount and again whenever the plugin registry's generation
    moves, so a matcher switched off in the ``P`` dialog stops answering and a
    re-enabled one starts again. Both arguments default to empty, which is how a
    test — and ``on_unmount`` — puts the module back.

    **Loaded, not enabled, is what gets installed.** A matcher that is out of
    service still contributes its kind, with a null callable; see
    :class:`MatcherSpec`.
    """

    _MATCHERS.clear()
    _SINKS.clear()
    for spec in matchers:
        _MATCHERS[spec.kind.casefold()] = spec
    _SINKS.extend(sinks)


def matcher_for(kind: str) -> Optional[MatcherSpec]:
    """The spec registered for *kind*, or ``None`` when nothing claims it."""

    return _MATCHERS.get(kind.casefold()) if kind else None


def matcher_kinds() -> tuple[str, ...]:
    """Every rule kind a rule may declare, built-in first.

    What the rules dialog cycles through. With no matchers installed this is
    ``("pattern",)`` and the dialog composes no control at all, so a build
    without watch plugins renders exactly what it rendered before they existed.
    """

    return (KIND_PATTERN,) + tuple(
        sorted(spec.kind for spec in _MATCHERS.values())
    )


def installed_sinks() -> tuple[SinkSpec, ...]:
    """Every registered destination, in load order."""

    return tuple(_SINKS)


def wants_entry_samples() -> bool:
    """Whether any installed sink asked for the lines themselves.

    The notifier keeps a bounded sample per rule only when this is true, so an
    operator with no content-reading sink pays no retention at all — and the
    usual case, which is no sinks whatsoever, pays nothing to find that out.
    """

    return any(spec.wants_entries for spec in _SINKS)


@dataclass(frozen=True)
class WatchRule:
    """A named pattern and what to do about it."""

    name: str
    pattern: str = ""
    action: str = ACTION_BOTH
    enabled: bool = True
    #: Query plugins :attr:`pattern` depends on. Same contract as
    #: :attr:`~clv.storage.SavedView.requires`: a rule naming a plugin that is
    #: not installed is kept byte-intact, never matches, and is listed with the
    #: plugin it needs. Without this the rule would fall through to
    #: ``compile_query`` and start matching as a regex — quietly highlighting
    #: the wrong lines, which is worse than highlighting none.
    requires: tuple[str, ...] = ()
    #: Which kind of rule this is. :data:`KIND_PATTERN` — the default, and what
    #: every rule written before this field existed loads as — means
    #: :attr:`pattern` is a query. Any other value names a plugin-supplied
    #: :class:`MatcherSpec`, and then :attr:`pattern` is that matcher's own
    #: parameter string, which CLV does not parse and does not interpret.
    kind: str = KIND_PATTERN

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
        # Hand-written rather than annotation-driven like `SavedView`'s, so the
        # house rule has to be restated: one bad element must not cost the list.
        stored = raw.get("requires")
        requires = (
            tuple(item for item in stored if isinstance(item, str))
            if isinstance(stored, (list, tuple))
            else ()
        )
        # Absent, blank or not a string all mean the same thing and all mean
        # the original behaviour: a rule file written before kinds existed is a
        # file full of pattern rules. Kept as written rather than casefolded --
        # every comparison below folds, and the record stays byte-intact.
        stored_kind = raw.get("kind")
        kind = (
            stored_kind.strip()
            if isinstance(stored_kind, str) and stored_kind.strip()
            else KIND_PATTERN
        )
        return cls(
            name=name.strip(),
            pattern=pattern,
            action=action if action in ACTIONS else ACTION_BOTH,
            enabled=enabled if isinstance(enabled, bool) else True,
            requires=requires,
            kind=kind,
        )

    @property
    def missing_plugins(self) -> tuple[str, ...]:
        """Plugins this rule needs that are not installed. Empty when usable."""

        return unsatisfied(self.requires)

    @property
    def missing_kind(self) -> str:
        """This rule's kind, when nothing installed provides it. Else empty.

        Named by *kind* and not by plugin, deliberately. The kind is the
        contract and a plugin is one implementation of it: a rule written on a
        machine carrying one ``burst`` matcher and opened on a machine carrying
        a different one is a rule that still runs, and recording the plugin name
        would have marked it broken. It is also the more useful sentence — "find
        something that provides ``burst``" is actionable in a way that "install
        acme-watch" is not.
        """

        kind = self.kind
        if not kind or kind.casefold() == KIND_PATTERN:
            return ""
        return "" if kind.casefold() in _MATCHERS else kind

    @property
    def unusable_reason(self) -> Optional[str]:
        """One clause saying why this rule cannot run, or ``None``.

        Three sources, one sentence, because an operator meeting this on the
        tree row, in the rules dialog and in a notification should meet the same
        words each time. The three are deliberately worded apart:

        * a query plugin that is **not installed** — the saved record is
          unusable and stays untouched until it is;
        * a rule kind **nothing provides** — the same, one level up;
        * a kind whose plugin is installed but **out of service** — not the same
          thing at all, and the record is fine the moment it is back.
        """

        absent = self.missing_plugins
        if absent:
            return describe_missing(absent)
        kind = self.missing_kind
        if kind:
            return describe_missing_kind(kind)
        spec = matcher_for(self.kind) if self.kind.casefold() != KIND_PATTERN else None
        if spec is not None and spec.matches is None:
            return f"needs the '{spec.plugin}' plugin, which is not in service"
        return None


def validate_pattern(
    pattern: str,
    known_fields: Iterable[str] = (),
    requires: Iterable[str] = (),
    kind: str = KIND_PATTERN,
) -> Optional[str]:
    """Why *pattern* is unusable, or ``None`` when it is fine.

    Used by the rules dialog so a bad pattern is reported where it was typed
    rather than swallowed at match time.

    *requires* is a stored rule's recorded plugin dependencies, checked first:
    an uninstalled plugin leaves nothing in the grammar to complain about, so
    the pattern would otherwise validate cleanly and then mean something else.
    A plugin that is merely *switched off* needs no special case — its token
    stays reserved and ``parse_query`` raises below, naming it.

    *kind* decides what *pattern* even is. For a plugin-supplied kind the string
    is the matcher's own parameter and CLV has no opinion about it whatsoever:
    it is not parsed as a query, not compiled as a regex, and the only thing
    that can say whether it is usable is the matcher, through its optional
    ``validate`` hook.
    """

    absent = unsatisfied(requires)
    if absent:
        return "Rule " + describe_missing(absent) + "."
    if not pattern.strip():
        return "Enter a pattern."
    if kind and kind.casefold() != KIND_PATTERN:
        spec = matcher_for(kind)
        if spec is None:
            return "Rule " + describe_missing_kind(kind) + "."
        if spec.matches is None:
            return f"Rule needs the '{spec.plugin}' plugin, which is not in service."
        return spec.validate(pattern) if spec.validate is not None else None
    try:
        parsed = parse_query(pattern, known_fields)
        compile_query(parsed.text)
    except QueryError as exc:
        return str(exc)
    return None


def _name_list(names: Sequence[str]) -> str:
    """``'a'``, ``'a' and 'b'``, ``'a', 'b' and 'c'`` — for a message."""

    quoted = [f"'{name}'" for name in names]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


#: Marks a saved view or watch rule whose query needs a plugin that is not
#: installed. Here rather than in any one widget because three surfaces show it
#: — the source tree, the view picker and the rules dialog — and a record that
#: is unusable in three places must not be unusable in three different glyphs.
#: It sits beside :func:`describe_missing` because the mark and the words it
#: introduces are one message split across however much width there is.
UNUSABLE_MARK = "⚠"


def describe_missing(names: Sequence[str]) -> str:
    """One clause naming the plugins a saved thing needs and does not have.

    Shared by the watch dialog, the view picker and the app's own notification
    so an operator meets the same sentence wherever they run into it — a saved
    thing that is unusable in three places should not be unusable in three
    different wordings.
    """

    if len(names) == 1:
        return f"needs the '{names[0]}' plugin, which is not installed"
    return f"needs the {_name_list(names)} plugins, which are not installed"


def describe_missing_kind(kind: str) -> str:
    """The same clause, for a rule kind no installed plugin provides.

    Beside :func:`describe_missing` because they are read in the same place,
    after the same glyph, and an operator should not have to notice which of the
    two they got.
    """

    return f"needs the '{kind}' rule kind, which no installed plugin provides"


class _CompiledRule:
    """A rule with its pattern prepared, or marked unusable."""

    __slots__ = ("rule", "terms", "pattern", "broken", "matcher")

    def __init__(self, rule: WatchRule, known_fields: Iterable[str]) -> None:
        self.rule = rule
        self.terms: tuple = ()
        self.pattern: Optional[re.Pattern[str]] = None
        self.matcher: Optional[Callable[[LogEntry, WatchRule], bool]] = None
        self.broken = False
        if rule.kind.casefold() != KIND_PATTERN:
            # A plugin kind, and the pattern is the matcher's parameter rather
            # than a query. **Not** parsed on the way past: a kind whose matcher
            # is missing must never fall through to the pattern path, because
            # "5/60" read as a query is a rule that matches something, and a
            # rule quietly matching the wrong lines is worse than one matching
            # none. Same argument, one level up, as the missing-operator case
            # below.
            spec = matcher_for(rule.kind)
            if spec is None or spec.matches is None:
                self.broken = True
                return
            self.matcher = spec.matches
            return
        if rule.missing_plugins:
            # **Before** the parse, not after it. An uninstalled operator is not
            # a syntax error: the token is simply unknown, so the whole pattern
            # falls through to `compile_query` and starts matching as a regex.
            # That is the one outcome Requirement 12 forbids, and it is silent.
            self.broken = True
            return
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
        if self.matcher is not None:
            # Already guarded: the callable came from `PluginRegistry.watch_stack`
            # wrapped in the disable-and-report path, so it returns False rather
            # than raising however badly the plugin behaves.
            return self.matcher(entry, self.rule)
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

    __slots__ = ("_rules", "_hits", "_known_fields", "evaluations", "_stateful")

    def __init__(
        self,
        rules: Sequence[WatchRule] = (),
        known_fields: Iterable[str] = (),
    ) -> None:
        self._known_fields = frozenset(known_fields)
        self._rules: list[_CompiledRule] = []
        self._hits: Dict[str, tuple[str, ...]] = {}
        #: Whether any rule is answered by a plugin matcher. See
        #: :meth:`evaluate` for what it costs and why it is worth it.
        self._stateful = False
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
        self._stateful = any(compiled.matcher is not None for compiled in self._rules)
        self.reset()

    def reset(self) -> None:
        self._hits.clear()
        self.evaluations = 0

    def evaluate(
        self, source: Optional[SourceRef], entries: Iterable[LogEntry]
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

        **A plugin matcher suspends the cache, and only the cache's reuse.**
        "Once per distinct line" is a sound optimisation for a pattern, which is
        a pure function of the text; it is wrong for a
        :class:`~clv.plugins.WatchMatcher`, which may be counting. A ``burst``
        rule asked about five identical lines has to be asked five times, or it
        sees one and the kind quietly does not work — with nothing to diagnose,
        because from the outside the rule simply never fires. Answers are still
        *stored*, so :meth:`hits` stays the lookup that keeps re-rendering free;
        what is skipped is reading one back. A rule set with no plugin kind in
        it — which is every rule set that existed before this — takes neither
        the flag nor the extra call.
        """

        if not self._rules:
            return []
        fired: list[tuple[LogEntry, tuple[str, ...]]] = []
        for entry in entries:
            key = mark_key(source, entry)
            names = None if self._stateful else self._hits.get(key)
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

    def hits(self, source: Optional[SourceRef], entry: LogEntry) -> tuple[str, ...]:
        """Rules this line hit. Empty for a line never evaluated."""

        return self._hits.get(mark_key(source, entry), ())

    def watched(self, source: Optional[SourceRef], entry: LogEntry) -> bool:
        return bool(self.hits(source, entry))

    def prune(self, source: Optional[SourceRef], entries: Iterable[LogEntry]) -> None:
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


@dataclass(frozen=True, slots=True)
class WatchHit:
    """What one rule did during one closed window.

    The unit of delivery, and the reason it is a record rather than the string
    it used to be: a toast wants the sentence, and a sink wants the name and the
    count as data. Both come out of the same coalescing, which is what makes
    "a sink cannot bypass the rate limiter" true by construction rather than by
    everyone remembering.
    """

    name: str
    count: int
    message: str
    #: A bounded sample of the lines that matched, kept only when some installed
    #: sink asked for content; see :data:`SINK_SAMPLE_LIMIT`. Empty otherwise,
    #: and empty is what a sink that did not ask for content is handed however
    #: full this is.
    entries: tuple[LogEntry, ...] = ()


class WatchNotifier:
    """Coalesces hits into at most one message per rule per window.

    A rule matching every line must not produce a notification storm — that is
    the behaviour that makes people turn a feature like this off. The first hit
    for a rule is reported immediately, because the first one is the news;
    everything inside the window after it is accumulated and reported as a
    count when the window closes.
    """

    __slots__ = ("window", "sample_limit", "_last_sent", "_pending", "_samples")

    def __init__(
        self, window: float = DEFAULT_RATE_LIMIT, *, sample_limit: int = 0
    ) -> None:
        self.window = window
        #: How many matching entries to keep per rule for a content-reading
        #: sink. **Zero by default**, which means nothing is retained: an
        #: operator with no such sink installed does not accumulate log lines in
        #: memory on the off-chance that something wants them.
        self.sample_limit = sample_limit
        self._last_sent: Dict[str, float] = {}
        self._pending: Dict[str, int] = {}
        self._samples: Dict[str, deque] = {}

    def reset(self) -> None:
        self._last_sent.clear()
        self._pending.clear()
        self._samples.clear()

    def record(
        self,
        names: Iterable[str],
        *,
        count: int = 1,
        entry: Optional[LogEntry] = None,
    ) -> None:
        for name in names:
            self._pending[name] = self._pending.get(name, 0) + count
            if entry is None or self.sample_limit <= 0:
                continue
            sample = self._samples.get(name)
            if sample is None:
                # `maxlen` does the capping, so a rule matching half a million
                # lines holds fifty of them and drops the oldest as it goes.
                sample = self._samples[name] = deque(maxlen=self.sample_limit)
            sample.append(entry)

    def due_hits(self, now: float) -> tuple[WatchHit, ...]:
        """What is due at *now*, emptying whatever it reports.

        The primary drain. :meth:`due` is the string projection of this, kept
        because a caller that only wants toasts should not have to know about
        the record — and because it is what this class has always returned.
        Both empty the same pending state, so a caller uses one of them.
        """

        hits: list[WatchHit] = []
        for name in sorted(self._pending):
            count = self._pending[name]
            if not count:
                continue
            last = self._last_sent.get(name)
            if last is not None and now - last < self.window:
                continue
            self._last_sent[name] = now
            self._pending[name] = 0
            sample = self._samples.pop(name, None)
            hits.append(
                WatchHit(
                    name=name,
                    count=count,
                    message=self.describe(name, count),
                    entries=tuple(sample) if sample else (),
                )
            )
        # Rules that reported are left at zero rather than deleted, so their
        # window keeps being honoured for as long as the rule exists.
        return tuple(hits)

    def due(self, now: float) -> list[str]:
        """Messages to show at *now*, emptying whatever they report."""

        return [hit.message for hit in self.due_hits(now)]

    def describe(self, name: str, count: int) -> str:
        if count == 1:
            return f"Watch '{name}' matched a line."
        return f"Watch '{name}' matched {count} lines."


#: How long a sink may be inside one ``deliver()`` before CLV stops waiting for
#: it. The app overrides this from ``plugin_sink_timeout_ms``; the constant is
#: the floor a dispatcher built with no config uses, which is every test that
#: does not care.
DEFAULT_SINK_TIMEOUT_MS = 5000

#: Deliveries queued for one sink before CLV decides it is not keeping up. The
#: notifier has already coalesced to one message per rule per window, so this is
#: 32 *windows* behind, not 32 lines behind — a sink that far adrift is broken
#: rather than busy.
_SINK_QUEUE_DEPTH = 32


class SinkDispatcher:
    """Delivers coalesced hits to every sink, and never on the event loop.

    A webhook that blocks would block the poll, and the poll is what draws the
    pane. So every third-party sink gets a thread of its own and a queue of its
    own: :meth:`deliver` is a non-blocking put, and a sink that wedges holds up
    nothing but itself. One shared worker would have let one sick sink starve
    every healthy one, which is the failure that looks exactly like CLV being
    slow.

    CLV's own toast is in the same list, marked :attr:`SinkSpec.inline`, because
    it paints and painting must happen on the event loop. That makes the lane a
    property of the sink rather than a branch in the caller, and there is one
    delivery path rather than a plugin path bolted beside a core one.

    **A hung sink is abandoned, not killed.** :meth:`settle` takes a sink that
    has not returned within the deadline out of service, reports it by name, and
    stops feeding it — and its thread goes on running, because a Python thread
    cannot be stopped from outside. That is the honest limit of what this class
    buys, and it is why ``PLUGIN_TODO.md`` Phase 13's subprocess host is the
    thing that makes a hang genuinely stoppable rather than merely ignorable.
    """

    __slots__ = (
        "timeout_ms",
        "_clock",
        "_specs",
        "_queues",
        "_threads",
        "_inflight",
        "_retired",
        "_lock",
    )

    def __init__(
        self,
        specs: Sequence[SinkSpec] = (),
        *,
        timeout_ms: float = DEFAULT_SINK_TIMEOUT_MS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.timeout_ms = float(timeout_ms)
        self._clock = clock
        self._specs: Dict[str, SinkSpec] = {}
        self._queues: Dict[str, queue.Queue] = {}
        self._threads: Dict[str, threading.Thread] = {}
        #: ``plugin -> when its current delivery started``. Written by the
        #: worker threads and read by :meth:`settle` on the event loop, so every
        #: touch is under :attr:`_lock`.
        self._inflight: Dict[str, float] = {}
        self._retired: set[str] = set()
        self._lock = threading.Lock()
        self.replace(specs)

    @property
    def active(self) -> bool:
        """Whether there is anything at all to deliver to."""

        return bool(self._specs)

    def replace(self, specs: Sequence[SinkSpec]) -> None:
        """Adopt a new sink set, keeping the workers that survive it.

        Called whenever the plugin generation moves. A sink that is still
        installed keeps its thread and its queue — rebuilding them on every
        unrelated plugin change would drop queued deliveries for no reason. A
        sink that has gone is stopped; one that is **back after being retired**
        starts again from nothing, which is the only reading of Re-enable that
        is not a lie, and matches what ``PluginBudget.forget`` does for strikes.
        """

        incoming = {spec.plugin: spec for spec in specs}
        for name in list(self._specs):
            if name not in incoming:
                self._shutdown_worker(name)
        for name in incoming:
            if name in self._retired:
                self._retired.discard(name)
                self._shutdown_worker(name)
        self._specs = incoming

    def deliver(self, hits: Sequence[WatchHit], context: Any = None) -> None:
        """Hand *hits* to every sink. Returns without waiting for any of them."""

        if not hits or not self._specs:
            return
        for spec in self._specs.values():
            if spec.inline:
                self._run(spec, hits, context)
                continue
            if spec.plugin in self._retired:
                continue
            try:
                self._queue_for(spec).put_nowait((hits, context))
            except queue.Full:
                self._retire(
                    spec,
                    f"is not keeping up: {_SINK_QUEUE_DEPTH} deliveries queued "
                    "and unread",
                )

    def settle(self, now: Optional[float] = None) -> None:
        """Retire any sink that has been inside one delivery for too long.

        Driven from the same poll :meth:`deliver` is, so the check costs a lock
        and a comparison on a dict that is empty whenever no sink is mid-call.
        """

        # Read without the lock on purpose: this runs on the event loop once
        # per poll, and an empty dict is the answer on every poll where no sink
        # is mid-call. A race here costs one poll's delay in noticing a hang,
        # which is nothing against a deadline measured in seconds.
        if self.timeout_ms <= 0 or not self._inflight:
            return
        with self._lock:
            moment = self._clock() if now is None else now
            limit = self.timeout_ms / 1000.0
            overdue = [
                name
                for name, started in self._inflight.items()
                if moment - started > limit
            ]
        for name in overdue:
            spec = self._specs.get(name)
            if spec is None:
                continue
            self._retire(
                spec,
                f"did not return within {self.timeout_ms:.0f} ms and was "
                "abandoned; its thread is still running and cannot be stopped",
            )

    def stop(self, timeout: float = 0.5) -> None:
        """Ask every worker to finish, and do not wait long.

        Called at shutdown, before the plugins are torn down. A sink that is
        wedged is *not* waited for: exiting the viewer must not depend on
        third-party code deciding to return.
        """

        for name in list(self._threads):
            self._shutdown_worker(name, timeout=timeout)

    # --- the workers --------------------------------------------------------

    def _queue_for(self, spec: SinkSpec) -> queue.Queue:
        """This sink's queue, starting its thread the first time it is needed.

        Lazily, so an installed sink that never fires costs no thread at all —
        which is the ordinary case for a rule that has not matched yet.
        """

        existing = self._queues.get(spec.plugin)
        if existing is not None:
            return existing
        pending: queue.Queue = queue.Queue(maxsize=_SINK_QUEUE_DEPTH)
        self._queues[spec.plugin] = pending
        thread = threading.Thread(
            target=self._work,
            args=(spec.plugin, pending),
            name=f"clv-sink-{spec.plugin}",
            daemon=True,
        )
        self._threads[spec.plugin] = thread
        thread.start()
        return pending

    def _work(self, name: str, pending: queue.Queue) -> None:
        while True:
            job = pending.get()
            if job is None:
                return
            hits, context = job
            spec = self._specs.get(name)
            if spec is None or name in self._retired:
                continue
            with self._lock:
                self._inflight[name] = self._clock()
            try:
                self._run(spec, hits, context)
            finally:
                with self._lock:
                    self._inflight.pop(name, None)

    def _run(self, spec: SinkSpec, hits: Sequence[WatchHit], context: Any) -> None:
        """One sink, every hit in the window. The whole of what a sink is given.

        **This is where content access is enforced**, in one place rather than
        at each caller: a sink that did not declare ``wants_entries`` is handed
        an empty tuple however many lines the window actually kept.
        """

        for hit in hits:
            spec.deliver(
                hit.name,
                hit.count,
                context,
                hit.entries if spec.wants_entries else (),
            )

    def _retire(self, spec: SinkSpec, reason: str) -> None:
        """Take *spec* out of service and stop feeding it, once."""

        if spec.plugin in self._retired:
            return
        self._retired.add(spec.plugin)
        with self._lock:
            self._inflight.pop(spec.plugin, None)
        # The queue is dropped rather than drained: whatever is in it is for a
        # sink that is no longer running, and the thread holding it is either
        # wedged or about to see `_retired` and skip the job.
        self._queues.pop(spec.plugin, None)
        self._threads.pop(spec.plugin, None)
        if spec.disable is not None:
            spec.disable(reason)

    def _shutdown_worker(self, name: str, *, timeout: float = 0.0) -> None:
        pending = self._queues.pop(name, None)
        thread = self._threads.pop(name, None)
        if pending is not None:
            try:
                pending.put_nowait(None)
            except queue.Full:  # pragma: no cover - the sentinel can wait
                pass
        if thread is not None and timeout > 0:
            thread.join(timeout)
        with self._lock:
            self._inflight.pop(name, None)


def notifying(names: Sequence[str], rules: Sequence[WatchRule]) -> tuple[str, ...]:
    """Of *names*, the rules whose action includes notifying.

    A highlight-only rule still marks its lines; it just says nothing.
    """

    wanted = {rule.name for rule in rules if rule.notifies}
    return tuple(name for name in names if name in wanted)


def rule_requirements(pattern: str, known_fields: Iterable[str] = ()) -> tuple[str, ...]:
    """The query plugins *pattern* depends on, for a rule about to be saved.

    A thin pass-through to :func:`clv.services.query.requirements`, re-exported
    here so the rules dialog does not have to reach past ``watch`` for it.
    """

    return requirements(pattern, known_fields)


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
    "UNUSABLE_MARK",
    "describe_missing",
    "describe_missing_kind",
    "rule_requirements",
    "ACTION_BOTH",
    "ACTION_HIGHLIGHT",
    "ACTION_NOTIFY",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_SINK_TIMEOUT_MS",
    "KIND_PATTERN",
    "SINK_SAMPLE_LIMIT",
    "MatcherSpec",
    "SinkSpec",
    "SinkDispatcher",
    "WatchHit",
    "WatchIndex",
    "WatchNotifier",
    "WatchRule",
    "describe_rules",
    "install_watch_plugins",
    "installed_sinks",
    "matcher_for",
    "matcher_kinds",
    "notifying",
    "toggled",
    "validate_pattern",
    "wants_entry_samples",
]
