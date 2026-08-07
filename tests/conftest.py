"""Shared test fixtures.

Every test runs against a throwaway HOME and XDG config directory. Without
this the suite reads and *writes* the developer's real
``~/.cache/clv/session.json`` and ``~/.config/clv/settings.conf``, which both
leaks state between tests and mutates the machine running them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path_factory, monkeypatch):
    """Point HOME, XDG_CONFIG_HOME and the log source at a temp directory."""

    root = tmp_path_factory.mktemp("clv-home")
    home = root / "home"
    config_home = root / "config"
    logs = root / "logs"
    for directory in (home, config_home, logs):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    # Pre-create the settings file so discovery never falls back to the
    # shipped default of /var/log and walks the real machine.
    settings_dir = config_home / "clv"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.conf").write_text(
        f"[log_viewer]\nlog_dirs = {logs}\nmax_buffer_lines = 5000\n"
        "default_show_lines = 500\nrefresh_hz = 2\n",
        encoding="utf-8",
    )

    yield root
