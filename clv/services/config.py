"""Settings file discovery, parsing and defaults.

Extracted from ``app.py`` so configuration can be tested and reused without a
running UI. Every value is validated and clamped: a malformed settings file
degrades to defaults rather than preventing startup.
"""

from __future__ import annotations

import configparser
import os
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .discovery import DEFAULT_EXCLUDE_GLOBS, DEFAULT_MAX_FILES, DiscoverySettings

CONFIG_SECTION = "log_viewer"

# Bounds keep a typo in settings.conf from producing a pathological UI.
_LIMITS: dict[str, tuple[int, int, int]] = {
    # option: (default, minimum, maximum)
    "max_buffer_lines": (5000, 100, 500_000),
    "default_show_lines": (500, 10, 500_000),
    "refresh_hz": (2, 1, 60),
    "min_show_lines": (10, 1, 10_000),
    "show_step": (50, 1, 10_000),
    "csv_max_rows": (20, 1, 5_000),
    "csv_max_cols": (10, 1, 200),
    "max_files": (DEFAULT_MAX_FILES, 1, 200_000),
    "tree_width": (38, 20, 120),
    # Bytes of log text one OSC 52 copy may carry. The default is under tmux's
    # ~74 kB passthrough limit with room for base64 expansion, so a copy that
    # fits the cap is a copy the terminal will actually accept.
    "clipboard_max_bytes": (65_536, 1_024, 1_000_000),
    # Seconds a watch rule waits before it may notify again. The floor is not
    # zero on purpose: a rule matching every tailed line would otherwise raise
    # a toast per line, which is the behaviour that gets a feature like this
    # switched off for good.
    "watch_rate_limit": (60, 5, 3_600),
}

DEFAULT_SETTINGS_TEMPLATE = f"""[{CONFIG_SECTION}]

# Folders and/or individual files to monitor, comma separated.
# Folders are searched recursively. CLV is not limited to *.log files: any
# readable text file is a valid source. Use include_globs/exclude_globs below
# to narrow that down.
log_dirs = /var/log

# Only list files matching these globs. Empty means "every text file".
# Example: include_globs = *.log, *.txt, syslog*
include_globs =

# Never list files matching these globs. Compressed archives and binary
# journals are excluded by default because they cannot be displayed as text.
exclude_globs = {", ".join(DEFAULT_EXCLUDE_GLOBS)}

# Follow symlinked directories while scanning (cycles are detected).
follow_symlinks = false

# Skip files whose first block contains NUL bytes (i.e. binaries).
skip_binary = true

# Stop discovery after this many files.
max_files = {DEFAULT_MAX_FILES}

# Lines held in memory per source. Older lines are dropped.
max_buffer_lines = 5000

# Lines shown when a source is first opened; adjust at runtime with + / -.
default_show_lines = 500
min_show_lines = 10
show_step = 50

# How often (Hz) to check for new content.
refresh_hz = 2

# Starting width of the source tree, in columns.
tree_width = 38

# Structured payload preview limits.
csv_max_rows = 20
csv_max_cols = 10

# Most log text one 'y' (OSC 52 clipboard copy) may carry. Oversized copies are
# truncated at a line boundary and the notification says how much was dropped.
clipboard_max_bytes = 65536

# Watch rules (W). Seconds one rule waits before it may notify again; matches
# inside the window are counted and reported together.
watch_rate_limit = 60

# Ring the terminal bell when a watch rule notifies. Off by default.
watch_bell = false
"""


@dataclass
class LogConfig:
    """Runtime configuration for one CLV session."""

    log_dirs: list[Path] = field(default_factory=list)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    max_buffer_lines: int = _LIMITS["max_buffer_lines"][0]
    default_show_lines: int = _LIMITS["default_show_lines"][0]
    refresh_hz: int = _LIMITS["refresh_hz"][0]
    min_show_lines: int = _LIMITS["min_show_lines"][0]
    show_step: int = _LIMITS["show_step"][0]
    csv_max_rows: int = _LIMITS["csv_max_rows"][0]
    csv_max_cols: int = _LIMITS["csv_max_cols"][0]
    tree_width: int = _LIMITS["tree_width"][0]
    clipboard_max_bytes: int = _LIMITS["clipboard_max_bytes"][0]
    watch_rate_limit: int = _LIMITS["watch_rate_limit"][0]
    #: Ring the terminal bell when a watch rule notifies. Off by default: a
    #: bell is a thing an operator opts into, never a thing a log does to them.
    watch_bell: bool = False

    def with_discovery(self, **changes) -> "LogConfig":
        """Return a copy with individual discovery settings replaced."""
        return replace(self, discovery=replace(self.discovery, **changes))


def get_xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".config"


def user_config_path() -> Path:
    return get_xdg_config_home() / "clv" / "settings.conf"


def bundled_config_path() -> Path:
    """Locate the shipped settings.conf template.

    Under PyInstaller the source tree does not exist on disk; the data file
    added with ``--add-data settings.conf:.`` lands in the extraction root that
    PyInstaller exposes as ``sys._MEIPASS``. Relying on ``__file__`` and
    counting parent directories happens to land in the same place for a onedir
    build, but it is an accident of the layout and breaks under onefile.
    """

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "settings.conf"
    # Development checkout: settings.conf sits beside the clv package.
    return Path(__file__).resolve().parents[2] / "settings.conf"


