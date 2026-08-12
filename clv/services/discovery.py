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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .compressed import is_compressed, probe
from .documents import document_format_for
from .reader import looks_binary

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
    path: Path
    #: The configured root this file was found under (a directory, or the file
    #: itself when the operator named a single file).
    root: Path
    size: int

    @property
    def relative(self) -> Path:
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
    roots: list[Path] = field(default_factory=list)
    directories: set[Path] = field(default_factory=set)
    #: Content CLV cannot show as text: a binary, an archive, a PDF.
    skipped_unsupported: int = 0
    #: Hidden by the operator's own include_globs / exclude_globs.
    skipped_filtered: int = 0
    #: Present and of a supported type, but the read failed.
    skipped_unreadable: int = 0
    unreadable_roots: list[Path] = field(default_factory=list)
    #: Named sources that were skipped, with the reason. A file the operator
    #: typed out by hand deserves to be named back at them rather than folded
    #: into a count -- otherwise adding a PDF as a source looks like CLV did
    #: nothing at all.
    skipped_sources: list[tuple[Path, str]] = field(default_factory=list)
    #: True when max_files stopped the walk early.
    truncated: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

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
        for path, reason in self.skipped_sources:
            lines.append(f"File skipped - {reason}: {path}")
        for root in self.unreadable_roots:
            lines.append(f"Could not read source: {root}")
        return lines


def matched_glob(path: Path, root: Path, globs: Sequence[str]) -> str | None:
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


def matches_any(path: Path, root: Path, globs: Sequence[str]) -> bool:
    """Test *path* against *globs* by name and by root-relative path."""

    return matched_glob(path, root, globs) is not None


def skip_reason(
    path: Path,
    root: Path,
    settings: DiscoverySettings,
    *,
    named: bool = False,
) -> str | None:
    """Why *path* would not be listed, or None if it is a valid source.

    Shared by the folder walk and by directly named files so both describe the
    same file the same way.

    *named* marks a file the operator listed individually rather than one found
    by walking a folder. Their own globs do not apply to it — they already said
    they want this one, and filters exist to narrow a walk — but the type-based
    exclusions still do, because those describe what can be displayed at all
    rather than what was asked for.
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
    if not os.access(path, os.R_OK):
        return UNREADABLE
    if is_compressed(path):
        # The binary sniff would reject every one of these for looking like
        # the compressed bytes they are, so the question asked instead is
        # whether the archive opens at all. A corrupt one is `unreadable` --
        # CLV supports the format, this particular file is damaged -- which is
        # a different answer from `unsupported` and the actionable one.
        return None if probe(path) else UNREADABLE
    if settings.skip_binary and not _is_document(path) and looks_binary(path):
        return UNSUPPORTED
    return None


def _count_skip(report: DiscoveryReport, reason: str) -> None:
    if reason == UNSUPPORTED:
        report.skipped_unsupported += 1
    elif reason == FILTERED:
        report.skipped_filtered += 1
    else:
        report.skipped_unreadable += 1


def _accepts(path: Path, root: Path, settings: DiscoverySettings, report: DiscoveryReport) -> bool:
    reason = skip_reason(path, root, settings)
    if reason is None:
        return True
    _count_skip(report, reason)
    return False


def _is_document(path: Path) -> bool:
    """True for container formats whose text CLV can extract.

    The binary test asks "can this be shown as text", and for these the answer
    is yes even though the bytes on disk say otherwise: an ODS file is a ZIP,
    so content sniffing will always call it binary. Checked before sniffing so
    a document costs no read at all during discovery.
    """

    return document_format_for(path) is not None


def _walk_directory(
    root: Path,
    settings: DiscoverySettings,
    report: DiscoveryReport,
    seen_dirs: set[tuple[int, int]],
) -> None:
    for current, subdirs, filenames in os.walk(
        root, followlinks=settings.follow_symlinks, onerror=lambda _err: None
    ):
        current_path = Path(current)

        if settings.follow_symlinks:
            # Guard against symlink cycles walking forever.
            try:
                info = current_path.stat()
            except OSError:
                subdirs.clear()
                continue
            identity = (info.st_dev, info.st_ino)
            if identity in seen_dirs:
                subdirs.clear()
                continue
            seen_dirs.add(identity)

        subdirs.sort(key=str.lower)
        for filename in sorted(filenames, key=str.lower):
            if report.file_count >= settings.max_files:
                report.truncated = True
                return
            candidate = current_path / filename
            try:
                if not candidate.is_file():
                    continue
                if not _accepts(candidate, root, settings, report):
                    continue
                size = candidate.stat().st_size
            except OSError:
                report.skipped_unreadable += 1
                continue
            report.files.append(DiscoveredFile(path=candidate, root=root, size=size))
            report.directories.add(current_path)


def discover(
    roots: Iterable[Path],
    settings: DiscoverySettings | None = None,
) -> DiscoveryReport:
    """Walk *roots* (directories and/or individual files) into a report.

    Pure and synchronous by design so callers can run it in a worker thread;
    it performs no UI work and holds no application state.
    """

    settings = settings or DiscoverySettings()
    report = DiscoveryReport()
    seen_dirs: set[tuple[int, int]] = set()

    for root in roots:
        report.roots.append(root)
        try:
            is_dir = root.is_dir()
            is_file = root.is_file()
        except OSError:
            report.unreadable_roots.append(root)
            continue

        if is_dir:
            if not os.access(root, os.R_OK | os.X_OK):
                report.unreadable_roots.append(root)
                continue
            _walk_directory(root, settings, report, seen_dirs)
        elif is_file:
            # A directly named file bypasses the operator's own globs: they
            # already said they want this one. Type-based exclusions still
            # apply, and a skip is named back at them rather than folded into
            # a count -- a source you typed out yourself going missing with no
            # explanation is the worst version of this.
            if report.file_count >= settings.max_files:
                report.truncated = True
                continue
            try:
                reason = skip_reason(root, root, settings, named=True)
                if reason is not None:
                    _count_skip(report, reason)
                    report.skipped_sources.append((root, reason))
                    continue
                size = root.stat().st_size
            except OSError:
                report.unreadable_roots.append(root)
                continue
            report.files.append(DiscoveredFile(path=root, root=root, size=size))
            report.directories.add(root.parent)
        else:
            report.unreadable_roots.append(root)

    report.files.sort(key=lambda item: str(item.path).lower())
    return report
