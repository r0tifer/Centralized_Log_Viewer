"""Bounded reading and tailing of log files.

CLV is pointed at whatever an operator names, including 100 MB+ journals and
multi-gigabyte application logs. Nothing here ever loads a whole file: the
initial read seeks backwards from the end and stops as soon as it has the
lines the viewer will actually show, and tailing reads only the bytes appended
since the last poll.
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .documents import DocumentFormat, document_format_for, extract_document

#: Backwards read granularity. Large enough that a few hundred lines usually
#: arrive in one seek, small enough that we rarely over-read. Divisible by 4 so
#: stepping by it never lands mid-character in a UTF-16 or UTF-32 file.
_CHUNK_SIZE = 64 * 1024

#: Hard ceiling on the initial backwards read, whatever max_lines asks for.
#: Bounds the work done for a file whose "lines" are enormous (minified JSON,
#: a file with no newlines at all).
DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024

#: Bytes sampled when deciding whether a file is text.
_SNIFF_SIZE = 8192


@dataclass(frozen=True, slots=True)
class TextEncoding:
    """How to turn this file's bytes into characters."""

    #: An explicit-endianness codec name. Never bare "utf-16": a tail read
    #: starts mid-file where there is no BOM left to infer endianness from.
    name: str
    #: Bytes of byte-order mark at the head of the file, which are metadata
    #: rather than content and must never be handed to the parser.
    bom_size: int = 0
    #: Width of one code unit. Reads must start and end on a multiple of this
    #: or they split a character in half.
    unit_size: int = 1

    @property
    def newline(self) -> bytes:
        """The encoded form of ``\\n``, for counting lines without decoding."""

        if self.unit_size == 1:
            # "utf-8-sig".encode would prepend a BOM to every string.
            return b"\n"
        return "\n".encode(self.name)


UTF8 = TextEncoding("utf-8")

#: Longest BOM first: UTF-32-LE begins with the whole of UTF-16-LE's BOM, so
#: testing UTF-16 first would claim every UTF-32-LE file.
_BOMS: tuple[tuple[bytes, TextEncoding], ...] = (
    (codecs.BOM_UTF32_LE, TextEncoding("utf-32-le", 4, 4)),
    (codecs.BOM_UTF32_BE, TextEncoding("utf-32-be", 4, 4)),
    (codecs.BOM_UTF8, TextEncoding("utf-8-sig", 3, 1)),
    (codecs.BOM_UTF16_LE, TextEncoding("utf-16-le", 2, 2)),
    (codecs.BOM_UTF16_BE, TextEncoding("utf-16-be", 2, 2)),
)


def detect_encoding(prefix: bytes) -> TextEncoding:
    """Identify an encoding from a file's leading bytes.

    BOM sniffing only. Statistical detection of BOM-less UTF-16 is guesswork
    that misfires on binaries, and getting it wrong would put a core dump in
    the viewer; a BOM-less file is read as UTF-8 with replacement, exactly as
    before.
    """

    for bom, encoding in _BOMS:
        if prefix.startswith(bom):
            return encoding
    return UTF8


