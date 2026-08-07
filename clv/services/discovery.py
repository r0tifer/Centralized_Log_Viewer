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

from .reader import looks_binary

#: Skipped unless the user opts in. These are readable files that are not
#: usefully viewable as text: compressed archives and binary journals. Rotated
#: plain-text logs (``app.log.1``) are NOT excluded — those are still text.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "*.gz",
    "*.bz2",
    "*.xz",
    "*.zst",
    "*.zip",
    "*.tar",
    "*.tgz",
    "*.journal",
    "*.journal~",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
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

    @classmethod
    def from_strings(
        cls,
        *,
        include: str = "",
        exclude: str = "",
        follow_symlinks: bool = False,
        skip_binary: bool = True,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> "DiscoverySettings":
        """Build settings from comma-separated glob strings (config/drawer input)."""

        return cls(
            include_globs=_split_globs(include),
            exclude_globs=_split_globs(exclude) or DEFAULT_EXCLUDE_GLOBS,
            follow_symlinks=follow_symlinks,
            skip_binary=skip_binary,
            max_files=max_files,
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


@dataclass
class DiscoveryReport:
    """What discovery found, and what it declined to list."""

    files: list[DiscoveredFile] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)
    directories: set[Path] = field(default_factory=set)
    skipped_binary: int = 0
    skipped_excluded: int = 0
    skipped_unreadable: int = 0
    unreadable_roots: list[Path] = field(default_factory=list)
    #: True when max_files stopped the walk early.
    truncated: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def skipped_total(self) -> int:
        return self.skipped_binary + self.skipped_excluded + self.skipped_unreadable

    def summary_lines(self) -> list[str]:
        """Human-readable discovery summary for the empty viewer pane."""

        lines = [
            f"Configured sources: {len(self.roots)}",
            f"Folders containing logs: {len(self.directories)}",
            f"Log files found: {self.file_count}",
        ]
        skipped: list[str] = []
        if self.skipped_binary:
            skipped.append(f"{self.skipped_binary} binary")
        if self.skipped_excluded:
            skipped.append(f"{self.skipped_excluded} excluded")
        if self.skipped_unreadable:
            skipped.append(f"{self.skipped_unreadable} unreadable")
        if skipped:
            lines.append("Skipped: " + ", ".join(skipped))
        if self.truncated:
            lines.append(f"Stopped at the {self.file_count}-file limit; narrow the include filter to see more.")
        for root in self.unreadable_roots:
            lines.append(f"Could not read source: {root}")
        return lines


def matches_any(path: Path, root: Path, globs: Sequence[str]) -> bool:
    """Test *path* against *globs* by name and by root-relative path."""

    if not globs:
        return False
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
            return True
    return False


def _accepts(path: Path, root: Path, settings: DiscoverySettings, report: DiscoveryReport) -> bool:
    if matches_any(path, root, settings.exclude_globs):
        report.skipped_excluded += 1
        return False
    if settings.include_globs and not matches_any(path, root, settings.include_globs):
        report.skipped_excluded += 1
        return False
    if not os.access(path, os.R_OK):
        report.skipped_unreadable += 1
        return False
    if settings.skip_binary and looks_binary(path):
        report.skipped_binary += 1
        return False
    return True


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
            # A directly named file bypasses the include filter: the operator
            # already said they want this one. Exclusions still apply so a
            # named binary is reported rather than silently listed.
            if report.file_count >= settings.max_files:
                report.truncated = True
                continue
            try:
                if settings.skip_binary and looks_binary(root):
                    report.skipped_binary += 1
                    continue
                if not os.access(root, os.R_OK):
                    report.unreadable_roots.append(root)
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
