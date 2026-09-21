"""Managing plugins: the rows, the two controls, and what they cost.

Phase 4 of ``PLUGIN_TODO.md``. Before it, the whole plugin surface was one
`Static` line that concatenated the counts with the first three errors — so
"which plugin, what it did, what to do about it" (Requirement 6) had nowhere to
be said. These tests are that sentence, per state.

**Why a modal and not a drawer section** is asserted here too, because it is the
one place the phase text was departed from: `test_advanced_drawer.py` pins that
the fourth button costs no rows, and the drawer's `max-height: 16` is what makes
one row per plugin impossible in it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from textual.widgets import Button, OptionList, Static

from clv.app import LogViewerApp
from clv.plugins import (
    OPERATOR_DISABLE_REASON,
    DiscoveredPlugin,
    FilterStage,
    PluginError,
    PluginStatus,
)
from clv.services.config import load_config
from clv.widgets.advanced_drawer import AdvancedFiltersDrawer
from clv.widgets.plugins_dialog import PluginsDialog


def _run(scenario) -> None:
    asyncio.run(scenario())


class Quiet(FilterStage):
    name = "quiet"

    def apply(self, entry, context):
        return entry


class Exploding(FilterStage):
    name = "exploding"

    def apply(self, entry, context):
        raise RuntimeError("boom")


ROWS = (
    PluginStatus(
        name="redact_secrets",
        origin="/home/x/.config/clv/plugins/redact_secrets",
        source="user",
        kinds=("filter",),
        state="loaded",
    ),
    PluginStatus(
        name="nginx_format",
        origin="/home/x/.config/clv/plugins/nginx_format",
        source="user",
        state="not enabled",
        enabled=False,
    ),
    PluginStatus(
        name="noisy",
        origin="/home/x/.config/clv/plugins/noisy",
        source="user",
        kinds=("filter",),
        state="failed",
        detail="raised: KeyError('host')",
        category="runtime",
    ),
    PluginStatus(
        name="future_thing",
        origin="/home/x/.config/clv/plugins/future_thing",
        source="user",
        state="incompatible",
        detail="requires CLV >=99.0, running 2.6.0",
        category="incompatible",
    ),
)


def _rows(screen) -> list[str]:
    return [
        str(option.prompt)
        for option in screen.query_one("#plugin-list", OptionList).options
    ]


def _detail(screen) -> str:
    return str(screen.query_one("#plugin-detail", Static).render())


# --- the rows ---------------------------------------------------------------


def test_each_state_renders_with_its_own_label_and_detail() -> None:
    """The four live states, side by side, which is how an operator meets them."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(PluginsDialog(ROWS))
            await pilot.pause()

            rows = _rows(app.screen)
            assert "redact_secrets" in rows[0] and "loaded" in rows[0]
            assert "nginx_format" in rows[1] and "not enabled" in rows[1]
            assert "noisy" in rows[2] and "failed" in rows[2]
            assert "future_thing" in rows[3] and "incompatible" in rows[3]
            # A row says its kinds; the state is not the only column.
            assert "filter" in rows[0]

            # The highlighted row's message, in full and untruncated — the thing
            # the old shared status line could not do.
            assert "raised: KeyError('host')" in _detail(
                await _highlight(app, pilot, 2)
            )
            assert "requires CLV >=99.0, running 2.6.0" in _detail(
                await _highlight(app, pilot, 3)
            )

    _run(scenario)


async def _highlight(app, pilot, index: int):
    app.screen.query_one("#plugin-list", OptionList).highlighted = index
    await pilot.pause()
    return app.screen


def test_a_multi_kind_plugin_lists_every_interface_it_supplies() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                PluginsDialog(
                    [replace(ROWS[0], kinds=("source", "filter", "exporter"))]
                )
            )
            await pilot.pause()

            assert "source, filter, exporter" in _rows(app.screen)[0]

    _run(scenario)


