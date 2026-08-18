from __future__ import annotations

from datetime import datetime

import pytest

from clv.services.filtering import (
    FilterSpec,
    QueryError,
    TimeWindow,
    compile_query,
    count_matches,
    describe_empty_result,
    filter_entries,
    parse_absolute_window,
    parse_relative_window,
)
from clv.services.parsing import parse_lines

# The mixed-format sample that the old filter emptied out entirely.
MIXED = [
    "Aug  7 09:25:01 myhost CRON[12345]: (root) CMD (/usr/bin/foo)",
    '10.0.0.1 - - [07/Aug/2026:09:25:01 +0000] "GET / HTTP/1.1" 500 123',
    '{"ts":"2026-08-07T09:25:01Z","level":"error","msg":"boom"}',
    "2026-08-07 09:25:01 - WARNING - disk almost full",
    "2026-08-07 09:25:01 - WARN - disk almost full",
    "Traceback (most recent call last):",
]


def _entries(lines=None):
    return parse_lines(lines if lines is not None else MIXED)


def test_query_matches_the_whole_raw_line_across_formats() -> None:
    """Previously returned 0 of 6; the JSON line vanished despite containing 'error'."""
    result = filter_entries(_entries(), FilterSpec(query="(?i)error"))
    raws = [entry.raw for entry in result.entries]
    assert any("boom" in raw for raw in raws)
    assert len(result) >= 1


def test_smart_case_makes_lowercase_queries_case_insensitive() -> None:
    sample = _entries(["upper ERROR here", "lower error here"])

    # All-lowercase query: case-insensitive, so both lines match.
    assert len(filter_entries(sample, FilterSpec(query="error"))) == 2

    # An uppercase character in the query opts back into case sensitivity.
    assert len(filter_entries(sample, FilterSpec(query="ERROR"))) == 1

    # An explicit flag overrides the heuristic in either direction.
    assert len(filter_entries(sample, FilterSpec(query="error", case_sensitive=True))) == 1
    assert len(filter_entries(sample, FilterSpec(query="ERROR", case_sensitive=False))) == 2


def test_query_can_match_text_outside_the_message_field() -> None:
    """Timestamps, hosts and status codes are all searchable now."""
    # Every line but the bare traceback carries this timestamp.
    assert len(filter_entries(_entries(), FilterSpec(query="09:25"))) == len(MIXED) - 1
    assert len(filter_entries(_entries(), FilterSpec(query="myhost"))) == 1
    assert len(filter_entries(_entries(), FilterSpec(query=r"\b500\b"))) == 1


def test_unparsed_lines_survive_a_query_that_matches_them() -> None:
    result = filter_entries(_entries(), FilterSpec(query="Traceback"))
    assert [entry.raw for entry in result.entries] == ["Traceback (most recent call last):"]


def test_severity_warn_matches_both_warn_and_warning() -> None:
    """The old filter compared 'warn' to 'WARNING' and hid the WARNING line."""
    result = filter_entries(_entries(), FilterSpec(severity="warn"))
    matched = [entry.raw for entry in result.entries]

    assert "2026-08-07 09:25:01 - WARNING - disk almost full" in matched
    assert "2026-08-07 09:25:01 - WARN - disk almost full" in matched
    # ...plus the trailing traceback, which inherits WARN from the line above.
    assert len(result) == 3


def test_severity_error_includes_the_carried_forward_traceback() -> None:
    lines = [
        "2026-08-07 09:25:01 - ERROR - request failed",
        "Traceback (most recent call last):",
        "2026-08-07 09:26:00 - INFO - fine",
    ]
    result = filter_entries(_entries(lines), FilterSpec(severity="error"))
    assert [entry.raw for entry in result.entries] == lines[:2]


def test_plain_substring_mode_escapes_regex_metacharacters() -> None:
    lines = ["a literal (paren) line", "no parens here"]
    result = filter_entries(_entries(lines), FilterSpec(query="(paren)", regex=False))
    assert len(result) == 1

    with pytest.raises(QueryError):
        compile_query("(paren", regex=True)


