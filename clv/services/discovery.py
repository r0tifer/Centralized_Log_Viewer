"""Log source discovery.

CLV is deliberately not restricted to ``*.log``. An operator names folders or
individual files, and everything readable underneath them is a candidate. The
filtering that does happen is either explicit (include/exclude globs the user
controls) or about readability rather than naming: a file whose first block
contains NUL bytes cannot be displayed as text, so it is skipped and counted
rather than listed as a dead end.
"""

from __future__ import annotations

import fnmatch
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .backend import (
    LOCAL,
    BackendResolver,
    ClassifyRequest,
    ClassifyResult,
    SourceBackend,
    WalkEntry,
)
from .compressed import PROBE_SIZE, is_compressed, probe_block
from .documents import document_format_for
from .reader import SNIFF_SIZE, looks_binary_block
from .refs import SourceRef

#: Skipped unless the user opts in. These are readable files that are not
#: usefully viewable as text: archives and binary journals. Rotated plain-text
#: logs (``app.log.1``) are NOT excluded — those are still text.
#:
#: ``*.gz``, ``*.bz2`` and ``*.xz`` left this list when CLV learned to read
#: them (see :mod:`clv.services.compressed`): a single compressed *file* is a
#: log, and excluding it kept the rotated half of ``/var/log`` out of reach.
#: ``*.zst`` stays because there is no stdlib decompressor for it, and the
#: archive formats stay because an archive is a container of files rather than
#: a log — the two are excluded for different reasons that happen to agree.
#:
#: ``*.pdf`` is here for a different reason than the rest. Its text is
#: extractable, but only into reflowed prose with no line structure, no
#: timestamps and no severity — every line would land in the parser's
#: unrecognised bucket. Listing it explicitly means PDFs are reported as an
#: unsupported file type rather than vanishing with no explanation, and an
#: operator who disagrees can drop it from ``exclude_globs``.
#:
#: Membership of this tuple is also what separates "CLV cannot display this"
#: from "your glob hid it" when a skip is reported — see :func:`skip_reason`.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "*.zst",
    "*.zip",
    "*.tar",
    "*.tgz",
    "*.journal",
    "*.journal~",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.pdf",
)

#: Ceiling on files returned from one discovery pass, so pointing CLV at a huge
#: tree degrades into "showing the first N" instead of freezing.
DEFAULT_MAX_FILES = 5000


@dataclass(frozen=True, slots=True)
class DiscoverySettings:
    """User-controllable rules for what counts as a log source."""

    #: Empty means "every file". Otherwise a file must match one of these
    #: globs, tested against both its name and its path relative to the root.
    include_globs: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = DEFAULT_EXCLUDE_GLOBS
    follow_symlinks: bool = False
    skip_binary: bool = True
    max_files: int = DEFAULT_MAX_FILES
    #: Present ``app.log`` + ``app.log.1`` + ``app.log.2.gz`` as one source.
    #: A presentation rule rather than a walk rule — every member is still
    #: discovered and still individually openable — but it lives here because
    #: it is the operator's answer to "what counts as a source".
    group_rotated: bool = True

    @classmethod
    def from_strings(
        cls,
        *,
        include: str = "",
        exclude: str = "",
        follow_symlinks: bool = False,
        skip_binary: bool = True,
        max_files: int = DEFAULT_MAX_FILES,
        group_rotated: bool = True,
    ) -> "DiscoverySettings":
        """Build settings from comma-separated glob strings (config/drawer input)."""

        return cls(
            include_globs=_split_globs(include),
            exclude_globs=_split_globs(exclude) or DEFAULT_EXCLUDE_GLOBS,
            follow_symlinks=follow_symlinks,
            skip_binary=skip_binary,
            max_files=max_files,
            group_rotated=group_rotated,
        )


