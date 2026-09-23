"""Regex field matching and entry age, as CLV query plugins.

A worked ``QueryOperator`` and ``ComputedField`` example. Drop this file in
``~/.config/clv/plugins/`` (CLV puts it there for you) and add it to
``settings.conf``::

    [log_viewer]
    plugins = field_regex

It adds two things the query grammar could not express:

``svc~^web[0-9]+``
    A regex against one *field*, rather than against the whole raw line.
    ``:`` is a substring test and the free-text half is a regex over everything,
    so "this field, and only this field, matches this pattern" had no spelling.

``age<60``
    Seconds since the line was written, which no log line carries and every
    operator wants. ``age<60 level:error`` is "what has gone wrong in the last
    minute" without touching the time window.

Both work in the query box, in a saved view and in a watch rule, because all
three route through ``parse_query``. Four things are worth copying out of this
file.

**A token may not look like a key.** ``~`` is legal; ``~x`` is not, because
``svc~xweb`` could not be told from a field called ``svc~xweb``... which is to
say, from nothing, since ``~`` is not a key character either. The rule CLV
checks is simply that a token shares no character with a field key, and it is
checked at load rather than at the first term.

**Follow CLV's conventions, or the operator has to remember two.** ``~`` is
smart-case because every other search in CLV is: lower case matches either,
and a capital means you meant it. Inventing a case-sensitive-always operator
would have been one line shorter and a worse answer.

**These run per entry, per render.** ``test()`` is called once per entry per
term, on every keystroke in the query box. The compile is cached on the pattern
string, so a query that does not change costs one dict lookup per line. A
plugin that cannot hold to that is taken out of service by the render budget
(``plugin_time_budget_ms``) rather than being allowed to make the pane stutter.

**Never raise, and never guess.** A malformed regex is something the operator
typed, not a fault in this plugin: it returns "no match" rather than throwing,
because throwing would disable the operator for the session over a half-finished
``svc~(``. An entry with no timestamp has no age, and ``None`` says so — which
CLV reports as "this source cannot answer that", not as "did not match".
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from clv.api import ComputedField, LogEntry, QueryOperator


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> Optional[re.Pattern[str]]:
    """*pattern* compiled, smart-case, or ``None`` if it is not a regex.

    Cached on the string because the query box re-filters the whole buffer on
    every keystroke: without this, a 50,000-line buffer would compile the same
    pattern 50,000 times per keypress. The failure is cached too, so a query
    half-typed into ``svc~(`` does not re-raise and re-catch per line.
    """

    flags = 0 if any(char.isupper() for char in pattern) else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


class RegexMatch(QueryOperator):
    """``key~pattern`` — the field matches the regular expression."""

    name = "field-regex"
    requires_api = ">=1.0,<2.0"
    token = "~"

    def test(self, stored: str, value: str) -> bool:
        pattern = _compiled(value)
        if pattern is None:
            # The operator is mid-typing. Matching nothing is the honest answer
            # and the only one that does not cost them the plugin.
            return False
        return pattern.search(stored) is not None


class NotRegexMatch(QueryOperator):
    """``key!~pattern`` — the field does not match.

    Its own token rather than a flag on :class:`RegexMatch`, because the grammar
    has no negation: ``!=`` is a token too, not a modifier on ``=``. Longest
    token wins, so ``!~`` is found before ``!=``'s ``!`` could confuse it and
    before a bare ``~``.
    """

    name = "field-regex"
    requires_api = ">=1.0,<2.0"
    token = "!~"

    def test(self, stored: str, value: str) -> bool:
        pattern = _compiled(value)
        if pattern is None:
            return False
        return pattern.search(stored) is None


class EntryAge(ComputedField):
    """``age`` — seconds since the entry's timestamp.

    The clearest case for a computed field: it is a fact about every entry, it
    is in no log line, and deriving it in a filter stage could not make it
    *queryable*.
    """

    name = "entry-age"
    requires_api = ">=1.0,<2.0"
    field_name = "age"

    def value(self, entry: LogEntry) -> Optional[str]:
        stamp = entry.timestamp
        if stamp is None:
            # Not "zero" and not "very old": this source's format carries no
            # date, and CLV has a distinct outcome for a question an entry
            # cannot answer. Returning a number would hide those lines behind a
            # filter that looks like it worked.
            return None
        # Logs mix aware and naive stamps constantly — JSON with offsets beside
        # syslog without — so the reference clock is chosen per entry rather
        # than once.
        now = datetime.now(timezone.utc) if stamp.tzinfo is not None else datetime.now()
        return str(int((now - stamp).total_seconds()))
