"""logfmt is claimed by structure, and refusing is the answer that matters.

Every other format here is *anchored*: a timestamp shape, a leading `<`, a
bracket. logfmt has none, and `key=value` is ordinary inside a syslog message,
so the branch runs on lines no anchored format wanted and has to decline almost
all of them. That makes the false-positive corpus the point of this file rather
than an appendix to it -- a regression here does not fail loudly, it quietly
relabels somebody's auth.log.

The other half is the deferral. `2026-08-07T09:25:01Z level=error msg="boom"`
matched `_RE_ISO_PLAIN` before this phase and became an `iso` line with no
level, no fields and its entire payload sitting in `message`. Teaching a
built-in branch to stand down is what a plugin is refused, which is why logfmt
is a phase and not the reference plugin.
"""

from __future__ import annotations

import pytest

from clv.services.parsing import (
    _JSON_LEVEL_KEYS,
    _JSON_MSG_KEYS,
    _JSON_TS_KEYS,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARN,
    parse_line,
    parse_lines,
)
from clv.services.query import NORMALISED_FIELD_KEYS, collect_field_names
from clv.services.timeline import build_timeline
from clv.widgets.columns import _JSON_CONSUMED


# --- the false-positive corpus ----------------------------------------------

#: Lines that mention `key=value` and are *not* logfmt. Each names the guard
#: that refuses it, because a change that breaks one breaks it silently.
REFUSED: list[tuple[str, str]] = [
    # Guard 1: the line does not open with a pair.
    ("Failed password for root from 10.0.0.5 rhost=10.0.0.5 user=root", "opens"),
    ("audit: type=1400 apparmor=DENIED pid=991", "opens"),
    ("error: connection refused host=web01 port=443", "opens"),
    # Guard 2: a token that is not a pair.
    ("level=info msg=hi trailing", "every token"),
    ("level=info msg=hi -- and then prose", "every token"),
    ('msg="unterminated level=info', "every token"),
    # Guard 3: one pair is a .env line, a .properties entry, an assignment.
    ("FOO=bar", "two pairs"),
    ("enable_ssh=true", "two pairs"),
    ("level=info", "two pairs"),
    # Guard 4: no timestamp, level or message key to fill the row with.
    ("dur=1.2ms code=500 path=/x", "anchor"),
    ("a=1 b=2", "anchor"),
    ("PATH=/usr/bin HOME=/root", "anchor"),
    # No pairs at all.
    ("a line no format recognises at all", "opens"),
]


@pytest.mark.parametrize("line, guard", REFUSED, ids=[g for _, g in REFUSED])
def test_a_line_that_is_not_logfmt_is_not_claimed(line: str, guard: str) -> None:
    assert parse_line(line).format_name != "logfmt", f"the {guard!r} guard let it through"


def test_a_syslog_line_mentioning_pairs_is_still_syslog() -> None:
    """The line this phase's first requirement is about.

    `rhost=` and `user=` in an sshd message are the commonest `key=value` in any
    real log, and the fields that matter on that line are the ones syslog
    recovered rather than the ones the message happens to spell.
    """

    line = (
        "Aug  7 09:25:01 web01 sshd[991]: Failed password for root "
        "from 10.0.0.5 rhost=10.0.0.5 user=root"
    )
    entry = parse_line(line)

    assert entry.format_name == "syslog"
    assert dict(entry.fields) == {"host": "web01", "tag": "sshd", "pid": "991"}


def test_a_syslog_line_whose_payload_is_logfmt_is_still_syslog() -> None:
    """Syslog does not defer, and the asymmetry with the ISO branches is the point.

    `host`, `tag` and `pid` are facts about who reported the line. Relabelling
    would trade three of them for a format name.
    """

    entry = parse_line(
        'Aug  7 09:25:01 web01 api[12]: level=info msg="served" svc=api'
    )

    assert entry.format_name == "syslog"
    assert entry.fields["tag"] == "api"


# --- what a claimed line yields ---------------------------------------------

