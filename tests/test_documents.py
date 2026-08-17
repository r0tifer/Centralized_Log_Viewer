"""UTF-16 sources and container documents.

Both cover the same failure: a file that is perfectly readable text but whose
raw bytes do not look like it. A PowerShell or Windows export is UTF-16, so
every ASCII character sits beside a NUL byte; an ODS file is a ZIP, so its
bytes are not text at all until the archive is opened. Neither used to appear
in the source tree, with nothing in the UI to say why.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from clv.services.discovery import DiscoverySettings, discover
from clv.services.documents import DocumentError, extract_document
from clv.services.reader import (
    DocumentReader,
    SourceReader,
    detect_file_encoding,
    looks_binary,
    open_reader,
    read_last_lines,
)


CONTENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
    xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:spreadsheet>
    <table:table table:name="Hosts">
      {rows}
    </table:table>
  </office:spreadsheet></office:body>
</office:document-content>
"""


def _row(*cells: str, repeated: int | None = None) -> str:
    parts = []
    for cell in cells:
        if cell == "":
            parts.append("<table:table-cell/>")
        else:
            parts.append(f"<table:table-cell><text:p>{cell}</text:p></table:table-cell>")
    attr = f' table:number-rows-repeated="{repeated}"' if repeated else ""
    return f"<table:table-row{attr}>{''.join(parts)}</table:table-row>"


