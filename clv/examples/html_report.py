"""The filtered view as a standalone HTML report, as a CLV exporter.

A worked ``Exporter`` example. Drop this file in ``~/.config/clv/plugins/``
(CLV puts it in ``examples/`` for you) and add it to ``settings.conf``::

    [log_viewer]
    plugins = html_report

    [plugin:html_report]
    # Optional. Runs the export in a process CLV can kill; see "Isolation" below.
    isolated = true

``Ctrl+E`` then offers **HTML report** beside JSON Lines, CSV and plain text.
It writes one self-contained file — no stylesheet, no script, no font, nothing
fetched when it is opened — carrying the query that produced it, the severity of
every row, and the fields CLV recovered. It is the thing to attach to a ticket
when "here is the log" means "here is the log as I was reading it".

Six things are worth copying out of this file.

**Ask for the destination, or you cannot honour the one the operator chose.**
``wants_path = True`` enables the export dialog's path input and passes the
result as ``destination``; ``suggested_extension`` is what the dialog fills in.
An exporter that leaves ``wants_path`` at ``False`` is the self-routing kind — a
syslog forwarder, a fixed report path — and is called with no path at all. The
two are different products and the flag is how CLV tells them apart.

**Escape everything, including what a format already parsed.** A log line is
attacker-controlled text far more often than it is not: anything that reaches a
web server's error log was typed by whoever was talking to it. ``html.escape``
runs over every cell, and over the query, and over the source name. A report
that renders ``<script>`` from a log line is a report that turned reading a log
into running someone else's code.

**Write atomically, because an export is a file someone else will open.**
CLV's own exporters write through a sibling temp file and ``os.replace``; that
helper is internal, so this does it in six lines. The failure it prevents is a
half-written report that looks complete — which, for a file whose whole job is
being sent to somebody, is worse than no file.

**Report what you did; do not raise for an ordinary failure.** A permission
error on the chosen path is the operator's to fix, and ``ExportResult(ok=False,
detail=...)`` puts the reason in front of them. Raising would work too, and CLV
would disable this exporter for the session over a typo in a filename.

**Isolation is available here, and only because of what kind this is.**
``Exporter`` is one of the four kinds CLV will run in a subprocess — the ones it
calls and waits for an answer from. A stage or a format cannot ask, because a
round trip per line is not a slower program but a different one. Declaring
``isolated`` on the class asks for it; the operator writing it in the section
above insists, and additionally keeps this module out of CLV's own process
entirely. Neither makes this plugin *safe* — the child runs as the operator —
and both make it killable.

**Bound what you build.** Every entry in the filtered set is turned into a row,
in memory, before anything is written. That is fine for a view and not fine for
an unbounded one, so ``MAX_ROWS`` caps it and the report says on its face that it
was capped. A truncation the reader cannot see is the same defect as a dropped
line.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from clv.api import Exporter, ExportResult, FilterContext, LogEntry

#: Rows written before the report truncates and says so. Generous, because an
#: export is deliberate and infrequent; finite, because "the whole filtered set"
#: is whatever the operator's buffer holds and this builds it all in memory.
MAX_ROWS = 20_000

#: Severity to a colour, using the level vocabulary CLV normalises onto rather
#: than whatever the log happened to spell. ``normalize_level`` is published for
#: exactly this reason — an exporter inventing its own mapping is an exporter
#: that disagrees with the pane about what the operator filtered for.
_COLOURS: Mapping[str, str] = {
    "critical": "#b3002d",
    "error": "#d13438",
    "warn": "#c08a00",
    "notice": "#0a7f8c",
    "info": "#3a7bd5",
    "debug": "#7a7a7a",
    "trace": "#9a9a9a",
}

_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
       margin: 2rem; background: #fff; color: #111; }
h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
p.meta { color: #666; margin: 0 0 1.5rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .25rem .6rem; vertical-align: top;
         border-bottom: 1px solid #e5e5e5; }
th { position: sticky; top: 0; background: #fafafa; }
td.level { white-space: nowrap; font-weight: 600; }
td.time { white-space: nowrap; color: #555; }
td.msg { white-space: pre-wrap; word-break: break-word; }
td.fields { color: #666; font-size: .9em; }
p.truncated { margin-top: 1.5rem; color: #b3002d; font-weight: 600; }
@media (prefers-color-scheme: dark) {
  body { background: #101010; color: #e8e8e8; }
  th { background: #1b1b1b; }
  th, td { border-bottom-color: #2a2a2a; }
  p.meta, td.time, td.fields { color: #9a9a9a; }
}
"""


