"""A format is four registrations, and three of them fail quietly.

Adding a format to :mod:`clv.services.parsing` and nowhere else used to fail
nothing. The parser would produce entries with the new ``format_name``; the
detail pane would fall back to the bare identifier; the column profile would
fall back to :data:`DEFAULT_PROFILE`, which renders a row with the right
timestamp and level and *no source cell and no chips*. Nothing raises, nothing
is logged, and the only symptom is that the new format's rows are worse than
everything around them.

So :data:`clv.services.parsing.FORMAT_NAMES` is the canonical list and this file
checks the other three against it, in both directions, with a real line per
name. Phase 7b of ``PLUGIN_TODO.md`` adds ``logfmt`` to the parser; this is what
makes it impossible to add it there alone.

The fixtures are deliberately literal rather than generated. A test that derived
the expected names from the tables it is checking would pass forever and mean
nothing.
"""

from __future__ import annotations

import pytest

from clv.services.formats import FORMAT_LABELS, NO_FIELD_REASONS
from clv.services.parsing import FORMAT_NAMES, parse_line
from clv.widgets.columns import FORMAT_PROFILES

#: One real line per name in ``FORMAT_NAMES``. Adding a format to the parser
#: without adding a line here fails ``test_every_format_name_has_a_line``,
#: which is the first gate: a name nothing can produce is not a format.
FORMAT_LINES: dict[str, str] = {
    "syslog": "Aug 21 09:25:01 web01 sshd[1234]: Failed password for root",
    "syslog-5424": (
        "<34>1 2026-08-21T09:25:01.000003Z web01 sshd 1234 ID47 - Failed password"
    ),
    "access-log": (
        '10.0.0.1 - deploy [21/Aug/2026:09:25:01 +0000] '
        '"GET /pay HTTP/1.1" 500 123'
    ),
    "json": '{"ts": "2026-08-21T09:25:01Z", "level": "error", "msg": "boom"}',
    "python-logging": "2026-08-21 09:25:01,123 - WARNING - disk almost full",
    "iso-level": "2026-08-21T09:25:01Z ERROR upstream refused the connection",
    "iso": "2026-08-21T09:25:01Z upstream refused the connection",
    "raw": "a line no format recognises at all",
}


def test_every_format_name_has_a_line() -> None:
    """The fixtures cover the canonical list. Both directions."""

    assert set(FORMAT_LINES) == set(FORMAT_NAMES)


@pytest.mark.parametrize("name", sorted(FORMAT_NAMES))
def test_a_real_line_parses_to_each_name(name: str) -> None:
    """The list is of names the parser can actually produce, not of intentions."""

    assert parse_line(FORMAT_LINES[name]).format_name == name


@pytest.mark.parametrize("name", sorted(FORMAT_NAMES))
def test_every_format_has_a_label(name: str) -> None:
    """The detail pane's `Format` row, which otherwise shows a terse identifier."""

    assert FORMAT_LABELS.get(name), f"{name} has no entry in FORMAT_LABELS"


@pytest.mark.parametrize("name", sorted(FORMAT_NAMES))
def test_a_format_that_recovers_fields_has_a_profile(name: str) -> None:
    """The quiet one. No profile means DEFAULT_PROFILE: no source cell, no chips."""

    entry = parse_line(FORMAT_LINES[name])
    if not entry.fields:
        return
    assert name in FORMAT_PROFILES, (
        f"{name} recovers fields and has no FORMAT_PROFILES entry, so its rows "
        "render with no source cell and no chips"
    )


@pytest.mark.parametrize("name", sorted(FORMAT_NAMES))
def test_a_format_that_recovers_nothing_says_why(name: str) -> None:
    """`raw` is a different statement and has its own sentence in the pane."""

    entry = parse_line(FORMAT_LINES[name])
    if entry.fields or name == "raw":
        return
    assert name in NO_FIELD_REASONS, (
        f"{name} recovers no fields and NO_FIELD_REASONS does not say why"
    )


@pytest.mark.parametrize(
    "table, label",
    [
        (FORMAT_LABELS, "FORMAT_LABELS"),
        (NO_FIELD_REASONS, "NO_FIELD_REASONS"),
        (FORMAT_PROFILES, "FORMAT_PROFILES"),
    ],
)
def test_no_table_names_a_format_the_parser_cannot_produce(table, label) -> None:
    """The other direction: a renamed or deleted format leaves a dead entry."""

    orphans = sorted(set(table) - FORMAT_NAMES)
    assert not orphans, f"{label} has entries for {orphans}, not in FORMAT_NAMES"


def test_a_profile_only_names_fields_its_format_recovers() -> None:
    """A source key the format never produces is a cell that is always empty.

    The same rule `PluginRegistry.add` enforces on a plugin format's `columns`,
    checked here against the built-ins so the two cannot diverge. Asserted
    against the *union* over a line per format rather than one line, because a
    profile legitimately names keys a particular line did not carry — `msgid` is
    NILVALUE in most RFC 5424 lines and dropped by the parser.
    """

    # Every key any of these formats has been seen to produce, plus the two the
    # session layer stamps on after parsing.
    produced: dict[str, set[str]] = {}
    for name, line in FORMAT_LINES.items():
        entry = parse_line(line)
        produced.setdefault(entry.format_name, set()).update(entry.fields)
    produced.setdefault("syslog-5424", set()).add("msgid")
    # The journald provider's own keys reach the `json` profile; it is an
    # allowlist over a payload this file does not generate.
    known_json = {
        "unit", "logger", "service", "component", "tag", "name", "pid", "host",
        "status", "code", "error", "exception", "method", "path", "duration_ms",
        "latency_ms", "request_id", "trace_id",
    }
    produced.setdefault("json", set()).update(known_json)

    for name, profile in FORMAT_PROFILES.items():
        # `consumed` names keys the format spends rather than recovers, so it is
        # excluded here and checked by the chip tests instead.
        referenced = profile.keys() - profile.consumed
        unknown = sorted(referenced - produced.get(name, set()))
        assert not unknown, f"{name}'s profile names {unknown}, which it never produces"