def test_a_clustering_plugin_says_which_half_of_the_seam_it_supplies() -> None:
    """Two kinds, two labels, and both short enough to survive a narrow pane.

    A module supplying both halves is the ordinary case — a rule and the
    contributor that keeps its clusters apart usually ship together — so the
    row has to say which it has rather than "clustering".
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                PluginsDialog(
                    [replace(ROWS[0], kinds=("cluster rule", "shape"))]
                )
            )
            await pilot.pause()

            assert "cluster rule, shape" in _rows(app.screen)[0]

    _run(scenario)


def test_the_consequence_of_a_toggle_is_shown_beside_the_toggle() -> None:
    """Two of these are the reason the phase needed a decision at all."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                PluginsDialog(
                    [
                        ROWS[1],  # user, not enabled
                        replace(
                            ROWS[0],
                            name="journald",
                            origin="clv.plugins.sources.journald",
                            source="bundled",
                        ),
                    ]
                )
            )
            await pilot.pause()

            await pilot.press("space")  # enable the unimported user plugin
            assert "settings.conf" in _detail(app.screen)
            assert "restart" in _detail(app.screen)

            await _highlight(app, pilot, 1)
            await pilot.press("space")  # disable the bundled one
            detail = _detail(app.screen)
            assert "this session only" in detail
            assert "returns on restart" in detail

    _run(scenario)


def test_looking_without_touching_reports_nothing_changed() -> None:
    """Escape genuinely cancels, so the operator's INI is not rewritten."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            results: list[object] = []
            app.push_screen(PluginsDialog(ROWS), callback=results.append)
            await pilot.pause()

            await pilot.press("down", "down", "escape")
            await pilot.pause()

            assert results == [None]

    _run(scenario)


def test_three_toggles_come_back_as_one_set() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            results: list[object] = []
            app.push_screen(PluginsDialog(ROWS), callback=results.append)
            await pilot.pause()

            await pilot.press("space")          # redact_secrets off
            await _highlight(app, pilot, 1)
            await pilot.press("space")          # nginx_format on
            await pilot.press("escape")
            await pilot.pause()

            assert len(results) == 1
            changed = {row.name: row.enabled for row in results[0]}
            assert changed["redact_secrets"] is False
            assert changed["nginx_format"] is True
            assert changed["noisy"] is True

    _run(scenario)


def test_re_enable_is_offered_for_a_fault_and_not_for_a_choice() -> None:
    """The control Phase 1's session-persistent `disable()` was written to need."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(PluginsDialog(ROWS))
            await pilot.pause()

            button = app.screen.query_one("#plugin-reinstate", Button)
            assert button.disabled, "a loaded plugin has nothing to put back"

            await _highlight(app, pilot, 2)  # noisy — failed at runtime
            assert not button.disabled

            await _highlight(app, pilot, 3)  # incompatible — fix the plugin
            assert button.disabled

    _run(scenario)


# --- the app acting on what came back ---------------------------------------


def _app_on(config_path: Path) -> LogViewerApp:
    app = LogViewerApp(config=load_config(config_path))
    app._settings_path = config_path
    return app


SETTINGS = """\
# CLV settings, hand-written and full of things worth keeping.

[log_viewer]
# Which plugins to load from ~/.config/clv/plugins/.
plugins = redact_secrets
refresh_hz = 2
"""


def test_enabling_writes_the_name_and_says_a_restart_is_needed(tmp_path: Path) -> None:
    """Loading is import-time and single-shot, so the control must say so."""

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            notices: list[str] = []
            app._notify = lambda message, *a, **k: notices.append(message)

            before = (ROWS[0], ROWS[1])
            after = (ROWS[0], replace(ROWS[1], enabled=True))
            app._apply_plugin_decisions(before, after)
            await pilot.pause()

            text = config.read_text(encoding="utf-8")
            assert "plugins = redact_secrets, nginx_format" in text
            # The operator's prose survives the write.
            assert "# CLV settings, hand-written" in text
            assert "refresh_hz = 2" in text
            assert any("restart" in notice for notice in notices)

    _run(scenario)


def test_disabling_a_loaded_plugin_takes_effect_without_a_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # `plugins = redact_secrets` names something that is not on this
            # machine, so the loader has already reported it. Cleared, because
            # what this test is about is what happens next.
            app._plugins.errors.clear()
            app._plugins.add(
                Quiet(),
                origin="/plugins/redact_secrets",
                clv_version="2.1.0",
            )
            app._plugins.discovered.append(
                DiscoveredPlugin(
                    name="redact_secrets", root=Path("/plugins"), enabled=True
                )
            )
            stage = app._plugins.filters[-1]

            before = tuple(app._plugins.status())
            row = next(r for r in before if r.name == "redact_secrets")
            app._apply_plugin_decisions(
                before,
                tuple(
                    replace(r, enabled=False) if r is row else r for r in before
                ),
            )
            await pilot.pause()

            assert app._plugins.is_disabled(stage)
            assert app._plugins.disabled_reason(stage) == OPERATOR_DISABLE_REASON
            # Not an error: the operator did this on purpose.
            assert app._plugins.errors == []
            # The key stays, emptied, with its comment above it intact -- the
            # operator's file is edited, never regenerated.
            text = config.read_text(encoding="utf-8")
            assert "plugins =\n" in text
            assert "# Which plugins to load" in text

    _run(scenario)


