"""OSC 52 clipboard copy (`y`): payload assembly, the size cap, and the UI path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual.widgets import Switch

from clv.app import LogViewerApp
from clv.services.clipboard import prepare_payload
from clv.storage import SessionState, StateStore


# --- payload assembly -------------------------------------------------------


def test_lines_are_joined_without_a_trailing_newline() -> None:
    payload = prepare_payload(["one", "two", "three"], max_bytes=1000)

    assert payload.text == "one\ntwo\nthree"
    assert payload.copied_lines == 3
    assert payload.dropped_lines == 0
    assert payload.truncated is False
    assert payload.summary == "Copied 3 lines."


def test_a_single_line_reports_itself_in_the_singular() -> None:
    assert prepare_payload(["only"], max_bytes=100).summary == "Copied 1 line."


def test_no_lines_is_an_empty_payload_not_an_error() -> None:
    payload = prepare_payload([], max_bytes=100)

    assert payload.empty is True
    assert payload.text == ""
    assert payload.summary == "Nothing to copy."


def test_the_cap_truncates_at_a_line_boundary_keeping_the_newest() -> None:
    lines = [f"line-{index}" for index in range(10)]  # 6 bytes + newline each

    payload = prepare_payload(lines, max_bytes=20)

    # Never a partial line: every kept line is whole, and they are the last ones.
    assert payload.text.split("\n") == ["line-7", "line-8", "line-9"]
    assert payload.copied_lines == 3
    assert payload.dropped_lines == 7
    assert payload.truncated is True
    assert "Copied 3 of 10 lines" in payload.summary
    assert "7 dropped" in payload.summary


def test_the_cap_counts_utf8_bytes_not_characters() -> None:
    # Three characters, six bytes each in UTF-8.
    lines = ["日本語", "日本語"]

    assert prepare_payload(lines, max_bytes=20).copied_lines == 2
    assert prepare_payload(lines, max_bytes=15).copied_lines == 1


def test_a_single_line_over_the_cap_copies_nothing_rather_than_half_a_line() -> None:
    payload = prepare_payload(["x" * 100], max_bytes=10)

    assert payload.empty is True
    assert payload.dropped_lines == 1


# --- the UI path ------------------------------------------------------------


def _log_file(tmp_path: Path, count: int = 20) -> Path:
    path = tmp_path / "app.log"
    path.write_text(
        "\n".join(
            f"2026-08-07 09:25:{index:02d} - INFO - "
            f"{'alpha' if index % 2 else 'beta'} line {index}"
            for index in range(count)
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class _Harness:
    """An app with the clipboard write and the notifications intercepted."""

    def __init__(self, app: LogViewerApp) -> None:
        self.app = app
        self.copied: list[str] = []
        self.notices: list[tuple[str, str]] = []
        app.copy_to_clipboard = self.copied.append  # type: ignore[method-assign]
        app.notify = lambda message, **kw: self.notices.append(
            (message, kw.get("severity", ""))
        )

    def messages(self) -> str:
        return " | ".join(message for message, _ in self.notices)


def test_y_copies_the_visible_lines(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._select_source(_log_file(tmp_path, 20), announce=False)
            harness = _Harness(app)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("y")
            await pilot.pause()

            assert len(harness.copied) == 1
            assert harness.copied[0].splitlines()[-1].endswith("line 19")
            assert "Copied 20 lines" in harness.messages()

    asyncio.run(scenario())


def test_copy_respects_the_active_filter(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._select_source(_log_file(tmp_path, 20), announce=False)
            harness = _Harness(app)
            app._update_state(query="alpha")
            await pilot.pause()

            app.action_copy_view()
            await pilot.pause()

            copied = harness.copied[0].splitlines()
            assert len(copied) == 10
            assert all("alpha" in line for line in copied)
            assert not any("beta" in line for line in copied)

    asyncio.run(scenario())


def test_copy_is_limited_to_the_visible_window(tmp_path: Path) -> None:
    """`y` copies what is on screen; Ctrl+E is the path for the whole set."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._select_source(_log_file(tmp_path, 20), announce=False)
            harness = _Harness(app)
            app._show_lines = 4
            await pilot.pause()

            app.action_copy_view()
            await pilot.pause()

            copied = harness.copied[0].splitlines()
            assert len(copied) == 4
            assert copied[-1].endswith("line 19")

    asyncio.run(scenario())


def test_the_cap_is_reported_when_it_bites(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._select_source(_log_file(tmp_path, 20), announce=False)
            harness = _Harness(app)
            app._config.clipboard_max_bytes = 120
            await pilot.pause()

            app.action_copy_view()
            await pilot.pause()

            assert len(harness.copied[0].encode("utf-8")) <= 120
            assert "of 20 lines" in harness.messages()
            assert harness.notices[-1][1] == "warning"

    asyncio.run(scenario())


def test_y_with_no_source_open_explains_itself(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            harness = _Harness(app)
            app.set_focus(app.log_panel)
            await pilot.pause()

            await pilot.press("y")
            await pilot.pause()

            assert harness.copied == []
            assert "Open a log before copying" in harness.messages()
            assert app.is_running

    asyncio.run(scenario())


def test_the_switch_off_stops_the_copy_and_points_at_the_fallback(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._select_source(_log_file(tmp_path, 5), announce=False)
            harness = _Harness(app)
            app._update_state(clipboard_osc52=False)
            await pilot.pause()

            app.action_copy_view()
            await pilot.pause()

            assert harness.copied == []
            assert "Ctrl+L" in harness.messages()

    asyncio.run(scenario())


def test_a_terminal_that_rejects_the_sequence_does_not_take_the_app_down(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._select_source(_log_file(tmp_path, 5), announce=False)
            harness = _Harness(app)

            def explode(_text: str) -> None:
                raise RuntimeError("no clipboard here")

            app.copy_to_clipboard = explode  # type: ignore[method-assign]
            await pilot.pause()

            app.action_copy_view()
            await pilot.pause()

            assert "Clipboard copy failed" in harness.messages()
            assert app.is_running

    asyncio.run(scenario())


# --- the switch and its persistence -----------------------------------------


def test_the_drawer_switch_toggles_the_setting(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app.advanced_drawer.show()
            await pilot.pause()

            app.advanced_drawer.query_one("#drawer-clipboard", Switch).value = False
            await pilot.pause()

            assert app.state.clipboard_osc52 is False

    asyncio.run(scenario())


def test_the_switch_is_visible_at_every_width(tmp_path: Path) -> None:
    """It has only one home, so the -merged rule must not hide it."""

    async def scenario() -> None:
        for width in (80, 120, 160):
            app = LogViewerApp()
            async with app.run_test(size=(width, 40)) as pilot:
                app.advanced_drawer.show()
                await pilot.pause()

                switch = app.advanced_drawer.query_one("#drawer-clipboard", Switch)
                assert switch.display is True, f"hidden at {width} columns"
                assert switch.region.right <= width, f"off screen at {width} columns"

    asyncio.run(scenario())


def test_the_setting_survives_a_restart(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    store.save(SessionState(clipboard_osc52=False))

    assert StateStore(root=tmp_path).load().clipboard_osc52 is False


def test_a_mistyped_clipboard_flag_falls_back_to_the_default(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    store.path.write_text(json.dumps({"clipboard_osc52": "yes please"}), encoding="utf-8")

    assert StateStore(root=tmp_path).load().clipboard_osc52 is True