def test_invert_flips_the_query() -> None:
    normal = filter_entries(_entries(), FilterSpec(query="myhost"))
    inverted = filter_entries(_entries(), FilterSpec(query="myhost", invert=True))
    assert len(normal) + len(inverted) == len(MIXED)


def test_time_window_filters_on_parsed_timestamps() -> None:
    lines = [
        "2026-08-07 09:00:00 - INFO - old",
        "2026-08-07 12:00:00 - INFO - new",
    ]
    window = TimeWindow(start=datetime(2026, 8, 7, 10), end=datetime(2026, 8, 7, 13))
    result = filter_entries(_entries(lines), FilterSpec(window=window))
    assert [entry.message for entry in result.entries] == ["new"]


def test_aware_and_naive_timestamps_can_coexist() -> None:
    """A JSON line with an offset next to syslog without one must not crash."""
    lines = [
        '{"ts":"2026-08-07T12:00:00+00:00","level":"info","msg":"aware"}',
        "2026-08-07 12:00:00 - INFO - naive",
    ]
    window = TimeWindow(start=datetime(2026, 8, 7, 11), end=datetime(2026, 8, 7, 13))
    result = filter_entries(_entries(lines), FilterSpec(window=window))
    assert len(result) == 2


def test_stats_explain_an_empty_pane() -> None:
    lines = ["no structure at all", "still nothing"]
    spec = FilterSpec(severity="error")
    result = filter_entries(_entries(lines), spec)

    assert len(result) == 0
    assert result.stats.hidden_by_severity == 2
    assert result.stats.hidden_missing_level == 2

    message = describe_empty_result(result.stats, spec)
    assert "no detected severity" in message


def test_relative_and_absolute_windows() -> None:
    now = datetime(2026, 8, 7, 12, 0, 0)
    window = parse_relative_window("15m", now=now)
    assert window.start == datetime(2026, 8, 7, 11, 45)

    assert not parse_relative_window("all", now=now).bounded

    with pytest.raises(ValueError):
        parse_relative_window("nonsense", now=now)

    absolute = parse_absolute_window("2026-08-07 09:00", "2026-08-07 10:00")
    assert absolute is not None and absolute.start == datetime(2026, 8, 7, 9)

    # Reversed input is normalised rather than rejected.
    reversed_window = parse_absolute_window("2026-08-07 10:00", "2026-08-07 09:00")
    assert reversed_window is not None
    assert reversed_window.start < reversed_window.end


def test_count_matches_powers_the_hit_counter() -> None:
    entries = _entries()
    assert count_matches(entries, None) == len(MIXED)
    assert count_matches(entries, compile_query("error")) >= 1


def test_an_unreachable_source_is_reported_rather_than_called_empty() -> None:
    """"No log entries in the selected source" is a claim about the source.

    CLV is in no position to make it about one it cannot currently see, and an
    unreachable source rendered as an empty one is the outcome Requirement 7
    exists to prevent. The reason wins over every filter explanation, because a
    filter that hid nothing is not why the pane is empty.
    """

    entries = parse_lines(["2026-08-07 09:25:01 - INFO - hello"])
    spec = FilterSpec(query="nothing-matches-this")
    result = filter_entries(entries, spec)

    assert describe_empty_result(result.stats, spec) == (
        "No matches — 1 filtered out by the query."
    )
    assert (
        describe_empty_result(
            result.stats, spec, unreachable="web01: the host did not answer."
        )
        == "web01: the host did not answer."
    )


def test_an_empty_unreachable_reason_changes_nothing() -> None:
    """The keyword is additive: every existing caller gets what it always did."""

    result = filter_entries([], FilterSpec())
    spec = FilterSpec()

    assert describe_empty_result(result.stats, spec, unreachable="") == (
        describe_empty_result(result.stats, spec)
    )
