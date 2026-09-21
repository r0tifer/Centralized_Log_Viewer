"""A watch rule kind and a watch destination, as CLV plugins.

A worked ``WatchMatcher`` and ``WatchSink`` example. Drop this file in
``~/.config/clv/plugins/`` (CLV puts it in ``examples/`` for you) and add it to
``settings.conf``::

    [log_viewer]
    plugins = watch_alerts

    [plugin:watch_alerts]
    # Until this is set the sink delivers nothing. That is deliberate — see
    # "Egress is a decision the operator makes" below.
    path = /var/log/clv-alerts.log

It adds two things a watch rule could not do before.

``burst`` rules
    A rule kind whose pattern is not a query but a parameter of this plugin's
    own: ``oom-killer x5/60`` fires when that substring has appeared five times
    within sixty seconds. "This happened, once" and "this is happening a lot"
    are different events, and only the first had a spelling.

A file destination
    Every watch hit appended to a file, so a rule can leave a record that
    outlives the session rather than a toast that is gone in four seconds.

Five things are worth copying out of this file.

**Your kind owns its pattern; CLV does not parse it.** The moment a rule
declares ``kind = "burst"``, ``oom-killer x5/60`` stops being a query and
becomes this plugin's string to read. Nothing checks it but
:meth:`Burst.validate`, so implementing that hook is what puts a typo in front
of the operator who typed it instead of leaving them a rule that silently never
fires.

**A matcher is offered lines, and only lines.** :meth:`Burst.matches` is called
per newly arrived entry — including repeats of a line CLV has already seen,
which a pattern rule's cached answer would have covered. There is no tick,
though, so a rule that must fire *because nothing arrived* cannot be written
here at all: a burst is expressible, a silence is not. Say what your kind can do
in its docstring; the operator cannot read the interface.

**Per-entry state is yours to bound.** This one keeps a deque of timestamps per
rule, capped by the window, and drops what falls out of it. A matcher that
accumulated without bound would exhaust the process, and nothing in CLV would
notice — the budget measures time, not memory.

**Egress is a decision the operator makes, not one this plugin makes for them.**
The sink ships inert: with no ``path`` in its ``[plugin:watch_alerts]`` section
it delivers nothing and says nothing, exactly as the journal provider reads its
opt-in before it offers a single source. A webhook version of this class would
be the same shape with ``urllib`` in place of the ``open()`` — and would have
the same obligation, for much stronger reasons. ``clv/plugins/AGENTS.md`` writes
that one out in full under *Watch*.

**This sink does not ask for your log lines.** ``wants_entries`` is left at
``False``, so it is handed a rule name and a count and could not write a log
line into the file if it tried. A sink that sets it is shown to the operator in
the ``P`` dialog with a ``⚑``, which is the right price for the capability —
but a sink that does not need it should not pay it, and this one does not: the
name and the count are the whole of what an alert record needs.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from clv.api import (
    FilterContext,
    LogEntry,
    WatchMatcher,
    WatchRule,
    WatchSink,
)

#: ``<text> x<count>/<seconds>`` — the whole of this kind's grammar, and it is
#: deliberately small. A rule parameter that needed a parser would be a DSL, and
#: an operator typing one into a dialog wants a spelling they can remember.
_BURST = re.compile(r"^(?P<text>.+?)\s+x(?P<count>\d+)/(?P<seconds>\d+)$")


class Burst(WatchMatcher):
    """``burst`` — fires when a line has repeated often enough, fast enough.

    Pattern: ``oom-killer x5/60``. The text is a plain substring, not a regex:
    this runs per entry and a substring scan is the cheap rejection the
    performance notes ask for.

    **Fires on the line that crosses the threshold, and then again only when a
    fresh burst builds.** The window is cleared on a hit, so a rule matching
    five hundred lines produces bursts rather than four hundred and ninety-six
    consecutive alerts. CLV's rate limiter would have coalesced those anyway;
    doing it here too means the *count* the sink is handed is a count of bursts,
    which is what the rule is about.
    """

    name = "watch-alerts"
    kind = "burst"

    def __init__(self) -> None:
        #: ``rule name -> the timestamps still inside its window``. Per rule,
        #: because two rules of this kind are two independent questions, and
        #: keyed by name because that is what identifies a rule to CLV.
        self._seen: Dict[str, deque] = {}

    def validate(self, pattern: str) -> Optional[str]:
        """Reject a malformed parameter where the operator typed it."""

        if _BURST.match(pattern) is None:
            return "A burst rule looks like: oom-killer x5/60 (text x count/seconds)."
        return None

    def matches(self, entry: LogEntry, rule: WatchRule) -> bool:
        parsed = _BURST.match(rule.pattern)
        if parsed is None:
            # Unreachable through the dialog, which calls `validate` first — but
            # a rule can arrive from a hand-edited state file, and a matcher
            # that raised on one would be disabled for the session over
            # somebody else's typo.
            return False
        if parsed.group("text") not in entry.raw:
            return False

        window = float(parsed.group("seconds"))
        needed = int(parsed.group("count"))
        now = self._now(entry)
        seen = self._seen.get(rule.name)
        if seen is None:
            seen = self._seen[rule.name] = deque()
        seen.append(now)
        # Bounded by the window rather than by a maxlen: what makes this deque
        # safe is that everything older than `window` leaves it on every call.
        while seen and now - seen[0] > window:
            seen.popleft()
        if len(seen) < needed:
            return False
        seen.clear()
        return True

    @staticmethod
    def _now(entry: LogEntry) -> float:
        """The entry's own time, falling back to the clock.

        A line's timestamp is the right clock for "five of these in a minute" —
        reading a file that was written an hour ago should still find the bursts
        in it. A format that carries no timestamp has no answer, and then the
        only honest fallback is now.
        """

        stamp = entry.timestamp
        if stamp is None:
            return datetime.now(timezone.utc).timestamp()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()


class AlertFile(WatchSink):
    """Appends one line per watch hit to a file named in ``settings.conf``.

    Inert until configured, and that is the whole of the consent pattern: a
    plugin that delivers somewhere should not decide where on the operator's
    behalf, and "nowhere" is the only safe default a plugin author can pick.
    """

    name = "watch-alerts"
    # Left at the default deliberately. See this module's docstring.
    wants_entries = False

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        #: The last write failure, kept rather than raised. A plugin that holds
        #: on to why it could not do its job has somewhere to look; one that
        #: swallows the exception has nothing.
        self._last_error: Optional[str] = None

    def configure(self, settings) -> None:
        # The mapping is live: CLV updates it in place when it re-reads the
        # settings file, so reading through it in `deliver` rather than copying
        # here would also work. Held as a Path because the alternative is
        # building one per delivery.
        raw = settings.get("path", "").strip()
        self._path = Path(raw).expanduser() if raw else None

    def deliver(
        self,
        name: str,
        count: int,
        context: FilterContext,
        entries: Sequence[LogEntry] = (),
    ) -> None:
        if self._path is None:
            # Not an error and not worth reporting: an unconfigured sink is the
            # state this plugin ships in, and an operator who has not set a path
            # has not asked for anything to happen.
            return
        moment = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = "line" if count == 1 else "lines"
        try:
            # This runs on CLV's sink thread, never on the event loop, so
            # blocking here is allowed in a way it is allowed nowhere else in a
            # plugin. There is still a deadline: see `plugin_sink_timeout_ms`.
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(f"{moment}\t{name}\t{count} {lines}\n")
        except OSError as exc:
            # Raising would be legitimate — CLV would disable this sink and name
            # it in the P dialog. It is refused here because a full disk or a
            # rotated-away directory is a transient condition, and taking the
            # sink out of service for the session would mean losing every later
            # alert to a problem that fixed itself.
            self._last_error = str(exc)


__all__ = ["AlertFile", "Burst"]
