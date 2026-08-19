"""Writing the filtered view out to a file.

Three built-in formats — JSON Lines, CSV and plain text — with one thing in
common: what they write is what the pane is showing, in the order it is showing
it. Nothing here decides *which* entries are exported; the caller hands over the
already-filtered list.

These are core rather than drop-in plugins under ``clv/plugins/exporters/`` for
two reasons: a built-in must not be able to fail to load the way third-party
code can, and the Advanced drawer's plugin count should keep meaning "plugins
someone installed". The :class:`~clv.plugins.Exporter` interface is still the
extension point, and the export dialog lists these three above whatever it
supplies.

This module deliberately does **not** import ``clv.plugins``. ``clv.plugins``
already imports ``clv.services``; importing back would couple the two layers in
both directions for no gain, since a writer needs nothing from the plugin
interfaces.

It does import ``session`` — for ``NODE_FIELD`` alone, and in the direction the
layers already run: ``session`` produces the entries a writer consumes and
imports nothing from here. Spelling ``"node"`` as a literal instead would put a
third copy of the key beside ``session`` and ``query``, which is one more than
can be renamed in a single edit.

Privacy note: :func:`write_atomically` is the only place in CLV that writes log
content to a path, and the temporary file it writes through sits **beside the
destination** the operator named — never in a cache or a temp directory. It is
removed whether the write succeeds or fails.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence, TextIO

from .parsing import LogEntry
from .refs import SourceRef, stem_of
from .session import NODE_FIELD

#: Signature every writer shares: consume entries, write them to an open text
#: handle. Bounded by the caller's list — no writer reads a file or a stream.
Writer = Callable[[Sequence[LogEntry], TextIO], None]

#: Columns of the CSV export. Fixed and rectangular on purpose: ``fields`` is a
#: different shape on every line, so it travels as one JSON column rather than
#: turning the table ragged (or forcing a pre-pass to union every key).
#:
#: ``node`` is promoted out of that blob and given a column of its own, because
#: it is the one key there that is *not* a different shape on every line — it is
#: one value per source, like ``level`` and ``format`` above it. A merge across
#: five machines whose only record of which machine a row came from was a JSON
#: string inside a cell would not be a table anyone could sort. It is empty for
#: a local source, which has no machine to name.
CSV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "level",
    "format",
    "continuation",
    "message",
    "raw",
    "node",
    "fields",
)


def _timestamp_text(entry: LogEntry) -> str:
    return entry.timestamp.isoformat() if entry.timestamp is not None else ""


def write_jsonl(entries: Sequence[LogEntry], handle: TextIO) -> None:
    """One JSON object per line, carrying the whole entry including ``fields``."""

    for entry in entries:
        payload = {
            "raw": entry.raw,
            "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
            "level": entry.level,
            "message": entry.message,
            "format": entry.format_name,
            "continuation": entry.continuation,
            # dict(): fields is a read-only mappingproxy, which json cannot
            # serialise directly.
            "fields": dict(entry.fields),
        }
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")


def write_csv(entries: Sequence[LogEntry], handle: TextIO) -> None:
    """A rectangular table; ``csv`` handles the quoting of embedded delimiters."""

    writer = csv.writer(handle)
    writer.writerow(CSV_COLUMNS)
    for entry in entries:
        writer.writerow(
            (
                _timestamp_text(entry),
                entry.level or "",
                entry.format_name,
                "true" if entry.continuation else "false",
                entry.message,
                entry.raw,
                entry.fields.get(NODE_FIELD, ""),
                json.dumps(dict(entry.fields), ensure_ascii=False, sort_keys=True)
                if entry.fields
                else "",
            )
        )


def write_text(entries: Sequence[LogEntry], handle: TextIO) -> None:
    """The raw lines, byte-identical to what is on screen."""

    for entry in entries:
        handle.write(entry.raw)
        handle.write("\n")


@dataclass(frozen=True, slots=True)
class ExportFormat:
    """One built-in output format, as the dialog needs to present it."""

    key: str
    label: str
    extension: str
    writer: Writer


#: Order the export dialog lists them in. JSON Lines first: it is the only one
#: that round-trips an entry without loss.
BUILTIN_FORMATS: tuple[ExportFormat, ...] = (
    ExportFormat("jsonl", "JSON Lines (full entry)", "jsonl", write_jsonl),
    ExportFormat("csv", "CSV", "csv", write_csv),
    ExportFormat("text", "Plain text (raw lines)", "log", write_text),
)


def builtin_format(key: str) -> ExportFormat | None:
    """Look a built-in up by key, or None when the key is not one of ours."""

    for candidate in BUILTIN_FORMATS:
        if candidate.key == key:
            return candidate
    return None


def write_atomically(path: Path, entries: Sequence[LogEntry], writer: Writer) -> int:
    """Write *entries* through a sibling temp file, then ``os.replace`` it.

    Same technique as :meth:`clv.storage.StateStore.save`: a crash or a full
    disk mid-write leaves the previous file intact rather than a half-written
    export. The temp file is a sibling because ``os.replace`` is only atomic
    within one filesystem — and because log content must not be written
    anywhere the operator did not point.

    Returns the number of entries written. ``OSError`` propagates: the caller
    knows how to report it to whoever pressed the button.
    """

    temp = path.with_name(f".{path.name}.clv-tmp")
    try:
        # newline="" is what csv.writer needs to control its own line endings.
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer(entries, handle)
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return len(entries)


def default_stem(
    source: "SourceRef | str | None", *, now: datetime | None = None
) -> str:
    """``syslog-20260811-142530`` — enough to tell two exports of one log apart.

    The extension is the caller's business: the export dialog appends whichever
    one the highlighted format uses. A dotted name like ``access.log.1`` keeps
    its identity, so the origin of an export stays readable from its name.

    A plain ``str`` is accepted because a merged set has a *label* rather than a
    source — ``"app+2-more"``. The caller used to wrap that label in a ``Path``
    purely to get a string back out of ``.name``, which made a name look like a
    location and left the last ``Path(<not a path>)`` in the app shell.

    A remote source names its machine, through :func:`~clv.services.refs.stem_of`
    — ``web01-syslog``. Without it an export of ``/var/log/syslog`` on ``web01``
    was called exactly what an export of the local file of that name is called,
    and the two would overwrite each other in a downloads folder.
    """

    moment = now or datetime.now()
    if source is None:
        stem = "clv-export"
    else:
        stem = (source if isinstance(source, str) else stem_of(source)).replace(
            os.sep, "_"
        )
    return f"{stem}-{moment:%Y%m%d-%H%M%S}"


def describe_formats(labels: Iterable[str]) -> str:
    """One-line summary for the Advanced drawer's read-only exporter list."""

    names = list(labels)
    if not names:
        return "Exporters: none"
    return "Exporters: " + ", ".join(names)


__all__ = [
    "BUILTIN_FORMATS",
    "CSV_COLUMNS",
    "ExportFormat",
    "Writer",
    "builtin_format",
    "default_stem",
    "describe_formats",
    "write_atomically",
    "write_csv",
    "write_jsonl",
    "write_text",
]
