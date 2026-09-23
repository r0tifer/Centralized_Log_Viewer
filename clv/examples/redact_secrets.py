"""Secrets kept out of the pane, as a CLV filter stage.

A worked ``FilterStage`` example. Drop this file in ``~/.config/clv/plugins/``
(CLV puts it in ``examples/`` for you) and add it to ``settings.conf``::

    [log_viewer]
    plugins = redact_secrets

    [plugin:redact_secrets]
    patterns = password, api_key, token, authorization
    replacement = ******

It rewrites ``password=hunter2`` into ``password=******`` everywhere that line
is shown: the pane, the detail pane, an export and the clipboard. A log that
prints a bearer token is not a rare log, and the line between "I am reading a
production log" and "I have pasted a production credential into a ticket" is one
``Ctrl+E`` wide.

Five things are worth copying out of this file.

**A stage runs on every entry of every render.** A render happens on every
keystroke in the query box, so this is the hottest seam CLV has — and the one
with the tightest budget (`plugin_time_budget_ms`, 250 ms over three
consecutive passes before CLV takes the stage out of service). The cheap
rejection is therefore first and is a plain substring scan: the regex below only
runs on a line that has already been shown to contain one of the key names.

**Return the entry you were given, not a copy of it.** The common case is a line
with no secret in it, and ``return entry`` costs nothing. A stage that rebuilt
every entry to change nothing would pay a ``dataclasses.replace`` per line per
render and would be indistinguishable, from the operator's side, from CLV having
got slower.

**Rewrite every view of the line, or the redaction is cosmetic.** ``raw`` is what
search, export and the clipboard read; ``message`` is what the pane draws; the
values in ``fields`` are what the detail pane tabulates and what ``token:`` in
the query box matches against. Rewriting one and leaving the others is a stage
that hides the secret in the place you were looking and leaves it in the two
places you were not.

**This one is not inert, and the difference from the other examples is egress.**
``watch_alerts`` and ``timeline_marks`` ship delivering nothing until their
section is filled in, because they act on the world. This stage sends nothing
anywhere — it only removes text on its way to the screen — so shipping it with
sensible defaults costs the operator nothing they have to consent to. The rule
is about what leaves the machine, not about whether a plugin has settings.

**Returning ``None`` drops the line.** This stage never does, and that is a
decision rather than an omission: "never silently lose a line" is CLV's second
product requirement, and a filter that deleted lines containing a keyword would
leave a pane with a gap in it and no count anywhere explaining the gap. A stage
that does want to drop should say so in its own documentation, because nothing
in the UI will.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Mapping, Optional, Pattern, Sequence

from clv.api import FilterContext, FilterStage, LogEntry, setting_list

#: Key names redacted when the operator has named none of their own. Defaults
#: rather than an empty list because a redactor that does nothing by default is
#: a redactor nobody finds out is switched off until after they needed it.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "api_key",
    "apikey",
    "secret",
    "token",
    "authorization",
)

DEFAULT_REPLACEMENT = "******"

#: What counts as the *value* after a key name. Two branches, and both are
#: there because the one-branch versions were each wrong:
#:
#: * A quoted run may contain spaces (``authorization: 'Bearer zzz'``), so it
#:   runs to its own closing quote rather than to the next separator.
#: * A bare run stops at the next separator, because a log line is usually
#:   several fields and a greedy value would swallow the timestamp of the next
#:   one along with the secret — which reads as CLV mangling the log.
#:
#: The bare branch requires at least one character. An earlier version let it
#: match the empty string, which made the quoted case "succeed" with an empty
#: value and emit ``authorization: ******'Bearer zzz'`` — the replacement in
#: front of the secret rather than over it.
_VALUE = r"""(?P<lead>\s*[=:]\s*)
             (?:(?P<quote>["\'])(?P<quoted>[^"\']*)(?P=quote)
                |(?P<bare>[^\s,;"\']+))"""


def _compile(patterns: Sequence[str]) -> Optional[Pattern[str]]:
    """One alternation over every key name, or ``None`` for none at all.

    Compiled once per ``configure()`` rather than per line. A stage that
    compiled in ``apply`` would pay for it on every entry of every render; the
    ``re`` module's own cache would hide most of that and would not be something
    to rely on.
    """

    names = [re.escape(name) for name in patterns if name]
    if not names:
        return None
    return re.compile(
        r"(?P<key>\b(?:" + "|".join(names) + r")\b)" + _VALUE,
        re.IGNORECASE | re.VERBOSE,
    )


class Redact(FilterStage):
    """Replaces the value beside a secret-looking key name."""

    name = "redact_secrets"
    requires_api = ">=1.0,<2.0"

    #: Ahead of the default 100 so that a redactor sees the line before a stage
    #: that reformats it. Ordering between two stages that fight over the same
    #: text is the operator's to set and CLV's to make deterministic — see
    #: *Ordering* in ``clv/plugins/AGENTS.md``.
    priority = 50

    def __init__(self) -> None:
        self._patterns: tuple[str, ...] = DEFAULT_PATTERNS
        self._replacement = DEFAULT_REPLACEMENT
        self._matcher = _compile(self._patterns)

    def configure(self, settings: Mapping[str, str]) -> None:
        """Adopt ``[plugin:redact_secrets]``.

        ``setting_list`` rather than ``value.split(",")`` so this plugin's list
        and CLV's own ``log_dirs`` parse identically — quotes stripped, empties
        dropped, whitespace tolerated. An operator should not have to learn a
        second comma convention for the same file.
        """

        named = setting_list(settings, "patterns")
        self._patterns = tuple(named) if named else DEFAULT_PATTERNS
        replacement = str(settings.get("replacement", "")).strip()
        self._replacement = replacement or DEFAULT_REPLACEMENT
        self._matcher = _compile(self._patterns)

    # --- the hot path --------------------------------------------------------

    def apply(self, entry: LogEntry, context: FilterContext) -> Optional[LogEntry]:
        matcher = self._matcher
        if matcher is None or not self._suspect(entry.raw):
            return entry

        raw = matcher.sub(self._mask, entry.raw)
        if raw == entry.raw:
            # The key name appeared but not as a `key = value` pair — a message
            # that merely mentions the word "token", which is exactly the line a
            # cruder redactor turns into asterisks for no reason.
            return entry

        message = (
            matcher.sub(self._mask, entry.message) if entry.message else entry.message
        )
        return replace_entry(entry, raw=raw, message=message, fields=self._fields(entry))

    def _suspect(self, raw: str) -> bool:
        """The cheap rejection, and it runs before the regex does."""

        lowered = raw.casefold()
        return any(name.casefold() in lowered for name in self._patterns)

    def _mask(self, found: "re.Match[str]") -> str:
        """The key and its separator kept, the value replaced, quotes preserved.

        The quote is re-emitted rather than dropped so that a redacted line is
        still the shape the log was written in — a downstream parser reading an
        exported file should not have to cope with CLV having unquoted a field.
        """

        quote = found.group("quote") or ""
        return (
            f"{found.group('key')}{found.group('lead')}"
            f"{quote}{self._replacement}{quote}"
        )

    def _fields(self, entry: LogEntry) -> Optional[Mapping[str, str]]:
        """Field values masked by key name, or ``None`` to leave them alone.

        Keyed on the *field name* rather than re-run over the value text: a
        parsed field already separated the key from the value, so there is
        nothing left to find with a regex and matching one would only risk
        redacting a value that merely looks like a pair.
        """

        if not entry.fields:
            return None
        suspect = {name.casefold() for name in self._patterns}
        masked = {
            key: (self._replacement if key.casefold() in suspect else value)
            for key, value in entry.fields.items()
        }
        if masked == dict(entry.fields):
            return None
        # Read-only on the way back, matching what every entry CLV builds
        # carries. A plain dict would work today and would be the one mutable
        # member of an otherwise frozen object.
        return MappingProxyType(masked)


def replace_entry(
    entry: LogEntry,
    *,
    raw: str,
    message: str,
    fields: Optional[Mapping[str, str]],
) -> LogEntry:
    """``dataclasses.replace``, with ``fields`` left alone when unchanged.

    A small wrapper because ``replace(entry, fields=None)`` would *set* the
    mapping to ``None`` rather than keep it, and ``fields`` is ``compare=False``
    so the mistake would survive an ``==`` in a test.
    """

    from dataclasses import replace

    if fields is None:
        return replace(entry, raw=raw, message=message)
    return replace(entry, raw=raw, message=message, fields=fields)


def register() -> list[FilterStage]:
    return [Redact()]
