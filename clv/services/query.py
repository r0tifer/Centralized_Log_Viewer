"""Field-aware query terms.

The query box has always been a regex over the whole raw line, which is
excellent for "find this string" and useless for "show me sshd on web01 with a
5xx". Since Item 1 every entry carries :attr:`~clv.services.parsing.LogEntry.fields`,
so the structure is there to ask about — this module is the grammar that asks.

A query is a mix of **field terms** and **free text**::

    tag:sshd host:web01 status>=500 timeout|refused
    └────────────── terms ────────┘ └── regex ───┘

Terms combine with implicit AND, and the leftover text is handed to
``compile_query`` exactly as the whole query used to be. There is deliberately
no ``OR``, no parentheses and no precedence: a query DSL is a stated non-goal
in ``TODO.md`` and this stops one step short of the line.

**Reversed 2026-08-14, and the reversal is narrow enough to state exactly.**
``PLUGIN_TODO.md`` Phase 8 adds two plugin interfaces — ``QueryOperator``, a new
comparison token, and ``ComputedField``, a queryable field derived rather than
parsed. Both add *vocabulary*. Neither adds *structure*: there is still no
``OR``, still no parentheses, still no precedence, and the three of them remain
out of scope in ``TODO.md``. The grammar stays implicit-AND and flat, and still
stops one step short of the line — a plugin can teach it a new word, not a new
sentence shape. Built-in tokens stay reserved so a plugin cannot redefine ``:``
or ``=``, and computed fields resolve *after* parsed ones so a plugin can never
shadow what a line actually said.

Why a key must be *known*
-------------------------

The compatibility bar for this feature is that an existing saved query keeps
doing what it did. That rules out treating every ``word:word`` token as a term,
because ``sshd:`` and ``kernel:`` are among the most common things anyone greps
a syslog for, and reading them as a field named ``sshd`` would quietly turn a
working search into one that hides every line.

So a token is a term only when its key is one the source could actually answer:
either a name the parser normalises across formats
(:data:`NORMALISED_FIELD_KEYS`) or a key present in the buffer being filtered.
Everything else stays part of the regex, byte for byte. The cost is that a
typo — ``hsot:web01`` — is searched for as text rather than reported; the
alternative costs working queries, which is worse. The UI narrows that gap by
offering the known names as completions.

**When no token is recognised as a term, the query string is passed through
unmodified.** That is the property the compatibility tests pin: a plain regex
never even reaches the tokeniser's rejoin step, so it cannot be reshaped by it.

Operators
---------

=========  ================================================================
``:``      substring, smart-case (case-insensitive unless the value has an
           uppercase character). An empty value — ``host:`` — tests that the
           field is *present*.
``=``      exact, case-sensitive.
``!=``     not equal, case-sensitive.
``>`` ``>=`` ``<`` ``<=``
           numeric when both sides parse as numbers, lexicographic otherwise.
=========  ================================================================

Those seven are :data:`BUILTIN_OPERATORS` and they are **reserved**. A plugin
may add a token beside them — see *Plugins* below — but never redefine one,
because every saved query already means something under them.

Values are compared as the parser stored them: strings, never coerced. That is
why ``>=`` has to decide between numeric and lexicographic per comparison
rather than per field — ``status`` is ``"500"`` and there is no schema to say
it is a number.

Quoting
-------

A double- or single-quoted run keeps its spaces and its colons together, so
``msg:"disk full"`` and ``path:"/var:log"`` are each one term. Quotes are
grouping syntax and are removed from the value. In the free-text part they
group a phrase the same way — but only in a query that also has a term, since
a query without one is passed through untouched.

Plugins
-------

Two seams, both installed by the app through :func:`install_query_plugins` and
neither reachable from here by import: this module never learns that
``clv.plugins`` exists, and what it is handed are :class:`OperatorSpec` and
:class:`ComputedSpec` records whose callables already carry their guard.

* A :class:`~clv.plugins.QueryOperator` supplies a token. The alternation in
  ``_TERM_RE`` is *built* from the installed set rather than written, longest
  token first, so ``>=`` still beats ``>`` and a plugin's ``~=`` still beats its
  ``~``. A token that shares a character with a field key is refused at load,
  because ``key~avalue`` could not be told from a longer key.
* A :class:`~clv.plugins.ComputedField` supplies a queryable name. It resolves
  **after** the parsed fields and per entry, so a line that carries the key
  answers with its own value and only a line that does not reaches the plugin.

**Two absences, reported differently, and that is the whole of Requirement 12.**
A plugin that is installed but *switched off* keeps its token registered and its
terms raise :class:`QueryError` naming it — dropping the token instead would
make ``svc~web`` stop parsing as a term and fall through to the regex, which is
the same class of silent reinterpretation *Why a key must be known* exists to
prevent. A plugin that is *not installed at all* leaves nothing here to notice,
so the record that notices lives on the saved thing: a ``SavedView`` and a
``WatchRule`` each carry ``requires``, and :func:`unsatisfied` is what marks one
unusable rather than letting it mean something else.

Missing fields are hidden and *counted*
---------------------------------------

:func:`match_terms` distinguishes "this entry has the field and does not match"
from "this entry has no such field", because the second is the case
``AGENTS.md`` requires the UI to explain rather than silently swallow. The
caller counts it into ``FilterStats.hidden_missing_field`` and
``describe_empty_result`` names the field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .parsing import LogEntry


class QueryError(ValueError):
    """Raised when a query string is not usable as written.

    Lives here rather than in ``filtering`` because both a bad regex and a
    malformed term are the same thing to the UI: something to report through
    the query input's validation line instead of raising. ``filtering``
    re-exports it, so every existing import site is unaffected.
    """


#: Keys the parser normalises across formats, from ``parsing``'s module
#: docstring. They are always recognised, even before a line carrying one has
#: been read, so ``status>=500`` means the same thing in an empty buffer as in
#: a full one.
NORMALISED_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "host", "tag", "pid", "msgid", "ident", "user", "request", "status",
        "size",
        # `node` is where CLV *read* a line from; `host` is what the line says
        # about itself, and it is untouched. Keeping them apart is what lets
        # `node:web01 status>=500` work on day one without changing what a
        # single saved query already means — a merged view across a fleet is
        # the reason this feature exists, and `host` alone could not express it
        # because every machine's syslog claims a different one.
        "node",
    }
)

#: Outcomes of :func:`match_terms`. Three, not two: "no such field" is a
#: different answer from "did not match" and the UI reports it differently.
MATCH_HIT = "hit"
MATCH_MISS = "miss"
MATCH_MISSING_FIELD = "missing-field"

#: A key is an identifier: it must start with a letter or underscore, which is
#: what keeps a bare ``10:30:00`` out of the grammar.
_KEY_PATTERN = r"[A-Za-z_][A-Za-z0-9_.\-]*"
_KEY_RE = re.compile(_KEY_PATTERN)

#: Every character a key may contain. A plugin operator token may use none of
#: them: ``foo`` + ``a`` + ``bar`` would be indistinguishable from the key
#: ``fooabar``, and the tokeniser has no way to prefer one reading over the
#: other. Checked at load, in ``PluginRegistry``.
KEY_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)

#: The comparison tokens CLV owns, in the order they enter the alternation.
#: Reserved: a plugin claiming one is rejected at load, because redefining
#: ``:`` or ``=`` would change what every saved query already means.
BUILTIN_OPERATORS: tuple[str, ...] = (">=", "<=", "!=", ">", "<", "=", ":")

#: The four that compare as an order. Split out so :meth:`FieldTerm.compare`
#: can tell a built-in it handles from a plugin token it has to look up.
_ORDERED_OPS: frozenset[str] = frozenset({">", ">=", "<", "<="})

_QUOTES = "\"'"


# --- the plugin registry ----------------------------------------------------
#
# Module-level, and installed by the app rather than carried on `FilterSpec`.
# `FilterSpec` is frozen, slotted, hashed into the render cache key and
# persisted into `SavedView`: a registry of live callables cannot ride on it.
# `install_query_plugins` is the same shape `columns.install_profiles` uses for
# a format's row profile, and for the same reason -- the service must not import
# `clv.plugins`, so what it receives is already-wrapped callables.


@dataclass(frozen=True, slots=True)
class OperatorSpec:
    """One installed comparison token, and the predicate behind it.

    :attr:`test` is ``None`` when the plugin is **loaded but out of service** --
    disabled by a fault, by the time budget, or by the operator in the ``P``
    dialog. The token stays registered in that state on purpose: dropping it
    would make ``svc~web`` stop parsing as a term and fall through to the regex,
    which is the silent reinterpretation this whole mechanism exists to stop. A
    term using it raises :class:`QueryError` naming the plugin instead.
    """

    token: str
    plugin: str
    test: Optional[Callable[[str, str], bool]] = None


@dataclass(frozen=True, slots=True)
class ComputedSpec:
    """One queryable field derived rather than parsed.

    :attr:`value` is ``None`` under exactly the same out-of-service rule as
    :attr:`OperatorSpec.test`, and with the same consequence.
    """

    field_name: str
    plugin: str
    value: Optional[Callable[[LogEntry], Optional[str]]] = None


#: Installed operators, keyed by token. Empty on every build with no query
#: plugins, which is what keeps this seam free: the two lookups below are
#: guarded by a truth test on these dicts.
_OPERATORS: dict[str, OperatorSpec] = {}
#: Installed computed fields, keyed by casefolded name -- `_lookup` already
#: falls back to a case-insensitive match and a plugin field must not be
#: stricter than a parsed one.
_COMPUTED: dict[str, ComputedSpec] = {}


def _rebuild_grammar() -> None:
    """Recompile the three patterns that depend on the installed token set.

    **Longest token first**, so ``>=`` still beats ``>`` and a plugin's ``~=``
    still beats its ``~``. The sort is by length *alone* and Python's sort is
    stable, which is what lets the built-ins keep their declared order: two
    tokens of equal length cannot be a prefix of one another, so their relative
    order is free. With nothing installed the alternation is therefore
    byte-identical to the one this module shipped with.
    """

    global _TERM_RE, _HAS_OPERATOR, _QUOTE_AFTER

    ordered = sorted(
        list(BUILTIN_OPERATORS) + sorted(_OPERATORS), key=lambda token: -len(token)
    )
    alternation = "|".join(re.escape(token) for token in ordered)
    _TERM_RE = re.compile(
        rf"^(?P<key>{_KEY_PATTERN})(?P<op>{alternation})(?P<value>.*)$",
        re.DOTALL,
    )

    # Cheap pre-test: no operator character means no term, so a plain regex
    # skips the tokeniser entirely. One representative character per token is
    # sound -- a token cannot be present unless every one of its characters is --
    # and the last one reproduces the built-in set's `[:=<>]` exactly. Built by
    # hand rather than through a set so the compiled pattern does not move
    # between runs under hash randomisation.
    representatives: list[str] = []
    for token in ordered:
        if token[-1] not in representatives:
            representatives.append(token[-1])
    _HAS_OPERATOR = re.compile("[" + re.escape("".join(representatives)) + "]")

    # Where a quote is allowed to open a group: at the start of a token, or
    # immediately after an operator. Every character of every token, because a
    # multi-character token is only half-consumed at the point `_tokenise` looks
    # back one character.
    _QUOTE_AFTER = frozenset("".join(ordered))


_TERM_RE: re.Pattern[str]
_HAS_OPERATOR: re.Pattern[str]
_QUOTE_AFTER: frozenset[str]
_rebuild_grammar()


def install_query_plugins(
    operators: Sequence[OperatorSpec] = (),
    computed: Sequence[ComputedSpec] = (),
) -> None:
    """Register what the enabled query plugins declared. Replaces, not merges.

    Called once at mount and again whenever the plugin registry's generation
    moves, so a plugin switched off in the ``P`` dialog stops answering and a
    re-enabled one starts again. Both arguments default to empty, which is how
    a test puts the grammar back.
    """

    _OPERATORS.clear()
    _COMPUTED.clear()
    for operator in operators:
        _OPERATORS[operator.token] = operator
    for field_spec in computed:
        _COMPUTED[field_spec.field_name.casefold()] = field_spec
    _rebuild_grammar()


def is_query_key(name: str) -> bool:
    """Whether *name* could appear as a term's key.

    The one definition of the key grammar, so ``PluginRegistry`` can reject a
    ``ComputedField`` whose ``field_name`` no query could ever reach without
    keeping a second copy of the pattern.
    """

    return bool(name) and _KEY_RE.fullmatch(name) is not None


def computed_field_names() -> frozenset[str]:
    """Every registered computed field name, in service or not.

    The app unions these into the query vocabulary so ``age>300`` parses as a
    term. Out-of-service names are included deliberately: dropping one would
    turn its term back into free text, which is the reinterpretation
    :class:`OperatorSpec` explains at length.
    """

    return frozenset(spec.field_name for spec in _COMPUTED.values())


def provided_plugins() -> frozenset[str]:
    """Names of the plugins currently supplying an operator or a field.

    **Loaded is provided**, whether or not the plugin is in service. The two
    absences are different and are reported differently: a plugin that is not
    installed at all makes a saved view *unusable* (:func:`unsatisfied`), while
    one that is installed and merely switched off makes its terms raise a
    :class:`QueryError` naming it. Collapsing the two would mean an operator who
    disabled a plugin for a minute found their saved views marked broken.
    """

    names = {spec.plugin for spec in _OPERATORS.values()}
    names.update(spec.plugin for spec in _COMPUTED.values())
    return frozenset(names)


def unsatisfied(requires: Iterable[str]) -> tuple[str, ...]:
    """Of *requires*, the plugins that are not installed. Sorted, deduplicated.

    The whole of the degradation check. A :class:`~clv.storage.SavedView` or
    :class:`~clv.services.watch.WatchRule` whose result here is non-empty is
    kept byte-intact, marked unusable, and reported with these names.
    """

    if not requires:
        return ()
    provided = provided_plugins()
    return tuple(sorted({name for name in requires if name and name not in provided}))


def requirements(query: str, known_keys: Iterable[str] = ()) -> tuple[str, ...]:
    """The query plugins *query* depends on, recorded when it is saved.

    A term contributes when its operator is plugin-supplied, or when its key
    names a registered computed field. The second case over-records against a
    computed field that collides with a parsed one, and that is deliberate: a
    query that meant "the computed ``age``" and now means "the ``age`` some
    lines happen to carry" has been silently reinterpreted, which is exactly
    what Requirement 12 forbids.

    Reads the grammar directly rather than going through :func:`parse_query`,
    which raises for an out-of-service plugin -- and a view saved while a plugin
    was switched off still has to record that it needs it.
    """

    if not _OPERATORS and not _COMPUTED:
        return ()
    if not query or not _HAS_OPERATOR.search(query):
        return ()
    known = {key.lower() for key in known_keys}
    if not known:
        return ()

    names: list[str] = []
    for token in _tokenise(query):
        match = _TERM_RE.match(token)
        if match is None:
            continue
        key = match.group("key")
        if key.lower() not in known:
            continue
        for spec in (
            _OPERATORS.get(match.group("op")),
            _COMPUTED.get(key.casefold()),
        ):
            if spec is not None and spec.plugin not in names:
                names.append(spec.plugin)
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class FieldTerm:
    """One ``key op value`` constraint."""

    key: str
    op: str
    value: str

    def test(self, fields: Mapping[str, str]) -> Optional[bool]:
        """Match against *fields*. ``None`` when the field is not there at all.

        The parsed-field half only. :func:`match_terms` is what also consults a
        computed field, because that needs the whole entry and this has only the
        mapping.
        """

        stored = _lookup(fields, self.key)
        if stored is None:
            return None
        return self.compare(stored)

    def compare(self, stored: str) -> bool:
        """Whether *stored* satisfies this term.

        Split out from :meth:`test` so the computed-field path can reuse it
        without inventing a second mapping to look the value up in.

        A plugin token resolves through the installed registry **here**, per
        comparison, rather than being bound onto the term at parse time:
        :class:`FieldTerm` is frozen and slotted and compares by value, and
        hanging a callable off it would make two identical terms unequal. The
        cost is one dict lookup per term per entry, on a dict that is empty
        unless a query plugin is installed.
        """

        op = self.op
        if op == ":":
            if not self.value:
                # `host:` asks whether the field is present, which it is.
                return True
            return _contains(stored, self.value)
        if op == "=":
            return stored == self.value
        if op == "!=":
            return stored != self.value
        if op in _ORDERED_OPS:
            return _ordered(stored, self.value, op)
        spec = _OPERATORS.get(op)
        if spec is None or spec.test is None:
            # Uninstalled between parse and compare -- a plugin switched off
            # mid-render. Nothing matches, rather than something wrong matching.
            return False
        return spec.test(stored, self.value)

    def render(self) -> str:
        """The term as an operator would write it, for a summary line."""

        value = self.value
        if " " in value or ":" in value:
            value = f'"{value}"'
        return f"{self.key}{self.op}{value}"


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """A query split into field terms and whatever is left for the regex."""

    terms: tuple[FieldTerm, ...] = ()
    #: The free-text remainder. Identical to the input string whenever no term
    #: was recognised — the compatibility guarantee this module rests on.
    text: str = ""

    @property
    def field_keys(self) -> tuple[str, ...]:
        """The distinct keys referenced, in first-seen order."""

        seen: list[str] = []
        for term in self.terms:
            if term.key not in seen:
                seen.append(term.key)
        return tuple(seen)


def collect_field_names(entries: Iterable[LogEntry]) -> frozenset[str]:
    """Field names actually present in *entries*.

    The completion source, deliberately without :data:`NORMALISED_FIELD_KEYS`
    folded in: offering ``msgid`` against a source that has never reported one
    is noise. Gating uses the union of the two — see ``LogViewerApp``.
    """

    names: set[str] = set()
    for entry in entries:
        names.update(entry.fields)
    return frozenset(names)


def parse_query(query: str, known_keys: Iterable[str] = ()) -> ParsedQuery:
    """Split *query* into field terms and free text.

    Only keys in *known_keys* (compared case-insensitively) become terms.

    Raises:
        QueryError: if a recognised key is followed by a comparison operator
            and no value.
    """

    if not query or not _HAS_OPERATOR.search(query):
        return ParsedQuery((), query)

    known = {key.lower() for key in known_keys}
    if not known:
        return ParsedQuery((), query)

    terms: list[FieldTerm] = []
    remainder: list[str] = []
    for token in _tokenise(query):
        term = _as_term(token, known)
        if term is None:
            remainder.append(token)
        else:
            terms.append(term)

    if not terms:
        # Untouched, not rebuilt: a plain regex must survive this function
        # exactly as it was typed, quotes and runs of spaces included.
        return ParsedQuery((), query)
    return ParsedQuery(tuple(terms), " ".join(remainder))


def match_terms(entry: LogEntry, terms: Sequence[FieldTerm]) -> str:
    """Test every term against *entry*, AND-ed.

    Returns :data:`MATCH_HIT`, :data:`MATCH_MISS`, or
    :data:`MATCH_MISSING_FIELD` when a referenced field is absent. Absence wins
    over a plain miss, because it is the outcome the operator needs told.
    """

    if not terms:
        return MATCH_HIT
    fields = entry.fields
    computed = _COMPUTED
    missing = False
    for term in terms:
        stored = _lookup(fields, term.key)
        if stored is None and computed:
            # **After** the parsed fields, never instead of them: a line that
            # actually carries `age` answers with its own value and only a line
            # that does not reaches the plugin. That is what makes "a computed
            # field can never shadow what a line said" true per entry rather
            # than per key.
            stored = _computed(entry, term.key)
        if stored is None:
            missing = True
        elif not term.compare(stored):
            return MATCH_MISS
    return MATCH_MISSING_FIELD if missing else MATCH_HIT


def entry_matches(
    entry: LogEntry,
    terms: Sequence[FieldTerm],
    pattern: Optional[re.Pattern[str]],
) -> bool:
    """Whole-query predicate: terms AND regex.

    Shared by the hit counter and by `n`/`N`, so neither can disagree with what
    the pane is showing. Inversion is not applied here — it belongs to the
    free-text half only, and ``filter_entries`` owns that decision.
    """

    if match_terms(entry, terms) != MATCH_HIT:
        return False
    return pattern is None or pattern.search(entry.raw) is not None


# --- internals --------------------------------------------------------------


def _tokenise(query: str) -> list[str]:
    """Split on whitespace, keeping quoted runs together and unquoting them.

    A quote only groups where one could plausibly be intended: at the start of
    a token, or immediately after an operator. Otherwise ``don't`` would open a
    quote that never closes and swallow the rest of the line. An unterminated
    quote is kept as a literal character for the same reason.
    """

    tokens: list[str] = []
    index = 0
    length = len(query)
    while index < length:
        if query[index].isspace():
            index += 1
            continue
        start = index
        buffer: list[str] = []
        while index < length and not query[index].isspace():
            char = query[index]
            groups = char in _QUOTES and (
                index == start or query[index - 1] in _QUOTE_AFTER
            )
            if groups:
                closing = query.find(char, index + 1)
                if closing != -1:
                    buffer.append(query[index + 1 : closing])
                    index = closing + 1
                    continue
            buffer.append(char)
            index += 1
        tokens.append("".join(buffer))
    return tokens


def _as_term(token: str, known: set[str]) -> Optional[FieldTerm]:
    match = _TERM_RE.match(token)
    if match is None:
        return None
    key = match.group("key")
    if key.lower() not in known:
        return None
    operator = match.group("op")
    value = match.group("value")
    if not value and operator != ":":
        # `status>=` cannot be answered. Reported through the same validation
        # line a broken regex uses, never raised at the operator.
        raise QueryError(f"{key}{operator} needs a value")
    if _OPERATORS or _COMPUTED:
        _require_service(key, operator)
    return FieldTerm(key, operator, value)


def _require_service(key: str, operator: str) -> None:
    """Raise when this term needs a plugin that is loaded but switched off.

    The same channel `status>=` uses, and for the same reason: "this query is
    not usable as written" is something to report where it was typed. It is also
    what makes a watch rule mark itself unusable rather than quietly matching --
    `validate_pattern` and `_CompiledRule` both route through `parse_query`.
    """

    token = _OPERATORS.get(operator)
    if token is not None and token.test is None:
        raise QueryError(
            f"{operator} needs the '{token.plugin}' plugin, which is not in service"
        )
    # Checked even for a built-in comparison: `age>60` is as unanswerable as
    # `svc~web` when the plugin that supplies `age` is the one switched off.
    field = _COMPUTED.get(key.casefold())
    if field is not None and field.value is None:
        raise QueryError(
            f"{key} needs the '{field.plugin}' plugin, which is not in service"
        )


def _computed(entry: LogEntry, key: str) -> Optional[str]:
    """A computed field's value for *entry*, or None when nothing supplies one.

    Never raises: the callable arrived already wrapped by the plugin registry,
    which is what catches a plugin throwing, charges its time and takes it out
    of service.
    """

    spec = _COMPUTED.get(key.casefold())
    if spec is None or spec.value is None:
        return None
    return spec.value(entry)


def _lookup(fields: Mapping[str, str], key: str) -> Optional[str]:
    """Field value for *key*, falling back to a case-insensitive match.

    Normalised keys are lower case, but a JSON payload's keys are whatever the
    author wrote, and nobody wants to be told their query is wrong because the
    log said ``Status``.
    """

    value = fields.get(key)
    if value is not None:
        return value
    folded = key.lower()
    for name, stored in fields.items():
        if name.lower() == folded:
            return stored
    return None


def _contains(stored: str, needle: str) -> bool:
    """Substring test with the same smart-case rule the regex path uses."""

    if any(char.isupper() for char in needle):
        return needle in stored
    return needle.lower() in stored.lower()


def _as_number(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def _ordered(stored: str, value: str, operator: str) -> bool:
    left: object = _as_number(stored)
    right: object = _as_number(value)
    if left is None or right is None:
        # Either side unparseable: compare as text, so `tag>a` still orders.
        left, right = stored, value
    if operator == ">":
        return left > right  # type: ignore[operator]
    if operator == ">=":
        return left >= right  # type: ignore[operator]
    if operator == "<":
        return left < right  # type: ignore[operator]
    return left <= right  # type: ignore[operator]


__all__ = [
    "BUILTIN_OPERATORS",
    "KEY_CHARS",
    "MATCH_HIT",
    "MATCH_MISS",
    "MATCH_MISSING_FIELD",
    "NORMALISED_FIELD_KEYS",
    "ComputedSpec",
    "FieldTerm",
    "OperatorSpec",
    "ParsedQuery",
    "QueryError",
    "collect_field_names",
    "computed_field_names",
    "entry_matches",
    "install_query_plugins",
    "is_query_key",
    "match_terms",
    "parse_query",
    "provided_plugins",
    "requirements",
    "unsatisfied",
]
