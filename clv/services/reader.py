"""Bounded reading and tailing of log files.

CLV is pointed at whatever an operator names, including 100 MB+ journals and
multi-gigabyte application logs. Nothing here ever loads a whole file: the
initial read seeks backwards from the end and stops as soon as it has the
lines the viewer will actually show, and tailing reads only the bytes appended
since the last poll.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Backwards read granularity. Large enough that a few hundred lines usually
#: arrive in one seek, small enough that we rarely over-read.
_CHUNK_SIZE = 64 * 1024

#: Hard ceiling on the initial backwards read, whatever max_lines asks for.
#: Bounds the work done for a file whose "lines" are enormous (minified JSON,
#: a file with no newlines at all).
DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024

#: Bytes sampled when deciding whether a file is text.
_SNIFF_SIZE = 8192


@dataclass(frozen=True, slots=True)
class TailRead:
    """The outcome of one read against a file."""

    lines: list[str]
    #: Byte offset to resume tailing from.
    offset: int
    #: True when the file was longer than we read (older lines exist on disk).
    truncated: bool = False
    #: True when the file shrank or was replaced since the last read.
    rotated: bool = False


def looks_binary(path: Path, *, sniff: int = _SNIFF_SIZE) -> bool:
    """Heuristic text/binary test: a NUL byte in the first block means binary.

    This is the same rule git uses. It keeps journals, archives and databases
    out of the viewer without maintaining an extension blocklist, which matters
    because CLV deliberately does not restrict sources by extension.
    """

    try:
        with path.open("rb") as handle:
            block = handle.read(sniff)
    except OSError:
        return False
    return b"\x00" in block


def read_last_lines(
    path: Path,
    max_lines: int,
    *,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
    encoding: str = "utf-8",
) -> TailRead:
    """Read up to *max_lines* lines from the end of *path*.

    Reads backwards in chunks and stops as soon as enough newlines are in hand,
    so cost is proportional to what is displayed rather than to file size.
    """

    if max_lines <= 0:
        try:
            return TailRead(lines=[], offset=path.stat().st_size)
        except OSError:
            return TailRead(lines=[], offset=0)

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return TailRead(lines=[], offset=0)

        budget = min(size, max_bytes)
        floor = size - budget
        position = size
        blocks: list[bytes] = []
        newlines = 0

        while position > floor and newlines <= max_lines:
            step = min(_CHUNK_SIZE, position - floor)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            blocks.append(block)
            newlines += block.count(b"\n")

        data = b"".join(reversed(blocks))

    text = data.decode(encoding, errors="replace")
    lines = text.splitlines()

    # Unless we reached byte 0, the first line is probably a fragment of a
    # line that started before our read window: drop it rather than show half.
    partial_start = position > 0
    if partial_start and lines:
        lines = lines[1:]

    truncated = partial_start or len(lines) > max_lines
    return TailRead(lines=lines[-max_lines:], offset=size, truncated=truncated)


class SourceReader:
    """Tracks read position for one file and yields newly appended lines.

    Handles the two ways a log file moves under you: truncation in place
    (``> file``) and rotation (the name now points at a new inode).
    """

    def __init__(
        self,
        path: Path,
        *,
        max_lines: int,
        encoding: str = "utf-8",
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> None:
        self.path = path
        self._max_lines = max_lines
        self._encoding = encoding
        self._max_bytes = max_bytes
        self._offset = 0
        self._remainder = ""
        self._identity: Optional[tuple[int, int]] = None

    @property
    def offset(self) -> int:
        return self._offset

    def _stat_identity(self) -> Optional[tuple[int, int]]:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def prime(self) -> TailRead:
        """Perform the initial bounded read and arm the tail position."""

        result = read_last_lines(
            self.path,
            self._max_lines,
            max_bytes=self._max_bytes,
            encoding=self._encoding,
        )
        self._offset = result.offset
        self._remainder = ""
        self._identity = self._stat_identity()
        return result

    def poll(self) -> TailRead:
        """Return lines appended since the last call.

        On rotation or truncation the reader re-primes from the new file, so a
        rotated log keeps streaming instead of going silent.
        """

        try:
            info = self.path.stat()
        except OSError:
            return TailRead(lines=[], offset=self._offset)

        identity = (info.st_dev, info.st_ino)
        if self._identity is not None and identity != self._identity:
            # The name now points at a different file: rotation.
            result = self.prime()
            return TailRead(
                lines=result.lines,
                offset=result.offset,
                truncated=result.truncated,
                rotated=True,
            )

        size = info.st_size
        if size < self._offset:
            # Truncated in place; restart from the beginning of the new content.
            self._offset = 0
            self._remainder = ""
            result = self._read_from(self._offset, size)
            return TailRead(
                lines=result.lines,
                offset=result.offset,
                truncated=result.truncated,
                rotated=True,
            )

        if size == self._offset:
            return TailRead(lines=[], offset=self._offset)

        return self._read_from(self._offset, size)

    def _read_from(self, start: int, size: int) -> TailRead:
        try:
            with self.path.open("rb") as handle:
                handle.seek(start)
                # Never ingest more than one bounded read's worth in a single
                # poll; a burst of writes must not stall the UI.
                chunk = handle.read(min(size - start, self._max_bytes))
                self._offset = start + len(chunk)
        except OSError:
            return TailRead(lines=[], offset=self._offset)

        if not chunk:
            return TailRead(lines=[], offset=self._offset)

        text = self._remainder + chunk.decode(self._encoding, errors="replace")
        lines = text.splitlines()
        if text.endswith(("\n", "\r")):
            self._remainder = ""
        else:
            # Hold the trailing partial line until its newline arrives.
            self._remainder = lines.pop() if lines else text

        return TailRead(lines=lines, offset=self._offset)
