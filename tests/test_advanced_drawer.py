"""Advanced drawer: real controls bound to real discovery and search state."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Input, Switch

from clv.app import LogViewerApp
from clv.services.discovery import DiscoverySettings
from clv.widgets.advanced_drawer import AdvancedFiltersDrawer, AdvancedSettings


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.changes: list[AdvancedFiltersDrawer.SettingsChanged] = []
        self.rescans = 0

    def compose(self) -> ComposeResult:
        self.drawer = AdvancedFiltersDrawer()
        self.drawer.show()
        yield self.drawer

    def on_advanced_filters_drawer_settings_changed(self, m) -> None:
        self.changes.append(m)

    def on_advanced_filters_drawer_rescan_requested(self, _m) -> None:
        self.rescans += 1


def test_drawer_is_hidden_by_default() -> None:
    drawer = AdvancedFiltersDrawer()

    assert "-hidden" in drawer.classes
    assert not drawer.visible


def test_toggle_flips_visibility() -> None:
    drawer = AdvancedFiltersDrawer()

    assert drawer.toggle() is True
    assert drawer.visible
    assert drawer.toggle() is False
    assert "-hidden" in drawer.classes


def test_settings_map_onto_discovery_settings() -> None:
    settings = AdvancedSettings(
        include_globs="*.log, *.txt",
        exclude_globs="*.gz",
        follow_symlinks=True,
        skip_binary=False,
    )

    discovery = settings.to_discovery(DiscoverySettings())

    assert discovery.include_globs == ("*.log", "*.txt")
    assert discovery.exclude_globs == ("*.gz",)
    assert discovery.follow_symlinks is True
    assert discovery.skip_binary is False


def test_only_discovery_fields_trigger_a_rescan() -> None:
    base = AdvancedSettings()

    assert base.affects_discovery(AdvancedSettings(include_globs="*.log"))
    assert base.affects_discovery(AdvancedSettings(follow_symlinks=True))
    # Search options change the view, not the file list.
    assert not base.affects_discovery(AdvancedSettings(invert_match=True))
    assert not base.affects_discovery(AdvancedSettings(case_sensitive=True))


def test_controls_emit_settings_changes() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()

            app.drawer.query_one("#include-globs", Input).value = "*.log"
            await pilot.pause()
            assert app.drawer.settings.include_globs == "*.log"
            assert app.changes[-1].needs_rescan is True

            app.drawer.query_one("#invert-match", Switch).value = True
            await pilot.pause()
            assert app.drawer.settings.invert_match is True
            assert app.changes[-1].needs_rescan is False

            app.drawer.query_one("#max-buffer-lines", Input).value = "1234"
            await pilot.pause()
            assert app.drawer.settings.max_buffer_lines == 1234

            # Partial numeric input is ignored rather than clamped to nonsense.
            app.drawer.query_one("#max-buffer-lines", Input).value = ""
            await pilot.pause()
            assert app.drawer.settings.max_buffer_lines == 1234

    asyncio.run(scenario())


def test_rescan_button_requests_a_rescan() -> None:
    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app.drawer.query_one("#rescan-sources").press()
            await pilot.pause()
            assert app.rescans == 1

    asyncio.run(scenario())


def test_drawer_include_glob_actually_narrows_discovery(tmp_path: Path) -> None:
    """End to end: editing the drawer changes which files the tree lists."""

    root = tmp_path / "logs"
    root.mkdir()
    (root / "app.log").write_text("a\n", encoding="utf-8")
    (root / "notes.txt").write_text("b\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            from clv.services import SourceManager

            app._source_manager = SourceManager([root], [])
            await app._rescan()
            await pilot.pause()
            assert app._report is not None
            assert app._report.file_count == 2

            app.advanced_drawer.query_one("#include-globs", Input).value = "*.log"
            await pilot.pause()
            # The drawer change queues a rescan worker; let it finish.
            await app._rescan()
            await pilot.pause()

            names = {item.path.name for item in app._report.files}
            assert names == {"app.log"}

    asyncio.run(scenario())


def test_search_options_reach_the_filter_spec() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()

            app.advanced_drawer.query_one("#invert-match", Switch).value = True
            app.advanced_drawer.query_one("#case-sensitive", Switch).value = True
            app.advanced_drawer.query_one("#use-regex", Switch).value = False
            await pilot.pause()

            spec = app._filter_spec()
            assert spec.invert is True
            assert spec.case_sensitive is True
            assert spec.regex is False

    asyncio.run(scenario())
