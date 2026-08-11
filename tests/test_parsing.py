from __future__ import annotations

import json
from datetime import datetime

import pytest

from clv.services.parsing import (
    LEVEL_CRITICAL,
    LEVEL_DEBUG,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARN,
    LogEntry,
    LogParser,
    normalize_level,
    parse_line,
    parse_lines,
    level_matches,
)


PYTHON_LOGGING = "2026-08-07 09:25:01 - WARNING - disk almost full"
SYSLOG = "Aug  7 09:25:01 myhost CRON[12345]: (root) CMD (/usr/bin/foo)"
SYSLOG_5424 = "<11>1 2026-08-07T09:25:01Z myhost app 123 - - database unreachable"
NGINX = '10.0.0.1 - - [07/Aug/2026:09:25:01 +0000] "GET / HTTP/1.1" 500 123'
NGINX_OK = '10.0.0.1 - - [07/Aug/2026:09:25:01 +0000] "GET / HTTP/1.1" 200 123'
JSONL = '{"ts":"2026-08-07T09:25:01Z","level":"error","msg":"boom"}'
ISO_BRACKET = "[2026-08-07 09:25:01] [error] upstream timed out"
ISO_PLAIN = "2026-08-07T09:25:01.123Z starting service"


@pytest.mark.parametrize(
    "line, expected_format, expected_level",
    [
        (PYTHON_LOGGING, "python-logging", LEVEL_WARN),
        (SYSLOG, "syslog", None),
        (SYSLOG_5424, "syslog-5424", LEVEL_ERROR),
        (NGINX, "access-log", LEVEL_ERROR),
        (NGINX_OK, "access-log", LEVEL_INFO),
        (JSONL, "json", LEVEL_ERROR),
        (ISO_BRACKET, "iso-level", LEVEL_ERROR),
    ],
)
def test_formats_are_recognised(line: str, expected_format: str, expected_level) -> None:
    entry = parse_line(line)
    assert entry.format_name == expected_format
    assert entry.level == expected_level
    assert entry.raw == line


def test_every_line_yields_an_entry_with_raw_preserved() -> None:
    junk = "!!! not a log line at all !!!"
    entry = parse_line(junk)
    assert entry.raw == junk
    assert entry.format_name == "raw"
    assert entry.timestamp is None


def test_timestamps_are_extracted() -> None:
    assert parse_line(PYTHON_LOGGING).timestamp == datetime(2026, 8, 7, 9, 25, 1)
    assert parse_line(ISO_PLAIN).timestamp is not None
    nginx_ts = parse_line(NGINX).timestamp
    assert nginx_ts is not None and nginx_ts.day == 7


def test_syslog_year_inference_does_not_jump_into_the_future() -> None:
    # A December stamp read in January belongs to the previous year.
    january = datetime(2026, 1, 2, 0, 0, 0)
    entry = parse_line("Dec 31 23:59:00 host proc: rotating", now=january)
    assert entry.timestamp is not None
    assert entry.timestamp.year == 2025


def test_warning_and_warn_both_normalise() -> None:
    assert normalize_level("WARNING") == LEVEL_WARN
    assert normalize_level("warn") == LEVEL_WARN
    assert normalize_level("Err") == LEVEL_ERROR
    assert normalize_level("fatal") == LEVEL_CRITICAL
    assert normalize_level(7) == LEVEL_DEBUG
    assert normalize_level("nonsense") is None


def test_severity_bucket_accepts_both_warn_spellings() -> None:
    """The bug that made a Warn filter hide every WARNING line."""
    assert level_matches(normalize_level("WARNING"), "warn")
    assert level_matches(normalize_level("WARN"), "warn")
    # error covers fatal/critical so a crash is never hidden from "Error"
    assert level_matches(LEVEL_CRITICAL, "error")
    assert not level_matches(LEVEL_INFO, "error")
    # "all" never filters, even for a line with no level at all
    assert level_matches(None, "all")
    assert not level_matches(None, "error")


def test_continuation_lines_inherit_their_parent_entry() -> None:
    lines = [
        "2026-08-07 09:25:01 - ERROR - request failed",
        "Traceback (most recent call last):",
        '  File "app.py", line 3, in handler',
        "ValueError: bad input",
        "2026-08-07 09:26:00 - INFO - recovered",
    ]
    entries = parse_lines(lines)

    traceback_entries = entries[1:4]
    for entry in traceback_entries:
        assert entry.continuation is True
        assert entry.level == LEVEL_ERROR
        assert entry.timestamp == datetime(2026, 8, 7, 9, 25, 1)

    assert entries[4].level == LEVEL_INFO
    assert entries[4].continuation is False


def test_leading_unstructured_lines_stay_unstructured() -> None:
    entries = parse_lines(["a preamble line", "another one"])
    assert all(entry.timestamp is None and not entry.continuation for entry in entries)


def test_parser_carries_structure_across_feeds() -> None:
    """A traceback split across two tail polls still inherits its ERROR."""
    parser = LogParser()
    first = parser.feed(["2026-08-07 09:25:01 - ERROR - exploded"])
    assert first[0].level == LEVEL_ERROR

    second = parser.feed(["Traceback (most recent call last):"])
    assert second[0].continuation is True
    assert second[0].level == LEVEL_ERROR

    parser.reset()
    third = parser.feed(["  still indented"])
    assert third[0].continuation is False
    assert third[0].level is None


