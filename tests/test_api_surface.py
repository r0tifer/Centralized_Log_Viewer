"""The published plugin API, frozen as literal data.

``clv/api.py`` is a promise, and a promise that can drift is not one. Every
name it publishes and the signature of every callable it publishes are written
out below as literals rather than derived from the module, so that widening the
API, narrowing it, or changing what a published callable takes is impossible
without editing this file — which puts it in the diff, where a reviewer sees it.

That is the whole mechanism. There is no cleverness to it and there should not
be: a test that computed the expected surface from the actual surface would
pass forever and mean nothing.

Each seam phase in ``PLUGIN_TODO.md`` adds names here, deliberately, as part of
its own change.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

import clv
import clv.api as api
from clv.services.parsing import parse_lines


# --- the freeze -------------------------------------------------------------

#: Exactly what ``clv.api`` publishes. Adding a name here is an API addition and
#: keeps the API version at 1.0; removing one is an API *break* and does not.
EXPECTED_API = frozenset(
    {
        "PLUGIN_API_VERSION",
        # interfaces
        "Plugin",
        "LogSourceProvider",
        "LogFormat",
        "QueryOperator",
        "ComputedField",
        "FilterStage",
        "ClusterRule",
        "ShapeContributor",
        "WatchMatcher",
        "WatchSink",
        "Exporter",
        # data handed to a plugin
        "LogEntry",
        "FilterContext",
        "FilterSpec",
        "TimeWindow",
        "ProviderSource",
        "SourceRef",
        # declaring a format
        "FormatProfile",
        "DEFAULT_PROFILE",
        "FORMAT_NAMES",
        # extending the watch rules
        "WatchRule",
        "KIND_PATTERN",
        "SINK_SAMPLE_LIMIT",
        # data a plugin hands back
        "ExportResult",
        # the severity vocabulary
        "normalize_level",
        "level_rank",
        "level_matches",
        "highest_level",
        "LEVEL_TRACE",
        "LEVEL_DEBUG",
        "LEVEL_INFO",
        "LEVEL_NOTICE",
        "LEVEL_WARN",
        "LEVEL_ERROR",
        "LEVEL_CRITICAL",
        "LEVEL_ORDER",
        "SEVERITY_BUCKETS",
        # reading a plugin's own settings
        "setting_bool",
        "setting_list",
        # the field vocabulary
        "NORMALISED_FIELD_KEYS",
        # extending the query
        "BUILTIN_OPERATORS",
        # crossing a process boundary
        "WIRE_VERSION",
        "entry_to_wire",
        "entry_from_wire",
    }
)

#: Every published callable, including the interface methods a plugin author
#: actually writes. ``str(inspect.signature(...))`` because the parameter names
#: are part of the contract too: a plugin may call ``export(..., destination=x)``
#: by keyword, so renaming that parameter is a break even if the shape holds.
EXPECTED_SIGNATURES: dict[str, str] = {
    "Plugin.describe": "(self) -> 'str'",
    "Plugin.configure": "(self, settings: 'Mapping[str, str]') -> 'None'",
    "Plugin.setup": "(self) -> 'None'",
    "Plugin.teardown": "(self) -> 'None'",
    "LogSourceProvider.discover": "(self) -> 'Iterable[Path | ProviderSource]'",
    "LogSourceProvider.open": "(self, path: 'Path') -> 'Iterator[str]'",
    "LogSourceProvider.open_reader": (
        "(self, path: 'Path', *, max_lines: 'int') -> 'Optional[Any]'"
    ),
    "LogFormat.parse": "(self, line: 'str') -> 'Optional[LogEntry]'",
    "QueryOperator.test": "(self, stored: 'str', value: 'str') -> 'bool'",
    "ComputedField.value": "(self, entry: 'LogEntry') -> 'Optional[str]'",
    "ShapeContributor.contribute": "(self, entry: 'LogEntry') -> 'str'",
    "WatchMatcher.matches": (
        "(self, entry: 'LogEntry', rule: 'WatchRule') -> 'bool'"
    ),
    "WatchMatcher.validate": "(self, pattern: 'str') -> 'Optional[str]'",
    "WatchSink.deliver": (
        "(self, name: 'str', count: 'int', context: 'FilterContext', "
        "entries: 'Sequence[LogEntry]' = ()) -> 'None'"
    ),
    "FormatProfile.keys": "(self) -> 'frozenset[str]'",
    "FilterStage.apply": (
        "(self, entry: 'LogEntry', context: 'FilterContext') -> 'Optional[LogEntry]'"
    ),
    "Exporter.export": (
        "(self, entries: 'Sequence[LogEntry]', context: 'FilterContext', *, "
        "destination: 'Optional[Path]' = None) -> 'ExportResult'"
    ),
    "TimeWindow.contains": "(self, moment: 'datetime') -> 'bool'",
    "normalize_level": "(raw: 'object') -> 'Optional[str]'",
    "level_rank": "(level: 'Optional[str]') -> 'int'",
    "level_matches": "(level: 'Optional[str]', bucket: 'str') -> 'bool'",
    "highest_level": "(levels: 'Iterable[Optional[str]]') -> 'Optional[str]'",
    "entry_to_wire": "(entry: 'LogEntry') -> 'dict[str, object]'",
    "entry_from_wire": "(payload: 'Mapping[str, object]') -> 'LogEntry'",
    "setting_bool": (
        "(settings: 'Mapping[str, str]', key: 'str', default: 'bool' = False) "
        "-> 'bool'"
    ),
    "setting_list": "(settings: 'Mapping[str, str]', key: 'str') -> 'list[str]'",
}

#: Attributes every plugin class carries, with their defaults. These are the
#: declarations an author writes, so they are as much the contract as the
#: methods are.
EXPECTED_PLUGIN_DEFAULTS: dict[str, object] = {
    "name": "unnamed plugin",
    "requires_clv": None,
    "requires_api": None,
    "priority": 100,
}


def test_the_published_names_are_exactly_the_frozen_list() -> None:
    """Both directions. One of them catches an addition nobody meant to make."""

    published = set(api.__all__)
    assert published - EXPECTED_API == set(), "clv.api publishes a name not in the freeze"
    assert EXPECTED_API - published == set(), "clv.api lost a name it had promised"


def test_every_published_name_actually_resolves() -> None:
    """``__all__`` is a claim about the module, not a wish list."""

    missing = [name for name in api.__all__ if not hasattr(api, name)]
    assert not missing, f"clv.api.__all__ names things it does not define: {missing}"


def test_the_all_list_has_no_duplicates() -> None:
    assert len(api.__all__) == len(set(api.__all__))


@pytest.mark.parametrize(("dotted", "expected"), sorted(EXPECTED_SIGNATURES.items()))
def test_a_published_callable_keeps_its_signature(dotted: str, expected: str) -> None:
    target: object = api
    for part in dotted.split("."):
        target = getattr(target, part)
    assert str(inspect.signature(target)) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("attribute", "default"), sorted(EXPECTED_PLUGIN_DEFAULTS.items())
)
def test_plugin_metadata_keeps_its_defaults(attribute: str, default: object) -> None:
    assert getattr(api.Plugin, attribute) == default


def test_a_cluster_rule_declares_a_pattern_and_a_placeholder() -> None:
    """The one published interface whose contract is data rather than a method.

    ``ClusterRule`` has nothing in ``EXPECTED_SIGNATURES`` because it has no
    method to implement — CLV performs the substitution — so these two names
    and their defaults *are* the frozen surface, and this is where a rename
    would have to be seen in the diff.
    """

    assert api.ClusterRule.pattern == ""
    assert api.ClusterRule.placeholder == ""


def test_an_exporter_declares_whether_it_wants_a_destination() -> None:
    """And the default is the behaviour every existing exporter already had."""

    assert api.Exporter.wants_path is False
    assert api.Exporter.suggested_extension == ""


# --- the two versions are two versions --------------------------------------


def test_the_api_version_is_one_point_zero() -> None:
    assert api.PLUGIN_API_VERSION == "1.0"


def test_the_api_version_is_not_clv_s_version() -> None:
    """The separation, asserted rather than assumed.

    If these two ever agree it will be by coincidence, and a later edit that
    "simplified" one into the other would take the whole point of `requires_api`
    with it: a plugin should not have to re-release because CLV shipped a
    release that changed nothing the plugin can see.
    """

    assert api.PLUGIN_API_VERSION != clv.__version__


# --- re-export, not wrapper -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "module_path", "attribute"),
    [
        ("LogEntry", "clv.services.parsing", "LogEntry"),
        ("FilterSpec", "clv.services.filtering", "FilterSpec"),
        ("TimeWindow", "clv.services.filtering", "TimeWindow"),
        ("FilterStage", "clv.plugins", "FilterStage"),
        ("Exporter", "clv.plugins", "Exporter"),
        ("LogSourceProvider", "clv.plugins", "LogSourceProvider"),
        ("ProviderSource", "clv.plugins", "ProviderSource"),
        ("SourceRef", "clv.services.refs", "SourceRef"),
        ("NORMALISED_FIELD_KEYS", "clv.services.query", "NORMALISED_FIELD_KEYS"),
    ],
)
def test_a_published_type_is_the_real_one(
    name: str, module_path: str, attribute: str
) -> None:
    """Identity, not equality.

    ``clv.api`` re-exports rather than wraps, because a conversion per entry per
    render is the one cost CLV cannot pay. A DTO layer sneaking in later would
    still pass an equality test and would still be the thing this module was
    written to refuse.
    """

    module = __import__(module_path, fromlist=[attribute])
    assert getattr(api, name) is getattr(module, attribute)


def test_clv_plugins_still_exports_what_it_always_did() -> None:
    """`clv.api` is the recommended path, not a forced migration."""

    import clv.plugins as plugins

    for name in ("Plugin", "LogSourceProvider", "FilterStage", "Exporter",
                 "FilterContext", "ExportResult", "ProviderSource"):
        assert getattr(plugins, name) is getattr(api, name)


# --- the API is usable without a screen -------------------------------------

_PURITY_PROBE = """
import sys
import clv.api  # noqa: F401

