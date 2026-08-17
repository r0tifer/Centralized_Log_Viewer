"""Every persisted field survives a restart — checked against the schema itself.

``SessionState.from_dict`` and ``SavedView.from_dict`` validate a stored value
by comparing the **text** of the field's annotation against a literal. That
works, and it is the kind of thing that keeps working right up until someone
writes a more accurate type. Rename ``tuple[str, ...]`` and no branch matches:
the raw JSON list is assigned through unvalidated, every tuple comparison
downstream fails, and there is no schema version anywhere to notice. The
operator's stars and views are simply gone on the next launch.

Round trips for individual fields already exist, next to the features that own
them — ``starred`` in ``test_sources``, ``merged`` in ``test_merged_view``,
``views`` in ``test_saved_views``, ``watch_rules`` in ``test_watch_rules``. What
none of them can do is notice a field added *tomorrow* whose annotation the
dispatcher does not handle. This file is driven off ``PERSISTED_FIELDS`` rather
than a hand-written list, so a new field with an unhandled type fails here
before it can reach anyone's disk.

The assertions are behavioural on purpose: they check that the *value survives*,
not that the dispatcher is written any particular way. Rewriting ``from_dict``
to use real types instead of annotation strings would be an improvement, and
should leave this file untouched and passing.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from clv.services.watch import WatchRule
from clv.storage import SavedView, SessionState, StateStore


#: One non-default value per annotation the schema uses. A field whose
#: annotation is missing here fails `test_every_persisted_annotation_is_known`,
#: which is the point: an unrecognised annotation is exactly what `from_dict`
#: would wave through unvalidated.
_SAMPLES: dict[str, object] = {
    "str": "round-trip-marker",
    "int": 4242,
    "tuple[str, ...]": ("ssh:web01/var/log/syslog", "journal:all"),
    "tuple[SavedView, ...]": (
        SavedView(name="a view", query="level:error", source="ssh:web01/var/log/syslog"),
    ),
    "tuple[WatchRule, ...]": (WatchRule(name="a rule", pattern="boom"),),
}


def _persisted_fields() -> dict[str, tuple[str, object]]:
    """``{name: (annotation, default)}`` for everything written to disk."""

    declared = {field.name: field for field in fields(SessionState)}
    return {
        name: (str(declared[name].type), declared[name].default)
        for name in SessionState.PERSISTED_FIELDS
    }


def _distinct_value(annotation: str, default: object) -> object:
    """A value that is *not* the default, so a dropped field cannot pass."""

    if annotation == "bool":
        return not default
    sample = _SAMPLES[annotation]
    assert sample != default, f"sample for {annotation} equals the field default"
    return sample


def test_every_persisted_annotation_is_known() -> None:
    """A new field type has to be considered here before it can ship.

    Failing this means `from_dict` almost certainly does not handle the new
    annotation either — its branches and this table cover the same set — so the
    field would be written to disk and silently discarded on load.
    """

    unknown = sorted(
        f"{name}: {annotation}"
        for name, (annotation, _) in _persisted_fields().items()
        if annotation != "bool" and annotation not in _SAMPLES
    )
    assert not unknown, (
        "These persisted fields use an annotation this suite has no sample for. "
        "Add one here *and* confirm SessionState.from_dict has a branch for it — "
        "an unhandled annotation is assigned through unvalidated and there is no "
        "schema version to catch it. Found:\n  " + "\n  ".join(unknown)
    )


@pytest.mark.parametrize("name", SessionState.PERSISTED_FIELDS)
def test_each_persisted_field_survives_a_restart(name: str, tmp_path: Path) -> None:
    """One field at a time, so a failure names the field that broke."""

    annotation, default = _persisted_fields()[name]
    value = _distinct_value(annotation, default)

    StateStore(root=tmp_path).save(SessionState(**{name: value}))
    restored = StateStore(root=tmp_path).load()

    assert getattr(restored, name) == value, (
        f"{name} ({annotation}) did not survive save/load. If its annotation was "
        "just changed, that is the cause: from_dict dispatches on the annotation "
        "text and silently passes an unrecognised one through."
    )


def test_all_persisted_fields_survive_together(tmp_path: Path) -> None:
    """The individual tests could all pass while the set interfered."""

    populated = {
        name: _distinct_value(annotation, default)
        for name, (annotation, default) in _persisted_fields().items()
    }

    StateStore(root=tmp_path).save(SessionState(**populated))
    restored = StateStore(root=tmp_path).load()

    assert {name: getattr(restored, name) for name in populated} == populated


def test_a_saved_view_survives_a_restart_field_by_field(tmp_path: Path) -> None:
    """`SavedView.from_dict` is a second, separately-coded copy of the rule.

    Two implementations of one convention, in one file, with no shared helper
    between them — so a fix applied to one is not applied to the other.
    """

    declared = {field.name: field for field in fields(SavedView)}
    view = SavedView(
        **{
            name: ("a view" if name == "name" else _distinct_value(str(f.type), f.default))
            for name, f in declared.items()
        }
    )

    StateStore(root=tmp_path).save(SessionState(views=(view,)))
    restored = StateStore(root=tmp_path).load()

    assert restored.views == (view,)
    for name in declared:
        assert getattr(restored.views[0], name) == getattr(view, name), name
