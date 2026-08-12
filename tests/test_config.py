"""Settings resolution, validation and the packaged-app template lookup."""

from __future__ import annotations

import sys
from pathlib import Path

from clv.services.config import (
    DEFAULT_SETTINGS_TEMPLATE,
    LogConfig,
    bundled_config_path,
    ensure_user_settings_file,
    load_config,
    parse_log_dirs,
    user_config_path,
)
from clv.services.discovery import DEFAULT_EXCLUDE_GLOBS


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# --- template location ------------------------------------------------------


def test_bundled_template_is_found_via_meipass_when_frozen(tmp_path, monkeypatch) -> None:
    """PyInstaller extracts data files to sys._MEIPASS, not beside the source."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundled_config_path() == tmp_path / "settings.conf"


def test_bundled_template_falls_back_to_the_source_tree(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    path = bundled_config_path()

    assert path.name == "settings.conf"
    # Development checkout: settings.conf sits beside the clv package.
    assert (path.parent / "clv").is_dir()


def test_shipped_template_is_usable(monkeypatch) -> None:
    """The real settings.conf must name sources, or first run is empty."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    template = bundled_config_path()

    assert template.exists()
    config = load_config(template)
    assert config.log_dirs, "shipped settings.conf must name at least one source"


# --- first run --------------------------------------------------------------


def test_first_run_creates_a_user_settings_file() -> None:
    target = user_config_path()
    if target.exists():
        target.unlink()

    created = ensure_user_settings_file()

    assert created == target
    assert target.exists()
    assert load_config(target).log_dirs


def test_a_template_naming_no_sources_is_not_copied(tmp_path, monkeypatch) -> None:
    """An empty log_dirs would hand the user a viewer with nothing in it."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    _write(tmp_path / "settings.conf", "[log_viewer]\nlog_dirs =\n")

    target = user_config_path()
    if target.exists():
        target.unlink()

    created = ensure_user_settings_file()

    assert created == target
    # Falls back to the built-in defaults rather than the empty template.
    assert created.read_text(encoding="utf-8") == DEFAULT_SETTINGS_TEMPLATE
    assert load_config(created).log_dirs


# --- parsing and validation -------------------------------------------------


def test_log_dirs_accepts_folders_files_and_relative_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()

    parsed = parse_log_dirs(" /var/log , rel , /srv/app.log , ")

    assert Path("/var/log") in parsed
    assert tmp_path / "rel" in parsed  # relative resolves against cwd
    assert Path("/srv/app.log") in parsed


def test_duplicate_log_dirs_are_collapsed() -> None:
    assert len(parse_log_dirs("/var/log, /var/log")) == 1


def test_values_are_clamped_not_trusted(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\nrefresh_hz = 9999\n"
            "max_buffer_lines = 0\ncsv_max_cols = -5\ntree_width = 1\n",
        )
    )

    assert config.refresh_hz <= 60
    assert config.max_buffer_lines >= 100
    assert config.csv_max_cols >= 1
    assert config.tree_width >= 20


def test_clipboard_cap_is_read_and_clamped(tmp_path) -> None:
    """A cap of zero would make `y` a no-op; a huge one would be dropped by tmux."""

    assert (
        load_config(
            _write(
                tmp_path / "settings.conf",
                "[log_viewer]\nlog_dirs = /var/log\nclipboard_max_bytes = 4096\n",
            )
        ).clipboard_max_bytes
        == 4096
    )

    clamped = load_config(
        _write(
            tmp_path / "low.conf",
            "[log_viewer]\nlog_dirs = /var/log\nclipboard_max_bytes = 0\n",
        )
    )
    assert clamped.clipboard_max_bytes >= 1024

    default = load_config(_write(tmp_path / "none.conf", "[log_viewer]\nlog_dirs = /var/log\n"))
    assert default.clipboard_max_bytes == LogConfig().clipboard_max_bytes


def test_garbage_values_fall_back_to_defaults(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\nrefresh_hz = banana\n"
            "follow_symlinks = maybe\n",
        )
    )

    assert config.refresh_hz == LogConfig().refresh_hz
    assert config.discovery.follow_symlinks is False


def test_a_missing_section_yields_defaults(tmp_path) -> None:
    config = load_config(_write(tmp_path / "settings.conf", "[something_else]\nx = 1\n"))

    assert config.log_dirs == []
    assert config.max_buffer_lines == LogConfig().max_buffer_lines


def test_unreadable_file_does_not_raise(tmp_path) -> None:
    assert isinstance(load_config(tmp_path / "does-not-exist.conf"), LogConfig)


def test_discovery_globs_round_trip(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n"
            "include_globs = *.log, syslog*\nexclude_globs = *.gz\n"
            "follow_symlinks = true\nskip_binary = false\n",
        )
    )

    assert config.discovery.include_globs == ("*.log", "syslog*")
    assert config.discovery.exclude_globs == ("*.gz",)
    assert config.discovery.follow_symlinks is True
    assert config.discovery.skip_binary is False


def test_omitted_excludes_keep_the_safe_defaults(tmp_path) -> None:
    config = load_config(
        _write(tmp_path / "settings.conf", "[log_viewer]\nlog_dirs = /var/log\n")
    )

    assert config.discovery.exclude_globs == DEFAULT_EXCLUDE_GLOBS
    # An empty include list means "every text file", which is the default.
    assert config.discovery.include_globs == ()


def test_show_lines_cannot_exceed_the_buffer(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n"
            "max_buffer_lines = 200\ndefault_show_lines = 5000\n",
        )
    )

    assert config.default_show_lines <= config.max_buffer_lines
