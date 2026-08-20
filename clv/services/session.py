"""Ownership of the readers and buffers behind the pane.

The app used to hold one reader, one parser and one bounded deque as three
attributes of the shell, which made "the source" and "the buffer" the same
thing. Every feature built on top of that — filters, marks, watch rules,
navigation, export — reached for ``self._entries`` and was single-source by
construction, not by choice.

Here the two are separated. A :class:`SourceBuffer` is one reader plus the
lines it produced; a :class:`SourceSession` is the ordered set of buffers the
pane is currently showing. **A single open log is a set of one**, and that is
the whole point: there is no separate single-source path to keep in step with
the merged one, so a feature that works on a session works on both.

UI-free, like every other service: this module reads files and holds lines, and
knows nothing about how any of it is rendered. The tail *clock* stays in the
app — :meth:`SourceSession.poll` is called by whatever timer the shell already
runs, so nothing here can drift out of step with the poll that drives watch
rules.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

from .backend import cheap_only
from .filtering import sortable_moment
from .parsing import LogEntry, LogParser
from .reader import AnyReader, TailRead, open_reader
from .refs import format_ref, parse_ref

#: Builds the reader for a path. Injectable so a provider-backed source (a
#: journal unit, say) can supply its own without this module learning about it.
ReaderFactory = Callable[..., AnyReader]

#: Field name carrying which **machine** a line was read from.
#:
#: `host` is taken and means *what the log says about itself* — syslog, access
#: logs and journald's `_HOSTNAME` all normalise into it. `node` means *where
#: CLV read it from*. Keeping them apart is what lets `node:web01 status>=500`
#: work without changing what a single saved query already means.
NODE_FIELD = "node"

#: Field name carrying which file a line came from, set on entries whose
#: source is not simply "the open log" — a member of a rotated set, or a
#: member of a merged view.
#:
#: Deliberately **not** in ``query.NORMALISED_FIELD_KEYS``: a key is a query
#: term only when it is known, and the buffer's own field names are part of
#: that vocabulary. So `source:app.log.1` filters exactly when entries carry an
#: origin, and stays plain text everywhere else — which is what keeps every
#: query anyone had saved meaning what it did.
ORIGIN_FIELD = "source"


#: One comparable form for a timestamp, for ordering a whole set. Defined in
#: `filtering` alongside the pairwise rule it is the bulk form of, and aliased
#: here because this module was where it was first needed — the timeline needs
#: exactly the same decision, and two copies of it would be two places for the
#: merge and the histogram to disagree about a mixed-timezone source.
_sortable = sortable_moment


def _localised(moment: datetime, facts: "SourceFacts") -> datetime:
    """*moment* as an absolute instant, given the machine it was written on.

    Applied **only to the sort key**, never to the entry. A displayed timestamp
    is always exactly what the log said — that rule does not bend for ordering,
    and a stamp that differed from the raw text with no explanation would be a
    worse failure than the misordering this fixes.

    Two corrections, in order:

    * A **naive** stamp is read in its own machine's zone. This is the fix that
      matters and it costs the merge nothing: once every stamp is aware, the
      existing comparison orders them correctly with no further special case.
    * The measured **skew** is subtracted when the host asked for it. Off by
      default, because a clock that is four seconds fast is a fact to report
      rather than one to quietly paper over.
    """

    if moment.tzinfo is None and facts.zone is not None:
        moment = moment.replace(tzinfo=facts.zone)
    if facts.correct_skew and facts.skew:
        moment = moment - facts.skew
    return moment


def tag_origins(
    entries: Sequence[LogEntry], origins: Sequence[Path]
) -> list[LogEntry]:
    """Record on each entry which file it came from.

    Provenance rather than parsed structure, which is why this happens here and
    not in the parser: a continuation line has no host of its own to report,
    but it certainly came from a file, so unlike the parser's fields this is
    honest to carry forward onto one.
    """

    tagged: list[LogEntry] = []
    for entry, origin in zip(entries, origins):
        # format_ref, so the value here is the same string the source persists
        # under. For two hosts that is what keeps `source:/var/log/syslog` from
        # matching both of them at once — the ref string is host-qualified, and
        # that is a correctness requirement rather than presentation.
        tagged.append(
            replace(entry, fields={**entry.fields, ORIGIN_FIELD: format_ref(origin)})
        )
    return tagged


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """What a source's *machine* contributes to reading and ordering it.

    Empty for a local source, which is why nothing local changes: an all-local
    set has one zone, no node names, and no skew, and every rule below then
    reduces to what it did before.
    """

    #: CLV's name for the machine, or ``None`` for this one. Carried onto every
    #: entry the source produces so ``node:web01`` is answerable.
    node: Optional[str] = None
    #: The zone a **naive** timestamp from this source should be read in. Syslog
    #: carries no offset at all, so without this a stamp from a UTC host and one
    #: from an EST host are compared as though both were local.
    zone: Optional[tzinfo] = None
    #: Remote clock minus local clock, as measured at connect. Always reported;
    #: only applied to ordering when the host asked for it.
    skew: timedelta = timedelta(0)
    #: Whether to apply :attr:`skew` when ordering. Off by default: skew is
    #: reported honestly and corrected only on request.
    correct_skew: bool = False


#: The answer for a source on this machine. A callable rather than a constant
#: because ``SourceSession`` takes it as an injected default.
def local_facts(ref: object) -> SourceFacts:
    """Facts for a local source: its zone, and nothing else.

    The zone is filled in even locally, and that is what makes a *mixed* set
    orderable: with one member local and one on a UTC host, both sides need a
    zone or there is nothing to compare across.
    """

    return SourceFacts(zone=datetime.now().astimezone().tzinfo)


@dataclass(slots=True)
class PollOutcome:
    """What one buffer produced on one tail poll."""

    buffer: "SourceBuffer"
    entries: list[LogEntry] = field(default_factory=list)
    #: The file was rotated or truncated: what the buffer holds was rebuilt
    #: rather than appended to, so the pane needs a full redraw.
    rotated: bool = False
    #: The ring buffer dropped older lines to fit these, so the visible window
    #: shifted and an incremental append would render the wrong thing.
    overflowed: bool = False
    #: Why this source has stopped producing, if it has. Empty on every ordinary
    #: poll — including one that found nothing, which is the common case and
    #: emphatically not a problem.
    #:
    #: Reported rather than raised, because a source going away is not an error
    #: worth taking the pane down for and never has been. What Phase 5 adds is
    #: that it is no longer *silent*: a remote log that stopped because its link
    #: dropped now looks different from one that stopped because nobody is
    #: writing to it, which is the whole of Requirement 7.
    problem: str = ""

    @property
    def notice(self) -> str:
        """The reload message for this buffer's reader kind."""

        return self.buffer.reload_notice