class HtmlReport(Exporter):
    """Writes the filtered entries as one self-contained HTML file."""

    #: **What ``Ctrl+E`` lists, and it is ``name`` rather than ``describe()``.**
    #: The dialog builds its label from this attribute directly, so overriding
    #: ``describe()`` -- which is what the base class suggests and what this
    #: file did first -- changes the ``P`` dialog and nothing else. Worth
    #: knowing before naming an exporter something only a developer would read.
    name = "html-report"
    requires_api = ">=1.0,<2.0"

    wants_path = True
    suggested_extension = ".html"

    # No `configure()`. The `[plugin:html_report]` section in this module's
    # docstring carries `isolated`, which **CLV** reads when it decides whether
    # to start a host -- not this class. A plugin only implements the hook when
    # it has settings of its own to act on, and an empty one is a hook that has
    # to be read to find out it does nothing.

    def export(
        self,
        entries: Sequence[LogEntry],
        context: FilterContext,
        *,
        destination: Optional[Path] = None,
    ) -> ExportResult:
        if destination is None:
            # Unreachable while `wants_path` is True -- kept because an
            # exporter is called by a dialog, and a dialog is a thing that gets
            # rewritten. Reporting beats an AttributeError three phases later.
            return ExportResult(
                ok=False, detail="This exporter needs a destination path."
            )

        shown = entries[:MAX_ROWS]
        document = self._render(shown, context, truncated=len(entries) - len(shown))
        try:
            written = _write_atomically(destination, document)
        except OSError as exc:
            return ExportResult(ok=False, detail=f"{destination}: {exc.strerror or exc}")

        rows = "row" if len(shown) == 1 else "rows"
        detail = f"{len(shown)} {rows}, {written} bytes"
        if len(entries) > len(shown):
            detail += f" ({len(entries) - len(shown)} truncated)"
        return ExportResult(ok=True, detail=detail, destination=destination)

    # --- rendering -----------------------------------------------------------

    def _render(
        self,
        entries: Sequence[LogEntry],
        context: FilterContext,
        *,
        truncated: int,
    ) -> str:
        spec = getattr(context, "spec", None)
        query = getattr(spec, "query", "") or "(no query)"
        severity = getattr(spec, "severity", "all")
        source = getattr(context, "source", None)
        stamped = datetime.now(timezone.utc).isoformat(timespec="seconds")

        head = [
            "<!DOCTYPE html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>CLV log report</title>",
            f"<style>{_CSS}</style>",
            "</head><body>",
            "<h1>CLV log report</h1>",
            '<p class="meta">'
            f"source: {_esc(source)} &middot; "
            f"query: {_esc(query)} &middot; "
            f"severity: {_esc(severity)} &middot; "
            f"exported: {_esc(stamped)}</p>",
            "<table><thead><tr>",
            "<th>Time</th><th>Level</th><th>Message</th><th>Fields</th>",
            "</tr></thead><tbody>",
        ]
        body = [self._row(entry) for entry in entries]
        tail = ["</tbody></table>"]
        if truncated > 0:
            tail.append(
                f'<p class="truncated">{truncated} further '
                f"{'row' if truncated == 1 else 'rows'} were not written: this "
                f"report is capped at {MAX_ROWS:,}.</p>"
            )
        tail.append("</body></html>")
        return "\n".join(head + body + tail) + "\n"

    def _row(self, entry: LogEntry) -> str:
        level = (entry.level or "").casefold()
        colour = _COLOURS.get(level, "inherit")
        stamp = entry.timestamp.isoformat(sep=" ") if entry.timestamp else ""
        # `message` when a format recovered one, `raw` when none did. Never
        # both, and never neither: a line CLV could not parse is still a line
        # and still belongs in the report.
        text = entry.message or entry.raw
        fields = ", ".join(f"{key}={value}" for key, value in entry.fields.items())
        return (
            "<tr>"
            f'<td class="time">{_esc(stamp)}</td>'
            f'<td class="level" style="color:{colour}">{_esc(entry.level or "")}</td>'
            f'<td class="msg">{_esc(text)}</td>'
            f'<td class="fields">{_esc(fields)}</td>'
            "</tr>"
        )


def _esc(value: object) -> str:
    """``html.escape`` over anything, including ``None`` and a ``SourceRef``.

    Quotes included (``quote=True`` is the default), because several of these
    land inside an attribute. Escaping by hand at each call site is how one of
    them eventually does not get escaped.
    """

    return html.escape(str(value)) if value is not None else ""


def _write_atomically(path: Path, text: str) -> int:
    """Write *text* through a sibling temp file, then rename it into place.

    A sibling rather than ``/tmp`` because ``os.replace`` is only atomic within
    one filesystem, and the operator's chosen path may be on any of them. The
    temp file is removed on failure so a refused export leaves the directory as
    it found it.
    """

    payload = text.encode("utf-8")
    temp = path.with_name(f".{path.name}.clv-partial")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return len(payload)


def register() -> list[Exporter]:
    return [HtmlReport()]
