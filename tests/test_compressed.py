"""Compressed members and rotated sets (Item 11).

Two things are being pinned down here. First, that a compressed member reads
correctly and *boundedly* — the line budget and the decompressed-byte cap are
the two promises that make reading a deflate stream acceptable at all. Second,
that a rotated set is one source: oldest-first across members, only the head
tailing, and the older members read once or not at all.
"""

from __future__ import annotations

import asyncio
import bz2
import gzip
import lzma
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import Static

from clv.app import LogTree, LogViewerApp
from clv.services import SourceManager

from clv.services.compressed import (
    CompressedError,
    CompressedReader,
    compression_for,
    is_compressed,
    probe,
    read_compressed_tail,
    strip_compression_suffix,
)
from clv.services.discovery import DEFAULT_EXCLUDE_GLOBS, discover
from clv.services.rotation import (
    RotatedSet,
    RotatedSetReader,
    describe_set,
    group_rotated,
)
from clv.services.session import ORIGIN_FIELD, SourceSession

CORPUS = [f"2026-08-{day:02d} 10:00:00 - INFO - line {day}" for day in range(1, 21)]


def _write(path: Path, lines: list[str], opener=None) -> Path:
    payload = "".join(f"{line}\n" for line in lines).encode("utf-8")
    if opener is None:
        path.write_bytes(payload)
    else:
        with opener(path, "wb") as handle:
            handle.write(payload)
    return path


# --- reading one compressed member ------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "opener"),
    [(".gz", gzip.open), (".bz2", bz2.open), (".xz", lzma.open)],
)
def test_every_supported_format_round_trips(tmp_path: Path, suffix, opener) -> None:
    path = _write(tmp_path / f"app.log{suffix}", CORPUS, opener)

    text = read_compressed_tail(path, 100)

    assert text.lines == CORPUS
    assert text.truncated is False


def test_the_line_budget_is_honoured_and_keeps_the_newest(tmp_path: Path) -> None:
    """A log's newest content is at the end — the opposite of a document."""

    path = _write(tmp_path / "app.log.gz", CORPUS, gzip.open)

    text = read_compressed_tail(path, 5)

    assert text.lines == CORPUS[-5:]
    assert text.truncated is True


def test_a_highly_compressible_file_stops_at_the_byte_cap(tmp_path: Path) -> None:
    """The bomb defence: bounded by decompressed bytes, not by size on disk."""

    path = tmp_path / "bomb.log.gz"
    with gzip.open(path, "wb") as handle:
        for _ in range(20_000):
            handle.write(b"x" * 1_000 + b"\n")

    assert path.stat().st_size < 1_000_000  # ~20 MB compresses to almost nothing

    text = read_compressed_tail(path, 100_000, max_bytes=64 * 1024)

    assert text.truncated is True
    # Whatever was read fits the budget; the point is that it stopped at all.
    assert 0 < len(text.lines) < 1_000


def test_a_member_with_no_trailing_newline_keeps_its_last_line(tmp_path: Path) -> None:
    path = tmp_path / "app.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"first\nsecond, unterminated")

    assert read_compressed_tail(path, 10).lines == ["first", "second, unterminated"]


def test_a_utf16_member_is_decoded_without_its_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "windows.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write("first\nsecond\n".encode("utf-16"))

    assert read_compressed_tail(path, 10).lines == ["first", "second"]


def test_a_damaged_member_raises_an_oserror(tmp_path: Path) -> None:
    """An OSError, so the app's existing read-failure path reports it."""

    path = tmp_path / "truncated.log.xz"
    path.write_bytes(b"\xfd7zXZ\x00 not really xz")

    with pytest.raises(CompressedError):
        read_compressed_tail(path, 10)
    assert issubclass(CompressedError, OSError)


def test_zero_budget_reads_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path / "app.log.gz", CORPUS, gzip.open)

    assert read_compressed_tail(path, 0).lines == []


