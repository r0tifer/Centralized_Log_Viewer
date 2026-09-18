"""What a log format declares about itself, apart from how it parses.

One dataclass, and it is here rather than in :mod:`clv.widgets.columns` -- where
it was born and where it is still used -- for a reason that is a test rather
than a preference. ``tests/test_api_surface.py`` asserts out of process that
importing :mod:`clv.api` pulls in no Textual widget and no Rich renderable, so
that a plugin author's own unit tests need no screen. ``columns.py`` imports
Rich. :class:`FormatProfile` is part of the ``LogFormat`` contract and therefore
has to be published, so it had to move somewhere Rich-free; ``columns.py``
re-exports it, and every existing import site is unaffected.

The label and no-field tables came from ``detail_pane.py`` on the same
argument, and one that is sharper: ``columns.format_label`` needs
``FORMAT_LABELS``, ``detail_pane`` imports Textual, and ``columns.py``'s own
docstring is explicit that nothing in it imports Textual. A format's *name for
an operator* is not a widget's business any more than its column profile is.
Both modules re-export what they used to define.

Nothing here carries rendering logic and nothing ever will. It is a *declaration* -- which of
a format's fields earns the source cell, which earns a chip, and which were
already spent on a cell and must not come back as one. What the renderer does
with that answer stays in ``columns.py``, which is the module that knows how
wide a cell is.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FormatProfile:
    """Which of a format's fields are worth a cell, and which a chip."""

    #: Candidates for the source cell, best answer first.
    source_keys: tuple[str, ...] = ()
    #: Appended to the source cell as ``tag[pid]`` when there is room for it.
    pid_key: str = ""
    #: Always shown when present. For a chip to be pinned it has to carry
    #: something the message cell cannot.
    pinned_chips: tuple[str, ...] = ()
    #: Shown only when the value actually varies across the rendered set.
    chips: tuple[str, ...] = ()
    #: Keys the format already spent on the time, level or message cell. A chip
    #: repeating one of them repeats the cell beside it.
    #:
    #: This was ``columns._JSON_CONSUMED``, reached through
    #: ``if entry.format_name == "json"``. Generalising it is not tidying: a
    #: format whose keys are the *writer's* choice rather than a regex's group
    #: names is exactly the case that needs it, and every plugin format is one.
    consumed: frozenset[str] = frozenset()

    def keys(self) -> frozenset[str]:
        """Every field name this profile refers to.

        What ``PluginRegistry.add`` checks against a format's ``field_names``:
        a profile naming a field the format never produces is a row that
        quietly loses its source cell, and it is checkable at load.
        """

        names = set(self.source_keys)
        names.update(self.pinned_chips)
        names.update(self.chips)
        names.update(self.consumed)
        if self.pid_key:
            names.add(self.pid_key)
        return frozenset(names)


#: What each format is called in the property list and, when a merge puts two
#: on screen, nowhere else. The parser's own names are terse identifiers; these
#: are what an operator would call them.
FORMAT_LABELS: dict[str, str] = {
    "syslog": "BSD syslog",
    "syslog-5424": "RFC 5424 syslog",
    "access-log": "Common Log Format",
    "json": "JSON",
    "python-logging": "Python logging",
    "iso-level": "ISO timestamp with level",
    "iso": "ISO timestamp",
    "raw": "unrecognised",
}

#: Why a matched line still has no fields. Keyed by format name; the fallback
#: covers the raw case, which is a different statement entirely.
NO_FIELD_REASONS: dict[str, str] = {
    "python-logging": (
        "The Python logging format carries a timestamp, a level and a message "
        "and nothing else to name."
    ),
    "iso-level": (
        "This line is a timestamp, a level and a message; there is no further "
        "structure in it to recover."
    ),
    "iso": (
        "This line is a timestamp and a message; there is no further structure "
        "in it to recover."
    ),
}


#: What a format with nothing to declare gets. Reaching it by *accident* -- a
#: format that registered a parser and no profile -- is the failure this type
#: exists to make visible, so the two lookup sites in ``columns.py`` fall back
#: to it last and the documentation says what it costs: a row with no source
#: cell and no chips.
DEFAULT_PROFILE = FormatProfile()


__all__ = [
    "DEFAULT_PROFILE",
    "FORMAT_LABELS",
    "NO_FIELD_REASONS",
    "FormatProfile",
]