def detect_file_encoding(path: Path) -> TextEncoding:
    """Read just enough of *path* to identify its encoding."""

    try:
        with path.open("rb") as handle:
            return detect_encoding(handle.read(4))
    except OSError:
        return UTF8


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

    The rule is applied to *characters*, not bytes, because it is a statement
    about content and UTF-16 encodes plain ASCII text with a NUL byte beside
    every character. Reading bytes alone rejects perfectly readable UTF-16
    files -- which is how Windows and PowerShell write their exports -- for
    looking like the binaries they are not.
    """

    try:
        with path.open("rb") as handle:
            block = handle.read(sniff)
    except OSError:
        return False

    encoding = detect_encoding(block)
    if encoding.unit_size == 1:
        return b"\x00" in block

    body = block[encoding.bom_size :]
    # The sniff boundary almost never lands on a character boundary; drop the
    # trailing fragment rather than decode a half character into U+FFFD.
    body = body[: len(body) - len(body) % encoding.unit_size]
    return "\x00" in body.decode(encoding.name, errors="replace")


def read_last_lines(
    path: Path,
    max_lines: int,
    *,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
    encoding: str | TextEncoding | None = None,
) -> TailRead:
    """Read up to *max_lines* lines from the end of *path*.

    Reads backwards in chunks and stops as soon as enough newlines are in hand,
    so cost is proportional to what is displayed rather than to file size.

    *encoding* defaults to sniffing the file's byte-order mark, falling back to
    UTF-8. Pass a codec name to override that.
    """

    text_encoding = _coerce_encoding(encoding) or detect_file_encoding(path)

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

        # Never read the byte-order mark itself: it is file metadata, and a
        # stray U+FEFF at the head of the first line is visible in the viewer.
        head = min(text_encoding.bom_size, size)
        floor = max(head, _align_up(size - min(size, max_bytes), text_encoding, head))
        position = size
        blocks: list[bytes] = []
        newline = text_encoding.newline
        newlines = 0

        while position > floor and newlines <= max_lines:
            step = min(_CHUNK_SIZE, position - floor)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            blocks.append(block)
            newlines += block.count(newline)

        data = b"".join(reversed(blocks))

    text = data.decode(text_encoding.name, errors="replace")
    lines = text.splitlines()

    # Unless we reached the start of the content, the first line is probably a
    # fragment of a line that began before our read window: drop it rather
    # than show half.
    partial_start = position > head
    if partial_start and lines:
        lines = lines[1:]

    truncated = partial_start or len(lines) > max_lines
    return TailRead(lines=lines[-max_lines:], offset=size, truncated=truncated)


def _coerce_encoding(encoding: str | TextEncoding | None) -> TextEncoding | None:
    """Accept a codec name, a resolved encoding, or None for "sniff it"."""

    if encoding is None or isinstance(encoding, TextEncoding):
        return encoding
    for _bom, known in _BOMS:
        if known.name == encoding:
            return known
    return TextEncoding(encoding)


def _align_up(offset: int, encoding: TextEncoding, head: int) -> int:
    """Round *offset* forward to the next character boundary after the BOM."""

    if encoding.unit_size == 1:
        return offset
    overshoot = (offset - head) % encoding.unit_size
    return offset if not overshoot else offset + encoding.unit_size - overshoot


class SourceReader:
    """Tracks read position for one file and yields newly appended lines.

    Handles the two ways a log file moves under you: truncation in place
    (``> file``) and rotation (the name now points at a new inode).
    """

    #: Shown when :meth:`poll` reports ``rotated``. Documents reload for a
    #: different reason and say so with their own notice.
    RELOAD_NOTICE = "{name} was rotated; reloaded."

    def __init__(
        self,
        path: Path,
        *,
        max_lines: int,
        encoding: str | TextEncoding | None = None,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> None:
        self.path = path
        self._max_lines = max_lines
        self._encoding = _coerce_encoding(encoding) or detect_file_encoding(path)
        self._max_bytes = max_bytes
        self._offset = 0
        self._remainder = ""
        #: Bytes of a character split across two polls, held until the rest of
        #: it is written. Decoding them alone would emit U+FFFD for a character
        #: that arrives intact a fraction of a second later.
        self._byte_remainder = b""
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

        # A rotated-in file may be a different encoding than the one it
        # replaced, so re-sniff rather than trust the previous read.
        self._encoding = detect_file_encoding(self.path)
        result = read_last_lines(
            self.path,
            self._max_lines,
            max_bytes=self._max_bytes,
            encoding=self._encoding,
        )
        self._offset = result.offset
        self._remainder = ""
        self._byte_remainder = b""
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
            self._offset = min(self._encoding.bom_size, size)
            self._remainder = ""
            self._byte_remainder = b""
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

        # A poll can land mid-character in a multi-byte encoding. Decode only
        # whole characters and carry the rest into the next poll.
        data = self._byte_remainder + chunk
        usable = len(data) - len(data) % self._encoding.unit_size
        self._byte_remainder = data[usable:]
        data = data[:usable]
        if not data:
            return TailRead(lines=[], offset=self._offset)

        text = self._remainder + data.decode(self._encoding.name, errors="replace")
        lines = text.splitlines()
        if text.endswith(("\n", "\r")):
            self._remainder = ""
        else:
            # Hold the trailing partial line until its newline arrives.
            self._remainder = lines.pop() if lines else text

        return TailRead(lines=lines, offset=self._offset)


class DocumentReader:
    """Reads a container document by extracting it whole.

    Interface-compatible with :class:`SourceReader` so the app does not care
    which one it holds, but the mechanics are inverted: there is no cheap tail
    of a deflated archive, so every read is a full extraction and "new content"
    means the file on disk changed rather than grew.

    A document is rewritten in place by whatever produced it, not appended to,
    so a change is reported as a reload (``rotated``) rather than as lines to
    append.
    """

    RELOAD_NOTICE = "{name} changed on disk; re-extracted."

    def __init__(
        self,
        path: Path,
        *,
        max_lines: int,
        document_format: DocumentFormat | None = None,
    ) -> None:
        self.path = path
        self.document_format = document_format or document_format_for(path)
        self._max_lines = max_lines
        self._offset = 0
        self._stamp: Optional[tuple[int, int]] = None

    @property
    def offset(self) -> int:
        return self._offset

    def _current_stamp(self) -> Optional[tuple[int, int]]:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def prime(self) -> TailRead:
        """Extract the document and arm change detection."""

        # Stamped before the read, not after: a write that lands *during*
        # extraction then still looks newer than what we hold, and the next
        # poll picks it up instead of trusting a half-written extraction.
        stamp = self._current_stamp()
        text = extract_document(self.path, self._max_lines)
        self._stamp = stamp
        self._offset = stamp[1] if stamp else 0
        return TailRead(lines=text.lines, offset=self._offset, truncated=text.truncated)

    def poll(self) -> TailRead:
        """Re-extract when the file changed on disk; otherwise do nothing."""

        stamp = self._current_stamp()
        if stamp is None or stamp == self._stamp:
            return TailRead(lines=[], offset=self._offset)

        try:
            result = self.prime()
        except OSError:
            # Almost certainly a document caught mid-save: an archive being
            # rewritten is not a valid archive yet. Record the stamp so we do
            # not retry every tick, and wait for the next change.
            self._stamp = stamp
            return TailRead(lines=[], offset=self._offset)

        return TailRead(
            lines=result.lines,
            offset=result.offset,
            truncated=result.truncated,
            rotated=True,
        )


#: Either reader; they expose the same ``path``/``prime``/``poll`` surface.
AnyReader = SourceReader | DocumentReader


def open_reader(path: Path, *, max_lines: int, **kwargs) -> AnyReader:
    """Build the right reader for *path*.

    Container documents get :class:`DocumentReader`; everything else is a
    stream of text lines and gets :class:`SourceReader`.
    """

    document_format = document_format_for(path)
    if document_format is not None:
        return DocumentReader(path, max_lines=max_lines, document_format=document_format)
    return SourceReader(path, max_lines=max_lines, **kwargs)
