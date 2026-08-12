"""Marked lines — the ones that matter, in a buffer of five thousand.

Starring works on *logs*. This works on *lines*: mark the three that matter
while reading, then step between them. That is how an incident actually gets
pieced together, and there was no way to do it.

Keyed by content, not by position
---------------------------------

A mark records the source path plus a digest of the line's raw text. The
obvious alternative — a buffer index — is wrong here, because the buffer is a
bounded deque: every tailed line that pushes one off the front shifts every
index below it, so a mark set at index 400 would silently start pointing at a
different line. A content key survives that, survives a filter change that hid
the line and brought it back, and quietly stops matching once the line has
rotated out of the buffer entirely. A mark is a reading convenience, not a
promise to keep the line alive.

Identical lines share a key, so marking one marks every copy in that source.
That is a real limitation and the right trade: the alternative is a positional
key, which is the bug above.

**Never persisted.** ``AGENTS.md`` is explicit that session state stores paths
and settings only, and a mark is neither — the digest is derived from log
content, and the set of lines someone marked is a record of what they were
reading. :class:`MarkSet` therefore has no ``to_dict``, and nothing in
``SessionState`` refers to it. This is a deliberate constraint, not an
oversight to be tidied up later.
"""

from __future__ import annotations

from hashlib import blake2b
from pathlib import Path
from typing import Iterable, Optional

from .parsing import LogEntry

#: Digest width. Eight bytes is 2^64 keys; a collision would need two different
#: lines in one source to land on the same digest, and the cost of one is a
#: wrongly-drawn gutter dot rather than lost data.
_DIGEST_BYTES = 8


def mark_key(source: Optional[Path], entry: LogEntry) -> str:
    """Stable identity for *entry* within *source*.

    The source is part of the key so marks in one log cannot show up in
    another, and so switching away from a source and back keeps them.
    """

    digest = blake2b(entry.raw.encode("utf-8", "surrogatepass"), digest_size=_DIGEST_BYTES)
    return f"{source or ''}\0{digest.hexdigest()}"


class MarkSet:
    """The marks for one session. Deliberately not serialisable — see above."""

    __slots__ = ("_keys",)

    def __init__(self) -> None:
        self._keys: set[str] = set()

    def __len__(self) -> int:
        return len(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    def contains(self, source: Optional[Path], entry: LogEntry) -> bool:
        return mark_key(source, entry) in self._keys

    def toggle(self, source: Optional[Path], entry: LogEntry) -> bool:
        """Flip *entry*'s mark. Returns True when it is now marked."""

        key = mark_key(source, entry)
        if key in self._keys:
            self._keys.discard(key)
            return False
        self._keys.add(key)
        return True

    def clear(self) -> None:
        self._keys.clear()

    def prune(self, source: Optional[Path], entries: Iterable[LogEntry]) -> int:
        """Drop marks for *source* whose lines are no longer in *entries*.

        Without this the count shown to the operator would keep rising as the
        ring buffer evicted the lines behind it. Marks belonging to *other*
        sources are left alone: they are still valid, just not on screen.

        Returns how many were dropped.
        """

        return self.retain(
            {mark_key(source, entry) for entry in entries}, sources=[source]
        )

    def retain(self, live: set[str], *, sources: Iterable[Optional[Path]]) -> int:
        """Keep only *live* marks among those belonging to *sources*.

        The general form, for a pane showing several sources at once: pruning
        one source at a time cannot work there, because the entries on screen
        belong to different ones and a per-source pass would see every other
        source's lines as missing. Marks outside *sources* are untouched.
        """

        prefixes = tuple(f"{source or ''}\0" for source in sources)
        if not prefixes:
            return 0
        stale = {
            key
            for key in self._keys
            if key.startswith(prefixes) and key not in live
        }
        self._keys -= stale
        return len(stale)

    def count_for(self, *sources: Optional[Path]) -> int:
        """How many marks belong to *sources* — what the status line reports."""

        prefixes = tuple(f"{source or ''}\0" for source in sources)
        if not prefixes:
            return 0
        return sum(1 for key in self._keys if key.startswith(prefixes))


__all__ = ["MarkSet", "mark_key"]