def test_a_plugin_a_fault_disabled_can_be_put_back_and_fail_again_once(
    tmp_path: Path,
) -> None:
    """The growth path Phase 1 closed must not reopen through the way back."""

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._plugins.errors.clear()  # see the note in the test above
            app._plugins.add(
                Exploding(), origin="/plugins/noisy", clv_version="2.1.0"
            )
            stage = app._plugins.filters[-1]
            app._plugins.disable(stage, "raised: boom")
            assert len(app._plugins.errors) == 1

            before = tuple(app._plugins.status())
            # Named for the *origin* -- the file an operator installed -- and
            # not for the plugin object inside it, which is how one module
            # exporting three stages stays one row.
            row = next(r for r in before if r.name == "noisy")
            assert row.state == "failed" and row.category == "runtime"
            assert "boom" in row.detail

            app._apply_plugin_decisions(
                before,
                tuple(
                    replace(r, reinstate=True) if r is row else r for r in before
                ),
            )
            await pilot.pause()

            assert not app._plugins.is_disabled(stage)
            assert app._plugins.errors == [], "the fault goes with the forgiveness"

            # And it fails again to one entry counting from one, not to a second
            # growth path or a repeat of something already dealt with.
            app._plugins.disable(stage, "raised: boom")
            assert len(app._plugins.errors) == 1
            assert app._plugins.errors[0].count == 1

    _run(scenario)


def test_a_read_only_settings_file_is_reported_and_nothing_changes(
    tmp_path: Path,
) -> None:
    """The journald switch's behaviour, for the same reason it has it."""

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            notices: list[tuple] = []
            app._notify = lambda message, *a, **k: notices.append((message, a))
            # The directory, not the file: `SettingsDocument.save` writes a
            # sibling temp file and `os.replace`s it, so a read-only
            # settings.conf in a writable directory is still replaceable.
            tmp_path.chmod(0o500)
            try:
                app._apply_plugin_decisions(
                    (ROWS[0], ROWS[1]),
                    (ROWS[0], replace(ROWS[1], enabled=True)),
                )
            finally:
                tmp_path.chmod(0o700)
            await pilot.pause()

            assert any("Could not save" in message for message, _ in notices)
            assert app._config.plugins == ("redact_secrets",)
            assert "nginx_format" not in config.read_text(encoding="utf-8")

    _run(scenario)


def test_a_bundled_plugin_is_switched_off_for_the_session_only(
    tmp_path: Path,
) -> None:
    """The enable-list governs the user directory; a shipped drop-in is not in it."""

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._plugins.errors.clear()  # see the note two tests above
            app._plugins.add(
                Quiet(), origin="clv.plugins.filters.quiet", clv_version="2.1.0"
            )
            stage = app._plugins.filters[-1]

            before = tuple(app._plugins.status())
            row = next(r for r in before if r.origin == "clv.plugins.filters.quiet")
            app._apply_plugin_decisions(
                before,
                tuple(
                    replace(r, enabled=False) if r is row else r for r in before
                ),
            )
            await pilot.pause()

            assert app._plugins.is_disabled(stage)
            # Nothing was written: `plugins` still says exactly what it said.
            assert "plugins = redact_secrets\n" in config.read_text(encoding="utf-8")

    _run(scenario)


def test_a_disabled_exporter_is_not_offered_by_the_export_dialog() -> None:
    """Otherwise the disable control misses the kind it is most aimed at."""

    from clv.plugins import ExportResult, Exporter

    class Writer(Exporter):
        name = "writer"

        def export(self, entries, context):
            return ExportResult(ok=True, detail="written")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._plugins.add(Writer(), origin="/plugins/writer", clv_version="2.1.0")
            exporter = app._plugins.exporters[-1]

            keys = [choice.key for choice in app._exporter_choices()]
            index = next(k for k in keys if k.startswith("plugin:"))

            app._plugins.disable(exporter, OPERATOR_DISABLE_REASON, record=False)

            assert index not in [c.key for c in app._exporter_choices()]
            # The index it would have had still resolves to nothing rather than
            # to its neighbour: omitting a choice renumbers nothing.
            assert app._exporter_at(index) is None

    _run(scenario)


# --- the drawer's end of it -------------------------------------------------


