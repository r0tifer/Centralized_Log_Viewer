"""Field-aware query terms (Item 8).

The compatibility tests at the bottom are the important ones: a plain regex
must come out of :func:`parse_query` exactly as it went in, or every saved
query an operator has changes meaning the day this lands.
"""

from __future__ import annotations

import pytest

from clv.services.filtering import (
    FilterSpec,
    QueryError,
    count_matches,
    describe_empty_result,
    filter_entries,
)
from clv.services.parsing import LogParser
from clv.services.query import (
    MATCH_HIT,
    MATCH_MISS,
    MATCH_MISSING_FIELD,
    NORMALISED_FIELD_KEYS,
    FieldTerm,
    collect_field_names,
    match_terms,
    parse_query,
)

KNOWN = NORMALISED_FIELD_KEYS


def parse_lines(*lines: str):
    return LogParser().feed(list(lines))


SYSLOG = "Aug  7 09:25:01 web01 sshd[4821]: Accepted password for root"
SYSLOG_OTHER = "Aug  7 09:25:02 db02 cron[91]: session opened"
ACCESS = '10.0.0.7 - alice [07/Aug/2026:09:25:03 +0000] "GET /admin HTTP/1.1" 500 271'
ACCESS_OK = '10.0.0.8 - bob [07/Aug/2026:09:25:04 +0000] "GET /health HTTP/1.1" 200 12'
PLAIN = "no structure here at all"


# --- the grammar ------------------------------------------------------------


def test_a_known_key_becomes_a_term_and_leaves_the_rest_as_text() -> None:
    parsed = parse_query("host:web01 timeout|refused", KNOWN)
    assert parsed.terms == (FieldTerm("host", ":", "web01"),)
    assert parsed.text == "timeout|refused"


def test_an_unknown_key_stays_part_of_the_regex() -> None:
    """`sshd:` and `kernel:` are what people actually grep a syslog for."""

    for query in ("sshd:", "kernel: oom-killer", "hsot:web01"):
        parsed = parse_query(query, KNOWN)
        assert parsed.terms == ()
        assert parsed.text == query


def test_a_key_is_matched_case_insensitively_against_the_vocabulary() -> None:
    assert parse_query("Host:web01", KNOWN).terms == (FieldTerm("Host", ":", "web01"),)


def test_a_bare_timestamp_is_not_a_term() -> None:
    """A key has to start with a letter, which is what protects `10:30:00`."""

    parsed = parse_query("10:30:00", KNOWN)
    assert parsed.terms == ()
    assert parsed.text == "10:30:00"


@pytest.mark.parametrize(
    "query, expected",
    [
        ('msg:"disk full"', FieldTerm("msg", ":", "disk full")),
        ("msg:'disk full'", FieldTerm("msg", ":", "disk full")),
        ('request:"GET /a:b"', FieldTerm("request", ":", "GET /a:b")),
    ],
)
def test_a_quoted_value_keeps_its_spaces_and_colons(query: str, expected) -> None:
    assert parse_query(query, KNOWN | {"msg"}).terms == (expected,)


def test_an_unterminated_quote_is_taken_literally() -> None:
    """Otherwise `"GET /admin` would swallow the rest of the query."""

    parsed = parse_query('host:web01 "GET /admin', KNOWN)
    assert parsed.terms == (FieldTerm("host", ":", "web01"),)
    assert parsed.text == '"GET /admin'


def test_several_terms_and_text_survive_together() -> None:
    parsed = parse_query("tag:sshd host:web01 status>=500 accepted", KNOWN)
    assert len(parsed.terms) == 3
    assert parsed.text == "accepted"
    assert parsed.field_keys == ("tag", "host", "status")


def test_a_comparison_without_a_value_is_a_query_error() -> None:
    with pytest.raises(QueryError):
        parse_query("status>=", KNOWN)


def test_a_colon_without_a_value_tests_for_presence() -> None:
    parsed = parse_query("host:", KNOWN)
    assert parsed.terms == (FieldTerm("host", ":", ""),)
    entry, plain = parse_lines(SYSLOG, PLAIN)
    assert match_terms(entry, parsed.terms) == MATCH_HIT
    assert match_terms(plain, parsed.terms) == MATCH_MISSING_FIELD


def test_no_vocabulary_means_no_terms() -> None:
    """Before a source is open there is nothing to ask about."""

    assert parse_query("host:web01", ()).terms == ()


# --- operators --------------------------------------------------------------


