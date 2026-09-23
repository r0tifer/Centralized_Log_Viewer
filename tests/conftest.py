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


@pytest.fixture(autouse=True)
def query_plugins_are_not_shared_between_tests():
    """Reset the query grammar between tests.

    ``clv.services.query`` holds its operators and computed fields in module
    state, installed by the app at mount and rebuilt whenever the plugin
    generation moves. That is the right design — ``FilterSpec`` is frozen,
    slotted and hashed into the render cache key, so a registry of live
    callables cannot ride on it — but it means one test's ``~`` operator is
    still installed when the next test runs.

    A leak there is not a noisy failure but a quiet one: a leftover operator
    changes what ``_TERM_RE`` matches, so a test asserting that a plain regex is
    passed through untouched could pass or fail on the order the files ran in.
    """

    yield
    from clv.services.query import install_query_plugins

    install_query_plugins()


@pytest.fixture(autouse=True)
def cluster_plugins_are_not_shared_between_tests():
    """Reset the installed cluster rules and shape contributors between tests.

    The third of the same argument, and the one with the longest reach. A
    leaked rule does not merely stay registered: ``install_cluster_plugins``
    clears the memoised shape cache, so every later test that clusters anything
    would be shaping its lines through a rule from a test that has already
    finished — and clustering that is subtly wrong reads exactly like
    clustering that works.
    """

    yield
    from clv.services.clustering import install_cluster_plugins

    install_cluster_plugins()


@pytest.fixture(autouse=True)
def timeline_plugins_are_not_shared_between_tests():
    """Reset the installed annotation providers and metric between tests.

    The fourth of the same argument, and it leaks in two directions at once. A
    leftover metric changes what every later bar is *scaled by* and what its
    caption claims to be showing; a leftover provider leaves marks — and the
    triples behind them — in the module's one-entry annotation cache, which is
    keyed on the window and would happily serve a later test whose grid covers
    the same seconds. ``install_timeline_plugins`` clears that cache, which is
    exactly why putting the module back is enough.
    """

    yield
    from clv.services.timeline import install_timeline_plugins

    install_timeline_plugins()


@pytest.fixture(autouse=True)
def watch_plugins_are_not_shared_between_tests():
    """Reset the installed watch matchers and sinks between tests.

    The same argument as the query fixture above, one seam along.
    ``clv.services.watch`` holds its matchers and sinks in module state for the
    same reason ``query`` does — a ``WatchRule`` is frozen and persisted, so a
    registry of live callables cannot ride on it — and a leak here fails just as
    quietly: a leftover ``burst`` matcher makes ``matcher_kinds()`` report two
    kinds, so the rules dialog composes a control that the test asserting a
    build with no watch plugins renders nothing new was written to catch.
    """

    yield
    from clv.services.watch import install_watch_plugins

    install_watch_plugins()
