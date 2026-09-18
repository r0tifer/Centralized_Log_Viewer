"""Full properties of the selected log line.

The Event Viewer interaction this exists for is *select an event, see its full
properties*. The log pane can only ever show one line's worth of text; the
parser recovered rather more than that — a canonical severity, a normalised
timestamp, which format matched, and whatever `fields` the matcher captured —
and until now all of it was thrown away at render time.

Empty is never blank
--------------------

Four of the parser's formats (`python-logging`, `iso-level`, `iso`, and `raw`)
carry no fields at all, so "no properties" is the *common* case, not an edge
one. A blank property list would read as a bug. Each case says which it is, in
the same voice `describe_empty_result` uses for an empty log pane: what is
missing, and why.

Layout is CSS
-------------

The pane sits beside the log when there is width for it, below it when there is
not, and takes the whole viewer at the compact breakpoint. All three are decided
by the ``-wide`` / ``-narrow`` / ``-compact`` class the app mirrors onto this
widget — ``DEFAULT_CSS`` is scoped to the widget subtree, so a selector rooted
at the app would never match from here.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..services.formats import FORMAT_LABELS, NO_FIELD_REASONS
from ..services.parsing import LogEntry
from .columns import format_label

# `FORMAT_LABELS` and `NO_FIELD_REASONS` are re-exported from
# `clv.services.formats`, which is where they live now. They moved because
# `columns.format_label` needs the first of them and `columns.py` may not import
# Textual -- and because what a format is *called* is a declaration about the
# format, not about this widget. Every existing import site is unaffected.

NO_FORMAT_REASON = (
    "No format matched this line, so only its raw text is available. "
    "It is still searchable."
)

CONTINUATION_NOTE = (
    "Continuation line: the timestamp and level above were inherited. Fields "
    "are not inherited — a stack trace frame has no host or PID of its own to "
    "report."
)

EMPTY_MESSAGE = "No line selected. Move the cursor in the log pane and press Enter."


class DetailPane(VerticalScroll):
    """Property list for the entry under the log cursor."""

    DEFAULT_CSS = """
    DetailPane {
        display: none;
        background: $surface 6%;
        border: solid $surface 20%;
        padding: 0 1;
        scrollbar-gutter: stable;
    }

    DetailPane.-visible { display: block; }

    /* Beside the log pane: #log-area switches to a horizontal layout at -wide,
       so a width is what decides the split. Fixed rather than a fraction so a
       very wide terminal gives the extra room to the log, which is the pane
       being read. */
    DetailPane.-wide {
        width: 46;
        min-width: 30;
        height: 1fr;
    }

    /* Stacked below: #log-area is vertical here, so a height decides it. */
    DetailPane.-narrow {
        width: 1fr;
        height: 40%;
        min-height: 6;
    }

    /* The panes cannot share 80 columns, so the detail pane takes the viewer
       and the log is hidden behind it (app CSS). */
    DetailPane.-compact {
        width: 1fr;
        height: 1fr;
    }

    DetailPane #detail-title {
        text-style: bold;
        height: 1;
    }

    DetailPane #detail-raw {
        height: auto;
        padding: 0 0 1 0;
        color: #dce3f7;
    }

    DetailPane #detail-properties { height: auto; }

    DetailPane #detail-note {
        height: auto;
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._entry: LogEntry | None = None

    def compose(self) -> ComposeResult:
        yield Static("Event detail", id="detail-title")
        yield Static("", id="detail-raw")
        yield Static("", id="detail-properties")
        yield Static("", id="detail-note")

    def on_mount(self) -> None:
        self._refresh_content()

    @property
    def entry(self) -> LogEntry | None:
        return self._entry

    def show(self, entry: LogEntry | None) -> None:
        """Render *entry*, or the "nothing selected" state when it is None."""

        self._entry = entry
        if self.is_mounted:
            self._refresh_content()

    # --- rendering ----------------------------------------------------------

    def _refresh_content(self) -> None:
        raw = self.query_one("#detail-raw", Static)
        properties = self.query_one("#detail-properties", Static)
        note = self.query_one("#detail-note", Static)

        entry = self._entry
        if entry is None:
            raw.update(Text(EMPTY_MESSAGE, style="dim"))
            properties.update("")
            note.update("")
            return

        # Text(), never markup: a log line is not ours to interpret, and a
        # stray "[" in a payload must not be read as a style tag.
        raw.update(Text(entry.raw))
        properties.update(self._property_table(entry))
        note.update(Text(self._note(entry), style="dim") if self._note(entry) else "")

    def _property_table(self, entry: LogEntry) -> Table:
        table = Table(box=None, show_header=False, show_edge=False, pad_edge=False)
        table.add_column("name", style="#94a3b8", overflow="fold", no_wrap=False)
        table.add_column("value", overflow="fold")

        table.add_row("Timestamp", entry.timestamp.isoformat() if entry.timestamp else "—")
        table.add_row("Level", entry.level or "—")
        # Through `columns` so a plugin format's own `label` is honoured; it
        # falls back to FORMAT_LABELS and then to the bare identifier.
        table.add_row("Format", format_label(entry.format_name))
        table.add_row("Continuation", "yes" if entry.continuation else "no")

        # dict(): fields is a read-only mappingproxy.
        for key, value in sorted(dict(entry.fields).items()):
            table.add_row(key, value)
        return table

    @staticmethod
    def _note(entry: LogEntry) -> str:
        """The sentence under the properties, or "" when there is nothing to say."""

        parts: list[str] = []
        if not entry.fields:
            if entry.format_name == "raw":
                parts.append(NO_FORMAT_REASON)
            else:
                parts.append(
                    NO_FIELD_REASONS.get(
                        entry.format_name,
                        "This line matched a format that recovers no named fields.",
                    )
                )
        if entry.continuation:
            parts.append(CONTINUATION_NOTE)
        return " ".join(parts)


__all__ = [
    "CONTINUATION_NOTE",
    "EMPTY_MESSAGE",
    "FORMAT_LABELS",
    "NO_FIELD_REASONS",
    "NO_FORMAT_REASON",
    "DetailPane",
]