class SourceBuffer:
    """One reader, its parser, and the bounded deque of what it has produced.

    The parser lives here rather than in the session because continuation
    carry-forward is per source: a stack trace in one log must never inherit a
    timestamp from a line that arrived in another.
    """

    __slots__ = (
        "path", "reader", "parser", "entries", "max_lines", "tag_origin",
        "revision", "facts",
    )

    def __init__(
        self,
        path: Optional[Path],
        *,
        max_lines: int,
        reader: AnyReader | None = None,
        tag_origin: bool = False,
        facts: SourceFacts | None = None,
    ) -> None:
        self.path = path
        self.reader = reader
        self.max_lines = max_lines
        self.parser = LogParser()
        self.entries: deque[LogEntry] = deque(maxlen=max_lines)
        #: Record this buffer's path on every entry it produces. Set when the
        #: buffer is one of several, so a merged view can say where a line came
        #: from — paid once per line as it is read, never per render.
        self.tag_origin = tag_origin
        #: Which machine this source is on, and what that means for ordering it.
        #: Empty for a local source, which is what keeps every local path here
        #: exactly as it was.
        self.facts = facts or SourceFacts()
        #: Bumped whenever `entries` changes, so a merge can be cached against
        #: it rather than recomputed on every keystroke in the query box.
        self.revision = 0

    @property
    def reload_notice(self) -> str:
        """Formatted reload message, or empty when there is no reader."""

        reader = self.reader
        if reader is None:
            return ""
        return reader.RELOAD_NOTICE.format(name=reader.path.name)

    def prime(self) -> None:
        """Perform the reader's initial bounded read. Raises ``OSError``."""

        if self.reader is None:
            return
        result = self.reader.prime()
        self.parser.reset()
        self.entries.clear()
        self.entries.extend(self._feed(result))
        self.revision += 1

    def _feed(self, result: TailRead) -> list[LogEntry]:
        """Parse a read's lines, recording where each one came from.

        ``LogParser.feed`` is one entry per line, which is what lets the
        reader's parallel ``origins`` line up with the entries produced.
        """

        entries = self.parser.feed(result.lines)
        if result.origins:
            entries = tag_origins(entries, result.origins)
        elif self.tag_origin and self.path is not None:
            entries = tag_origins(entries, [self.path] * len(entries))
        return self._tag_node(entries)

    def _tag_node(self, entries: list[LogEntry]) -> list[LogEntry]:
        """Record which machine these lines were read from, when it is not this one.

        Provenance, exactly like :func:`tag_origins` — and applied whether or
        not the buffer is part of a merge, because ``node:web01`` should answer
        on a single open remote source too.

        A local source is left completely untouched: no key is added, no entry
        is copied, and the local path costs nothing it did not cost before.
        """

        node = self.facts.node
        if node is None:
            return entries
        return [
            replace(entry, fields={**entry.fields, NODE_FIELD: node})
            for entry in entries
        ]

    def poll(self) -> PollOutcome:
        """Read whatever arrived since the last poll.

        The single funnel every reader's ``poll()`` passes through, which is why
        the cheap-only guard is entered here rather than in each reader. Inside
        it a backend method declared blocking raises instead of stalling: this
        runs on a ``set_interval`` timer at ``refresh_hz``, per member of the
        set, and a round trip here is a frozen UI twice a second.

        :class:`~clv.services.backend.BlockingCallError` is deliberately **not**
        caught. It is a design error rather than a source that went away — a
        reader whose poll needs a blocking read (re-priming a changed archive,
        say) cannot be driven from here at all, and must follow the source with
        something drained non-blocking instead. Swallowing it would hide that
        until the UI froze.
        """

        reader = self.reader
        if reader is None:
            return PollOutcome(self)
        try:
            with cheap_only():
                result = reader.poll()
        except OSError:
            # A source that vanished mid-session is not an error worth taking
            # the pane down for; the next poll finds it again or it stays quiet.
            return PollOutcome(self)

        if result.rotated:
            # A rotated file can be a different shape entirely, so the parser
            # starts over rather than carrying state across the boundary.
            self.parser.reset()
            self.entries.clear()
            entries = self._feed(result)
            self.entries.extend(entries)
            self.revision += 1
            return PollOutcome(self, entries, rotated=True, problem=result.problem)

        if not result.lines:
            # The one place a *silent* stop used to be indistinguishable from a
            # quiet log. Both still return here; only one of them says why.
            return PollOutcome(self, problem=result.problem)

        entries = self._feed(result)
        overflowed = len(self.entries) + len(entries) > (self.entries.maxlen or 0)
        self.entries.extend(entries)
        self.revision += 1
        return PollOutcome(
            self, entries, overflowed=overflowed, problem=result.problem
        )

    def close(self) -> None:
        """Release whatever the reader holds open.

        Most readers hold nothing between calls, which is why this is optional
        on the reader protocol. A provider-backed reader may own a subprocess,
        and that is exactly the thing that must not leak on a source switch.
        """

        closer = getattr(self.reader, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001 - third-party readers
                pass


class SourceSession:
    """The ordered set of buffers the pane is showing.

    One member is the ordinary case and costs nothing extra: :attr:`entries`
    hands back that buffer's deque itself rather than a copy, so rendering a
    single log does no merging work at all.
    """

    def __init__(
        self,
        *,
        max_lines: int,
        reader_factory: ReaderFactory = open_reader,
        facts: Callable[[Path], SourceFacts] = local_facts,
    ) -> None:
        self._buffers: list[SourceBuffer] = []
        self._max_lines = max_lines
        self._reader_factory = reader_factory
        #: What machine a source is on, and what that means for ordering it.
        #: Injected for the same reason `reader_factory` is: this module may not
        #: know that SSH exists, and the answer for a remote source comes from
        #: the host that was probed at connect.
        self._facts = facts
        #: Stands in for a buffer's deque when nothing is open, so callers can
        #: always treat `entries` as a sized iterable.
        self._empty: deque[LogEntry] = deque(maxlen=max_lines)
        #: (buffer revisions, merged list). A render happens on every keystroke
        #: in the query box; a merge must not.
        self._merge_cache: Optional[tuple[tuple[int, ...], list[LogEntry]]] = None
        self._anchored = 0

    # --- membership ---------------------------------------------------------

    @property
    def buffers(self) -> tuple[SourceBuffer, ...]:
        return tuple(self._buffers)

    @property
    def is_merged(self) -> bool:
        return len(self._buffers) > 1

    @property
    def primary(self) -> SourceBuffer | None:
        """The first buffer — the only one, unless a merge is active."""

        return self._buffers[0] if self._buffers else None

    @property
    def primary_path(self) -> Optional[Path]:
        buffer = self.primary
        return buffer.path if buffer is not None else None

    @property
    def max_lines(self) -> int:
        return self._max_lines

    def set_primary_path(self, path: Optional[Path]) -> None:
        """Point the session at *path* without opening a reader for it.

        The detached case: a caller that has lines of its own and only needs
        the session to agree about where they came from.
        """

        if path is None:
            self.close()
            return
        buffer = self.primary
        if buffer is None:
            self._buffers.append(SourceBuffer(path, max_lines=self._max_lines))
        else:
            buffer.path = path

    def open_single(self, path: Path) -> SourceBuffer:
        """Replace the set with one primed buffer on *path*.

        Raises ``OSError`` if the initial read fails, leaving the previous set
        untouched — a source that would not open must not also cost the one
        that was working.
        """

        reader = self._reader_factory(path, max_lines=self._max_lines)
        buffer = SourceBuffer(
            path,
            max_lines=self._max_lines,
            reader=reader,
            facts=self._facts(path),
        )
        buffer.prime()
        self.close()
        self._buffers = [buffer]
        self._merge_cache = None
        return buffer

    def open_rotated(self, rotated_set) -> SourceBuffer:
        """Replace the set with one buffer spanning a whole rotated log.

        Same contract as :meth:`open_single` — several files, still one source,
        and the lines come back tagged with the member each one is from.
        Imported where it is used because rotation builds on this module's
        readers rather than the other way round.
        """

        from .rotation import RotatedSetReader

        reader = RotatedSetReader(
            rotated_set,
            max_lines=self._max_lines,
            # The session's own factory, so a set's live head is opened by
            # whatever opens every other source. Local behaviour is identical —
            # the default factory *is* `open_reader` — and it stops this being
            # the one path that decides for itself.
            reader_factory=self._reader_factory,
        )
        buffer = SourceBuffer(
            reader.path,
            max_lines=self._max_lines,
            reader=reader,
            facts=self._facts(reader.path),
        )
        buffer.prime()
        self.close()
        self._buffers = [buffer]
        self._merge_cache = None
        return buffer

    def open_many(self, paths: Sequence[Path]) -> tuple[list[SourceBuffer], list[tuple[Path, str]]]:
        """Open several sources as one merged stream.

        Returns the buffers that opened and the ones that did not, with why:
        a member that has been deleted since it was chosen must not stop the
        others from opening, and must not disappear without being mentioned.

        ``max_buffer_lines`` applies **per source**, so the memory cost is
        ``n * max_buffer_lines`` and every member keeps a full history of its
        own rather than the loudest one crowding out the rest.
        """

        opened, failed = self.prepare_many(paths)
        self.install_many(opened)
        return opened, failed

    def prepare_many(
        self, paths: Sequence[Path]
    ) -> tuple[list[SourceBuffer], list[tuple[Path, str]]]:
        """Build and prime buffers for *paths* **without** committing them.

        Split out of :meth:`open_many` so the expensive half can run somewhere
        other than the event loop. Every ``prime()`` below is a bounded read —
        microseconds locally, a round trip per member remotely — and a merge of
        five remote logs done inline is five round trips of frozen UI.

        Touches no session state, so a caller may abandon what it gets back if
        the operator has moved on; :meth:`install_many` is what commits it.
        """

        opened: list[SourceBuffer] = []
        failed: list[tuple[Path, str]] = []
        for path in paths:
            try:
                reader = self._reader_factory(path, max_lines=self._max_lines)
                buffer = SourceBuffer(
                    path,
                    max_lines=self._max_lines,
                    reader=reader,
                    # Several members means "which source" stops having a
                    # constant answer, so every line records its own.
                    tag_origin=len(paths) > 1,
                    facts=self._facts(path),
                )
                buffer.prime()
            except OSError as exc:
                failed.append((path, str(exc)))
                continue
            opened.append(buffer)
        return opened, failed

    def install_many(self, buffers: Sequence[SourceBuffer]) -> list[SourceBuffer]:
        """Replace the set with several **already primed** buffers.

        :meth:`open_many`'s other half, for the same reason :meth:`install` is
        :meth:`open_single`'s: priming a remote member is a round trip, so it
        belongs on a worker, while committing belongs on the event loop where it
        can be checked against what the operator has selected since.

        An empty *buffers* leaves the current set alone — a merge where nothing
        opened must not also close the source that was working, which is the
        rule ``open_many`` already follows.
        """

        if not buffers:
            return []
        self.close()
        self._buffers = list(buffers)
        self._merge_cache = None
        return self._buffers

    def install(self, buffer: SourceBuffer) -> SourceBuffer:
        """Replace the set with a buffer that has **already** been primed.

        The split :meth:`open_single` cannot offer: priming a remote source is a
        network round trip and belongs on a worker, but committing it belongs on
        the event loop, where it can be checked against what the operator has
        selected *since*. Doing both in the worker would race two opens into one
        session; doing both on the loop would freeze the UI for a round trip.

        The previous set is closed only once the new buffer is in hand, so a
        source that would not open still does not cost the one that was working.
        """

        self.close()
        self._buffers = [buffer]
        self._merge_cache = None
        return buffer

    def adopt(self, path: Path, reader) -> SourceBuffer:
        """Replace the set with one buffer over a reader built elsewhere.

        For sources core cannot open by itself: a plugin knows how to read a
        journal unit, and this is where what it built joins everything else.
        """

        buffer = SourceBuffer(
            path,
            max_lines=self._max_lines,
            reader=reader,
            # A provider source has a machine too, now that one can be on
            # another host: without this a remote journal unit carried no
            # `node`, so `node:web01` would not answer for it and a cross-host
            # merge would order it by this machine's clock.
            facts=self._facts(path),
        )
        buffer.prime()
        self.close()
        self._buffers = [buffer]
        self._merge_cache = None
        return buffer

    def push_severity(self, severity: str) -> bool:
        """Offer the severity bucket to readers that can filter at the source.

        Most cannot and are left alone. One that can — a journal follow, which
        would otherwise carry every debug line across a pipe for the client to
        throw away — restarts, and says so by returning True, because the lines
        it already handed over are from the old query.
        """

        restarted = False
        for buffer in self._buffers:
            setter = getattr(buffer.reader, "set_severity", None)
            if setter is None:
                continue
            try:
                if setter(severity):
                    buffer.prime()
                    restarted = True
            except OSError:
                continue
        return restarted

    def close(self) -> None:
        """Drop every buffer, releasing whatever their readers hold."""

        for buffer in self._buffers:
            buffer.close()
        self._buffers = []

    def resize(self, max_lines: int) -> None:
        """Adopt a new buffer cap, as a config reload can produce."""

        self._max_lines = max_lines
        self._empty = deque(maxlen=max_lines)

    # --- content ------------------------------------------------------------

    @property
    def entries(self):
        """Every entry the pane should consider, in order.

        With one member this **is** that buffer's deque, not a copy: callers
        mutate it (the tests that seed a pane do exactly that) and the app's
        common path must not pay for a merge that has nothing to merge.
        """

        if not self._buffers:
            return self._empty
        if len(self._buffers) == 1:
            return self._buffers[0].entries
        return self._merged()

    def set_entries(self, entries: Iterable[LogEntry]) -> None:
        """Replace the primary buffer's lines wholesale."""

        if isinstance(entries, deque):
            replacement = entries
        else:
            replacement = deque(entries, maxlen=self._max_lines)
        buffer = self.primary
        if buffer is None:
            self._empty = replacement
            return
        buffer.entries = replacement

    def _merged(self) -> list[LogEntry]:
        """Every member's lines as one timestamp-ordered stream.

        A **view** over the buffers, never a fourth copy of the lines: the
        entries in the list returned are the same objects the buffers hold, so
        ``n * max_buffer_lines`` remains the whole memory cost of a merge.

        Cached against the buffers' revisions rather than recomputed, because
        this is read on every render and a render happens on every keystroke in
        the query box. The cost is therefore paid per *poll*, not per keystroke.
        """

        revisions = tuple(buffer.revision for buffer in self._buffers)
        if self._merge_cache is not None and self._merge_cache[0] == revisions:
            return self._merge_cache[1]

        # Aware and naive stamps are compared by dropping the offset, which is
        # the rule `filtering._comparable` already established. Only *needed*
        # when both kinds are present, so a set that is entirely aware keeps
        # its offsets and orders across time zones correctly.
        naive = any(
            entry.timestamp is not None and entry.timestamp.tzinfo is None
            for buffer in self._buffers
            for entry in buffer.entries
        )
        # ...except when the members are on machines in *different* zones, where
        # dropping the offset is exactly the bug. Syslog carries no offset at
        # all, so a naive stamp from a UTC host and one from an EST host are
        # otherwise compared directly: five hours of confident misordering, with
        # the error appearing before the request that caused it and nothing on
        # screen suggesting anything is amiss.
        #
        # Engaged **only** when the zones actually disagree. A single-zone set —
        # every all-local merge, and every merge of hosts that share a zone —
        # takes the branch above unchanged, which is what keeps local behaviour
        # identical rather than merely equivalent.
        spans_zones = self._spans_zones()

        anchored = 0
        streams: list[list[tuple[tuple, LogEntry]]] = []
        for index, buffer in enumerate(self._buffers):
            keyed: list[tuple[tuple, LogEntry]] = []
            last: Optional[datetime] = None
            for position, entry in enumerate(buffer.entries):
                moment = entry.timestamp
                if moment is None:
                    # Anchored after the last timestamped entry from *its own
                    # source*, never dropped and never guessed at: the position
                    # tiebreaker keeps it directly after the line it followed.
                    anchored += 1
                elif spans_zones:
                    last = _localised(moment, buffer.facts)
                else:
                    last = _sortable(moment, naive=naive)
                if last is None:
                    # Nothing to anchor to yet — this source has not produced a
                    # timestamp at all. Sorts before everything, in its own
                    # source's order. `None` is only ever compared for
                    # equality here, because rank keeps the ranks apart.
                    keyed.append(((0, None, index, position), entry))
                else:
                    keyed.append(((1, last, index, position), entry))
            # A log is usually written in order, but nothing guarantees it, and
            # heapq.merge trusts its inputs. Sorting here is what makes the
            # merge correct for a source whose own stamps jump around.
            keyed.sort(key=lambda item: item[0])
            streams.append(keyed)

        merged = [entry for _key, entry in heapq.merge(*streams, key=lambda item: item[0])]
        self._anchored = anchored
        self._merge_cache = (revisions, merged)
        return merged

    def _spans_zones(self) -> bool:
        """Whether the members disagree about what a naive stamp means.

        Two things have to be true: more than one member, and more than one
        distinct UTC offset among them. Either alone is the ordinary case and
        must keep the ordinary behaviour — which is what makes this safe to add
        to a merge that has worked for years.

        Skew correction also counts as a disagreement, because a corrected
        stamp and an uncorrected one are on different clocks even when they are
        in the same zone.
        """

        if len(self._buffers) < 2:
            return False
        if any(buffer.facts.correct_skew and buffer.facts.skew for buffer in self._buffers):
            return True
        offsets = {
            None if buffer.facts.zone is None else buffer.facts.zone.utcoffset(None)
            for buffer in self._buffers
        }
        return len(offsets) > 1 and None not in offsets

    def skew_spread(self) -> timedelta:
        """The widest disagreement between the members' clocks.

        Reported **always**, corrected only on request. A set whose hosts agree
        says nothing; one that disagrees says by how much, so an operator
        reading causation out of the interleaving knows how much to trust it.
        """

        if len(self._buffers) < 2:
            return timedelta(0)
        skews = [buffer.facts.skew for buffer in self._buffers]
        return max(skews) - min(skews)

    @property
    def corrected(self) -> bool:
        """Whether any member's timestamps are being adjusted for skew.

        The pane says so when this is true: a displayed stamp that differs from
        the raw log text must never be unexplained.
        """

        return any(
            buffer.facts.correct_skew and buffer.facts.skew
            for buffer in self._buffers
        )

    @property
    def anchored(self) -> int:
        """How many merged entries had no timestamp of their own.

        Reported rather than hidden: they were placed by inference, and the
        "never silently lose a line" rule applies to ordering too.
        """

        if self.is_merged:
            self._merged()
        return self._anchored

    def lands_at_the_end(self, arrived: Sequence[LogEntry]) -> bool:
        """Whether *arrived* sorted to the very end of the merged stream.

        The question a caller asks before appending rows to a pane instead of
        rebuilding it. Tailing several live logs at once is the case where the
        answer is yes — they are all producing "now" — and a line that sorts
        into the middle is the case where an append would put it in the wrong
        place, so the caller redraws instead. Identity, not equality: two
        identical lines from two sources are different entries.
        """

        if not arrived or not self.is_merged:
            # One source appends to itself by definition; there is no ordering
            # decision to second-guess, and `entries` is that buffer's deque.
            return True
        merged = self.entries
        if len(merged) < len(arrived):
            return False
        tail = {id(entry) for entry in merged[-len(arrived) :]}
        return all(id(entry) in tail for entry in arrived)

    def origin_of(self, entry: LogEntry) -> Optional[Path]:
        """Which source *entry* came from.

        Marks and watch rules key on this rather than on "the open log", so two
        identical lines from two different logs stay two different lines.

        An entry that was tagged at read time answers for itself — that is the
        rotated-set case, where one source is several files. Otherwise the
        answer is the one open source, which is why this costs nothing at all
        in the ordinary case.
        """

        tagged = entry.fields.get(ORIGIN_FIELD)
        if tagged:
            return parse_ref(tagged)
        if len(self._buffers) == 1:
            return self._buffers[0].path
        return None

    def moment_mapper(self) -> Optional[Callable[[LogEntry], Optional[datetime]]]:
        """How the histogram should read a stamp — or ``None`` for "as written".

        The timeline has to key its buckets by the same thing :meth:`_merged`
        sorts by, or the bar and the pane beneath it describe two different
        orderings of the same lines. That only diverges when the members
        disagree about what a naive stamp means, so this returns ``None`` in
        every other case and the histogram keeps the code path it has always
        had — the same guard, and the same reason for it, as the merge.

        Returns a **mapper rather than a per-entry method** so the origin
        lookup is built once for a whole histogram instead of once per line: a
        filtered set is thousands of entries and the buffer list is single
        digits, and rebuilding the map inside the loop would put that product
        on the redraw path. The closure stays valid for as long as the buffers
        do, which is exactly as long as the grid it is stored on does.
        """

        if not self._spans_zones():
            return None

        facts = {format_ref(buffer.path): buffer.facts for buffer in self._buffers}
        fallback = self._buffers[0].facts

        def mapper(entry: LogEntry) -> Optional[datetime]:
            moment = entry.timestamp
            if moment is None:
                return None
            # An untagged entry can only be the single-buffer case, which
            # `_spans_zones` has already excluded — but a member whose origin
            # is unknown must not be silently placed on another machine's
            # clock, so it falls back to the first member exactly as the merge
            # would have read it before any of this existed.
            origin = entry.fields.get(ORIGIN_FIELD, "")
            return _localised(moment, facts.get(origin, fallback))

        return mapper

    # --- tailing ------------------------------------------------------------

    def poll(self) -> list[PollOutcome]:
        """Poll every member, newest content first within each.

        Returns one outcome per buffer that produced something, so the caller
        can decide between an incremental append and a redraw without asking
        each buffer again.

        **A stoppage counts as something produced**, and that is the second
        place this phase had to fix: filtering on lines alone meant a source
        that had died reported nothing to report, because a source that has died
        is precisely one with no lines. The verdict never reached the pane, and
        the drop stayed as quiet here as it had been a layer below.
        """

        outcomes: list[PollOutcome] = []
        for buffer in self._buffers:
            outcome = buffer.poll()
            if outcome.entries or outcome.rotated or outcome.problem:
                outcomes.append(outcome)
        return outcomes

    def __iter__(self) -> Iterator[SourceBuffer]:
        return iter(self._buffers)

    def __len__(self) -> int:
        return len(self._buffers)

    def __bool__(self) -> bool:
        return bool(self._buffers)


__all__ = ["PollOutcome", "ReaderFactory", "SourceBuffer", "SourceSession"]
