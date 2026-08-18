"""The readers-and-buffers layer behind the pane.

These cover the contract the app now depends on rather than the app itself:
that a single open log is a session of one, that a buffer resets its parser
across a rotation, and that ``origin_of`` names the source a line came from.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from clv.services.parsing import LogEntry
from clv.services.reader import open_reader
from clv.services.session import SourceBuffer, SourceSession


def _log(tmp_path: Path, name: str = "app.log", lines: int = 3) -> Path:
    path = tmp_path / name
    path.write_text(
        "".join(f"2026-08-11 10:00:{i:02d} - INFO - line {i}\n" for i in range(lines)),
        encoding="utf-8",
    )
    return path


# --- buffers ----------------------------------------------------------------


def test_priming_a_buffer_fills_it_from_the_reader(tmp_path: Path) -> None:
    session = SourceSession(max_lines=100)
    buffer = session.open_single(_log(tmp_path))

    assert [entry.message for entry in buffer.entries] == ["line 0", "line 1", "line 2"]


def test_polling_returns_only_what_arrived(tmp_path: Path) -> None:
    path = _log(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_single(path)

    with path.open("a", encoding="utf-8") as handle:
        handle.write("2026-08-11 10:00:09 - WARN - appended\n")

    outcomes = session.poll()

    assert len(outcomes) == 1
    assert [entry.message for entry in outcomes[0].entries] == ["appended"]
    assert len(session.entries) == 4


def test_a_quiet_source_produces_no_outcome(tmp_path: Path) -> None:
    session = SourceSession(max_lines=100)
    session.open_single(_log(tmp_path))

    assert session.poll() == []


def test_rotation_resets_the_parser_and_rebuilds_the_buffer(tmp_path: Path) -> None:
    path = _log(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_single(path)

    # Truncate in place: the reader reports this as a rotation, and what the
    # buffer holds must be the new content rather than the old plus the new.
    path.write_text("2026-08-11 11:00:00 - ERROR - after\n", encoding="utf-8")
    outcomes = session.poll()

    assert len(outcomes) == 1
    assert outcomes[0].rotated is True
    assert [entry.message for entry in session.entries] == ["after"]


def test_overflow_is_reported_so_the_caller_can_redraw(tmp_path: Path) -> None:
    """A ring buffer that dropped lines shifted the window; an append would lie."""

    path = _log(tmp_path, lines=4)
    session = SourceSession(max_lines=5)
    session.open_single(path)

    with path.open("a", encoding="utf-8") as handle:
        for i in range(4):
            handle.write(f"2026-08-11 10:01:{i:02d} - INFO - more {i}\n")

    outcomes = session.poll()

    assert outcomes[0].overflowed is True
    assert len(session.entries) == 5


def test_a_failed_open_leaves_the_working_source_alone(tmp_path: Path) -> None:
    def factory(path: Path, **kwargs):
        if path.name == "missing.log":
            raise OSError("nope")
        return open_reader(path, **kwargs)

    session = SourceSession(max_lines=100, reader_factory=factory)
    session.open_single(_log(tmp_path))

    with pytest.raises(OSError):
        session.open_single(tmp_path / "missing.log")

    assert session.primary_path is not None
    assert session.primary_path.name == "app.log"
    assert len(session.entries) == 3


def test_closing_a_buffer_closes_a_reader_that_holds_something() -> None:
    """A provider-backed reader may own a subprocess; it must not leak."""

    closed: list[bool] = []

    class Holder:
        path = Path("/virtual/source")
        RELOAD_NOTICE = "{name} reloaded."

        def close(self) -> None:
            closed.append(True)

    buffer = SourceBuffer(Path("/virtual/source"), max_lines=10, reader=Holder())
    buffer.close()

    assert closed == [True]


def test_a_reader_that_raises_on_close_does_not_escape() -> None:
    class Angry:
        path = Path("/virtual/source")
        RELOAD_NOTICE = "{name} reloaded."

        def close(self) -> None:
            raise RuntimeError("third-party code")

    buffer = SourceBuffer(Path("/virtual/source"), max_lines=10, reader=Angry())
    buffer.close()  # must not raise


# --- sessions ---------------------------------------------------------------


def test_a_single_source_session_hands_back_its_own_deque(tmp_path: Path) -> None:
    """The common path must not pay for a merge that has nothing to merge."""

    session = SourceSession(max_lines=100)
    buffer = session.open_single(_log(tmp_path))

    assert session.entries is buffer.entries
    assert isinstance(session.entries, deque)


def test_an_empty_session_still_looks_like_a_buffer() -> None:
    session = SourceSession(max_lines=100)

    assert len(session.entries) == 0
    assert session.primary_path is None
    assert bool(session) is False


def test_origin_of_names_the_source_a_line_came_from(tmp_path: Path) -> None:
    path = _log(tmp_path)
    session = SourceSession(max_lines=100)
    session.open_single(path)

    entry = next(iter(session.entries))

    assert session.origin_of(entry) == path


def test_a_detached_path_can_be_set_without_opening_anything() -> None:
    """A caller with lines of its own still has to say where they came from."""

    session = SourceSession(max_lines=100)
    session.set_primary_path(Path("/tmp/example.log"))
    session.set_entries(deque([LogEntry(raw="hand-made")]))

    assert session.primary_path == Path("/tmp/example.log")
    assert [entry.raw for entry in session.entries] == ["hand-made"]
    assert session.origin_of(LogEntry(raw="hand-made")) == Path("/tmp/example.log")


def test_setting_the_path_to_none_closes_the_session(tmp_path: Path) -> None:
    session = SourceSession(max_lines=100)
    session.open_single(_log(tmp_path))
    session.set_primary_path(None)

    assert session.primary_path is None
    assert len(session.entries) == 0


def test_resize_adopts_a_new_cap(tmp_path: Path) -> None:
    session = SourceSession(max_lines=100)
    session.resize(7)
    session.open_single(_log(tmp_path, lines=20))

    assert len(session.entries) == 7


# --- a source that stopped, versus one that is merely quiet -----------------
#
# `SourceBuffer.poll` swallows `OSError` on purpose: a source that vanished
# mid-session is not an error worth taking the pane down for. That is right for
# a rotated local file and silent for a dropped connection, so a reader that
# knows *why* it stopped says so in band instead.


class _StoppedReader:
    """A reader whose source went away and that is willing to say so."""

    RELOAD_NOTICE = "{name} was rotated; reloaded."

    def __init__(self, path: Path, problem: str = "") -> None:
        self.path = path
        self._problem = problem

    def prime(self):
        from clv.services.reader import TailRead

        return TailRead(lines=["first"], offset=5)

    def poll(self):
        from clv.services.reader import TailRead

        problem, self._problem = self._problem, ""
        return TailRead(lines=[], offset=5, problem=problem)


def test_a_quiet_poll_reports_no_problem(tmp_path: Path) -> None:
    """The overwhelmingly common case, and it must stay silent.

    A log with nothing new to say is not a problem, and a session that treated
    it as one would put a warning on screen twice a second for every idle log.
    """

    buffer = SourceBuffer(
        tmp_path / "app.log", max_lines=10, reader=_StoppedReader(tmp_path / "app.log")
    )
    buffer.prime()

    assert buffer.poll().problem == ""


def test_a_reader_that_stopped_says_so_through_the_outcome(tmp_path: Path) -> None:
    """The regression this phase exists for, at the session layer.

    Both polls above and below return no lines. Before Phase 5 they were the
    same value, so a dropped link and an idle log were indistinguishable to
    everything downstream — which is how a pane went quiet with no explanation.
    """

    reader = _StoppedReader(tmp_path / "app.log", "web01: the host did not answer.")
    buffer = SourceBuffer(tmp_path / "app.log", max_lines=10, reader=reader)
    buffer.prime()

    outcome = buffer.poll()

    assert outcome.problem == "web01: the host did not answer."
    assert outcome.entries == []
    # The buffer keeps what it had. A source that stopped is not a source that
    # was emptied, and the operator must not lose what is on screen.
    assert [entry.message for entry in buffer.entries] == ["first"]


def test_a_local_reader_never_sets_a_problem(tmp_path: Path) -> None:
    """Requirement 13, at this seam: nothing local moved.

    `SourceReader` does not know the word, so every local poll carries the empty
    default and the local path is byte-for-byte what it was.
    """

    path = _log(tmp_path)
    session = SourceSession(max_lines=100)
    buffer = session.open_single(path)

    assert buffer.poll().problem == ""

    with path.open("a", encoding="utf-8") as handle:
        handle.write("2026-08-11 10:00:09 - INFO - line 9\n")

    outcome = buffer.poll()
    assert [entry.message for entry in outcome.entries] == ["line 9"]
    assert outcome.problem == ""
