"""Advanced drawer: real controls bound to real discovery and search state."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Input, Static, Switch

from clv.app import LogViewerApp
from clv.services.discovery import DiscoverySettings
from clv.widgets.advanced_drawer import AdvancedFiltersDrawer, AdvancedSettings


def _run(scenario) -> None:
    asyncio.run(scenario())


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.changes: list[AdvancedFiltersDrawer.SettingsChanged] = []
        self.view_changes: list[AdvancedFiltersDrawer.ViewToggleChanged] = []
        self.rescans = 0

    def compose(self) -> ComposeResult:
        self.drawer = AdvancedFiltersDrawer()
        self.drawer.show()
        yield self.drawer

    def on_advanced_filters_drawer_settings_changed(self, m) -> None:
        self.changes.append(m)

    def on_advanced_filters_drawer_view_toggle_changed(self, m) -> None:
        self.view_changes.append(m)

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


# --- view toggles mirrored into the drawer ---------------------------------


def test_exactly_one_copy_of_the_view_toggles_is_visible() -> None:
    """They live in the query bar when it is wide, in the drawer when it is not."""

    async def scenario() -> None:
        from clv.app import BREAKPOINT_MERGE

        for width in (80, 120, BREAKPOINT_MERGE - 1, BREAKPOINT_MERGE, 190):
            app = LogViewerApp()
            async with app.run_test(size=(width, 34)) as pilot:
                await pilot.pause()
                await pilot.pause()
                app.advanced_drawer.show()
                await pilot.pause()

                in_bar = app.query_bar.query_one("#toggles").display
                in_drawer = app.advanced_drawer.query_one("#view-toggles").display

                assert in_bar != in_drawer, (
                    f"at {width} cols the toggles are "
                    f"{'in both places' if in_bar else 'nowhere'}"
                )
                assert in_bar is (width >= BREAKPOINT_MERGE)

    asyncio.run(scenario())


def test_drawer_toggles_drive_real_behaviour(tmp_path: Path) -> None:
    """Not just widget state: the log panel and the render must follow."""

    source = tmp_path / "a.log"
    source.write_text("2026-08-07 09:00:00 - INFO - one\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.pause()
            await pilot.pause()
            app._select_source(source)
            app.advanced_drawer.show()
            await pilot.pause()

            assert app.state.auto_scroll is True
            assert app.log_panel.auto_scroll is True

            app.advanced_drawer.query_one("#drawer-auto-scroll", Switch).value = False
            await pilot.pause()
            assert app.state.auto_scroll is False
            assert app.log_panel.auto_scroll is False
            # Auto-scroll governs the viewport, never ingestion.
            assert app._tail_timer is not None

            app.advanced_drawer.query_one("#drawer-structured", Switch).value = True
            await pilot.pause()
            assert app.state.pretty_rendering is True

    asyncio.run(scenario())


def test_the_two_copies_stay_in_step() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(190, 34)) as pilot:
            await pilot.pause()
            await pilot.pause()

            # Flip from the query bar (visible at this width).
            app.query_bar.query_one("#auto-scroll-toggle", Switch).value = False
            await pilot.pause()
            assert app.state.auto_scroll is False
            assert app.advanced_drawer.query_one("#drawer-auto-scroll", Switch).value is False

            # Flip from the drawer's mirror and it propagates back.
            app.advanced_drawer.query_one("#drawer-auto-scroll", Switch).value = True
            await pilot.pause()
            assert app.state.auto_scroll is True
            assert app.query_bar.query_one("#auto-scroll-toggle", Switch).value is True

    asyncio.run(scenario())


def test_view_state_survives_crossing_the_breakpoint() -> None:
    """Resizing swaps which control is shown; it must not change the setting."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.pause()
            await pilot.pause()
            app.advanced_drawer.show()
            await pilot.pause()

            app.advanced_drawer.query_one("#drawer-auto-scroll", Switch).value = False
            await pilot.pause()
            assert app.state.auto_scroll is False

            await pilot.resize_terminal(190, 34)
            await pilot.pause()
            await pilot.pause()
            assert app.state.auto_scroll is False
            assert app.query_bar.query_one("#auto-scroll-toggle", Switch).value is False

            await pilot.resize_terminal(80, 34)
            await pilot.pause()
            await pilot.pause()
            assert app.state.auto_scroll is False
            assert app.advanced_drawer.query_one("#drawer-auto-scroll", Switch).value is False

    asyncio.run(scenario())


