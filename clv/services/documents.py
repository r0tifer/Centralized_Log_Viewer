"""Text extraction for container documents.

CLV's normal path treats a source as an append-only stream of lines and reads
it backwards from the end. Some readable formats do not work that way: an
OpenDocument spreadsheet is a ZIP archive whose text exists only after
unpacking an inner XML part, and it has no meaningful end to seek to. Those
formats are handled here, by extracting the document into lines up front.

Two deliberate differences from :mod:`clv.services.reader`:

* **Work is bounded by lines, not bytes.** There is no way to read the tail of
  a deflated stream cheaply, so the budget is a line count and extraction stops
  as soon as it is met.
* **Extraction keeps the FIRST lines, not the last.** A log's newest content is
  at the bottom; a spreadsheet's most valuable line is its header row, at the
  top. Taking the tail of a sheet would reliably discard the one line that
  says what the columns mean.

Everything here is stdlib: a document format that needs a third-party parser
does not belong in a log viewer.

**Reads go through a** :class:`~clv.services.backend.SourceBackend`, split the
same way :mod:`clv.services.compressed` is: :func:`extract_ods_from` takes an
open handle and :func:`extract_ods` is the wrapper that obtains one.
``zipfile.ZipFile`` accepts a file object — but it requires a **seekable** one,
because the central directory is at the end of the archive. That is a real
constraint on any future backend rather than a detail, and it is the reason
``SourceBackend.open`` promises seekability.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Iterator
from xml.etree.ElementTree import Element, iterparse

from .backend import LOCAL, SourceBackend

#: Cell and row repeat counts are how ODF encodes padding, and the numbers are
#: enormous by design -- a sheet routinely declares 1024 repeated empty columns
#: and a million repeated empty rows. Materialising those verbatim would hang
#: the extractor, so a single run is capped at a width no real sheet exceeds.
MAX_REPEAT = 4096

_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"

_TABLE_TAG = f"{_TABLE}table"
_ROW_TAG = f"{_TABLE}table-row"
_CELL_TAGS = (f"{_TABLE}table-cell", f"{_TABLE}covered-table-cell")
_NAME_ATTR = f"{_TABLE}name"
_COLS_REPEATED = f"{_TABLE}number-columns-repeated"
_ROWS_REPEATED = f"{_TABLE}number-rows-repeated"
_VALUE_ATTR = f"{_OFFICE}value"

#: Cells are joined with tabs rather than commas: a spreadsheet cell may
#: legitimately contain a comma, and nothing here quotes or escapes.
_CELL_SEPARATOR = "\t"


class DocumentError(OSError):
    """Raised when a document is recognised by suffix but cannot be read.

    An OSError because that is what it is from a caller's point of view -- a
    source that would not open -- and because it lets the existing read-failure
    handling report a damaged spreadsheet without learning a new exception.
    """


@dataclass(frozen=True, slots=True)
class DocumentText:
    """Extracted lines, and whether the line budget cut them short."""

    lines: list[str]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DocumentFormat:
    """A container format whose text CLV can recover without a dependency."""

    name: str
    #: Lowercase suffixes, including the dot.
    suffixes: tuple[str, ...]
    #: ``(path, max_lines, *, backend) -> DocumentText``. Loosely typed because
    #: the keyword is optional and a format is free to ignore it.
    extract: Callable[..., DocumentText]


def _sheet_banner(name: str) -> str:
    return f"===== Sheet: {name} ====="


def _cell_text(cell: Element) -> str:
    """The displayed text of one cell, flattened to a single line.

    A cell holds one ``text:p`` per visual line; joining them with a space
    keeps the row on one line, which is what makes a sheet greppable here.
    Numeric and date cells carry a formatted ``text:p`` too, so the raw
    ``office:value`` is only a fallback for cells that somehow lack one.
    """

    paragraphs = ["".join(node.itertext()).strip() for node in cell]
    text = " ".join(part for part in paragraphs if part)
    if text:
        return text
    return (cell.get(_VALUE_ATTR) or "").strip()


def _repeat(element: Element, attribute: str) -> int:
    raw = element.get(attribute)
    if raw is None:
        return 1
    try:
        return max(1, min(int(raw), MAX_REPEAT))
    except ValueError:
        return 1


def _row_line(row: Element) -> str:
    """Flatten one ``table:table-row`` into a tab-separated line."""

    cells: list[str] = []
    pending_blank = 0
    for cell in row:
        if cell.tag not in _CELL_TAGS:
            continue
        repeat = _repeat(cell, _COLS_REPEATED)
        text = _cell_text(cell)
        if not text:
            # Held back rather than appended: blank runs are only real content
            # when a populated cell follows. Trailing ones are padding and are
            # dropped when the loop ends.
            pending_blank += repeat
            continue
        if pending_blank:
            cells.extend([""] * pending_blank)
            pending_blank = 0
        cells.extend([text] * repeat)
    return _CELL_SEPARATOR.join(cells)


def _iter_sheet_lines(handle) -> Iterator[str]:
    """Stream ``content.xml`` as one line per populated spreadsheet row."""

    sheet = ""
    banner_pending = False
    blank_rows = 0
    table: Element | None = None

    for event, element in iterparse(handle, events=("start", "end")):
        if event == "start":
            if element.tag == _TABLE_TAG:
                table = element
                sheet = element.get(_NAME_ATTR) or ""
                banner_pending = True
                blank_rows = 0
            continue

        if element.tag != _ROW_TAG:
            continue

        line = _row_line(element)
        repeat = _repeat(element, _ROWS_REPEATED)

        if not line:
            # Same reasoning as blank cells: a run of empty rows only matters
            # if the sheet continues past it.
            blank_rows += repeat
        else:
            if banner_pending:
                yield _sheet_banner(sheet)
                banner_pending = False
            yield from [""] * blank_rows
            blank_rows = 0
            yield from [line] * repeat

        # Rows accumulate under the table element as iterparse builds it.
        # Clearing the parent after each row keeps memory flat on a large
        # sheet; the sheet name was captured at "start", before it was wiped.
        element.clear()
        if table is not None:
            table.clear()


def extract_ods_from(
    handle: IO[bytes], max_lines: int, *, name: str = "document"
) -> DocumentText:
    """Extract an OpenDocument spreadsheet from an open, **seekable** handle.

    *name* appears in errors only; this function never learns where the handle
    came from, which is the point of it taking one.
    """

    lines: list[str] = []
    truncated = False
    try:
        with zipfile.ZipFile(handle) as archive:
            with archive.open("content.xml") as content:
                for line in _iter_sheet_lines(content):
                    if len(lines) >= max_lines:
                        truncated = True
                        break
                    lines.append(line)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise DocumentError(f"{name} is not a readable ODF spreadsheet: {exc}") from exc
    except SyntaxError as exc:  # malformed content.xml
        raise DocumentError(f"{name} contains damaged spreadsheet XML: {exc}") from exc
    return DocumentText(lines=lines, truncated=truncated)


def extract_ods(
    path: Path, max_lines: int, *, backend: SourceBackend = LOCAL
) -> DocumentText:
    """Extract an OpenDocument spreadsheet as tab-separated rows."""

    try:
        handle = backend.open(path, "rb")
    except OSError as exc:
        # The same message the archive itself would produce, because to a
        # caller "would not open" and "is not an ODF file" are one fact.
        raise DocumentError(
            f"{path.name} is not a readable ODF spreadsheet: {exc}"
        ) from exc
    with handle:
        return extract_ods_from(handle, max_lines, name=path.name)


ODS = DocumentFormat(name="OpenDocument spreadsheet", suffixes=(".ods",), extract=extract_ods)

#: Every format CLV can extract, keyed by lowercase suffix.
FORMATS: dict[str, DocumentFormat] = {suffix: ODS for suffix in ODS.suffixes}


def document_format_for(path: Path) -> DocumentFormat | None:
    """The extractor for *path*, or None if it is not a container document.

    Suffix-based on purpose. Everywhere else CLV decides by content, but a
    document has to be identified before it is opened: the whole point is that
    its bytes look like the binary it technically is.
    """

    return FORMATS.get(path.suffix.lower())


def extract_document(
    path: Path, max_lines: int, *, backend: SourceBackend = LOCAL
) -> DocumentText:
    """Extract *path* using its registered format."""

    fmt = document_format_for(path)
    if fmt is None:
        raise DocumentError(f"{path.name} is not a supported document format")
    return fmt.extract(path, max_lines, backend=backend)