def test_suffix_helpers(tmp_path: Path) -> None:
    assert is_compressed(Path("app.log.gz")) is True
    assert is_compressed(Path("app.log")) is False
    assert compression_for(Path("app.log.zst")) is None  # no stdlib decompressor
    assert strip_compression_suffix(Path("app.log.2.gz")) == Path("app.log.2")
    assert strip_compression_suffix(Path("app.log.2")) == Path("app.log.2")


def test_probe_separates_a_real_archive_from_a_damaged_one(tmp_path: Path) -> None:
    good = _write(tmp_path / "good.log.gz", CORPUS, gzip.open)
    bad = tmp_path / "bad.log.gz"
    bad.write_bytes(b"\x1f\x8b not gzip")

    assert probe(good) is True
    assert probe(bad) is False


# --- the reader -------------------------------------------------------------


def test_a_rotated_out_member_is_not_re_read_on_poll(tmp_path: Path) -> None:
    """Nothing appends to syslog.2.gz, so polling one must cost no read."""

    path = _write(tmp_path / "app.log.1.gz", CORPUS, gzip.open)
    reader = CompressedReader(path, max_lines=100)
    reader.prime()

    assert reader.poll().lines == []
    assert reader.poll().lines == []


def test_a_member_replaced_on_disk_is_reported_as_a_reload(tmp_path: Path) -> None:
    path = _write(tmp_path / "app.log.1.gz", CORPUS, gzip.open)
    reader = CompressedReader(path, max_lines=100)
    reader.prime()

    _write(path, ["2026-09-01 00:00:00 - WARN - replaced"], gzip.open)
    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["2026-09-01 00:00:00 - WARN - replaced"]


# --- discovery --------------------------------------------------------------


def test_compressed_logs_are_no_longer_excluded_by_default() -> None:
    for glob in ("*.gz", "*.bz2", "*.xz"):
        assert glob not in DEFAULT_EXCLUDE_GLOBS
    # Still excluded, and for two different reasons: no stdlib decompressor,
    # and an archive is a container of files rather than a log.
    for glob in ("*.zst", "*.zip", "*.tar", "*.tgz"):
        assert glob in DEFAULT_EXCLUDE_GLOBS