#: Backwards-compatible alias; the template is only a "repo" path when running
#: from a source checkout.
repo_config_path = bundled_config_path


def ensure_user_settings_file() -> Optional[Path]:
    """Create the per-user settings file if absent, returning a usable path."""

    target = user_config_path()
    if target.exists():
        return target

    template = repo_config_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return template if template.exists() else None

    # Only copy the shipped template if it actually names sources; an empty
    # log_dirs would hand the user a viewer with nothing in it.
    if template.exists() and _names_sources(template):
        try:
            shutil.copyfile(template, target)
            return target
        except OSError:
            pass

    try:
        target.write_text(DEFAULT_SETTINGS_TEMPLATE, encoding="utf-8")
        return target
    except OSError:
        return template if template.exists() else None


def _names_sources(path: Path) -> bool:
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except (OSError, configparser.Error):
        return False
    if CONFIG_SECTION not in parser:
        return False
    return bool(parser[CONFIG_SECTION].get("log_dirs", "").strip())


def get_config_file() -> Optional[Path]:
    user = ensure_user_settings_file()
    if user:
        return user
    repo = repo_config_path()
    return repo if repo.exists() else None


class _EmptySection:
    """Stand-in for a missing ``[log_viewer]`` section, so reads return defaults."""

    @staticmethod
    def get(_option: str, fallback=None):
        return fallback

    @staticmethod
    def getint(_option: str, fallback=None):
        return fallback

    @staticmethod
    def getboolean(_option: str, fallback=None):
        return fallback


_EMPTY = _EmptySection()


def _clamp(value: int, option: str) -> int:
    default, minimum, maximum = _LIMITS[option]
    if not isinstance(value, int):
        return default
    return max(minimum, min(value, maximum))


def _read_int(section, option: str) -> int:
    default = _LIMITS[option][0]
    try:
        raw = section.getint(option, fallback=default)
    except (ValueError, AttributeError):
        return default
    return _clamp(raw if raw is not None else default, option)


def _read_bool(section, option: str, default: bool) -> bool:
    try:
        value = section.getboolean(option, fallback=default)
    except (ValueError, AttributeError):
        return default
    return default if value is None else bool(value)


def _read_globs(section, option: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = section.get(option, fallback=None)
    if raw is None:
        return default
    parts = tuple(piece.strip() for piece in raw.split(",") if piece.strip())
    # An explicitly empty value means "no filtering", which is meaningful for
    # include_globs and must not silently fall back to the default.
    return parts


def parse_log_dirs(raw: str) -> list[Path]:
    """Turn the comma-separated ``log_dirs`` value into absolute paths.

    Relative entries are resolved against the working directory rather than
    discarded, so ``log_dirs = ./logs`` behaves the way it reads.
    """

    entries: list[Path] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        text = piece.strip().strip('"').strip("'")
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            path = path.resolve(strict=False)
        except OSError:
            pass
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        entries.append(path)
    return entries


def load_config(path: Optional[Path] = None) -> LogConfig:
    """Load configuration, falling back to defaults for anything unusable."""

    parser = configparser.ConfigParser()
    resolved = path if path is not None else get_config_file()
    if resolved:
        try:
            parser.read(resolved)
        except (OSError, configparser.Error):
            pass

    section = parser[CONFIG_SECTION] if CONFIG_SECTION in parser else _EMPTY

    log_dirs = parse_log_dirs(section.get("log_dirs", fallback="") or "")

    discovery = DiscoverySettings(
        include_globs=_read_globs(section, "include_globs", ()),
        exclude_globs=_read_globs(section, "exclude_globs", DEFAULT_EXCLUDE_GLOBS),
        follow_symlinks=_read_bool(section, "follow_symlinks", False),
        skip_binary=_read_bool(section, "skip_binary", True),
        max_files=_read_int(section, "max_files"),
    )

    config = LogConfig(
        log_dirs=log_dirs,
        discovery=discovery,
        max_buffer_lines=_read_int(section, "max_buffer_lines"),
        default_show_lines=_read_int(section, "default_show_lines"),
        refresh_hz=_read_int(section, "refresh_hz"),
        min_show_lines=_read_int(section, "min_show_lines"),
        show_step=_read_int(section, "show_step"),
        csv_max_rows=_read_int(section, "csv_max_rows"),
        csv_max_cols=_read_int(section, "csv_max_cols"),
        tree_width=_read_int(section, "tree_width"),
        clipboard_max_bytes=_read_int(section, "clipboard_max_bytes"),
        watch_rate_limit=_read_int(section, "watch_rate_limit"),
        watch_bell=_read_bool(section, "watch_bell", False),
    )

    # default_show_lines must not exceed what the buffer can hold.
    if config.default_show_lines > config.max_buffer_lines:
        config.default_show_lines = config.max_buffer_lines
    if config.min_show_lines > config.max_buffer_lines:
        config.min_show_lines = config.max_buffer_lines
    return config