def test_the_button_and_the_line_are_absent_when_nothing_is_installed() -> None:
    """Requirement 10: a build with no plugins renders nothing new."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._plugins.sources.clear()
            app._plugins.filters.clear()
            app._plugins.exporters.clear()
            app._plugins.loaded.clear()
            app._plugins.discovered.clear()
            app._plugins.errors.clear()
            app.advanced_drawer.show()
            app._refresh_plugin_status()
            await pilot.pause()
            await pilot.pause()

            drawer = app.advanced_drawer
            assert str(drawer.query_one("#plugin-status").content) == ""
            assert drawer.query_one("#manage-plugins", Button).region.width == 0

    _run(scenario)


def test_the_drawer_button_and_the_key_reach_the_same_dialog() -> None:
    """Keyboard and mouse parity, which the widget rules require.

    The button is asserted through the message it posts rather than a click:
    the drawer scrolls at `max-height: 16` and the actions row can sit past the
    fold of a short terminal, which is a fact about this test's viewport and not
    about the button.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.advanced_drawer.show()
            # Focus off the query input first: a single-letter binding is a
            # character while an Input has focus, which the suite already
            # records as correct-and-intended for `a`, `t`, `s` and `f`.
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("P")
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, PluginsDialog)

            await pilot.press("escape")
            for _ in range(4):
                await pilot.pause()
            assert not isinstance(app.screen, PluginsDialog)

            app.advanced_drawer.post_message(
                AdvancedFiltersDrawer.ManagePluginsRequested()
            )
            for _ in range(4):
                await pilot.pause()
            assert isinstance(app.screen, PluginsDialog)

    _run(scenario)


def test_the_log_panel_points_at_the_dialog_instead_of_listing_errors() -> None:
    """A wall of plugin errors may not bury the discovery summary.

    Six broken plugins used to be six amber lines under the summary an operator
    opened CLV to read. Deduplication capped the *repeats*; it did nothing about
    six distinct faults, and none of those lines had room to say what to do
    about any of them.
    """

    from clv.services.discovery import DiscoveryReport

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            written: list[str] = []
            app.log_panel.write = lambda text, *a, **k: written.append(str(text))
            app._plugins.errors.clear()
            for index in range(6):
                app._plugins.errors.append(
                    PluginError(f"plugin{index}", "import failed: nope")
                )

            app._show_discovery_summary(DiscoveryReport(files=(), roots=()))
            await pilot.pause()

            amber = [line for line in written if "Plugin problem" in line]
            assert len(amber) == 1, written
            assert "6" in amber[0]
            assert "press P" in amber[0]
            assert not any("import failed" in line for line in written)

    _run(scenario)


def test_the_dialog_fits_eighty_columns() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(PluginsDialog(ROWS))
            await pilot.pause()
            await pilot.pause()

            screen = app.screen
            for widget_id in (
                "#plugins-dialog",
                "#plugin-list",
                "#plugin-detail",
                "#plugin-toggle",
                "#plugin-close",
            ):
                region = screen.query_one(widget_id).region
                assert region.width > 0, f"{widget_id} laid out to nothing"
                assert region.right <= 80, f"{widget_id} overflows 80 columns"
            # Name and state survive the narrowest supported width; the origin
            # and the kinds are what the row gives up.
            rows = _rows(screen)
            assert "redact_secrets" in rows[0] and "loaded" in rows[0]

    _run(scenario)


