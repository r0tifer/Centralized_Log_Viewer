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

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .parsing import LogEntry, LogParser
from .reader import AnyReader, open_reader

#: Builds the reader for a path. Injectable so a provider-backed source (a
#: journal unit, say) can supply its own without this module learning about it.
ReaderFactory = Callable[..., AnyReader]


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

    __slots__ = ("path", "reader", "parser", "entries", "max_lines")

    def __init__(
        self,
        path: Optional[Path],
        *,
        max_lines: int,
        reader: AnyReader | None = None,
    ) -> None:
        self.path = path
        self.reader = reader
        self.max_lines = max_lines
        self.parser = LogParser()
        self.entries: deque[LogEntry] = deque(maxlen=max_lines)

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
        self.entries.extend(self.parser.feed(result.lines))

    def poll(self) -> PollOutcome:
        """Read whatever arrived since the last poll."""

        reader = self.reader
        if reader is None:
            return PollOutcome(self)
        try:
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
            entries = self.parser.feed(result.lines)
            self.entries.extend(entries)
            return PollOutcome(self, entries, rotated=True)

        if not result.lines:
            return PollOutcome(self)

        entries = self.parser.feed(result.lines)
        overflowed = len(self.entries) + len(entries) > (self.entries.maxlen or 0)
        self.entries.extend(entries)
        return PollOutcome(self, entries, overflowed=overflowed)

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
    ) -> None:
        self._buffers: list[SourceBuffer] = []
        self._max_lines = max_lines
        self._reader_factory = reader_factory
        #: Stands in for a buffer's deque when nothing is open, so callers can
        #: always treat `entries` as a sized iterable.
        self._empty: deque[LogEntry] = deque(maxlen=max_lines)

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
        buffer = SourceBuffer(path, max_lines=self._max_lines, reader=reader)
        buffer.prime()
        self.close()
        self._buffers = [buffer]
        return buffer

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
        """Placeholder until Item 13 lands the k-way merge."""

        merged: list[LogEntry] = []
        for buffer in self._buffers:
            merged.extend(buffer.entries)
        return merged

    def origin_of(self, entry: LogEntry) -> Optional[Path]:
        """Which source *entry* came from.

        Marks and watch rules key on this rather than on "the open log", so two
        identical lines from two different logs stay two different lines. With
        one member the answer is that member, which is why this costs nothing
        until there is more than one.
        """

        if len(self._buffers) == 1:
            return self._buffers[0].path
        return None

    # --- tailing ------------------------------------------------------------

    def poll(self) -> list[PollOutcome]:
        """Poll every member, newest content first within each.

        Returns one outcome per buffer that produced something, so the caller
        can decide between an incremental append and a redraw without asking
        each buffer again.
        """

        outcomes: list[PollOutcome] = []
        for buffer in self._buffers:
            outcome = buffer.poll()
            if outcome.entries or outcome.rotated:
                outcomes.append(outcome)
        return outcomes

    def __iter__(self) -> Iterator[SourceBuffer]:
        return iter(self._buffers)

    def __len__(self) -> int:
        return len(self._buffers)

    def __bool__(self) -> bool:
        return bool(self._buffers)


__all__ = ["PollOutcome", "ReaderFactory", "SourceBuffer", "SourceSession"]