def test_syncing_the_mirror_does_not_emit() -> None:
    """Echoing the app's value back must not read as a user action."""

    async def scenario() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            settings_before = len(app.changes)

            app.drawer.sync_view_toggles(auto_scroll=False, structured=True)
            await pilot.pause()

            assert app.drawer.query_one("#drawer-auto-scroll", Switch).value is False
            assert app.drawer.query_one("#drawer-structured", Switch).value is True
            # A sync is the app telling the drawer, not the user telling the app.
            assert app.view_changes == []
            assert len(app.changes) == settings_before

            # A real flip does report, so the suppression is not simply stuck on.
            app.drawer.query_one("#drawer-structured", Switch).value = False
            await pilot.pause()
            assert [(m.field, m.value) for m in app.view_changes] == [("structured", False)]

    asyncio.run(scenario())


def test_drawer_mirror_is_seeded_from_the_restored_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        from clv.storage import SessionState, StateStore

        store = StateStore(root=tmp_path)
        store.save(SessionState(auto_scroll=False, pretty_rendering=True))

        app = LogViewerApp(store=StateStore(root=tmp_path))
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.pause()
            await pilot.pause()
            app.advanced_drawer.show()
            await pilot.pause()

            assert app.advanced_drawer.query_one("#drawer-auto-scroll", Switch).value is False
            assert app.advanced_drawer.query_one("#drawer-structured", Switch).value is True

    asyncio.run(scenario())


def test_section_headings_are_actually_painted() -> None:
    """`height: 1` with `padding-bottom: 1` leaves zero rows for the text.

    The headings still laid out at the right coordinates, so geometry
    assertions passed while every one of them painted nothing.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            await pilot.pause()
            app.advanced_drawer.show()
            await pilot.pause()
            await pilot.pause()

            strips = app.screen._compositor.render_strips()
            painted = "\n".join(
                "".join(segment.text for segment in strip) for strip in strips
            )

            for heading in ("View", "Source discovery"):
                assert heading in painted, f"{heading!r} lays out but paints nothing"

            for label in app.advanced_drawer.query(".drawer-heading"):
                assert label.region.height >= 2, (
                    "a heading with bottom padding needs height for the text too"
                )

    asyncio.run(scenario())


def test_the_query_syntax_reminder_and_watch_switch_are_present() -> None:
    """Both were added below "Source discovery", which must still paint.

    That heading is the one an earlier drawer change pushed past `max-height:
    16`, where it laid out correctly and painted nothing — so the guard here is
    not "the new controls exist" but "they exist and cost the old ones nothing".
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            app.advanced_drawer.show()
            await pilot.pause()
            await pilot.pause()

            drawer = app.advanced_drawer
            for widget_id in ("#query-syntax", "#watch-status", "#drawer-watch-rules"):
                region = drawer.query_one(widget_id).region
                assert region.width > 0 and region.height > 0, widget_id

            hint = drawer.query_one("#query-syntax", Static)
            assert "field terms" in hint.render().plain

            # Every heading still has rows to paint into. The syntax line went
            # in *after* Search options and the watch switch replaced a spacer,
            # so neither can have squeezed a heading to nothing — this is the
            # assertion that would catch it if one did.
            for label in drawer.query(".drawer-heading"):
                assert label.region.height >= 2, str(label.render())

    _run(scenario)