@pytest.mark.parametrize(
    "term, expected",
    [
        (FieldTerm("host", ":", "web"), True),
        (FieldTerm("host", ":", "WEB"), False),  # smart case: upper is literal
        (FieldTerm("host", ":", "db"), False),
        (FieldTerm("host", "=", "web01"), True),
        (FieldTerm("host", "=", "WEB01"), False),  # `=` is never smart-cased
        (FieldTerm("host", "!=", "db02"), True),
        (FieldTerm("tag", ":", "SSH"), False),
        (FieldTerm("tag", ":", "ssh"), True),
    ],
)
def test_string_operators(term: FieldTerm, expected: bool) -> None:
    (entry,) = parse_lines(SYSLOG)
    assert term.test(entry.fields) is expected


@pytest.mark.parametrize(
    "term, expected",
    [
        (FieldTerm("status", ">=", "500"), True),
        (FieldTerm("status", ">", "500"), False),
        (FieldTerm("status", "<", "500"), False),
        (FieldTerm("status", "<=", "500"), True),
        (FieldTerm("size", ">", "100"), True),
        # Numeric, not lexicographic: "271" < "9" as text.
        (FieldTerm("size", ">", "9"), True),
    ],
)
def test_numeric_comparison_when_both_sides_are_numbers(term: FieldTerm, expected: bool) -> None:
    (entry,) = parse_lines(ACCESS)
    assert term.test(entry.fields) is expected


def test_comparison_falls_back_to_text_when_either_side_is_not_a_number() -> None:
    (entry,) = parse_lines(SYSLOG)
    assert FieldTerm("host", ">", "abc").test(entry.fields) is True
    assert FieldTerm("host", "<", "abc").test(entry.fields) is False


def test_a_json_key_matches_whatever_case_the_log_wrote_it_in() -> None:
    (entry,) = parse_lines('{"level": "error", "Status": "503"}')
    assert FieldTerm("status", "=", "503").test(entry.fields) is True


def test_an_absent_field_reports_none_rather_than_false() -> None:
    (entry,) = parse_lines(PLAIN)
    assert FieldTerm("host", ":", "web01").test(entry.fields) is None


def test_missing_beats_miss_when_terms_disagree() -> None:
    (entry,) = parse_lines(SYSLOG)
    terms = (FieldTerm("host", ":", "web01"), FieldTerm("status", ">=", "500"))
    assert match_terms(entry, terms) == MATCH_MISSING_FIELD
    terms = (FieldTerm("host", ":", "nope"), FieldTerm("status", ">=", "500"))
    assert match_terms(entry, terms) == MATCH_MISS


# --- through the filter -----------------------------------------------------


def spec(query: str, **changes) -> FilterSpec:
    names = collect_field_names(parse_lines(SYSLOG, SYSLOG_OTHER, ACCESS, ACCESS_OK, PLAIN))
    return FilterSpec(query=query, known_fields=NORMALISED_FIELD_KEYS | names, **changes)


def test_a_field_term_filters_and_a_regex_still_applies() -> None:
    entries = parse_lines(SYSLOG, SYSLOG_OTHER)
    result = filter_entries(entries, spec("host:web01 Accepted"))
    assert [entry.raw for entry in result.entries] == [SYSLOG]
    assert result.stats.hidden_by_query == 1


def test_an_entry_without_the_field_is_hidden_and_counted_separately() -> None:
    entries = parse_lines(SYSLOG, ACCESS, PLAIN)
    result = filter_entries(entries, spec("status>=500"))
    assert [entry.raw for entry in result.entries] == [ACCESS]
    # syslog and the unparsed line have no status; neither is a query miss.
    assert result.stats.hidden_missing_field == 2
    assert result.stats.hidden_by_query == 0


def test_a_present_field_that_does_not_match_is_a_query_miss() -> None:
    entries = parse_lines(ACCESS, ACCESS_OK)
    result = filter_entries(entries, spec("status>=500"))
    assert result.stats.hidden_by_query == 1
    assert result.stats.hidden_missing_field == 0


def test_describe_empty_result_names_the_field() -> None:
    entries = parse_lines(SYSLOG, PLAIN)
    filter_spec = spec("status>=500")
    result = filter_entries(entries, filter_spec)
    message = describe_empty_result(result.stats, filter_spec)
    assert "'status' field" in message
    assert "2 carry no" in message


def test_describe_empty_result_names_several_fields() -> None:
    entries = parse_lines(PLAIN)
    filter_spec = spec("host:web01 status>=500")
    result = filter_entries(entries, filter_spec)
    message = describe_empty_result(result.stats, filter_spec)
    assert "'host', 'status' fields" in message