def _ods(path: Path, rows: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", CONTENT_XML.format(rows=rows))
    return path


def _utf16(path: Path, text: str, *, endian: str = "le") -> Path:
    path.write_bytes(("﻿" + text).encode(f"utf-16-{endian}"))
    return path


# --- UTF-16 text ------------------------------------------------------------


def test_utf16_file_is_not_mistaken_for_a_binary(tmp_path: Path) -> None:
    """The NUL bytes beside every ASCII character are padding, not content."""
    path = _utf16(tmp_path / "export.xml", "<Objs>\n  <Obj/>\n</Objs>\n")

    assert b"\x00" in path.read_bytes()
    assert looks_binary(path) is False


def test_real_binaries_are_still_rejected(tmp_path: Path) -> None:
    path = tmp_path / "core.dump"
    path.write_bytes(b"\x7fELF\x00\x01\x02\x00payload")

    assert looks_binary(path) is True


def test_utf16_with_embedded_nul_characters_is_still_binary(tmp_path: Path) -> None:
    """A BOM does not make arbitrary bytes text; NUL *characters* still count."""
    path = tmp_path / "odd.bin"
    path.write_bytes("﻿".encode("utf-16-le") + "abc\x00def".encode("utf-16-le"))

    assert looks_binary(path) is True


def test_utf16_discovery_lists_the_file(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    _utf16(root / "OnPrem_PFStatistics.xml", "<Objs>\n</Objs>\n")

    report = discover([root])

    assert [item.path.name for item in report.files] == ["OnPrem_PFStatistics.xml"]
    assert report.skipped_unsupported == 0


def test_reading_utf16_decodes_and_drops_the_bom(tmp_path: Path) -> None:
    path = _utf16(tmp_path / "export.xml", "alpha\nbeta\ngamma\n")

    result = read_last_lines(path, 10)

    # No stray U+FEFF on the first line, and no NUL-riddled mojibake anywhere.
    assert result.lines == ["alpha", "beta", "gamma"]


def test_utf16_be_is_decoded_with_the_right_endianness(tmp_path: Path) -> None:
    path = _utf16(tmp_path / "export.xml", "alpha\nbeta\n", endian="be")

    assert detect_file_encoding(path).name == "utf-16-be"
    assert read_last_lines(path, 10).lines == ["alpha", "beta"]


def test_utf16_backwards_read_stays_on_character_boundaries(tmp_path: Path) -> None:
    """A misaligned seek would decode every later character as garbage."""
    body = "".join(f"line {index:05d}\n" for index in range(4000))
    path = _utf16(tmp_path / "big.log", body)

    result = read_last_lines(path, 20, max_bytes=8191)

    assert result.truncated is True
    assert len(result.lines) == 20
    assert result.lines[-1] == "line 03999"
    assert all(line.startswith("line 0") for line in result.lines)


def test_utf8_files_are_unaffected(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("one\ntwo\n", encoding="utf-8")

    assert detect_file_encoding(path).name == "utf-8"
    assert read_last_lines(path, 10).lines == ["one", "two"]


def test_utf8_bom_is_stripped(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("one\ntwo\n", encoding="utf-8-sig")

    assert read_last_lines(path, 10).lines == ["one", "two"]


# --- UTF-16 tailing ---------------------------------------------------------


def test_tailing_a_utf16_file_yields_appended_lines(tmp_path: Path) -> None:
    path = _utf16(tmp_path / "export.log", "first\n")
    reader = SourceReader(path, max_lines=100)
    assert reader.prime().lines == ["first"]

    with path.open("ab") as handle:
        handle.write("second\n".encode("utf-16-le"))

    assert reader.poll().lines == ["second"]


def test_tailing_holds_a_character_split_across_polls(tmp_path: Path) -> None:
    """A poll landing between a character's two bytes must not emit U+FFFD."""
    path = _utf16(tmp_path / "export.log", "first\n")
    reader = SourceReader(path, max_lines=100)
    reader.prime()

    encoded = "second\n".encode("utf-16-le")
    with path.open("ab") as handle:
        handle.write(encoded[:5])  # cuts the third character in half
        handle.flush()
    assert reader.poll().lines == []

    with path.open("ab") as handle:
        handle.write(encoded[5:])

    assert reader.poll().lines == ["second"]


def test_truncated_utf16_file_restarts_after_the_bom(tmp_path: Path) -> None:
    path = _utf16(tmp_path / "export.log", "first\nsecond\n")
    reader = SourceReader(path, max_lines=100)
    reader.prime()

    _utf16(path, "fresh\n")
    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["fresh"]


# --- ODS extraction ---------------------------------------------------------


def test_ods_rows_become_tab_separated_lines(tmp_path: Path) -> None:
    path = _ods(tmp_path / "hosts.ods", _row("Host", "Status") + _row("40-5755", "Online"))

    result = extract_document(path, 100)

    assert result.lines == [
        "===== Sheet: Hosts =====",
        "Host\tStatus",
        "40-5755\tOnline",
    ]
    assert result.truncated is False


def test_ods_keeps_the_header_row_when_the_budget_runs_out(tmp_path: Path) -> None:
    """Documents keep their first lines, not their last: row one names the columns."""
    rows = _row("Host", "Status") + "".join(_row(f"host-{n}", "Online") for n in range(50))
    path = _ods(tmp_path / "hosts.ods", rows)

    result = extract_document(path, 5)

    assert result.truncated is True
    assert result.lines[:2] == ["===== Sheet: Hosts =====", "Host\tStatus"]
    assert len(result.lines) == 5


def test_ods_padding_columns_and_rows_are_dropped(tmp_path: Path) -> None:
    """ODF pads sheets to 1024 columns and a million rows; none of it is content."""
    rows = (
        _row("Host", "Status")
        + '<table:table-row><table:table-cell><text:p>only</text:p></table:table-cell>'
        '<table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
        + _row("", repeated=1048576)
    )
    path = _ods(tmp_path / "hosts.ods", rows)

    result = extract_document(path, 5000)

    assert result.lines == ["===== Sheet: Hosts =====", "Host\tStatus", "only"]


def test_ods_gaps_between_populated_cells_are_preserved(tmp_path: Path) -> None:
    """A blank cell in the middle shifts every later column and must be kept."""
    path = _ods(tmp_path / "hosts.ods", _row("Host", "", "Status"))

    result = extract_document(path, 100)

    assert result.lines[1] == "Host\t\tStatus"


def test_a_damaged_ods_reports_rather_than_crashes(tmp_path: Path) -> None:
    path = tmp_path / "hosts.ods"
    path.write_bytes(b"PK\x03\x04 truncated archive")

    try:
        extract_document(path, 100)
    except DocumentError as exc:
        assert "hosts.ods" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a damaged document must raise DocumentError")


# --- documents in discovery and the reader factory --------------------------


def test_discovery_lists_documents_despite_binary_content(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    _ods(root / "hosts.ods", _row("Host"))

    report = discover([root])

    assert [item.path.name for item in report.files] == ["hosts.ods"]
    assert report.skipped_unsupported == 0


def test_pdfs_are_excluded_and_counted(tmp_path: Path) -> None:
    """Excluded rather than invisible, so the summary explains the absence."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "addendum.pdf").write_bytes(b"%PDF-1.6\n\x00binary")
    (root / "app.log").write_text("one\n", encoding="utf-8")

    report = discover([root])

    assert [item.path.name for item in report.files] == ["app.log"]
    assert report.skipped_unsupported == 1
    assert "Skipped: 1 unsupported file type" in " ".join(report.summary_lines())


def test_documents_can_be_opted_back_out(tmp_path: Path) -> None:
    """exclude_globs still wins; the document path is not a privileged bypass."""
    root = tmp_path / "docs"
    root.mkdir()
    _ods(root / "hosts.ods", _row("Host"))

    report = discover([root], DiscoverySettings.from_strings(exclude="*.ods"))

    # The operator's own glob, so it is their filter -- not CLV's verdict
    # on the file type.
    assert report.files == []
    assert report.skipped_filtered == 1
    assert report.skipped_unsupported == 0


def test_open_reader_picks_the_reader_the_file_needs(tmp_path: Path) -> None:
    document = _ods(tmp_path / "hosts.ods", _row("Host"))
    stream = tmp_path / "app.log"
    stream.write_text("one\n", encoding="utf-8")

    assert isinstance(open_reader(document, max_lines=10), DocumentReader)
    assert isinstance(open_reader(stream, max_lines=10), SourceReader)


def test_document_reader_re_extracts_when_the_file_changes(tmp_path: Path) -> None:
    path = _ods(tmp_path / "hosts.ods", _row("Host", "Status"))
    reader = open_reader(path, max_lines=100)
    assert reader.prime().lines[1] == "Host\tStatus"

    assert reader.poll().lines == []

    _ods(path, _row("Host", "Status") + _row("40-5755", "Online"))
    result = reader.poll()

    # A document is rewritten whole, so the change is a reload, not an append.
    assert result.rotated is True
    assert result.lines[-1] == "40-5755\tOnline"


def test_document_reader_survives_a_document_caught_mid_save(tmp_path: Path) -> None:
    path = _ods(tmp_path / "hosts.ods", _row("Host"))
    reader = open_reader(path, max_lines=100)
    reader.prime()

    path.write_bytes(b"PK\x03\x04 half written")
    result = reader.poll()

    assert result.lines == []
    assert result.rotated is False


# --- reading from a handle rather than a path -------------------------------


def test_extract_ods_from_reads_an_open_handle(tmp_path: Path) -> None:
    """The shape a remote backend hands over. ``zipfile`` needs it seekable,
    which is why ``SourceBackend.open`` promises seekability rather than
    leaving it to be discovered by a stack trace from inside the stdlib."""

    from io import BytesIO

    from clv.services.documents import extract_ods_from

    path = _ods(tmp_path / "hosts.ods", _row("Host") + _row("web01"))
    handle = BytesIO(path.read_bytes())

    text = extract_ods_from(handle, max_lines=10, name="hosts.ods")

    assert "Host" in "\n".join(text.lines)
    assert "web01" in "\n".join(text.lines)


def test_extract_ods_from_names_the_document_in_its_error() -> None:
    from io import BytesIO

    from clv.services.documents import DocumentError, extract_ods_from

    with pytest.raises(DocumentError) as caught:
        extract_ods_from(BytesIO(b"not a zip"), max_lines=10, name="hosts.ods")

    assert "hosts.ods" in str(caught.value)


def test_a_document_extracts_through_an_injected_backend(tmp_path: Path) -> None:
    from clv.services.backend import LOCAL
    from clv.services.documents import extract_document

    path = _ods(tmp_path / "hosts.ods", _row("Host"))

    assert extract_document(path, 10).lines == extract_document(
        path, 10, backend=LOCAL
    ).lines