def test_a_compressed_log_is_discovered(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    _write(root / "app.log", CORPUS)
    _write(root / "app.log.1.gz", CORPUS, gzip.open)

    report = discover([root])

    assert {item.path.name for item in report.files} == {"app.log", "app.log.1.gz"}


def test_a_corrupt_archive_is_reported_as_unreadable_when_named(tmp_path: Path) -> None:
    """A file the operator typed out is named back at them, never a tally."""

    bad = tmp_path / "broken.log.gz"
    bad.write_bytes(b"\x1f\x8b not gzip")

    report = discover([bad])

    assert report.files == []
    assert [path for path, _reason in report.skipped_sources] == [bad]
    assert report.skipped_sources[0][1] == "unreadable"


# --- grouping ---------------------------------------------------------------


def test_numeric_rotation_groups_into_one_set(tmp_path: Path) -> None:
    paths = [
        tmp_path / "app.log",
        tmp_path / "app.log.1",
        tmp_path / "app.log.2.gz",
        tmp_path / "app.log.3.gz",
    ]
    sets, singles = group_rotated(paths)

    assert singles == []
    assert len(sets) == 1
    assert [member.name for member in sets[0].members] == [
        "app.log",
        "app.log.1",
        "app.log.2.gz",
        "app.log.3.gz",
    ]
    assert sets[0].name == "app.log"


def test_a_gap_in_the_numbering_is_not_a_problem(tmp_path: Path) -> None:
    """The set is what is on disk, not what a sequence implies."""

    sets, singles = group_rotated(
        [tmp_path / "app.log", tmp_path / "app.log.1", tmp_path / "app.log.4.gz"]
    )

    assert singles == []
    assert [member.name for member in sets[0].members] == [
        "app.log",
        "app.log.1",
        "app.log.4.gz",
    ]


def test_a_set_with_no_live_head_still_groups(tmp_path: Path) -> None:
    sets, singles = group_rotated([tmp_path / "app.log.1", tmp_path / "app.log.2.gz"])

    assert singles == []
    assert sets[0].head.name == "app.log.1"


@pytest.mark.parametrize(
    "names",
    [
        ("app.log", "app.log-20260810", "app.log-20260809"),
        ("app.log", "app.log.2026-08-10", "app.log.2026-08-09"),
        ("app.log", "app.log-20260810.gz", "app.log-20260809.gz"),
    ],
)
def test_dated_rotation_groups_newest_first(tmp_path: Path, names) -> None:
    sets, singles = group_rotated([tmp_path / name for name in names])

    assert singles == []
    assert [member.name for member in sets[0].members] == list(names)


def test_unrelated_files_are_left_alone(tmp_path: Path) -> None:
    sets, singles = group_rotated(
        [tmp_path / "app.log", tmp_path / "auth.log", tmp_path / "notes.txt"]
    )

    assert sets == []
    assert [path.name for path in singles] == ["app.log", "auth.log", "notes.txt"]


def test_a_lone_rotated_file_is_a_file_not_a_set(tmp_path: Path) -> None:
    sets, singles = group_rotated([tmp_path / "app.log.1"])

    assert sets == []
    assert [path.name for path in singles] == ["app.log.1"]


# --- reading a set ----------------------------------------------------------


def _rotated_tree(tmp_path: Path) -> list[Path]:
    """A three-member set: live head, plain .1, compressed .2."""

    return [
        _write(tmp_path / "app.log", ["head 1", "head 2"]),
        _write(tmp_path / "app.log.1", ["middle 1", "middle 2"]),
        _write(tmp_path / "app.log.2.gz", ["oldest 1", "oldest 2"], gzip.open),
    ]


def test_entries_emerge_oldest_first_across_members(tmp_path: Path) -> None:
    paths = _rotated_tree(tmp_path)
    rotated = group_rotated(paths)[0][0]

    result = RotatedSetReader(rotated, max_lines=100).prime()

    assert result.lines == [
        "oldest 1",
        "oldest 2",
        "middle 1",
        "middle 2",
        "head 1",
        "head 2",
    ]


def test_every_line_knows_which_member_it_came_from(tmp_path: Path) -> None:
    paths = _rotated_tree(tmp_path)
    rotated = group_rotated(paths)[0][0]

    result = RotatedSetReader(rotated, max_lines=100).prime()

    assert result.origins is not None
    assert [origin.name for origin in result.origins] == [
        "app.log.2.gz",
        "app.log.2.gz",
        "app.log.1",
        "app.log.1",
        "app.log",
        "app.log",
    ]


def test_the_budget_is_shared_and_spent_newest_first(tmp_path: Path) -> None:
    """A set whose head fills the buffer never opens the older members."""

    paths = _rotated_tree(tmp_path)
    rotated = group_rotated(paths)[0][0]
    reader = RotatedSetReader(rotated, max_lines=2)

    result = reader.prime()

    assert result.lines == ["head 1", "head 2"]
    assert reader.members_read == 1
    assert reader.members_available == 3
    assert result.truncated is True


def test_a_partial_budget_takes_the_newest_of_the_next_member(tmp_path: Path) -> None:
    paths = _rotated_tree(tmp_path)
    rotated = group_rotated(paths)[0][0]

    result = RotatedSetReader(rotated, max_lines=3).prime()

    assert result.lines == ["middle 2", "head 1", "head 2"]


def test_only_the_live_member_tails(tmp_path: Path) -> None:
    paths = _rotated_tree(tmp_path)
    rotated = group_rotated(paths)[0][0]
    reader = RotatedSetReader(rotated, max_lines=100)
    reader.prime()

    with paths[0].open("a", encoding="utf-8") as handle:
        handle.write("head 3\n")
    # Touching a rotated-out member must produce nothing: it is finished, and
    # re-reading it on every poll is the cost this design exists to avoid.
    _write(paths[1], ["middle 1", "middle 2", "middle 3"])

    result = reader.poll()

    assert result.lines == ["head 3"]
    assert result.origins == (paths[0],)


def test_a_damaged_member_costs_only_itself(tmp_path: Path) -> None:
    paths = _rotated_tree(tmp_path)
    paths[2].write_bytes(b"\x1f\x8b not gzip")
    rotated = group_rotated(paths)[0][0]

    result = RotatedSetReader(rotated, max_lines=100).prime()

    assert result.lines == ["middle 1", "middle 2", "head 1", "head 2"]
    assert result.truncated is True


def test_describe_set_says_what_it_actually_read(tmp_path: Path) -> None:
    rotated = group_rotated(_rotated_tree(tmp_path))[0][0]

    assert describe_set(rotated, 3) == "app.log: read 3 members."
    assert "read 1 of 3 members" in describe_set(rotated, 1)


def test_a_session_opens_a_set_and_tags_its_entries(tmp_path: Path) -> None:
    paths = _rotated_tree(tmp_path)
    rotated = group_rotated(paths)[0][0]

    session = SourceSession(max_lines=100)
    session.open_rotated(rotated)
    entries = list(session.entries)

    assert [entry.raw for entry in entries][:2] == ["oldest 1", "oldest 2"]
    assert entries[0].fields[ORIGIN_FIELD] == str(paths[2])
    # The whole point of tagging: the status line can name the member the
    # cursor is in, and marks in one member cannot collide with another's.
    assert session.origin_of(entries[0]) == paths[2]
    assert session.origin_of(entries[-1]) == paths[0]


# --- the app ----------------------------------------------------------------


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _status(app: LogViewerApp) -> str:
    return app.query_one("#status-bar", Static).render().plain


def _rotated_root(tmp_path: Path) -> Path:
    root = tmp_path / "logs"
    root.mkdir()
    _write(root / "app.log", ["head 1", "head 2"])
    _write(root / "app.log.1", ["middle 1", "middle 2"])
    _write(root / "app.log.2.gz", ["oldest 1", "oldest 2"], gzip.open)
    _write(root / "auth.log", ["unrelated"])
    return root


def test_a_rotated_set_is_one_node_with_its_members_underneath(tmp_path: Path) -> None:
    root = _rotated_root(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            sets = [node for node in _walk(tree.root) if isinstance(node.data, RotatedSet)]

            assert len(sets) == 1
            assert "3 files" in str(sets[0].label)
            # The members are still individually openable underneath it.
            assert {Path(str(child.data)).name for child in sets[0].children} == {
                "app.log",
                "app.log.1",
                "app.log.2.gz",
            }
            # An unrelated log is untouched by grouping.
            leaves = {
                Path(str(node.data)).name
                for node in _walk(tree.root)
                if isinstance(node.data, Path) and node.parent is not sets[0]
            }
            assert "auth.log" in leaves

    asyncio.run(scenario())


def test_group_rotated_false_lists_the_members_individually(tmp_path: Path) -> None:
    root = _rotated_root(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app.advanced_drawer._settings = replace(
                app.advanced_drawer.settings, group_rotated=False
            )
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            assert not [n for n in _walk(tree.root) if isinstance(n.data, RotatedSet)]
            names = {
                Path(str(node.data)).name
                for node in _walk(tree.root)
                if isinstance(node.data, Path) and node.data.is_file()
            }
            assert {"app.log", "app.log.1", "app.log.2.gz", "auth.log"} <= names

    asyncio.run(scenario())


def test_opening_a_set_shows_every_member_and_names_the_one_under_the_cursor(
    tmp_path: Path,
) -> None:
    root = _rotated_root(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            rotated = next(n.data for n in _walk(tree.root) if isinstance(n.data, RotatedSet))
            app._select_rotated_set(rotated)
            await pilot.pause()

            assert [entry.raw for entry in app._entries] == [
                "oldest 1",
                "oldest 2",
                "middle 1",
                "middle 2",
                "head 1",
                "head 2",
            ]

            # The status line names the member the cursor is in — the one
            # thing about a multi-file source that nothing else on screen says.
            app.log_panel.move_cursor(0)
            await pilot.pause()
            assert "in app.log.2.gz" in _status(app)

            app.log_panel.move_cursor(len(app.log_panel.rows) - 1)
            await pilot.pause()
            # The head is the source itself, so repeating its name would be noise.
            assert "in app.log" not in _status(app)

    asyncio.run(scenario())


def test_a_set_tails_its_live_member(tmp_path: Path) -> None:
    root = _rotated_root(tmp_path)

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()

            tree = app.query_one("#source-tree", LogTree)
            rotated = next(n.data for n in _walk(tree.root) if isinstance(n.data, RotatedSet))
            app._select_rotated_set(rotated)
            await pilot.pause()

            with (root / "app.log").open("a", encoding="utf-8") as handle:
                handle.write("head 3\n")
            app._poll_tail()
            await pilot.pause()

            assert [entry.raw for entry in app._entries][-1] == "head 3"

    asyncio.run(scenario())


# --- reading from a handle rather than a path -------------------------------
#
# The split that makes a *remote* .gz readable: `decompress_tail` never learns
# where its bytes came from. Half of /var/log is rotated and compressed, so a
# transport that could not read one would have met a fraction of the goal.


def test_decompress_tail_reads_from_any_binary_handle(tmp_path: Path) -> None:
    """A BytesIO is the shape a remote backend hands over."""

    from io import BytesIO

    from clv.services.compressed import GZIP, decompress_tail

    payload = BytesIO()
    with gzip.GzipFile(fileobj=payload, mode="wb") as handle:
        handle.write(b"alpha\nbravo\ncharlie\n")

    text = decompress_tail(BytesIO(payload.getvalue()), GZIP, max_lines=2)

    assert text.lines == ["bravo", "charlie"]
    assert text.truncated is True


def test_decompress_tail_names_the_member_in_its_error() -> None:
    """The handle carries no name, so one is passed. A damaged archive still
    has to say *which* archive it was."""

    from io import BytesIO

    from clv.services.compressed import GZIP, CompressedError, decompress_tail

    with pytest.raises(CompressedError) as caught:
        decompress_tail(BytesIO(b"not gzip at all"), GZIP, max_lines=10, name="syslog.2.gz")

    assert "syslog.2.gz" in str(caught.value)


def test_a_compressed_member_reads_through_an_injected_backend(tmp_path: Path) -> None:
    """Same bytes, same lines, whoever opened the file."""

    from clv.services.backend import LOCAL
    from clv.services.compressed import read_compressed_tail

    member = tmp_path / "app.log.1.gz"
    with gzip.open(member, "wb") as handle:
        handle.write(b"alpha\nbravo\n")

    assert read_compressed_tail(member, 10).lines == ["alpha", "bravo"]
    assert read_compressed_tail(member, 10, backend=LOCAL).lines == ["alpha", "bravo"]


def test_probe_goes_through_the_backend(tmp_path: Path) -> None:
    """Discovery's cheap "does this archive open at all" test, which on a
    remote root must not become a second round trip per file."""

    from clv.services.backend import LOCAL
    from clv.services.compressed import probe

    good = tmp_path / "good.gz"
    with gzip.open(good, "wb") as handle:
        handle.write(b"alpha\n")
    bad = tmp_path / "bad.gz"
    bad.write_bytes(b"definitely not gzip")

    opened: list[Path] = []

    class Counting:
        capabilities = LOCAL.capabilities

        def open(self, ref, mode="rb"):
            opened.append(ref)
            return ref.open(mode)

    assert probe(good, backend=Counting()) is True
    assert probe(bad, backend=Counting()) is False
    assert opened == [good, bad], "one open per candidate, and only one"
