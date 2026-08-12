"""Reading compressed log members.

``/var/log`` is mostly ``syslog.1`` and ``syslog.2.gz``. Excluding those meant
an operator investigating anything older than a few hours had to leave CLV,
which is a strange thing for a log viewer to insist on when ``gzip``, ``bz2``
and ``lzma`` are all in the standard library.

This is the **second deliberate exception to Requirement 3**, and it inverts
the same rule :mod:`clv.services.documents` does, for the same kind of reason:

* **There is no cheap backwards seek in a deflate stream.** ``read_last_lines``
  works by seeking to the end and stepping backwards; a compressed member has
  to be decompressed from the front to know what is at the back. So the stream
  is read *forward*, and what bounds the work is a decompressed **byte** cap
  rather than the file's size on disk.
* **Memory is bounded by a line budget, not by the file.** Lines stream through
  a ``deque`` capped at the budget, so a 400 MB decompressed member costs the
  same memory as a small one. Work is proportional to the member; memory is
  not. Saying otherwise would be dishonest — that is what the byte cap is for.

Unlike a document, the **last** lines are kept rather than the first: this is a
log, and its newest content is at the end. A member that trips the byte cap is
the exception, and is reported as truncated so the caller can say so.
"""

from __future__ import annotations

import bz2
import codecs
import gzip
import lzma
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO

from .reader import TailRead, detect_encoding

#: Decompressed bytes read from one member before giving up. A ratio of 1000:1
#: is ordinary for a log full of repeated lines, and a zip bomb is several
#: orders beyond that -- this is the number that turns "exhausts memory" into
#: "showing what we read of it".
DEFAULT_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024

#: Decompressed read granularity.
_CHUNK_SIZE = 256 * 1024


class CompressedError(OSError):
    """Raised when a member is recognised by suffix but cannot be read.

    An ``OSError`` for the same reason :class:`~clv.services.documents.DocumentError`
    is one: to a caller this is a source that would not open, and the existing
    read-failure handling should report a damaged archive without learning a
    new exception. ``lzma`` in particular raises ``LZMAError``, which is not an
    ``OSError``, so without this the app would see an unhandled exception.
    """


@dataclass(frozen=True, slots=True)
class Compression:
    """A compression format CLV can read without a dependency."""

    name: str
    #: Lowercase suffixes, including the dot.
    suffixes: tuple[str, ...]
    open: Callable[..., IO[bytes]]


GZIP = Compression("gzip", (".gz",), gzip.open)
BZIP2 = Compression("bzip2", (".bz2",), bz2.open)
XZ = Compression("xz", (".xz", ".lzma"), lzma.open)

#: Every format CLV can decompress, keyed by lowercase suffix. ``.zst`` is
#: deliberately absent: there is no stdlib decompressor for it, so supporting
#: it would mean CLV's first new runtime dependency.
COMPRESSIONS: dict[str, Compression] = {
    suffix: compression
    for compression in (GZIP, BZIP2, XZ)
    for suffix in compression.suffixes
}


@dataclass(frozen=True, slots=True)
class CompressedText:
    """Extracted lines, and whether a budget cut them short."""

    lines: list[str]
    truncated: bool = False


def compression_for(path: Path) -> Compression | None:
    """The decompressor for *path*, or None if it is not a compressed member.

    Suffix-based, like :func:`~clv.services.documents.document_format_for` and
    for the same reason: the file has to be identified before it is opened,
    because its bytes are compressed and every content test calls them binary.
    """

    return COMPRESSIONS.get(path.suffix.lower())


def is_compressed(path: Path) -> bool:
    return compression_for(path) is not None


def strip_compression_suffix(path: Path) -> Path:
    """``app.log.2.gz`` → ``app.log.2``; anything else unchanged.

    Rotation grouping works on the name underneath the compression, because
    ``.gz`` says how a member is stored and not which member it is.
    """

    return path.with_suffix("") if is_compressed(path) else path


def probe(path: Path) -> bool:
    """Whether *path* opens and decompresses far enough to be worth listing.

    Discovery calls this instead of the NUL-byte sniff, which would reject
    every compressed file for looking like the binary it is. One small read:
    enough to catch a truncated download or a file that is not the archive its
    name claims, cheap enough to run over a whole ``/var/log``.
    """

    compression = compression_for(path)
    if compression is None:
        return False
    try:
        with compression.open(path, "rb") as handle:
            handle.read(1)
    except Exception:  # noqa: BLE001 - every decompressor raises its own
        return False
    return True