def test_the_bare_dialect_recovers_level_message_and_every_pair() -> None:
    entry = parse_line('level=info msg="thing happened" dur=1.2ms')

    assert entry.format_name == "logfmt"
    assert entry.level == LEVEL_INFO
    assert entry.message == "thing happened"
    assert entry.timestamp is None
    assert dict(entry.fields) == {
        "level": "info",
        "msg": "thing happened",
        "dur": "1.2ms",
    }


def test_a_ts_pair_is_parsed_into_the_timestamp() -> None:
    entry = parse_line('ts=2026-08-07T09:25:01Z level=error msg="boom" svc=api')

    assert entry.timestamp is not None
    assert entry.timestamp.hour == 9 and entry.timestamp.minute == 25
    # And it stays a field: the pair is what the writer wrote.
    assert entry.fields["ts"] == "2026-08-07T09:25:01Z"


@pytest.mark.parametrize("spelling", ["info", "INFO", "Info", "inf"])
def test_the_level_comes_from_the_value_and_not_from_the_scanner(spelling: str) -> None:
    """The test that fails the day the branch reaches for `_scan_level`.

    `_RE_BARE_LEVEL` is not `re.IGNORECASE`, so the scanner reads `level=INFO`
    and misses `level=info` -- which is exactly what the `iso` path did to these
    lines before this phase.
    """

    assert parse_line(f"level={spelling} msg=up").level == LEVEL_INFO


def test_a_line_with_no_message_key_falls_back_to_its_own_text() -> None:
    """As `_parse_json` falls back to the whole line. The row is never blank."""

    line = "ts=2026-08-07T09:25:01Z level=warn code=500"
    entry = parse_line(line)

    assert entry.format_name == "logfmt"
    assert entry.level == LEVEL_WARN
    assert entry.message == line


# --- quoting and escaping ----------------------------------------------------

def test_spaces_survive_inside_quotes() -> None:
    entry = parse_line('level=info msg="thing happened here" svc=api')
    assert entry.fields["msg"] == "thing happened here"
    assert entry.fields["svc"] == "api"


def test_escaped_quotes_and_backslashes_unescape() -> None:
    entry = parse_line(r'level=info msg="she said \"hi\"" sep="a\\b"')
    assert entry.fields["msg"] == 'she said "hi"'
    assert entry.fields["sep"] == "a\\b"


def test_a_windows_path_keeps_its_backslashes() -> None:
    """`path="C:\\Users\\bob"` is the common case; a deliberate `\\n` is not."""

    entry = parse_line(r'path="C:\Users\bob" msg="opened" level=info')
    assert entry.fields["path"] == r"C:\Users\bob"


def test_an_empty_value_is_kept() -> None:
    """As `_parse_json` keeps `{"err":""}`. A writer who typed `err=` said something."""

    entry = parse_line("err= msg=done level=info")
    assert entry.fields["err"] == ""
    assert "err" in entry.fields


@pytest.mark.parametrize(
    "line",
    [
        'level=info msg="never closed',
        # The one the separate value groups exist for: a *bare* token can both
        # start and end with a quote, so "was it closed" cannot be read off the
        # first character. Caught during this phase by an assertion that passed
        # in the prototype and failed in the branch.
        'level=info msg="ends with an escape\\"',
        'msg="never closed level=info svc=api',
    ],
)
def test_an_unterminated_quote_refuses_the_whole_line(line: str) -> None:
    assert parse_line(line).format_name == "raw"


# --- bounds ------------------------------------------------------------------

def test_sixty_five_pairs_yield_sixty_four_fields_in_document_order() -> None:
    pairs = ["msg=start"] + [f"k{index:02d}={index}" for index in range(64)]
    entry = parse_line(" ".join(pairs))

    assert entry.format_name == "logfmt"
    assert len(entry.fields) == 64
    assert list(entry.fields)[:3] == ["msg", "k00", "k01"]
    assert "k63" not in entry.fields


def test_a_repeated_key_keeps_the_last_value() -> None:
    """What `json.loads` does with a repeated object key."""

    entry = parse_line("level=info msg=first msg=second")
    assert entry.fields["msg"] == "second"
    assert entry.message == "second"


