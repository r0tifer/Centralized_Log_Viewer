from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Sequence


ACCESS_HINT = (
    "Re-launch Centralized Log Viewer with elevated permissions (for example `sudo`) "
    "to include this source."
)


@dataclass
class SourceMessage:
    text: str
    severity: Literal["info", "warning", "error"] = "info"


@dataclass
class SourceAddition:
    success: bool
    path: Path | None = None
    messages: list[SourceMessage] = field(default_factory=list)


def _marker(path: Path) -> str:
    """Return a normalized string key for *path* suitable for deduping."""

    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def normalize_path(raw: str | Path) -> Path:
    """Expand and absolutize a user supplied path without failing on missing targets."""

    if isinstance(raw, Path):
        path = raw
    else:
        path = Path(str(raw))
    path = path.expanduser()
    try:
        if path.is_absolute():
            return path.resolve(strict=False)
        return (Path.cwd() / path).resolve(strict=False)
    except OSError:
        return path


def check_access(path: Path) -> tuple[bool, str | None]:
    """Verify CLV can read from *path* before incorporating it."""

    try:
        exists = path.exists()
    except PermissionError:
        return False, f"Permission denied while checking '{path}'. {ACCESS_HINT}"

    if not exists:
        return False, f"Path '{path}' does not exist."

    if path.is_file():
        if not os.access(path, os.R_OK):
            return False, f"Read access required for file '{path}'. {ACCESS_HINT}"
        return True, None

    if path.is_dir():
        if not os.access(path, os.R_OK | os.X_OK):
            return False, f"List access required for directory '{path}'. {ACCESS_HINT}"
        try:
            with os.scandir(path) as iterator:
                next(iterator, None)
        except PermissionError:
            return False, f"Permission denied while listing '{path}'. {ACCESS_HINT}"
        except FileNotFoundError:
            return False, f"Directory '{path}' is not accessible."
        return True, None

    return False, f"Path '{path}' is neither a file nor a directory."


class SourceManager:
    """Manage configured and ad-hoc log sources for the current session."""

    def __init__(
        self,
        directories: Iterable[Path],
        files: Iterable[Path],
    ) -> None:
        self._directories = self._prepare(directories)
        self._files = self._prepare(files)
        self._markers = {_marker(path) for path in self._directories + self._files}
        self._added: set[Path] = set()

    @staticmethod
    def _prepare(items: Iterable[Path]) -> list[Path]:
        unique: dict[str, Path] = {}
        for entry in items:
            marker = _marker(entry)
            if marker not in unique:
                unique[marker] = entry
        return sorted(unique.values(), key=lambda p: str(p).lower())

    @property
    def directories(self) -> list[Path]:
        return list(self._directories)

    @property
    def files(self) -> list[Path]:
        return list(self._files)

    @property
    def added_paths(self) -> list[Path]:
        return sorted(self._added, key=lambda p: str(p).lower())

    def all_sources(self) -> list[Path]:
        return self.directories + self.files

    def clear_added(self) -> None:
        self._added.clear()

    def contains(self, path: Path) -> bool:
        return _marker(path) in self._markers

    def add(self, raw_path: str) -> SourceAddition:
        cleaned = raw_path.strip().strip('"')
        if not cleaned:
            return SourceAddition(success=False, messages=[])

        path = normalize_path(cleaned)
        marker = _marker(path)
        if marker in self._markers:
            return SourceAddition(
                success=False,
                path=path,
                messages=[
                    SourceMessage(f"{path} is already part of this session.", "warning"),
                ],
            )

        allowed, reason = check_access(path)
        if not allowed:
            return SourceAddition(
                success=False,
                path=path,
                messages=[SourceMessage(reason or f"Permission denied for '{path}'.", "error")],
            )

        try:
            resolved = path.resolve()
        except OSError:
            resolved = path

        if resolved.is_dir():
            self._directories.append(resolved)
            self._directories.sort(key=lambda p: str(p).lower())
        elif resolved.is_file():
            self._files.append(resolved)
            self._files.sort(key=lambda p: str(p).lower())
        else:
            return SourceAddition(
                success=False,
                path=resolved,
                messages=[SourceMessage(f"Path '{resolved}' does not exist.", "error")],
            )

        self._markers.add(_marker(resolved))
        self._added.add(resolved)

        messages = [SourceMessage(f"Added {resolved} to the current session.", "info")]
        if resolved.is_file() and resolved.suffix.lower() != ".log":
            messages.insert(
                0,
                SourceMessage(
                    f"{resolved.name} does not end with .log; added anyway.",
                    "warning",
                ),
            )
        return SourceAddition(success=True, path=resolved, messages=messages)


def persist_log_sources(settings_path: Path, entries: Sequence[Path]) -> None:
    """Merge *entries* into the `log_dirs` line within *settings_path*."""

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        lines = settings_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["[log_viewer]", ""]

    entry_strings = [
        str(path)
        for path in sorted({ _marker(path): path for path in entries }.values(), key=lambda p: str(p).lower())
    ]

    def _merge_values(raw: str) -> str:
        values = [piece.strip() for piece in raw.split(",") if piece.strip()]
        merged = list(dict.fromkeys(values + entry_strings))
        return ", ".join(merged)

    replaced = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("log_dirs"):
            continue
        leading = line[: len(line) - len(stripped)]
        _, _, value = line.partition("=")
        merged = _merge_values(value)
        lines[idx] = f"{leading}log_dirs = {merged}"
        replaced = True
        break

    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"log_dirs = {', '.join(entry_strings)}")

    payload = "\n".join(lines)
    if not payload.endswith("\n"):
        payload += "\n"
    settings_path.write_text(payload, encoding="utf-8")
