"""Shared test fixtures.

Every test runs against a throwaway HOME and XDG config directory. Without
this the suite reads and *writes* the developer's real
``~/.cache/clv/session.json`` and ``~/.config/clv/settings.conf``, which both
leaks state between tests and mutates the machine running them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def plugin_tree_is_not_a_scratch_directory():
    """Fail the run if any test wrote into ``clv/plugins/``.

    Three tests used to drop ``.py`` files into the live plugin packages to
    exercise discovery, unlinking them in a ``finally``. The ``.py`` files went
    away; the ``.pyc`` files in ``__pycache__`` did not, and an interrupted run
    left the source behind as well — a stray plugin that then loads in every
    later run and in the developer's own viewer.

    Tests reach the loader through a temp directory placed on the package's own
    ``__path__`` instead (the ``drop_in`` fixture in ``test_plugins.py``), which
    exercises the same code path without touching the tree. This is what stops
    the old pattern coming back.
    """

    root = Path(__file__).resolve().parent.parent / "clv" / "plugins"

    def snapshot() -> set[str]:
        return {
            str(path.relative_to(root))
            for path in root.rglob("*")
            if "__pycache__" not in path.parts
        }

    before = snapshot()
    yield
    added = snapshot() - before
    assert not added, (
        "a test wrote into the clv/plugins/ source tree: "
        + ", ".join(sorted(added))
        + " — use the drop_in fixture instead"
    )


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