def test_invert_applies_to_the_text_half_only() -> None:
    """Field terms stay positive; `!=` is the per-term negation."""

    entries = parse_lines(SYSLOG, SYSLOG_OTHER)
    result = filter_entries(entries, spec("host:web01 Accepted", invert=True))
    # host:web01 still selects web01; the inverted regex then rejects it.
    assert result.entries == []
    result = filter_entries(entries, spec("host:web01 session", invert=True))
    assert [entry.raw for entry in result.entries] == [SYSLOG]


def test_terms_work_with_regex_switched_off() -> None:
    entries = parse_lines(SYSLOG, SYSLOG_OTHER)
    result = filter_entries(entries, spec("host:web01", regex=False))
    assert [entry.raw for entry in result.entries] == [SYSLOG]


def test_count_matches_agrees_with_the_filter() -> None:
    entries = parse_lines(SYSLOG, SYSLOG_OTHER, ACCESS, PLAIN)
    filter_spec = spec("host:web01")
    parsed = filter_spec.parse()
    assert count_matches(entries, None, parsed.terms) == len(
        filter_entries(entries, filter_spec).entries
    )


def test_collect_field_names_reports_only_what_is_there() -> None:
    names = collect_field_names(parse_lines(SYSLOG))
    assert names == frozenset({"host", "tag", "pid"})
    assert collect_field_names(parse_lines(PLAIN)) == frozenset()


# --- the compatibility bar --------------------------------------------------

CORPUS = [
    "error",
    "error|timeout",
    "sshd:",
    "kernel: oom-killer",
    r"\d{2}:\d{2}:\d{2}",
    "GET /admin",
    '"GET /admin HTTP/1.1"',
    "10:30:00",
    "^Aug  7",
    "(warn|crit)",
    "a  b",
    "http://example.com/x",
    "user@host",
    "[0-9]+",
    "",
]


@pytest.mark.parametrize("query", CORPUS)
def test_a_plain_query_reaches_the_regex_unmodified(query: str) -> None:
    parsed = parse_query(query, KNOWN | {"level", "msg"})
    assert parsed.terms == ()
    assert parsed.text == query


@pytest.mark.parametrize("query", CORPUS)
def test_a_plain_query_filters_exactly_as_it_did_before(query: str) -> None:
    """Compared against the pre-Item-8 behaviour: raw regex over every line."""

    import re

    entries = parse_lines(SYSLOG, SYSLOG_OTHER, ACCESS, ACCESS_OK, PLAIN)
    expected = (
        [entry.raw for entry in entries]
        if not query
        else [
            entry.raw
            for entry in entries
            if re.search(query, entry.raw, 0 if any(c.isupper() for c in query) else re.I)
        ]
    )
    result = filter_entries(entries, spec(query))
    assert [entry.raw for entry in result.entries] == expected


# --- through the app --------------------------------------------------------


def test_the_app_learns_the_vocabulary_from_the_source_it_opens(tmp_path) -> None:
    """A field term only works because the buffer taught the app the name."""

    import asyncio

    from clv.app import LogViewerApp
    from clv.services.config import LogConfig
    from clv.services.discovery import DiscoverySettings

    log = tmp_path / "access.log"
    log.write_text(f"{ACCESS}\n{ACCESS_OK}\n", encoding="utf-8")

    async def scenario() -> None:
        config = LogConfig(log_dirs=[tmp_path], discovery=DiscoverySettings())
        app = LogViewerApp(config=config)
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            app._select_source(log)
            await pilot.pause()

            assert {"host", "user", "request", "status", "size"} <= app._field_names
            # Offered as completions, and usable as terms.
            assert app.query_bar.completions is not None

            app._update_state(query="status>=500")
            app._render_log()
            await pilot.pause()

            shown = [entry.raw for entry in app.log_panel.entries]
            assert shown == [ACCESS]

    asyncio.run(scenario())


def test_a_source_with_no_fields_leaves_every_query_a_regex(tmp_path) -> None:
    import asyncio

    from clv.app import LogViewerApp
    from clv.services.config import LogConfig
    from clv.services.discovery import DiscoverySettings

    log = tmp_path / "plain.log"
    log.write_text("alpha:one\nbeta:two\n", encoding="utf-8")

    async def scenario() -> None:
        config = LogConfig(log_dirs=[tmp_path], discovery=DiscoverySettings())
        app = LogViewerApp(config=config)
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            app._select_source(log)
            await pilot.pause()

            assert app._field_names == frozenset()

            # `alpha:` is not a known key, so this is the substring search it
            # has always been rather than a term nothing can answer.
            app._update_state(query="alpha:one")
            app._render_log()
            await pilot.pause()

            assert [entry.raw for entry in app.log_panel.entries] == ["alpha:one"]

    asyncio.run(scenario())