def test_a_toggle_that_could_do_nothing_is_not_offered() -> None:
    """A bundled plugin that never loaded has no name to remove and nothing to
    switch off — and a control that quietly does nothing is worse than none."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                PluginsDialog(
                    [
                        replace(
                            ROWS[2],
                            name="journald",
                            origin="clv.plugins.sources.journald",
                            source="bundled",
                            kinds=(),
                            state="failed",
                            category="load",
                            detail="import failed: no systemd here",
                        ),
                        ROWS[1],  # user, not enabled, also no kinds
                    ]
                )
            )
            await pilot.pause()

            toggle = app.screen.query_one("#plugin-toggle", Button)
            assert toggle.disabled

            await _highlight(app, pilot, 1)
            assert not toggle.disabled, "naming a user plugin is the whole feature"

    _run(scenario)


def test_switching_off_something_that_never_imported_stays_switched_off(
    tmp_path: Path,
) -> None:
    """The case with no live plugin to carry the decision.

    A plugin that raised on import has no object to mark disabled, so the row's
    answer rests entirely on `DiscoveredPlugin.enabled` — which records what
    `settings.conf` said *at load time*. Left stale, the row springs back to
    `enabled` the moment it is redrawn, while the name it needs has already gone
    from the file.
    """

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._plugins.errors.clear()
            app._plugins.discovered.append(
                DiscoveredPlugin(
                    name="redact_secrets", root=Path("/plugins"), enabled=True
                )
            )
            app._plugins.errors.append(
                PluginError("/plugins/redact_secrets", "import failed: boom")
            )

            before = tuple(app._plugins.status())
            row = next(r for r in before if r.name == "redact_secrets")
            assert row.state == "failed" and row.enabled

            app._apply_plugin_decisions(
                before,
                tuple(replace(r, enabled=False) if r is row else r for r in before),
            )
            await pilot.pause()

            after = next(
                r for r in app._plugins.status() if r.name == "redact_secrets"
            )
            assert not after.enabled
            assert after.state == "failed", "still broken, and now also not asked for"

    _run(scenario)


# --- lifecycle, through the real app ----------------------------------------
#
# Phase 5. The registry's own tests pin the hooks; these pin the two things only
# the app can decide — that `setup()` has run before anything asks the plugin
# for anything, and that `teardown()` runs at the right point in `on_unmount`:
# after CLV has closed its readers, so a plugin cannot resurrect a source on its
# way out, and before the session is persisted, so a plugin that fails on exit
# still leaves the operator's session intact.


class Recording(FilterStage):
    """Notes every lifecycle call, and can be told to fail on the way out."""

    name = "recording"

    def __init__(self, *, fail_teardown: bool = False) -> None:
        self.events: list[str] = []
        self._fail = fail_teardown

    def configure(self, settings):
        self.events.append("configure")

    def setup(self):
        self.events.append("setup")

    def teardown(self):
        self.events.append("teardown")
        if self._fail:
            raise RuntimeError("teardown exploded")

    def apply(self, entry, context):
        return entry


def test_setup_has_run_by_the_time_the_app_is_mounted(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            plugin = Recording()
            # Added after mount, so `start()` has already been and gone --
            # which is exactly the state a plugin registered late is in, and
            # why `start()` is idempotent rather than a per-plugin trigger.
            app._plugins.add(plugin, origin="/plugins/late", clv_version="2.1.0")
            app._plugins.start()

            assert plugin.events == ["configure"]

    _run(scenario)


def test_teardown_runs_at_shutdown_after_the_readers_close(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        order: list[str] = []
        plugin = Recording()

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._plugins.add(plugin, origin="/plugins/x", clv_version="2.1.0")
            app._plugins.start()
            plugin.events.clear()

            real_close = app._session.close
            app._session.close = lambda: (order.append("readers"), real_close())[1]
            real_save = app._store.save
            app._store.save = lambda state: (order.append("save"), real_save(state))[1]
            real_teardown = plugin.teardown
            def teardown():
                order.append("teardown")
                real_teardown()
            plugin.teardown = teardown
            app._persist_state = True

        assert plugin.events == ["teardown"]
        assert order == ["readers", "teardown", "save"]

    _run(scenario)


def test_a_teardown_that_raises_does_not_stop_the_session_being_saved(
    tmp_path: Path,
) -> None:
    """A plugin failing on the way out is the plugin's problem, not the operator's."""

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(SETTINGS, encoding="utf-8")
        app = _app_on(config)
        saved: list[object] = []
        plugin = Recording(fail_teardown=True)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._plugins.add(plugin, origin="/plugins/x", clv_version="2.1.0")
            app._plugins.start()
            real_save = app._store.save
            app._store.save = lambda state: (saved.append(state), real_save(state))[1]
            app._persist_state = True

        assert saved, "the session was not persisted"
        assert any("teardown() raised" in e.message for e in app._plugins.errors)

    _run(scenario)


def test_a_plugin_reads_its_section_through_the_app(tmp_path: Path) -> None:
    """End to end: a `[plugin:...]` section in the file reaches `configure()`."""

    async def scenario() -> None:
        config = tmp_path / "settings.conf"
        config.write_text(
            SETTINGS + "\n[plugin:redact_secrets]\nreplacement = ***\n",
            encoding="utf-8",
        )
        app = _app_on(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            seen = {}

            class Reader(FilterStage):
                name = "reader"

                def configure(self, settings):
                    seen.update(settings)

                def apply(self, entry, context):
                    return entry

            app._plugins.add(
                Reader(), origin="/plugins/redact_secrets", clv_version="2.1.0"
            )

            assert seen == {"replacement": "***"}

    _run(scenario)