def read_compressed_tail(
    path: Path,
    max_lines: int,
    *,
    max_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
) -> CompressedText:
    """Read up to *max_lines* lines from the end of a compressed member.

    Streams forward through the decompressed bytes holding only the budget in
    memory. Stops early if *max_bytes* of decompressed data go by, which is
    what keeps a decompression bomb to "here is what we read" rather than an
    exhausted machine.
    """

    compression = compression_for(path)
    if compression is None:
        raise CompressedError(f"{path.name} is not a supported compressed format")
    if max_lines <= 0:
        return CompressedText(lines=[])

    kept: deque[str] = deque(maxlen=max_lines)
    #: Lines dropped off the front of the budget, or left unread past the byte
    #: cap. Either way there is more on disk than is on screen, and the caller
    #: has to be able to say so.
    dropped = False
    remainder = ""
    decoder: codecs.IncrementalDecoder | None = None
    total = 0

    try:
        with compression.open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if decoder is None:
                    encoding = detect_encoding(chunk)
                    decoder = codecs.getincrementaldecoder(encoding.name)(
                        errors="replace"
                    )
                    # The byte-order mark is file metadata; a stray U+FEFF at
                    # the head of the first line is visible in the viewer.
                    chunk = chunk[encoding.bom_size :]
                total += len(chunk)
                text = remainder + decoder.decode(chunk)
                lines = text.splitlines()
                if text.endswith(("\n", "\r")):
                    remainder = ""
                else:
                    # Hold the trailing partial line until the rest of it
                    # arrives in the next chunk.
                    remainder = lines.pop() if lines else text
                for line in lines:
                    if len(kept) == max_lines:
                        dropped = True
                    kept.append(line)
                if total >= max_bytes:
                    dropped = True
                    break
            if remainder and decoder is not None:
                if len(kept) == max_lines:
                    dropped = True
                kept.append(remainder)
    except CompressedError:
        raise
    except Exception as exc:  # noqa: BLE001 - one exception type per format
        raise CompressedError(f"{path.name} could not be decompressed: {exc}") from exc

    return CompressedText(lines=list(kept), truncated=dropped)


class CompressedReader:
    """Reads a compressed member, interface-compatible with ``SourceReader``.

    A member that has rotated out is finished: nothing appends to
    ``syslog.2.gz``. So there is no tail here at all — :meth:`poll` compares a
    stamp and does nothing, and the only way to read the file twice is for it
    to actually change on disk, which means it was replaced rather than
    appended to and is reported as a reload.
    """

    RELOAD_NOTICE = "{name} changed on disk; re-read."

    def __init__(
        self,
        path: Path,
        *,
        max_lines: int,
        max_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    ) -> None:
        self.path = path
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._offset = 0
        self._stamp: tuple[int, int] | None = None

    @property
    def offset(self) -> int:
        return self._offset

    def _current_stamp(self) -> tuple[int, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def prime(self) -> TailRead:
        # Stamped before the read, like DocumentReader: a write that lands
        # *during* decompression then still looks newer than what we hold.
        stamp = self._current_stamp()
        text = read_compressed_tail(self.path, self._max_lines, max_bytes=self._max_bytes)
        self._stamp = stamp
        self._offset = stamp[1] if stamp else 0
        return TailRead(lines=text.lines, offset=self._offset, truncated=text.truncated)

    def poll(self) -> TailRead:
        stamp = self._current_stamp()
        if stamp is None or stamp == self._stamp:
            return TailRead(lines=[], offset=self._offset)
        try:
            result = self.prime()
        except OSError:
            # Almost certainly caught mid-write: half an archive is not an
            # archive. Record the stamp so this does not retry every tick.
            self._stamp = stamp
            return TailRead(lines=[], offset=self._offset)
        return TailRead(
            lines=result.lines,
            offset=result.offset,
            truncated=result.truncated,
            rotated=True,
        )


__all__ = [
    "COMPRESSIONS",
    "Compression",
    "CompressedError",
    "CompressedReader",
    "CompressedText",
    "DEFAULT_MAX_DECOMPRESSED_BYTES",
    "compression_for",
    "is_compressed",
    "probe",
    "read_compressed_tail",
    "strip_compression_suffix",
]
