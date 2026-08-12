from __future__ import annotations

import os
from pathlib import Path

from clv.services.discovery import (
    DiscoverySettings,
    discover,
)
from clv.services.reader import SourceReader, looks_binary, read_last_lines


# --- discovery --------------------------------------------------------------


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "logs"
    (root / "nested").mkdir(parents=True)
    (root / "app.log").write_text("one\ntwo\n", encoding="utf-8")
    (root / "notes.txt").write_text("plain text\n", encoding="utf-8")
    (root / "report.json").write_text('{"a":1}\n', encoding="utf-8")
    (root / "nested" / "service.log").write_text("nested\n", encoding="utf-8")
    (root / "archive.gz").write_bytes(b"\x1f\x8b fake gzip")
    # Named like a log but actually binary: only the content sniff catches it.
    (root / "corrupt.log").write_bytes(b"\x00\x01binary payload")
    return root


def test_discovery_is_not_limited_to_log_files(tmp_path: Path) -> None:
    """Any readable text file under a named folder is a valid source."""
    root = _tree(tmp_path)
    report = discover([root])

    names = {item.path.name for item in report.files}
    assert names == {"app.log", "notes.txt", "report.json", "service.log"}


def test_binary_and_archive_files_are_skipped_and_counted(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    report = discover([root])

    names = {item.path.name for item in report.files}
    assert "archive.gz" not in names  # excluded by glob
    assert "corrupt.log" not in names  # caught by the NUL-byte sniff
    # Both are the same thing to an operator: CLV cannot show this file.
    assert report.skipped_unsupported == 2
    assert report.skipped_filtered == 0
    assert "Skipped: 2 unsupported file types" in "\n".join(report.summary_lines())


def test_binary_skip_can_be_disabled(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    report = discover([root], DiscoverySettings(skip_binary=False))

    assert "corrupt.log" in {item.path.name for item in report.files}


def test_include_globs_narrow_the_result(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    report = discover([root], DiscoverySettings(include_globs=("*.log",)))

    assert {item.path.name for item in report.files} == {"app.log", "service.log"}


def test_exclude_globs_can_match_nested_paths(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    report = discover([root], DiscoverySettings(exclude_globs=("nested/*",)))

    assert "service.log" not in {item.path.name for item in report.files}


def test_individual_files_can_be_named_directly(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    target = root / "notes.txt"
    report = discover([target])

    assert [item.path for item in report.files] == [target]
    assert report.roots == [target]


def test_named_file_bypasses_include_filter(tmp_path: Path) -> None:
    """Asking for one file by name means you get it, filters notwithstanding."""
    root = _tree(tmp_path)
    target = root / "notes.txt"
    report = discover([target], DiscoverySettings(include_globs=("*.log",)))

    assert [item.path for item in report.files] == [target]


def test_missing_root_is_reported_not_raised(tmp_path: Path) -> None:
    report = discover([tmp_path / "does-not-exist"])

    assert report.files == []
    assert report.unreadable_roots
    assert "Could not read source" in "\n".join(report.summary_lines())


def test_max_files_bounds_the_walk(tmp_path: Path) -> None:
    root = tmp_path / "many"
    root.mkdir()
    for index in range(20):
        (root / f"file{index:02d}.log").write_text("x\n", encoding="utf-8")

    report = discover([root], DiscoverySettings(max_files=5))

    assert report.file_count == 5
    assert report.truncated is True
    assert "file limit" in "\n".join(report.summary_lines())


def test_symlinked_directory_cycle_terminates(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.log").write_text("a\n", encoding="utf-8")
    try:
        os.symlink(root, root / "sub" / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # platform without usable symlinks

    report = discover([root], DiscoverySettings(follow_symlinks=True, max_files=200))

    assert report.truncated is False
    assert report.file_count >= 1


# --- reader -----------------------------------------------------------------


def test_looks_binary_detects_nul_bytes(tmp_path: Path) -> None:
    text = tmp_path / "a.log"
    text.write_text("hello\n", encoding="utf-8")
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"hello\x00world")

    assert looks_binary(text) is False
    assert looks_binary(binary) is True


def test_read_last_lines_only_reads_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(10_000)), encoding="utf-8")

    result = read_last_lines(path, 5)

    assert result.lines == [f"line {i}" for i in range(9995, 10000)]
    assert result.offset == path.stat().st_size
    assert result.truncated is True


def test_read_last_lines_handles_short_and_empty_files(tmp_path: Path) -> None:
    short = tmp_path / "short.log"
    short.write_text("only\n", encoding="utf-8")
    assert read_last_lines(short, 100).lines == ["only"]
    assert read_last_lines(short, 100).truncated is False

    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    assert read_last_lines(empty, 100).lines == []


def test_read_last_lines_respects_a_byte_budget(tmp_path: Path) -> None:
    path = tmp_path / "wide.log"
    path.write_text("".join(f"{'x' * 100}\n" for _ in range(1000)), encoding="utf-8")

    result = read_last_lines(path, 500, max_bytes=1024)

    assert len(result.lines) < 500
    assert result.truncated is True


def test_reader_streams_appended_lines(tmp_path: Path) -> None:
    path = tmp_path / "stream.log"
    path.write_text("first\n", encoding="utf-8")

    reader = SourceReader(path, max_lines=100)
    assert reader.prime().lines == ["first"]
    assert reader.poll().lines == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("second\nthird\n")

    assert reader.poll().lines == ["second", "third"]


def test_reader_holds_partial_lines_until_the_newline_arrives(tmp_path: Path) -> None:
    path = tmp_path / "partial.log"
    path.write_text("first\n", encoding="utf-8")

    reader = SourceReader(path, max_lines=100)
    reader.prime()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("half")
    assert reader.poll().lines == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write(" done\n")
    assert reader.poll().lines == ["half done"]


def test_reader_recovers_from_truncation(tmp_path: Path) -> None:
    path = tmp_path / "trunc.log"
    path.write_text("old line\n" * 10, encoding="utf-8")

    reader = SourceReader(path, max_lines=100)
    reader.prime()

    path.write_text("fresh\n", encoding="utf-8")  # truncate in place
    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["fresh"]


def test_reader_follows_rotation_to_a_new_inode(tmp_path: Path) -> None:
    path = tmp_path / "rotate.log"
    path.write_text("before\n", encoding="utf-8")

    reader = SourceReader(path, max_lines=100)
    reader.prime()

    path.rename(tmp_path / "rotate.log.1")
    path.write_text("after\n", encoding="utf-8")

    result = reader.poll()
    assert result.rotated is True
    assert result.lines == ["after"]


def test_reader_survives_a_deleted_file(tmp_path: Path) -> None:
    path = tmp_path / "gone.log"
    path.write_text("here\n", encoding="utf-8")

    reader = SourceReader(path, max_lines=100)
    reader.prime()
    path.unlink()

    assert reader.poll().lines == []


# --- how a skip is explained ------------------------------------------------
#
# "Excluded" used to mean two unrelated things -- CLV cannot display this file
# type, and your own glob hid it -- so the count could not be acted on. The
# labels below keep those apart and name a skipped source rather than folding
# it into a tally.


def test_unsupported_types_and_user_filters_are_counted_apart(tmp_path: Path) -> None:
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "report.pdf").write_bytes(b"%PDF-1.6\n\x00binary")
    (root / "notes.tmp").write_text("scratch\n", encoding="utf-8")
    (root / "app.log").write_text("one\n", encoding="utf-8")

    report = discover([root], DiscoverySettings.from_strings(exclude="*.pdf, *.tmp"))

    assert [item.path.name for item in report.files] == ["app.log"]
    # *.pdf ships as a default and describes a file type; *.tmp is the
    # operator's own filter and must not be blamed on the file.
    assert report.skipped_unsupported == 1
    assert report.skipped_filtered == 1
    summary = "\n".join(report.summary_lines())
    assert "Skipped: 1 unsupported file type, 1 filtered out" in summary


def test_include_globs_count_as_filtered_not_unsupported(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "app.log").write_text("one\n", encoding="utf-8")
    (root / "notes.txt").write_text("two\n", encoding="utf-8")

    report = discover([root], DiscoverySettings(include_globs=("*.log",)))

    assert report.skipped_filtered == 1
    assert report.skipped_unsupported == 0


def test_a_named_source_that_is_skipped_is_named_back(tmp_path: Path) -> None:
    """A source the operator typed out must never vanish without explanation."""
    target = tmp_path / "addendum.pdf"
    target.write_bytes(b"%PDF-1.6\n\x00binary")

    report = discover([target])

    assert report.files == []
    assert report.skipped_sources == [(target, "unsupported file type")]
    assert f"File skipped - unsupported file type: {target}" in "\n".join(
        report.summary_lines()
    )


def test_a_named_source_bypasses_the_operators_own_globs(tmp_path: Path) -> None:
    """Naming a file is the operator saying they want it; filters narrow walks."""
    target = tmp_path / "notes.tmp"
    target.write_text("scratch\n", encoding="utf-8")

    report = discover([target], DiscoverySettings.from_strings(exclude="*.tmp"))

    assert [item.path for item in report.files] == [target]
    assert report.skipped_sources == []


def test_a_named_document_is_still_listed(tmp_path: Path) -> None:
    """Type-based exclusions apply to named files, but .ods is a supported type."""
    import zipfile

    target = tmp_path / "hosts.ods"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("content.xml", "<x/>")

    report = discover([target])

    assert [item.path for item in report.files] == [target]
    assert report.skipped_sources == []
