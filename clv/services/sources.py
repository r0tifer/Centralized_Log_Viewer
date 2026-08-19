from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Sequence

from .backend import LOCAL, LOCAL_ACCESS_HINT, BackendResolver, SourceBackend
from .refs import SourceRef, identity, normalize_ref, ref_key
from .settings_file import DEFAULT_SECTION, SettingsDocument


#: What to tell an operator whose local read was refused.
#:
#: Now **the local backend's** answer rather than the only one:
#: :func:`check_access` takes its hint from ``backend.capabilities``, because
#: "re-launch with sudo" is not merely unhelpful for a file on another machine
#: — it recommends the one thing CLV refuses to do anywhere. Kept as a name
#: here because it is part of this module's public surface.
ACCESS_HINT = LOCAL_ACCESS_HINT


@dataclass
class SourceMessage:
    text: str
    severity: Literal["info", "warning", "error"] = "info"


@dataclass
class SourceAddition:
    success: bool
    path: Path | None = None
    messages: list[SourceMessage] = field(default_factory=list)


#: Dedupe key for a source. Was a local ``_marker``; ``refs.ref_key`` is the
#: same function with the scheme guard added, and unifying it with ``app``'s
#: identical ``_resolve`` is half the point of ``refs``.
_marker = ref_key


def normalize_path(raw: str | SourceRef) -> SourceRef:
    """Expand and absolutize a user supplied path without failing on missing targets.

    This is the **user-input** boundary — what a person typed into
    ``settings.conf`` or the add-source dialog. A string CLV itself persisted is
    already canonical and must go through ``refs.parse_ref`` instead: expanding
    one is what turns ``journal:all`` into ``$CWD/journal:all``.
    """

    return normalize_ref(raw)


def check_access(
    path: Path, *, backend: SourceBackend = LOCAL
) -> tuple[bool, str | None]:
    """Verify CLV can read from *path* before incorporating it.

    The hint appended to every refusal comes from
    ``backend.capabilities.access_hint``, not from this module: what to do
    about an unreadable file is a property of where the file lives.

    The directory case tests listing for real rather than trusting the
    permission bits, which is deliberate — an ACL or an SELinux label can
    refuse a listing that ``access`` says is fine. That is why
    ``SourceBackend.list_dir`` exists and *raises*, where ``walk`` skips.
    """

    hint = backend.capabilities.access_hint
    kind = backend.kind(path)

    if kind == "denied":
        return False, f"Permission denied while checking '{path}'. {hint}"

    if kind == "missing":
        return False, f"Path '{path}' does not exist."

    if kind == "file":
        if not backend.access(path, os.R_OK):
            return False, f"Read access required for file '{path}'. {hint}"
        return True, None

    if kind == "dir":
        if not backend.access(path, os.R_OK | os.X_OK):
            return False, f"List access required for directory '{path}'. {hint}"
        try:
            next(iter(backend.list_dir(path)), None)
        except PermissionError:
            return False, f"Permission denied while listing '{path}'. {hint}"
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
        *,
        backends: BackendResolver = LOCAL,
    ) -> None:
        self._directories = self._prepare(directories)
        self._files = self._prepare(files)
        self._markers = {_marker(path) for path in self._directories + self._files}
        self._added: set[Path] = set()
        #: A resolver rather than a backend: an ad-hoc source may name another
        #: machine, and the one being added is not necessarily on the same one
        #: as the last.
        self._backends = backends

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
        backend = self._backends.for_ref(path)
        marker = _marker(path)
        if marker in self._markers:
            return SourceAddition(
                success=False,
                path=path,
                messages=[
                    SourceMessage(f"{path} is already part of this session.", "warning"),
                ],
            )

        allowed, reason = check_access(path, backend=backend)
        if not allowed:
            return SourceAddition(
                success=False,
                path=path,
                messages=[SourceMessage(reason or f"Permission denied for '{path}'.", "error")],
            )

        resolved = identity(path)
        kind = backend.kind(resolved)

        if kind == "dir":
            self._directories.append(resolved)
            self._directories.sort(key=lambda p: str(p).lower())
        elif kind == "file":
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
        if kind == "file" and resolved.suffix.lower() != ".log":
            messages.insert(
                0,
                SourceMessage(
                    f"{resolved.name} does not end with .log; added anyway.",
                    "warning",
                ),
            )
        return SourceAddition(success=True, path=resolved, messages=messages)


def persist_setting(
    settings_path: Path, option: str, value: str, *, section: str = DEFAULT_SECTION
) -> None:
    """Write ``option = value`` into *settings_path*, in place.

    The settings file is the operator's, full of their comments, so it is edited
    rather than regenerated — see :mod:`clv.services.settings_file`, which owns
    that and is where the section scoping lives. Used for choices made through
    the UI that must outlive the session: enabling the journal is the case that
    needed it, because consent to run a subprocess is not something to ask for
    again every launch.

    *section* defaults to the global one. It exists because ``[ssh:<name>]``
    sections made "append at the end of the file" wrong — the end of the file is
    inside the last host.
    """

    document = SettingsDocument.load(settings_path)
    document.set(section, option, value)
    document.save(settings_path)


def persist_log_sources(settings_path: Path, entries: Sequence[Path]) -> None:
    """Merge *entries* into the `log_dirs` line within *settings_path*.

    Scoped to ``[log_viewer]``, which is load-bearing rather than tidy:
    ``log_dirs`` is a key in the remote-host schema too, so an unscoped search
    for the first one in the file could rewrite **another machine's** roots with
    paths from this one.
    """

    entry_strings = [
        str(path)
        for path in sorted(
            {_marker(path): path for path in entries}.values(),
            key=lambda p: str(p).lower(),
        )
    ]

    document = SettingsDocument.load(settings_path)
    current = document.get(DEFAULT_SECTION, "log_dirs") or ""
    values = [piece.strip() for piece in current.split(",") if piece.strip()]
    document.set(
        DEFAULT_SECTION,
        "log_dirs",
        ", ".join(dict.fromkeys(values + entry_strings)),
    )
    document.save(settings_path)