def _split_globs(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: SourceRef
    #: The configured root this file was found under (a directory, or the file
    #: itself when the operator named a single file).
    root: SourceRef
    size: int

    @property
    def relative(self) -> SourceRef:
        try:
            return self.path.relative_to(self.root)
        except ValueError:
            return Path(self.path.name)


#: Why a file was not listed, in the operator's terms rather than the
#: implementation's. "Excluded" used to cover both of the first two, which
#: made the count useless: it could not distinguish "CLV cannot display this"
#: from "your own glob hid it", and only one of those is worth acting on.
UNSUPPORTED = "unsupported file type"
FILTERED = "filtered out"
UNREADABLE = "unreadable"

_SKIP_PLURALS = {
    UNSUPPORTED: "unsupported file types",
    FILTERED: FILTERED,
    UNREADABLE: UNREADABLE,
}


@dataclass
class DiscoveryReport:
    """What discovery found, and what it declined to list."""

    files: list[DiscoveredFile] = field(default_factory=list)
    roots: list[SourceRef] = field(default_factory=list)
    directories: set[SourceRef] = field(default_factory=set)
    #: Content CLV cannot show as text: a binary, an archive, a PDF.
    skipped_unsupported: int = 0
    #: Hidden by the operator's own include_globs / exclude_globs.
    skipped_filtered: int = 0
    #: Present and of a supported type, but the read failed.
    skipped_unreadable: int = 0
    unreadable_roots: list[SourceRef] = field(default_factory=list)
    #: Named sources that were skipped, with the reason. A file the operator
    #: typed out by hand deserves to be named back at them rather than folded
    #: into a count -- otherwise adding a PDF as a source looks like CLV did
    #: nothing at all.
    skipped_sources: list[tuple[SourceRef, str]] = field(default_factory=list)
    #: True when max_files stopped the walk early.
    truncated: bool = False
    #: The roots whose walk was cut short, so the report can say **whose** files
    #: were dropped. A global bool was enough while every root was on this
    #: machine and shared one budget; with a per-host ceiling, "stopped at the
    #: limit" without naming the host is not something an operator can act on.
    truncated_roots: list[SourceRef] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def extend(self, other: "DiscoveryReport") -> "DiscoveryReport":
        """Fold *other* into this report, in place.

        Discovery runs once per host so each can have its own ``max_files``
        budget, and the results have to become one report because the tree, the
        summary and the starred-set lookup are all built from a single one.
        Counts add; the file list is re-sorted by :func:`discover`'s own rule so
        a merged report is ordered exactly as an unsplit one would have been.
        """

        self.files.extend(other.files)
        self.roots.extend(other.roots)
        self.directories |= other.directories
        self.skipped_unsupported += other.skipped_unsupported
        self.skipped_filtered += other.skipped_filtered
        self.skipped_unreadable += other.skipped_unreadable
        self.unreadable_roots.extend(other.unreadable_roots)
        self.skipped_sources.extend(other.skipped_sources)
        self.truncated = self.truncated or other.truncated
        self.truncated_roots.extend(other.truncated_roots)
        self.files.sort(key=lambda item: str(item.path).lower())
        return self

    @property
    def skipped_total(self) -> int:
        return self.skipped_unsupported + self.skipped_filtered + self.skipped_unreadable

    def summary_lines(self) -> list[str]:
        """Human-readable discovery summary for the empty viewer pane."""

        lines = [
            f"Configured sources: {len(self.roots)}",
            f"Folders containing logs: {len(self.directories)}",
            f"Log files found: {self.file_count}",
        ]
        counts = (
            (self.skipped_unsupported, UNSUPPORTED),
            (self.skipped_filtered, FILTERED),
            (self.skipped_unreadable, UNREADABLE),
        )
        skipped = [
            f"{count} {label if count == 1 else _SKIP_PLURALS[label]}"
            for count, label in counts
            if count
        ]
        if skipped:
            lines.append("Skipped: " + ", ".join(skipped))
        if self.truncated:
            lines.append(f"Stopped at the {self.file_count}-file limit; narrow the include filter to see more.")
        for root in self.truncated_roots:
            lines.append(f"Reached its file limit: {root}")
        for path, reason in self.skipped_sources:
            lines.append(f"File skipped - {reason}: {path}")
        for root in self.unreadable_roots:
            lines.append(f"Could not read source: {root}")
        return lines


def matched_glob(path: SourceRef, root: SourceRef, globs: Sequence[str]) -> str | None:
    """The first of *globs* matching *path*, by name and by root-relative path.

    Returns the pattern rather than a bool because *which* glob matched decides
    how the skip is reported: a default like ``*.pdf`` describes a file type,
    while one the operator added describes a filter they chose.
    """

    if not globs:
        return None
    name = path.name
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = name
    full = str(path)
    for pattern in globs:
        if (
            fnmatch.fnmatch(name, pattern)
            or fnmatch.fnmatch(relative, pattern)
            or fnmatch.fnmatch(full, pattern)
        ):
            return pattern
    return None


def matches_any(path: SourceRef, root: SourceRef, globs: Sequence[str]) -> bool:
    """Test *path* against *globs* by name and by root-relative path."""

    return matched_glob(path, root, globs) is not None


def skip_reason(
    path: SourceRef,
    root: SourceRef,
    settings: DiscoverySettings,
    *,
    named: bool = False,
    backend: SourceBackend = LOCAL,
    measured: ClassifyResult | None = None,
) -> str | None:
    """Why *path* would not be listed, or None if it is a valid source.

    Shared by the folder walk and by directly named files so both describe the
    same file the same way.

    *named* marks a file the operator listed individually rather than one found
    by walking a folder. Their own globs do not apply to it — they already said
    they want this one, and filters exist to narrow a walk — but the type-based
    exclusions still do, because those describe what can be displayed at all
    rather than what was asked for.

    *measured* is this file's entry from a ``backend.classify`` batch, when the
    caller already has one. The walk always does — that is how a remote tree
    costs one round trip instead of one per file — and passing it here is what
    keeps the *judgement* in a single function rather than forking a second
    copy for the batched path. Absent, the measurement is taken for this one
    file, which is what a directly named source does.
    """

    reason = _name_skip(path, root, settings, named=named)
    if reason is not None:
        return reason
    if measured is None:
        request = classify_request(path, settings)
        measured = backend.classify([request]).get(request.ref)
    return _content_skip(path, settings, measured)


def _name_skip(
    path: SourceRef,
    root: SourceRef,
    settings: DiscoverySettings,
    *,
    named: bool,
) -> str | None:
    """The half that costs no IO, so a filtered file is never read.

    Split out because it is what decides whether a file is worth putting in a
    ``classify`` batch at all. Ordering is unchanged: globs first, always.
    """

    exclude_globs = settings.exclude_globs
    if named:
        exclude_globs = tuple(
            glob for glob in exclude_globs if glob in DEFAULT_EXCLUDE_GLOBS
        )

    excluded_by = matched_glob(path, root, exclude_globs)
    if excluded_by is not None:
        # The shipped exclusions are all statements about file type: an
        # archive, a database, a PDF. A glob the operator added instead is
        # their filter, and calling that "unsupported" would blame CLV for a
        # choice they made.
        return UNSUPPORTED if excluded_by in DEFAULT_EXCLUDE_GLOBS else FILTERED
    if (
        not named
        and settings.include_globs
        and not matches_any(path, root, settings.include_globs)
    ):
        return FILTERED
    return None


def classify_request(
    path: SourceRef, settings: DiscoverySettings
) -> ClassifyRequest:
    """How much of *path* a verdict needs, and therefore what to ask for.

    Three answers, and which one applies is decided from the **name** alone so
    it costs nothing: a compressed member needs enough to parse its container
    header, an ordinary file needs the text sniff block, and a document — or
    any file when ``skip_binary`` is off — needs no bytes at all, only whether
    it can be read.
    """

    if is_compressed(path):
        return ClassifyRequest(ref=path, head_bytes=PROBE_SIZE)
    if settings.skip_binary and not _is_document(path):
        return ClassifyRequest(ref=path, head_bytes=SNIFF_SIZE)
    return ClassifyRequest(ref=path, head_bytes=0)


def _content_skip(
    path: SourceRef,
    settings: DiscoverySettings,
    measured: ClassifyResult | None,
) -> str | None:
    """The half that needed the file's bytes, over bytes already in hand.

    A ref missing from the batch is unreadable: the backend could not measure
    it, which is the same conclusion ``access`` returning False used to reach.
    """

    if measured is None or not measured.readable:
        return UNREADABLE
    if is_compressed(path):
        # The binary sniff would reject every one of these for looking like
        # the compressed bytes they are, so the question asked instead is
        # whether the archive opens at all. A corrupt one is `unreadable` --
        # CLV supports the format, this particular file is damaged -- which is
        # a different answer from `unsupported` and the actionable one.
        return (
            None
            if probe_block(path, measured.head, complete=measured.complete)
            else UNREADABLE
        )
    if (
        settings.skip_binary
        and not _is_document(path)
        and looks_binary_block(measured.head)
    ):
        return UNSUPPORTED
    return None


def _count_skip(report: DiscoveryReport, reason: str) -> None:
    if reason == UNSUPPORTED:
        report.skipped_unsupported += 1
    elif reason == FILTERED:
        report.skipped_filtered += 1
    else:
        report.skipped_unreadable += 1


def _accepts(
    path: SourceRef,
    root: SourceRef,
    settings: DiscoverySettings,
    report: DiscoveryReport,
    backend: SourceBackend,
    measured: ClassifyResult | None = None,
) -> bool:
    reason = skip_reason(path, root, settings, backend=backend, measured=measured)
    if reason is None:
        return True
    _count_skip(report, reason)
    return False


def _is_document(path: SourceRef) -> bool:
    """True for container formats whose text CLV can extract.

    The binary test asks "can this be shown as text", and for these the answer
    is yes even though the bytes on disk say otherwise: an ODS file is a ZIP,
    so content sniffing will always call it binary. Checked before sniffing so
    a document costs no read at all during discovery.
    """

    return document_format_for(path) is not None


#: Candidates measured in one :meth:`~clv.services.backend.SourceBackend.classify`
#: call.
#:
#: A ceiling on two things at once. It bounds the argv a remote backend builds
#: from the batch — a single command naming five thousand paths would exceed
#: ``ARG_MAX`` — and it bounds the lookahead below, so a tree that is about to
#: hit ``max_files`` measures at most one batch it never lists. Large enough
#: that a 400-file ``/var/log`` is one round trip, which is the number
#: Requirement 4 is about.
CLASSIFY_BATCH = 500


def _walk_directory(
    root: SourceRef,
    settings: DiscoverySettings,
    report: DiscoveryReport,
    seen_dirs: set[object],
    backend: SourceBackend,
) -> None:
    """Fold one root's files into *report*.

    The traversal itself — the symlink-cycle guard, the ordering, the
    swallowing of a directory that will not list — belongs to the backend now.
    What stays here is the part that is discovery's judgement rather than the
    filesystem's: the ``max_files`` ceiling, and which skips are counted where.

    *seen_dirs* is shared across every root in one pass, which is why it is
    threaded through rather than owned by the walk: two configured roots that
    overlap must not walk the shared subtree twice.

    **The loop is the one it always was, with a buffer in front of it.** Entries
    arrive in batches of :data:`CLASSIFY_BATCH` and are measured together, then
    handed to the same per-entry judgement in the same order — so the accepted
    set, the skip tallies and where ``max_files`` truncates are all unchanged.
    What changes is the cost of asking: locally one open per file either way,
    remotely one command per batch instead of one round trip per file.
    """

    entries = backend.walk(
        root, follow_symlinks=settings.follow_symlinks, seen=seen_dirs
    )
    buffered: deque[WalkEntry] = deque()
    measured: dict[SourceRef, ClassifyResult] = {}

    while True:
        if not buffered:
            measured = _fill_batch(
                entries, buffered, root, settings, backend, report.file_count
            )
            if not buffered:
                return
        if report.file_count >= settings.max_files:
            report.truncated = True
            report.truncated_roots.append(root)
            return
        entry = buffered.popleft()
        if entry.unreadable:
            report.skipped_unreadable += 1
            continue
        candidate = entry.ref
        if not _accepts(
            candidate, root, settings, report, backend, measured.get(candidate)
        ):
            continue
        report.files.append(
            DiscoveredFile(path=candidate, root=root, size=entry.size)
        )
        report.directories.add(candidate.parent)


def _fill_batch(
    entries: Iterator[WalkEntry],
    buffered: deque[WalkEntry],
    root: SourceRef,
    settings: DiscoverySettings,
    backend: SourceBackend,
    found: int,
) -> dict[SourceRef, ClassifyResult]:
    """Pull one batch off the walk and measure the part of it that needs it.

    Only entries that survive the **name** filters are measured, which is what
    keeps a filtered-out tree free: ``skip_reason`` has always returned on a
    glob before touching the file, and batching must not quietly start reading
    what the operator's own ``exclude_globs`` hid.

    **Laziness survives, and the budget is what bounds it.** The batch is capped
    at whatever is left of ``max_files`` plus one — the extra entry being how
    truncation is detected at all, exactly as the unbatched loop discovered it —
    so pointing CLV at a 100 000-file tree with ``max_files = 5`` still measures
    six files and not five hundred. That upper bound is a *pre-existing*
    assertion in ``tests/test_discovery_reader.py``, and it is the reason this
    parameter exists.
    """

    remaining = settings.max_files - found
    limit = max(1, min(CLASSIFY_BATCH, remaining + 1))

    requests: list[ClassifyRequest] = []
    for entry in entries:
        buffered.append(entry)
        if not entry.unreadable and _name_skip(
            entry.ref, root, settings, named=False
        ) is None:
            requests.append(classify_request(entry.ref, settings))
        if len(buffered) >= limit:
            break
    return backend.classify(requests) if requests else {}


def discover(
    roots: Iterable[SourceRef],
    settings: DiscoverySettings | None = None,
    *,
    backends: BackendResolver = LOCAL,
) -> DiscoveryReport:
    """Walk *roots* (directories and/or individual files) into a report.

    Pure and synchronous by design so callers can run it in a worker thread;
    it performs no UI work and holds no application state.

    *backends* is a resolver rather than a single backend because *roots* is a
    mixed list: one entry may be a folder on this machine and the next a folder
    on another, and no single backend can answer for both.
    """

    settings = settings or DiscoverySettings()
    report = DiscoveryReport()
    seen_dirs: set[object] = set()

    for root in roots:
        report.roots.append(root)
        backend = backends.for_ref(root)
        # One call, not an exists/is_file/is_dir triple: remotely that triple
        # is three round trips to answer one question.
        kind = backend.kind(root)

        if kind == "dir":
            if not backend.access(root, os.R_OK | os.X_OK):
                report.unreadable_roots.append(root)
                continue
            _walk_directory(root, settings, report, seen_dirs, backend)
        elif kind == "file":
            # A directly named file bypasses the operator's own globs: they
            # already said they want this one. Type-based exclusions still
            # apply, and a skip is named back at them rather than folded into
            # a count -- a source you typed out yourself going missing with no
            # explanation is the worst version of this.
            if report.file_count >= settings.max_files:
                report.truncated = True
                if root not in report.truncated_roots:
                    report.truncated_roots.append(root)
                continue
            reason = skip_reason(root, root, settings, named=True, backend=backend)
            if reason is not None:
                _count_skip(report, reason)
                report.skipped_sources.append((root, reason))
                continue
            info = backend.stat(root)
            if info is None:
                report.unreadable_roots.append(root)
                continue
            report.files.append(DiscoveredFile(path=root, root=root, size=info.size))
            report.directories.add(root.parent)
        else:
            # `missing`, `denied`, and anything that is neither file nor
            # directory. Three facts, one report line -- as before: what the
            # operator can act on is that this root produced nothing.
            report.unreadable_roots.append(root)

    report.files.sort(key=lambda item: str(item.path).lower())
    return report