# --- the ISO deferral --------------------------------------------------------

def test_the_iso_plain_branch_defers_and_keeps_its_timestamp() -> None:
    """The line this phase exists for. `iso` before, `logfmt` after."""

    entry = parse_line('2026-08-07T09:25:01Z level=error msg="boom" svc=api')

    assert entry.format_name == "logfmt"
    assert entry.timestamp is not None and entry.timestamp.hour == 9
    assert entry.level == LEVEL_ERROR
    assert entry.message == "boom"
    # The timestamp came off the branch, not off a pair, so it is not a field.
    assert dict(entry.fields) == {"level": "error", "msg": "boom", "svc": "api"}


def test_the_iso_level_branch_defers_and_keeps_the_level_it_recovered() -> None:
    entry = parse_line('2026-08-07T09:25:01Z ERROR msg="boom" svc=api')

    assert entry.format_name == "logfmt"
    assert entry.level == LEVEL_ERROR
    assert entry.timestamp is not None
    assert dict(entry.fields) == {"msg": "boom", "svc": "api"}


def test_an_iso_line_whose_remainder_is_not_logfmt_is_unchanged() -> None:
    """The deferral is a guarded offer, not a change of branch."""

    assert parse_line("2026-08-07T09:25:01Z starting service").format_name == "iso"
    assert parse_line("[2026-08-07 09:25:01] [error] upstream timed out").format_name == (
        "iso-level"
    )


# --- the carry-forward trade -------------------------------------------------

def test_a_logfmt_line_is_not_a_continuation_of_the_line_above_it() -> None:
    """Deliberate, and the cost side of the trade.

    A logfmt line after an ISO line used to inherit that line's timestamp and
    render dimmed. It is now a first-class row with its own level and fields --
    and an empty time cell when it carries no `ts=`, because a continuation must
    not claim facts its line never stated.
    """

    entries = parse_lines(
        [
            "2026-08-07T09:25:01Z starting service",
            'level=error msg="boom" svc=api',
        ]
    )

    assert entries[1].format_name == "logfmt"
    assert entries[1].continuation is False
    assert entries[1].timestamp is None
    assert entries[1].level == LEVEL_ERROR


def test_a_dateless_logfmt_line_is_counted_as_undated_rather_than_placed() -> None:
    entries = parse_lines(
        [
            "ts=2026-08-07T09:25:01Z level=info msg=up",
            'level=error msg="boom" svc=api',
        ]
    )
    timeline = build_timeline(entries, width=40)

    assert timeline.undated == 1


# --- the field vocabulary ----------------------------------------------------

def test_a_logfmt_lines_keys_reach_completion() -> None:
    """Through `collect_field_names`, with no change to `query.py` at all."""

    entries = parse_lines(['level=info msg=up svc=api request_id=abc'])
    assert collect_field_names(entries) == frozenset(
        {"level", "msg", "svc", "request_id"}
    )


def test_logfmt_keys_do_not_join_the_normalised_vocabulary() -> None:
    """They are the writer's, exactly as JSON's are, and README publishes the list."""

    assert "svc" not in NORMALISED_FIELD_KEYS
    assert "msg" not in NORMALISED_FIELD_KEYS
    assert NORMALISED_FIELD_KEYS == frozenset(
        {"host", "tag", "pid", "msgid", "ident", "user", "request", "status",
         "size", "node"}
    )


# --- the hand-copy this format now shares ------------------------------------

def test_the_consumed_set_still_matches_the_parsers_own_key_tuples() -> None:
    """`_JSON_CONSUMED` is a hand-copy, and two formats now depend on it.

    It decides which chips the `json` and `logfmt` profiles withhold, and the
    same three tuples decide which keys the parser spends and which anchor a
    logfmt line. Nothing noticed if they drifted.
    """

    assert _JSON_CONSUMED == frozenset(
        _JSON_TS_KEYS + _JSON_LEVEL_KEYS + _JSON_MSG_KEYS
    )
