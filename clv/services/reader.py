"""Bounded reading and tailing of log files.

CLV is pointed at whatever an operator names, including 100 MB+ journals and
multi-gigabyte application logs. Nothing here ever loads a whole file: the
initial read seeks backwards from the end and stops as soon as it has the
lines the viewer will actually show, and tailing reads only the bytes appended
since the last poll.

Every byte read here comes through a
:class:`~clv.services.backend.SourceBackend`, and the split in that protocol is
what makes tailing safe: :meth:`~clv.services.backend.SourceBackend.stat` is
cheap on every backend, so :meth:`SourceReader.poll` can ask "did this grow?"
from the event loop, while :meth:`~clv.services.backend.SourceBackend.open` may
block and is only reached once there is something to read. A backend's handle
must be **seekable** — the backwards read below is the reason.
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .backend import LOCAL, SourceBackend
from .documents import DocumentFormat, document_format_for, extract_document

#: Backwards read granularity. Large enough that a few hundred lines usually
#: arrive in one seek, small enough that we rarely over-read. Divisible by 4 so
#: stepping by it never lands mid-character in a UTF-16 or UTF-32 file.
_CHUNK_SIZE = 64 * 1024

#: Hard ceiling on the initial backwards read, whatever max_lines asks for.
#: Bounds the work done for a file whose "lines" are enormous (minified JSON,
#: a file with no newlines at all).
DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024

#: Bytes sampled when deciding whether a file is text. Public because discovery
#: asks a backend for exactly this many in a batch — see
#: :class:`~clv.services.backend.ClassifyRequest`.
SNIFF_SIZE = 8192

#: Bytes of content remembered at the read boundary, to prove the next read is a
#: continuation of the same file rather than assume it.
#:
#: Small because it is re-read on every poll that has new data — and it costs no
#: extra syscall, since the read that follows it was going to seek and read
#: anyway. Large enough that two different files agreeing across it means their
#: content genuinely matches, in which case the distinction was not worth
#: drawing. See :meth:`SourceReader.poll`.
ANCHOR_SIZE = 64


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


def detect_file_encoding(path: Path, *, backend: SourceBackend = LOCAL) -> TextEncoding:
    """Read just enough of *path* to identify its encoding."""

    try:
        with backend.open(path, "rb") as handle:
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
    #: Which file each line came from, parallel to ``lines``, for readers that
    #: span more than one -- a rotated set is one source made of several files.
    #: ``None`` for the ordinary case, where every line came from ``path`` and
    #: a per-line answer would be the same answer repeated.
    origins: tuple[Path, ...] | None = None
    #: The last :data:`ANCHOR_SIZE` **bytes** ending at :attr:`offset`, so the
    #: next read can prove it is a continuation rather than assume it. Empty
    #: when the read produced nothing to anchor to. See
    #: :meth:`SourceReader.poll`.
    anchor: bytes = b""


def looks_binary(
    path: Path, *, sniff: int = SNIFF_SIZE, backend: SourceBackend = LOCAL
) -> bool:
    """Heuristic text/binary test: a NUL byte in the first block means binary.

    Obtains the block and applies :func:`looks_binary_block` to it. Discovery
    goes the other way round — it collects blocks for a whole batch through
    ``backend.classify`` and calls the rule directly — so that a remote tree
    costs one round trip instead of one per file. Both paths reach the same
    function, which is the point of the split.
    """

    try:
        with backend.open(path, "rb") as handle:
            block = handle.read(sniff)
    except OSError:
        return False

    return looks_binary_block(block)


def looks_binary_block(block: bytes) -> bool:
    """The rule itself, over bytes already in hand.

    This is the same rule git uses. It keeps journals, archives and databases
    out of the viewer without maintaining an extension blocklist, which matters
    because CLV deliberately does not restrict sources by extension.

    The rule is applied to *characters*, not bytes, because it is a statement
    about content and UTF-16 encodes plain ASCII text with a NUL byte beside
    every character. Reading bytes alone rejects perfectly readable UTF-16
    files -- which is how Windows and PowerShell write their exports -- for
    looking like the binaries they are not.

    Separated from :func:`looks_binary` so it can be applied to a block a
    *batch* produced. A remote backend that answered "binary: yes" itself would
    have had to reimplement the paragraph above in ``sh``, which is how the
    UTF-16 case would quietly come back.
    """

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
    backend: SourceBackend = LOCAL,
) -> TailRead:
    """Read up to *max_lines* lines from the end of *path*.

    Reads backwards in chunks and stops as soon as enough newlines are in hand,
    so cost is proportional to what is displayed rather than to file size. The
    handle *backend* returns must therefore be seekable.

    *encoding* defaults to sniffing the file's byte-order mark, falling back to
    UTF-8. Pass a codec name to override that.
    """

    text_encoding = _coerce_encoding(encoding) or detect_file_encoding(
        path, backend=backend
    )

    if max_lines <= 0:
        info = backend.stat(path)
        return TailRead(lines=[], offset=info.size if info else 0)

    with backend.open(path, "rb") as handle:
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
    return TailRead(
        lines=lines[-max_lines:],
        offset=size,
        truncated=truncated,
        # The tail of the raw block, which ends at the end of the file whatever
        # the line budget trimmed. Handed back so the caller can anchor its next
        # read to it without paying for a second open.
        anchor=data[-ANCHOR_SIZE:],
    )


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
        backend: SourceBackend = LOCAL,
    ) -> None:
        self.path = path
        self._max_lines = max_lines
        self._backend = backend
        self._encoding = _coerce_encoding(encoding) or detect_file_encoding(
            path, backend=backend
        )
        self._max_bytes = max_bytes
        self._offset = 0
        self._remainder = ""
        #: Bytes of a character split across two polls, held until the rest of
        #: it is written. Decoding them alone would emit U+FFFD for a character
        #: that arrives intact a fraction of a second later.
        self._byte_remainder = b""
        self._identity: object | None = None
        #: The last bytes read, so the next read can *prove* it continues this
        #: file rather than assume it. See :meth:`poll`.
        self._anchor = b""

    @property
    def offset(self) -> int:
        return self._offset

    def _stat_identity(self) -> object | None:
        """The backend's opaque "is this still the same file" comparable.

        ``None`` from a backend that has no stable answer — see
        :meth:`poll` for what that costs.
        """

        return self._backend.identity(self.path)

    def prime(self) -> TailRead:
        """Perform the initial bounded read and arm the tail position."""

        # A rotated-in file may be a different encoding than the one it
        # replaced, so re-sniff rather than trust the previous read.
        self._encoding = detect_file_encoding(self.path, backend=self._backend)
        result = read_last_lines(
            self.path,
            self._max_lines,
            max_bytes=self._max_bytes,
            encoding=self._encoding,
            backend=self._backend,
        )
        self._offset = result.offset
        self._remainder = ""
        self._byte_remainder = b""
        self._identity = self._stat_identity()
        self._anchor = result.anchor
        return result

    def poll(self) -> TailRead:
        """Return lines appended since the last call.

        On rotation or truncation the reader re-primes from the new file, so a
        rotated log keeps streaming instead of going silent.

        **One** ``stat`` per poll, and it is the backend's cheap one: size,
        mtime and identity arrive together because at 2 Hz per merged source a
        second call would be a second round trip.

        **When the backend has no stable identity** — ``capabilities
        .stable_identity`` false, which an SFTP-style backend and a non-GNU
        remote both are — rotation is *not* inferred from ``(size, mtime)``.
        That comparison changes on every ordinary append, so using it would
        report a reload twice a second on a live log. What survives instead is
        the shrink test below: a file replaced by a **smaller** one is still
        caught, and one replaced by a same-size file is not. The degradation is
        conservative and silent by design, and it is why
        ``stable_identity`` is reported rather than assumed.

        **The continuation itself is verified, not assumed.** Every check above
        is metadata, and metadata can agree while the content has been replaced
        underneath it:

        * ``logrotate copytruncate`` truncates **in place**, so the inode never
          changes. The shrink test catches it only while the file is still
          shorter than the offset held here — a small log rewritten past that
          between two polls slips through both tests.
        * A deleted-and-recreated log can be handed the very same inode back;
          ext4 does this routinely, so the identity test cannot see it either.

        In both cases the reader used to carry on from an offset that meant
        nothing in the new file, showing a fragment of it and dropping
        everything before — a silent loss, which Requirement 2 of ``AGENTS.md``
        forbids outright. So :data:`ANCHOR_SIZE` bytes of content are remembered
        at the read boundary and re-read as part of the next read; if they are
        not what was left there, the file is treated as replaced.

        It costs **no extra syscall** — the read was going to seek and read
        anyway, and simply starts a little earlier — and it cannot fire on an
        ordinary append, which is what makes it affordable at ``refresh_hz``
        per merged source.
        """

        info = self._backend.stat(self.path)
        if info is None:
            return TailRead(lines=[], offset=self._offset)

        identity = info.identity
        if (
            identity is not None
            and self._identity is not None
            and identity != self._identity
        ):
            # The name now points at a different file: rotation.
            result = self.prime()
            return TailRead(
                lines=result.lines,
                offset=result.offset,
                truncated=result.truncated,
                rotated=True,
            )

        size = info.size
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

        result = self._read_from(self._offset, size, verify=True)
        if not result.rotated:
            return result
        # The content check refused the continuation. Re-prime, which is the
        # same recovery a detected rotation already takes.
        primed = self.prime()
        return TailRead(
            lines=primed.lines,
            offset=primed.offset,
            truncated=primed.truncated,
            rotated=True,
        )

    def _read_from(self, start: int, size: int, *, verify: bool = False) -> TailRead:
        """Read ``[start, size)`` and turn it into lines.

        *verify* asks for the continuity check described in :meth:`poll`: the
        read begins :data:`ANCHOR_SIZE` bytes early and the bytes that come back
        are compared against what was read last time. It is a keyword rather
        than the default because the two callers that re-prime have just
        established a new anchor and have nothing to compare against.

        Returns a read whose :attr:`TailRead.rotated` is set when the check
        fails, which the caller turns into a re-prime.
        """

        anchor = self._anchor if verify else b""
        # Never look further back than the file's own start, or past the BOM,
        # which is metadata rather than content.
        back = min(len(anchor), max(0, start - self._encoding.bom_size))
        try:
            with self._backend.open(self.path, "rb") as handle:
                handle.seek(start - back)
                # Never ingest more than one bounded read's worth in a single
                # poll; a burst of writes must not stall the UI. The anchor
                # rides along in the same read, so checking costs no syscall.
                chunk = handle.read(back + min(size - start, self._max_bytes))
        except OSError:
            return TailRead(lines=[], offset=self._offset)

        if back:
            seen, chunk = chunk[:back], chunk[back:]
            if seen != anchor[-back:]:
                # The bytes before our position are not the ones we read there.
                # Whatever this file is now, it is not a continuation of the one
                # we were reading — and neither `stat` nor the inode said so.
                return TailRead(lines=[], offset=self._offset, rotated=True)

        self._offset = start + len(chunk)
        self._anchor = (self._anchor + chunk)[-ANCHOR_SIZE:] if chunk else self._anchor

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
        backend: SourceBackend = LOCAL,
    ) -> None:
        self.path = path
        self.document_format = document_format or document_format_for(path)
        self._max_lines = max_lines
        self._backend = backend
        self._offset = 0
        self._stamp: Optional[tuple[int, int]] = None

    @property
    def offset(self) -> int:
        return self._offset

    def _current_stamp(self) -> Optional[tuple[int, int]]:
        info = self._backend.stat(self.path)
        if info is None:
            return None
        return (info.mtime_ns, info.size)

    def prime(self) -> TailRead:
        """Extract the document and arm change detection."""

        # Stamped before the read, not after: a write that lands *during*
        # extraction then still looks newer than what we hold, and the next
        # poll picks it up instead of trusting a half-written extraction.
        stamp = self._current_stamp()
        text = extract_document(self.path, self._max_lines, backend=self._backend)
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


#: Any reader; they all expose the same ``path``/``prime``/``poll`` surface.
#: Deliberately not an exhaustive union any more -- a plugin-supplied reader is
#: one of these too, and the contract is the surface rather than the class.
AnyReader = SourceReader | DocumentReader


def open_reader(
    path: Path, *, max_lines: int, backend: SourceBackend = LOCAL, **kwargs
) -> AnyReader:
    """Build the right reader for *path*.

    Container documents get :class:`DocumentReader`, compressed members get a
    :class:`~clv.services.compressed.CompressedReader`; everything else is a
    stream of text lines and gets :class:`SourceReader`. Whichever it is, it
    reads through *backend* — the choice is made on the name, which costs no IO
    and so needs no backend to decide.
    """

    document_format = document_format_for(path)
    if document_format is not None:
        return DocumentReader(
            path,
            max_lines=max_lines,
            document_format=document_format,
            backend=backend,
        )
    # Imported here rather than at module scope: compressed.py needs this
    # module's BOM detection, so the dependency has to run one way only.
    from .compressed import CompressedReader, is_compressed

    if is_compressed(path):
        return CompressedReader(path, max_lines=max_lines, backend=backend)
    return SourceReader(path, max_lines=max_lines, backend=backend, **kwargs)