leaked = sorted(
    name
    for name in sys.modules
    if name == "clv.app"
    or name.split(".")[0] in {"textual", "rich"}
    or name.startswith("clv.widgets")
)
print(",".join(leaked))
"""


def test_importing_the_api_pulls_in_no_ui() -> None:
    """A plugin's own unit tests must not need a Textual screen to run.

    Out of process on purpose. In-process this would pass whenever some earlier
    test in the session had already imported a widget, which is every session —
    the assertion would be about ``sys.modules`` rather than about ``clv.api``,
    and it would go on passing long after the thing it guards had broken.
    """

    result = subprocess.run(
        [sys.executable, "-c", _PURITY_PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(",") if name]
    assert not leaked, (
        "importing clv.api dragged in the UI layer: " + ", ".join(leaked)
    )


# --- the wire form ----------------------------------------------------------

#: One line per built-in format, so a format whose entry cannot survive the
#: round trip fails by name rather than in a heap.
FORMAT_LINES: dict[str, str] = {
    "syslog": "Aug 21 09:25:01 web01 sshd[1234]: Failed password for root",
    "syslog-5424": (
        "<34>1 2026-08-21T09:25:01.000003Z web01 sshd 1234 ID47 - Failed password"
    ),
    "access-log": (
        '10.0.0.5 - deploy [21/Aug/2026:09:25:01 +0000] '
        '"POST /pay HTTP/1.1" 500 1234'
    ),
    "json": (
        '{"timestamp": "2026-08-21T09:25:01", "level": "warn", '
        '"message": "disk filling", "unit": "backup.service", "pid": "77"}'
    ),
    "iso-level": "2026-08-21 09:25:01,123 ERROR something broke",
    "iso": "2026-08-21T09:25:01 something happened",
    "raw": "no format on earth recognises this line",
}


def _round_trip(entry):
    """Encode, assert JSON-safety, decode, and check what `==` cannot."""

    payload = api.entry_to_wire(entry)
    json.dumps(payload)  # raises if anything on the wire is not a JSON scalar
    restored = api.entry_from_wire(payload)

    assert restored == entry
    # `LogEntry.fields` is `compare=False`, so the assertion above passes even
    # if the fields were dropped entirely. This is the one that would fail.
    assert dict(restored.fields) == dict(entry.fields)
    with pytest.raises(TypeError):
        restored.fields["injected"] = "x"  # type: ignore[index]
    return restored


@pytest.mark.parametrize(("format_name", "line"), sorted(FORMAT_LINES.items()))
def test_an_entry_from_every_builtin_format_survives_the_wire(
    format_name: str, line: str
) -> None:
    entry = parse_lines([line])[0]
    assert entry.format_name == format_name, (
        f"the fixture for {format_name} now parses as {entry.format_name}"
    )
    _round_trip(entry)


def test_a_continuation_entry_survives_with_its_inherited_stamp() -> None:
    """Carry-forward is state the wire form has to carry, not recompute."""

    entries = parse_lines(
        ["2026-08-21 09:25:01 ERROR boom", "    at frobnicate(Thing.java:42)"]
    )
    assert entries[1].continuation
    restored = _round_trip(entries[1])
    assert restored.continuation
    assert restored.timestamp == entries[0].timestamp
    assert restored.level == entries[0].level


def test_an_entry_with_no_fields_comes_back_on_the_shared_mapping() -> None:
    """The common case must not start allocating a mapping per decoded entry."""

    entry = parse_lines(["a line with no structure at all"])[0]
    restored = _round_trip(entry)
    assert restored.fields is parse_lines(["another such line"])[0].fields


def test_a_non_ascii_entry_survives() -> None:
    entry = parse_lines(["2026-08-21 09:25:01 ERROR café — naïve 日本語 🔥"])[0]
    assert "café" in _round_trip(entry).raw


def test_an_entry_with_an_embedded_nul_survives() -> None:
    """A NUL is a byte a log file genuinely contains, and JSON escapes it."""

    entry = parse_lines(["2026-08-21 09:25:01 ERROR before\x00after"])[0]
    assert "\x00" in _round_trip(entry).raw


def test_an_aware_timestamp_keeps_its_offset_and_microseconds() -> None:
    entry = parse_lines(["2026-08-21T09:25:01.123456+02:00 ERROR boom"])[0]
    restored = _round_trip(entry)
    assert restored.timestamp is not None
    assert restored.timestamp.utcoffset() == timedelta(hours=2)
    assert restored.timestamp.microsecond == 123456


def test_a_naive_timestamp_stays_naive() -> None:
    """Not "becomes UTC". A stamp with no zone is a fact about the log."""

    entry = parse_lines(["2026-08-21 09:25:01 ERROR boom"])[0]
    restored = _round_trip(entry)
    assert restored.timestamp is not None
    assert restored.timestamp.tzinfo is None


def test_the_wire_form_is_json_safe_by_construction() -> None:
    """Every value, not just the ones the fixtures happened to produce."""

    entry = parse_lines(["Aug 21 09:25:01 web01 sshd[1234]: hello"])[0]
    payload = api.entry_to_wire(entry)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["v"] == api.WIRE_VERSION
    assert isinstance(payload["fields"], dict)
    assert all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload["fields"].items()
    )


def test_an_unknown_wire_version_is_refused_whole() -> None:
    """Named, and refused before a single field is read.

    A payload from a future CLV that decoded into a plausible-looking entry
    would be worse than one that raised: the entry would be wrong in ways
    nothing downstream could detect.
    """

    entry = parse_lines(["2026-08-21 09:25:01 ERROR boom"])[0]
    payload = dict(api.entry_to_wire(entry))
    payload["v"] = 99

    with pytest.raises(ValueError) as caught:
        api.entry_from_wire(payload)
    assert "99" in str(caught.value)
    assert str(api.WIRE_VERSION) in str(caught.value)


def test_a_payload_with_no_version_is_refused() -> None:
    with pytest.raises(ValueError):
        api.entry_from_wire({"raw": "looks like an entry, carries no version"})


def test_the_encoder_stamps_the_current_version() -> None:
    """So a payload can never be produced that the decoder would refuse."""

    entry = parse_lines(["2026-08-21 09:25:01 ERROR boom"])[0]
    assert api.entry_to_wire(entry)["v"] == api.WIRE_VERSION


def test_a_hand_built_entry_round_trips() -> None:
    """Not everything on the wire came out of the parser.

    A `LogFormat` plugin builds entries itself, and Phase 13 moves those across
    the same boundary.
    """

    entry = api.LogEntry(
        raw="hand built",
        timestamp=datetime(2026, 8, 21, 9, 25, 1, tzinfo=timezone.utc),
        level=api.LEVEL_CRITICAL,
        message="hand built",
        format_name="nginx-error",
        fields=MappingProxyType({"upstream": "10.0.0.9", "status": "502"}),
    )
    restored = _round_trip(entry)
    assert restored.format_name == "nginx-error"
    assert restored.fields["upstream"] == "10.0.0.9"
