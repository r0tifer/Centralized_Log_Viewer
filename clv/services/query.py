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
from typing import Iterable, Mapping, Optional, Sequence

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
    {"host", "tag", "pid", "msgid", "ident", "user", "request", "status", "size"}
)

#: Outcomes of :func:`match_terms`. Three, not two: "no such field" is a
#: different answer from "did not match" and the UI reports it differently.
MATCH_HIT = "hit"
MATCH_MISS = "miss"
MATCH_MISSING_FIELD = "missing-field"

#: A key is an identifier: it must start with a letter or underscore, which is
#: what keeps a bare ``10:30:00`` out of the grammar.
_TERM_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P<op>>=|<=|!=|>|<|=|:)(?P<value>.*)$",
    re.DOTALL,
)

#: Cheap pre-test: no operator character means no term, so a plain regex skips
#: the tokeniser entirely.
_HAS_OPERATOR = re.compile(r"[:=<>]")

_QUOTES = "\"'"


@dataclass(frozen=True, slots=True)
class FieldTerm:
    """One ``key op value`` constraint."""

    key: str
    op: str
    value: str

    def test(self, fields: Mapping[str, str]) -> Optional[bool]:
        """Match against *fields*. ``None`` when the field is not there at all."""

        stored = _lookup(fields, self.key)
        if stored is None:
            return None
        if self.op == ":":
            if not self.value:
                # `host:` asks whether the field is present, which it is.
                return True
            return _contains(stored, self.value)
        if self.op == "=":
            return stored == self.value
        if self.op == "!=":
            return stored != self.value
        return _ordered(stored, self.value, self.op)

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
    missing = False
    for term in terms:
        result = term.test(fields)
        if result is None:
            missing = True
        elif not result:
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
            groups = char in _QUOTES and (index == start or query[index - 1] in ":=<>!")
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
    return FieldTerm(key, operator, value)


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
    "MATCH_HIT",
    "MATCH_MISS",
    "MATCH_MISSING_FIELD",
    "NORMALISED_FIELD_KEYS",
    "FieldTerm",
    "ParsedQuery",
    "QueryError",
    "collect_field_names",
    "entry_matches",
    "match_terms",
    "parse_query",
]