def test_bare_level_scan_only_looks_at_the_head_of_a_line() -> None:
    assert parse_line("[ERROR] something broke").level == LEVEL_ERROR
    tail = "x" * 200 + " ERROR "
    assert parse_line(tail).level is None


# --- parsed fields ----------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected_fields",
    [
        (SYSLOG, {"host": "myhost", "tag": "CRON", "pid": "12345"}),
        # RFC 5424's APP-NAME is filed under `tag`, the same key BSD syslog
        # uses, so one query reaches both dialects.
        (SYSLOG_5424, {"host": "myhost", "tag": "app", "pid": "123"}),
        (
            NGINX,
            {
                "host": "10.0.0.1",
                "request": "GET / HTTP/1.1",
                "status": "500",
                "size": "123",
            },
        ),
        (JSONL, {"ts": "2026-08-07T09:25:01Z", "level": "error", "msg": "boom"}),
        # Formats that capture nothing beyond timestamp and level.
        (PYTHON_LOGGING, {}),
        (ISO_BRACKET, {}),
        (ISO_PLAIN, {}),
    ],
)
def test_fields_are_carried_off_the_line(line: str, expected_fields: dict) -> None:
    assert dict(parse_line(line).fields) == expected_fields


def test_an_unrecognised_line_has_no_fields() -> None:
    assert parse_line("!!! not a log line at all !!!").fields == {}


def test_absent_groups_do_not_become_a_literal_dash() -> None:
    """`-` is RFC 5424's NILVALUE and CLF's absent ident/user."""

    # Both ident and user are "-" in NGINX, and neither should appear at all.
    assert "ident" not in parse_line(NGINX).fields
    assert "user" not in parse_line(NGINX).fields

    nil = "<11>1 2026-08-07T09:25:01Z myhost app - - - database unreachable"
    fields = parse_line(nil).fields
    assert dict(fields) == {"host": "myhost", "tag": "app"}


def test_a_syslog_line_without_a_pid_omits_the_key() -> None:
    entry = parse_line("Aug  7 09:25:01 myhost systemd: Started something")
    assert dict(entry.fields) == {"host": "myhost", "tag": "systemd"}


def test_continuation_inherits_timestamp_and_level_but_never_fields() -> None:
    """A stack trace frame has no host of its own to report."""

    entries = parse_lines(
        [
            "Aug  7 09:25:01 myhost app[99]: ERROR request failed",
            "Traceback (most recent call last):",
        ]
    )
    parent, continuation = entries

    assert parent.fields["host"] == "myhost"
    assert continuation.continuation is True
    assert continuation.timestamp == parent.timestamp
    assert continuation.level == parent.level
    assert continuation.fields == {}


def test_continuation_across_feeds_does_not_inherit_fields() -> None:
    parser = LogParser()
    parser.feed(["Aug  7 09:25:01 myhost app[99]: ERROR request failed"])
    (continuation,) = parser.feed(['  File "app.py", line 3, in handler'])

    assert continuation.continuation is True
    assert continuation.fields == {}


def test_json_objects_flatten_to_dotted_keys() -> None:
    line = '{"msg":"hi","req":{"id":"abc","headers":{"host":"web01"}}}'
    assert dict(parse_line(line).fields) == {
        "msg": "hi",
        "req.id": "abc",
        "req.headers.host": "web01",
    }


def test_json_flattening_honours_the_depth_cap() -> None:
    """Beyond the cap the subtree is kept as one string, not more keys."""

    line = '{"a":{"b":{"c":{"d":{"e":"deep"}}}}}'
    fields = parse_line(line).fields

    assert list(fields) == ["a.b.c.d"]
    assert fields["a.b.c.d"] == '{"e":"deep"}'


def test_json_flattening_honours_the_key_count_cap() -> None:
    payload = {f"k{index}": index for index in range(200)}
    fields = parse_line(json.dumps(payload)).fields

    assert len(fields) == 64
    # The cap truncates in document order rather than dropping arbitrarily.
    assert list(fields)[:2] == ["k0", "k1"]


def test_json_values_are_stringified_never_coerced() -> None:
    line = '{"status":500,"ok":true,"ratio":0.5,"missing":null,"tags":["a","b"]}'
    assert dict(parse_line(line).fields) == {
        "status": "500",
        "ok": "true",
        "ratio": "0.5",
        "missing": "",
        # A list is stringified rather than exploded into indices.
        "tags": '["a","b"]',
    }


def test_an_empty_nested_object_keeps_its_key() -> None:
    assert dict(parse_line('{"ctx":{}}').fields) == {"ctx": "{}"}


def test_a_json_key_colliding_with_a_normalised_name_keeps_the_json_value() -> None:
    line = '{"host":"from-json","tag":"from-json","msg":"hi"}'
    fields = parse_line(line).fields

    assert fields["host"] == "from-json"
    assert fields["tag"] == "from-json"


def test_log_entry_is_constructible_without_fields() -> None:
    """Backward compatibility: every existing construction site still works."""

    entry = LogEntry(raw="hello")
    assert entry.fields == {}


def test_fields_are_read_only_and_do_not_break_hashing() -> None:
    entry = parse_line(SYSLOG)

    with pytest.raises(TypeError):
        entry.fields["host"] = "elsewhere"  # type: ignore[index]

    # Item 7 keys bookmarks off entry content, so entries must stay hashable.
    assert hash(entry) == hash(parse_line(SYSLOG))
