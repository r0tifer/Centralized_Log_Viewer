#!/usr/bin/env python3
"""
Textual Log Viewer

A terminal-based application built with Textual that automatically discovers the project's
`logs/` directory, recursively scans for all `*.log` files, and provides an interactive
tree-based UI to browse, filter, and tail log output.

Features:
  - Recursive discovery of `*.log` files under `logs/`
  - Interactive file-tree navigation for quick access
  - Regex and time-range filtering of log lines
  - Color-coded log levels (ERROR, WARNING, INFO, DEBUG)
  - Auto-scrolling and adjustable pane sizes for live tailing
"""
from __future__ import annotations

__author__ = "Michael Levesque"
__version__ = "1.0"

# ───────────── Import Standard Librarys ─────────────
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from queue import SimpleQueue
from typing import List, Optional

# ───────────── Import Third Party Librarys ─────────────
from rich.text import Text
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ───────────── Import Textual Librarys ─────────────
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets    import Button, Checkbox, Footer, Header, Input, RichLog, Static, Tree, Select
from .paths import get_resource_path

# ───────────── Handle Loading Settings ─────────────
from log_viewer.config import load_config
_config = load_config()

# ─────────────────────────── Globals ────────────────────────────
LOG_ROOTS = _config["log_dirs"]
MAX_BUFFER_LINES = _config["max_buffer_lines"]
DEFAULT_SHOW_LINES = _config["default_show_lines"]
REFRESH_HZ = _config["refresh_hz"]
DEFAULT_TREE_WIDTH = _config["default_tree_width"]
MIN_SHOW_LINES = _config["min_show_lines"]
SHOW_STEP = _config["show_step"]
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.conf"


ACCESS_HINT = (
    "Re-launch Centralized Log Viewer with elevated permissions (e.g. run with sudo or as administrator) "
    "to include this source."
)

# ──────────────────────── Tail Handler ──────────────────────────
class TailHandler(FileSystemEventHandler):
    """Watch a single log file for changes and enqueue any newly appended content lines to a sink queue."""
    def __init__(self, file_path: Path, sink: SimpleQueue[str]):
        """
        Initialize the TailHandler.

        Args:
            file_path (Path): Path to the log file to monitor.
            sink (SimpleQueue[str]): Queue where new file content chunks will be placed.
        """

        super().__init__()
        self.file_path = file_path
        self._sink = sink
        self._offset = file_path.stat().st_size if file_path.exists() else 0

    def on_modified(self, event):
        """
        Handle filesystem modification events.

        When the monitored file is modified, read from the last known offset
        to the new end, and enqueue the new text in `self._sink`.
        """
        if Path(event.src_path) != self.file_path:
            return
        try:
            with self.file_path.open("r", encoding="utf-8", errors="ignore") as fp:
                fp.seek(self._offset)
                chunk = fp.read()
                self._offset = fp.tell()
                if chunk:
                    self._sink.put(chunk)
        except Exception:
            pass

# ───────────────────── Copy-safe RichLog ───────────────────────
class CopySafeRichLog(RichLog):
    """RichLog that asks the app to collapse surrounding chrome while SHIFT-copying."""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.shift and isinstance(self.app, LogViewer):
            self.app.enter_copy_mode()
        if hasattr(super(), "on_mouse_down"):
            super().on_mouse_down(event)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if hasattr(super(), "on_mouse_up"):
            super().on_mouse_up(event)
        if isinstance(self.app, LogViewer):
            self.app.exit_copy_mode()

    def on_mouse_leave(self, event: events.MouseLeave) -> None:
        if hasattr(super(), "on_mouse_leave"):
            super().on_mouse_leave(event)
        if isinstance(self.app, LogViewer):
            self.app.exit_copy_mode()


class SessionMenu(Static):
    """Simple horizontal menu bar exposing session management actions."""

    def compose(self) -> ComposeResult:  # type: ignore[override]
        with Horizontal(id="menu_bar"):
            yield Button(
                "Add Log Source",
                id="menu_add_source",
                variant="primary",
                action="open_add_source",
            )
            yield Button(
                "Save Session",
                id="menu_save_session",
                variant="success",
                action="save_session",
            )


class AddPathScreen(ModalScreen[Optional[str]]):
    """Modal dialog prompting the user for a log directory or file path."""

    DEFAULT_CSS = """
    AddPathScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #add_path_modal {
        width: 70;
        max-width: 90vw;
        padding: 1 2;
        border: round #00afff;
        background: #101028;
        layout: vertical;
    }

    #add_path_title {
        text-style: bold;
        margin-bottom: 1;
    }

    #add_path_input {
        width: 1fr;
        border: round #00afff;
        background: #1b1b30;
        margin-bottom: 1;
    }

    #add_path_buttons {
        layout: horizontal;
        width: 100%;
    }

    #add_path_buttons Button#submit {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:  # type: ignore[override]
        with Vertical(id="add_path_modal"):
            yield Static("Add Log Source", id="add_path_title")
            yield Static(
                "Enter an absolute path to a log directory or a single .log file.",
                id="add_path_hint",
            )
            yield Input(
                placeholder="/var/log/app" if os.name != "nt" else r"C:\\logs",
                id="add_path_input",
            )
            with Horizontal(id="add_path_buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add", id="submit", variant="success")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "submit":
            value = self.query_one(Input).value.strip()
            self.dismiss(value or None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

# ──────────────────────── Log Viewer App ───────────────────────
class LogViewer(App):
    """
    Interactive Textual application for discovering, browsing, filtering, and tailing log files.

    Features include:
    - A tree view of all `.log` files under configured roots
    - Regex and time-range filtering
    - Color-coded levels, auto-scroll, and adjustable pane sizes
    """

    TITLE = "📜 Centralized Log Viewer "
    TITLE = "Centralized Log Viewer"
    _css_path = get_resource_path("log_viewer.css")
    CSS_PATH = str(_css_path) if _css_path else None
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_tree", "Reload tree"),
        Binding("enter", "open_highlighted", "Open / tail log"),
        Binding("[", "shrink_tree", "Narrow tree"),
        Binding("]", "expand_tree", "Widen tree"),
        Binding("+", "more_lines", "Taller pane"),
        Binding("-", "fewer_lines", "Shorter pane"),
        Binding("ctrl+l", "toggle_copy_mode", "CTRL+L Enter/exit Copy Mode"),
        Binding("a", "open_add_source", "Add Log Source"),
        Binding("ctrl+s", "save_session", "Save Session"),
    ]

    current_log: reactive[Optional[Path]] = reactive(None)
    regex_filter: reactive[str] = reactive("")
    time_filter: reactive[str] = reactive("")
    severity_filter: reactive[str] = reactive("")
    auto_scroll: reactive[bool] = reactive(True)
    show_lines: reactive[int] = reactive(DEFAULT_SHOW_LINES)
    copy_mode: reactive[bool] = reactive(False)

    _lines: List[str]
    _queue: SimpleQueue[str]
    _observer: Optional[Observer] = None
    _tree_width: int = DEFAULT_TREE_WIDTH
    _last_rendered_idx: int = 0
    _last_filter_sig: tuple[str, str, str, int] = ("", "", "", 0)
    _copy_mode_requests: int = 0
    _copy_mode_manual: bool = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._session_dirs: List[Path] = []
        self._session_files: List[Path] = []
        self._added_paths: set[Path] = set()
        self._all_sources: set[str] = set()
        for raw in LOG_ROOTS:
            path = self._normalize_path(raw)
            marker = str(path)
            if marker in self._all_sources:
                continue
            self._all_sources.add(marker)
            if path.is_file():
                self._session_files.append(path)
            else:
                self._session_dirs.append(path)
        self._session_dirs.sort(key=lambda p: str(p).lower())
        self._session_files.sort(key=lambda p: str(p).lower())

    # ───────────────────────── UI Compose ──────────────────────────
    def compose(self) -> ComposeResult:
        """
        Build and return the UI layout.

        Yields:
            Header, tree sidebar, filter inputs, auto-scroll checkbox,
            log output pane (RichLog), metadata pane (Static), and Footer.
        """
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Tree("Logs", id="tree")
            with Vertical(id="right"):
                with Vertical(id="filters"):
                    with Horizontal(classes="filter_row"):
                        yield Static("Regex:", classes="f_label")
                        yield Input(placeholder="e.g. ERROR|WARN", id="regex")
                    with Horizontal(classes="filter_row"):
                        yield Static("Time:", classes="f_label")
                        yield Input(placeholder="e.g. 15m | 2025-05-21 08:00 to 09:00", id="time")
                    with Horizontal(classes="filter_row severity_row"):
                        yield Static("Log\nSeverity:", classes="f_label")
                        yield Select(
                            options=[
                                ("Low", "Low"),
                                ("Medium", "Medium"),
                                ("High", "High"),
                                ("Critical", "Critical"),
                                ("Other", "other"),
                            ],
                            id="severity_select",
                        )
                    with Horizontal(classes="filter_row"):
                        yield Static("Custom:", classes="f_label")
                        yield Input(
                            placeholder="e.g. ERROR",
                            id="severity_other",
                            disabled=True,
                        )
                    yield Checkbox(label="Auto-Scroll", value=True, id="auto_scroll")
                with Horizontal(id="view"):
                    with Vertical(id="output_scroll"):
                        yield CopySafeRichLog(id="output", auto_scroll=True)
                    yield Static("", id="meta")

        yield Footer()

    # ───────────────────────── Lifecycle ───────────────────────────
    async def on_mount(self) -> None:
        """
        Called when the app mounts.

        Initializes internal queues, line buffer, sets up the log-file tree,
        applies default widths, drains any pending events, focuses the tree,
        and schedules periodic queue-draining at REFRESH_HZ.
        """

        self._queue = SimpleQueue()
        self._lines = []
        self._observer = None
        await self._populate_tree()
        self._apply_tree_width()
        self._drain_queue()
        self.set_focus(self.query_one("#tree"))
        self.set_interval(1 / REFRESH_HZ, self._drain_queue, name="flush")

    # ───────────────────────── Copy Mode ───────────────────────────
    def watch_copy_mode(self, active: bool) -> None:
        """Toggle CSS class that collapses UI chrome while copying."""
        self.set_class(active, "-copy-mode")
        if active:
            try:
                self.set_focus(self.query_one("#output", CopySafeRichLog))
            except Exception:
                pass

    def enter_copy_mode(self) -> None:
        """Collapse surrounding widgets so shift-select only includes log lines."""
        if self._copy_mode_manual:
            return
        self._copy_mode_requests += 1
        if not self.copy_mode:
            self.copy_mode = True

    def exit_copy_mode(self, force: bool = False) -> None:
        """Restore full layout once copy operation is finished."""
        if force:
            self._copy_mode_manual = False
            self._copy_mode_requests = 0
            if self.copy_mode:
                self.copy_mode = False
            return
        if self._copy_mode_manual:
            return
        if self._copy_mode_requests > 0:
            self._copy_mode_requests -= 1
        if self._copy_mode_requests == 0 and self.copy_mode:
            self.copy_mode = False

    def action_toggle_copy_mode(self) -> None:
        """Manual toggle so users can stage the layout before copying via keyboard."""
        if self._copy_mode_manual:
            self.exit_copy_mode(force=True)
            return
        self._copy_mode_manual = True
        self._copy_mode_requests = 0
        if not self.copy_mode:
            self.copy_mode = True

    # ───────── _render_output()  ───────────────────────────────────
    def _render_output(self) -> None:
        """
        Render buffered log lines to the RichLog widget.

        Applies regex and time-range filters, handles full redraw vs incremental,
        scrolls if auto_scroll is enabled, and updates metadata (file size,
        modification timestamp, and total lines shown).
        """

        log_widget: RichLog = self.query_one("#output", RichLog)
        meta: Static = self.query_one("#meta")

        regex_raw = (self.regex_filter or "").strip()
        severity_value = self.severity_filter
        if isinstance(severity_value, str):
            severity_raw = severity_value.strip()
        else:
            severity_raw = ""
        filter_sig = (regex_raw, self.time_filter.strip(), severity_raw, self.show_lines)
        full_redraw = filter_sig != self._last_filter_sig

        try:
            pattern = re.compile(regex_raw, re.IGNORECASE) if regex_raw else None
        except re.error:
            pattern = None
        try:
            level_pattern = re.compile(severity_raw, re.IGNORECASE) if severity_raw else None
        except re.error:
            level_pattern = None

        start_ts, end_ts = self._parse_time_filter(self.time_filter)

        if full_redraw:
            log_widget.clear()

            shown: list[str] = []
            for ln in reversed(self._lines):
                if pattern and not pattern.search(ln):
                    continue
                if level_pattern and not level_pattern.search(ln):
                    continue
                if start_ts or end_ts:
                    try:
                        ts = datetime.fromisoformat(ln.split(" ", 1)[0])
                        if (start_ts and ts < start_ts) or (end_ts and ts > end_ts):
                            continue
                    except Exception:
                        pass
                shown.append(ln)
            shown.reverse()

            for ln in shown:
                self._write_line_to_log(log_widget, ln)

        else:
            new_lines = self._lines[self._last_rendered_idx:]
            for ln in new_lines:
                if pattern and not pattern.search(ln):
                    continue
                if level_pattern and not level_pattern.search(ln):
                    continue
                if start_ts or end_ts:
                    try:
                        ts = datetime.fromisoformat(ln.split(" ", 1)[0])
                        if (start_ts and ts < start_ts) or (end_ts and ts > end_ts):
                            continue
                    except Exception:
                        pass
                self._write_line_to_log(log_widget, ln)

        if self.auto_scroll:
            log_widget.scroll_end(animate=False)

        if self.current_log:
            stats = self.current_log.stat()
            size_kb = stats.st_size / 1024
            mod_ts = datetime.fromtimestamp(stats.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            meta.update(
                f"Size: {size_kb:.1f} KB\n"
                f"Modified: {mod_ts}\n"
                f"Lines shown: {len(self._lines)}"
            )
        else:
            meta.update("No file selected")

        self._last_rendered_idx = len(self._lines)
        self._last_filter_sig = filter_sig

    # ─────────────────────── Log Loading Helpers ─────────────────────────
    def _write_line_to_log(self, log_widget: RichLog, ln: str) -> None:
        """
        Write a single log line to the RichLog with level-based coloring.

        Lines containing ERROR are red, WARNING/WARN orange, INFO green,
        DEBUG blue, and all others default styling.
        """
        ln = ln.strip()
        if   "ERROR"   in ln: log_widget.write(Text(ln, style="red"))
        elif "WARNING" in ln or "WARN" in ln: log_widget.write(Text(ln, style="orange1"))
        elif "INFO"    in ln: log_widget.write(Text(ln, style="green"))
        elif "DEBUG"   in ln: log_widget.write(Text(ln, style="blue"))
        else: log_widget.write(ln)


    # ─────────────────────── Width Helpers ─────────────────────────
    def _apply_tree_width(self) -> None:
        """Set the Tree widget's width; let the right-hand panel fill the rest."""
        tree = self.query_one("#tree")
        tree.styles.width = self._tree_width
        tree.refresh(layout=True)


    def action_shrink_tree(self) -> None:
        """Handle — shrink the tree pane by 2 columns, minimum 15."""
        self._tree_width = max(15, self._tree_width - 2)
        self._apply_tree_width()


    def action_expand_tree(self) -> None:
        """Handle — expand the tree pane by 2 columns, maximum 80."""
        self._tree_width = min(80, self._tree_width + 2)
        self._apply_tree_width()

    # ──────────────── Output-pane height helpers ──────────────────
    VIEW_LINE_HEIGHT = 1

    def _apply_view_height(self) -> None:
        """Set the height of the log view pane based on the `show_lines` count."""
        view = self.query_one("#view")
        view.styles.height = self.show_lines * self.VIEW_LINE_HEIGHT + 2
        view.refresh(layout=True)

    def action_more_lines(self):
        """Increase the number of visible log lines by SHOW_STEP and re-render."""
        self.show_lines += SHOW_STEP
        self._apply_view_height()
        self._render_output()

    def action_fewer_lines(self):
        """Decrease the number of visible log lines by SHOW_STEP (not below MIN_SHOW_LINES) and re-render."""
        self.show_lines = max(MIN_SHOW_LINES, self.show_lines - SHOW_STEP)
        self._apply_view_height()
        self._render_output()

    # ───────────────────────── Tree Build ──────────────────────────
    @staticmethod
    def _clear_node(node):
        """Remove all children from a Tree node, using clear() if available or
        falling back to individually removing each child."""
        if hasattr(node, "clear") and callable(node.clear):
            node.clear()
        else:
            for child in list(node.children):
                try:
                    child.remove()
                except Exception:
                    pass
    
    def _build_root_tree_nodes(self, tree: Tree) -> list[tuple[Path, Tree.Node]]:
        """Prepare the root node and return (Path, Tree.Node) pairs for directory population."""
        root = tree.root

        # Clear root children if any
        if hasattr(root, "clear"):
            root.clear()
        else:
            for child in list(root.children):
                child.remove()

        session_dirs = list(dict.fromkeys(self._session_dirs))
        search_roots: list[tuple[Path, Tree.Node]] = []

        if session_dirs:
            root.label = "Log Sources"
            for base in session_dirs:
                node = root.add(str(base), data=base)
                search_roots.append((base, node))
        else:
            root.label = "Log Sources" if self._session_files else "Log Viewer"

        return search_roots

    async def _populate_tree(self):
        """Build the tree, supporting one or many LOG_ROOTS dynamically."""
        tree: Tree = self.query_one("#tree")
        search_roots = self._build_root_tree_nodes(tree)
        dir_nodes: dict[Path, Tree.Node] = {base: node for base, node in search_roots}

        def _scan_directories():
            successes: list[tuple[Path, list[Path]]] = []
            issues: list[tuple[Path, str]] = []
            for base, _ in search_roots:
                try:
                    files = sorted(
                        base.rglob("*.log"),
                        key=lambda p: str(p.relative_to(base)).lower(),
                    )
                except PermissionError:
                    issues.append((base, "permission denied"))
                    files = []
                except OSError as exc:
                    issues.append((base, str(exc)))
                    files = []
                successes.append((base, files))
            return successes, issues

        all_file_lists, scan_issues = await asyncio.to_thread(_scan_directories)
        for base, reason in scan_issues:
            self._inform(f"Skipping '{base}': {reason}", severity="warning")

        total_dirs = set()
        total_files = len(self._session_files)

        for base, files in all_file_lists:
            total_dirs.add(base)
            parent_node = dir_nodes[base]
            for file_path in files:
                total_files += 1
                rel = file_path.relative_to(base)
                node = parent_node
                for part in rel.parts[:-1]:
                    subpath = base / part
                    if subpath not in dir_nodes:
                        dir_nodes[subpath] = node.add(part, data=subpath)
                    node = dir_nodes[subpath]
                    total_dirs.add(subpath)
                node.add_leaf(rel.name, data=file_path)

        tree.root.expand()

        if self._session_files:
            parent = tree.root
            for file_path in sorted(self._session_files, key=lambda p: str(p).lower()):
                parent.add_leaf(str(file_path), data=file_path)

        output = self.query_one("#output")
        output.clear()
        output.write(
            Text.from_markup(
                f"[green]Discovered {len(total_dirs)} log folder(s)[/green]\n"
                f"[cyan]Found      {total_files} log file(s)[/cyan]\n\n"
                "[bold]Use the tree on the left to select which log to tail.[/bold]"
            )
        )

        meta = self.query_one("#meta")
        meta.update(
            f"Root:    {tree.root.label}\n"
            f"Folders: {len(total_dirs)}\n"
            f"Files:   {total_files}"
        )


    # ─────────────────────── User Actions ─────────────────────────
    async def action_refresh_tree(self):
        """Action handler to rebuild the log-file tree by calling `_populate_tree`."""
        await self._populate_tree()

    async def action_open_add_source(self) -> None:
        """Display modal dialog to add a new log directory or log file."""
        self.push_screen(AddPathScreen(), self._on_add_path_submitted)

    def _on_add_path_submitted(self, result: Optional[str]) -> None:
        if not result:
            return
        asyncio.create_task(self._register_log_source(result))

    async def _register_log_source(self, raw_path: str) -> None:
        raw_path = raw_path.strip().strip('"')
        if not raw_path:
            return

        path = Path(raw_path).expanduser()
        try:
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve(strict=False)
            else:
                path = path.resolve(strict=False)
        except PermissionError:
            self._inform(
                f"Permission denied while resolving '{raw_path}'. {ACCESS_HINT}",
                severity="error",
            )
            return

        marker = str(path)
        if marker in self._all_sources:
            self._inform(f"{path} is already part of this session.", severity="warning")
            return

        allowed, denial_reason = self._check_access(path)
        if not allowed:
            self._inform(denial_reason or f"Permission denied for '{path}'.", severity="error")
            return

        if path.is_dir():
            self._session_dirs.append(path)
            self._session_dirs.sort(key=lambda p: str(p).lower())
        elif path.is_file():
            if path.suffix.lower() != ".log":
                self._inform(
                    f"{path.name} does not end with .log; adding anyway.",
                    severity="warning",
                )
            self._session_files.append(path)
            self._session_files.sort(key=lambda p: str(p).lower())
        else:
            self._inform(f"Path '{path}' does not exist.", severity="error")
            return

        self._added_paths.add(path)
        self._all_sources.add(marker)
        await self._populate_tree()
        self._inform(f"Added {path} to the current session.")

    def action_save_session(self) -> None:
        """Persist newly added log sources back to `settings.conf`."""
        if not self._added_paths:
            self._inform("No new log sources to save.", severity="warning")
            return

        additions = sorted(self._added_paths, key=lambda p: str(p).lower())
        try:
            self._persist_session(additions)
        except Exception as exc:  # noqa: BLE001
            self._inform(f"Failed to save session: {exc}", severity="error")
            return

        for path in additions:
            self._all_sources.add(str(path))
        self._added_paths.clear()
        self._inform(f"Saved {len(additions)} new log source(s) to settings.")

    def _cursor_node(self):
        """Return the currently highlighted Tree.Node, handling API differences."""
        tree: Tree = self.query_one("#tree")
        node = getattr(tree, "cursor_node", None) or (
            tree.get_node(tree.cursor) if hasattr(tree, "cursor") else None
        )
        return node

    def action_open_highlighted(self):
        """If the cursor-highlighted node represents a file, open it for tailing."""
        node = self._cursor_node()
        if node and isinstance(node.data, Path) and node.data.is_file():
            self._open_log(node.data)

    async def on_tree_node_selected(self, event: Tree.NodeSelected):
        """Event handler invoked when a tree node is selected; opens the file if applicable."""
        if isinstance(event.node.data, Path) and event.node.data.is_file():
            self._open_log(event.node.data)

    def _current_severity(self) -> str:
        select = self.query_one("#severity_select", Select)
        value = select.value
        if value == "other":
            return self.query_one("#severity_other", Input).value
        if isinstance(value, str):
            return value
        return ""

    def on_input_changed(self, event: Input.Changed):
        """Handle changes to any input fields and refresh output."""
        if event.input.id == "severity_other" and self.query_one("#severity_select", Select).value != "other":
            return
        self.regex_filter = self.query_one("#regex").value
        self.time_filter = self.query_one("#time").value
        self.severity_filter = self._current_severity()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "severity_select":
            other = self.query_one("#severity_other", Input)
            if event.value == "other":
                other.disabled = False
                other.focus()
            else:
                other.disabled = True
                other.value = ""
            self.severity_filter = self._current_severity()
            self._render_output()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Flip our auto_scroll flag whenever the box is toggled."""
        if event.checkbox.id == "auto_scroll":
            self.auto_scroll = event.value
    
    @staticmethod
    def _normalize_path(path: Path) -> Path:
        """Normalize user-provided paths to absolute, resolved Paths."""
        expanded = Path(path).expanduser()
        try:
            return expanded.resolve(strict=False)
        except Exception:  # noqa: BLE001
            return expanded

    def _persist_session(self, additions: List[Path]) -> None:
        """Append newly added log sources to `settings.conf`, preserving existing comments."""
        if not SETTINGS_PATH.exists():
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.touch()

        lines = SETTINGS_PATH.read_text(encoding="utf-8").splitlines()
        entry_strings = [str(p) for p in additions]

        def _merge_values(raw: str) -> str:
            values = [piece.strip() for piece in raw.split(",") if piece.strip()]
            merged = list(dict.fromkeys(values + entry_strings))
            return ", ".join(merged)

        replaced = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("log_dirs"):
                continue
            indent = line[: len(line) - len(stripped)]
            _, _, value = line.partition("=")
            merged = _merge_values(value)
            lines[idx] = f"{indent}log_dirs = {merged}"
            replaced = True
            break

        if not replaced:
            prefix = "log_dirs = "
            merged = ", ".join(dict.fromkeys(entry_strings))
            lines.append(prefix + merged)

        SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _inform(self, message: str, severity: str = "information") -> None:
        """Send a user-facing notification, falling back to console logging."""
        notifier = getattr(self, "notify", None)
        if callable(notifier):
            notifier(message, severity=severity)
        else:
            self.console.log(f"[{severity}] {message}")

    @staticmethod
    def _check_access(path: Path) -> tuple[bool, Optional[str]]:
        """Verify read/list permissions on *path* before adding it to the session."""
        try:
            exists = path.exists()
        except PermissionError:
            return False, f"Permission denied while checking '{path}'. {ACCESS_HINT}"

        if not exists:
            return False, f"Path '{path}' does not exist."

        if path.is_file():
            if not os.access(path, os.R_OK):
                return False, f"Read access required for file '{path}'. {ACCESS_HINT}"
            return True, None

        if path.is_dir():
            if not os.access(path, os.R_OK | os.X_OK):
                return False, f"List access required for directory '{path}'. {ACCESS_HINT}"
            try:
                with os.scandir(path) as it:
                    next(it, None)
            except PermissionError:
                return False, f"Permission denied while listing '{path}'. {ACCESS_HINT}"
            except FileNotFoundError:
                return False, f"Directory '{path}' is not accessible."
            return True, None

        return False, f"Path '{path}' is neither a file nor a directory."

    # ─────────────────────── Tail & Render ───────────────────────
    def _open_log(self, file_path: Path):
        """Stop existing tail, clear buffer, and start tailing *file_path*."""
        # ── Tear down previous tailer
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

        # ── Load the last MAX_BUFFER_LINES lines from the new file
        self._lines.clear()
        try:
            exists = file_path.exists()
        except PermissionError:
            exists = False
            self._inform(
                f"Permission denied accessing '{file_path}'. {ACCESS_HINT}",
                severity="error",
            )

        if exists:
            try:
                with file_path.open("r", encoding="utf-8", errors="ignore") as fp:
                    self._lines.extend(fp.readlines()[-MAX_BUFFER_LINES:])
            except PermissionError:
                self._inform(
                    f"Permission denied reading '{file_path}'. {ACCESS_HINT}",
                    severity="error",
                )
                self._lines.append(f"ERROR: permission denied for {file_path}\n")
            except Exception as exc:  # noqa: BLE001
                self._inform(f"Failed to read '{file_path}': {exc}", severity="error")
                self._lines.append(f"ERROR: failed to read {file_path}\n")
        else:
            self._lines.append(f"ERROR: {file_path} is not accessible.\n")

        # ── Start a new watchdog tailer
        self._queue = SimpleQueue()
        handler = TailHandler(file_path, self._queue)
        observer = Observer()
        try:
            observer.schedule(handler, str(file_path.parent), recursive=False)
            observer.start()
        except Exception as exc:  # noqa: BLE001
            self._inform(
                f"Live tail disabled for '{file_path}': {exc}",
                severity="warning",
            )
        else:
            self._observer = observer

        # Update state and force a full redraw
        self.current_log = file_path
        self._last_rendered_idx = 0
        # pick a signature that won’t match any real filter, so full_redraw=True
        self._last_filter_sig   = (None, None, None, None)
        
        # Render logs
        self._render_output()
        log = self.query_one("#output", RichLog)

        # Wait until after layout to auto scroll to bottom of the selected log
        self.set_interval(0.05, lambda: log.scroll_end(animate=False), name="swait_for_render", repeat=False)

        # Auto set focus on log view pane
        self.set_focus(log)

    # ───────── _drain_queue()  ─────────────────────────────────────
    def _drain_queue(self) -> None:
        """Drain pending text chunks from the queue into the internal line buffer,
        trim to MAX_BUFFER_LINES, and trigger a render if new lines arrived."""
        while not self._queue.empty():
            self._lines.extend(self._queue.get().splitlines(True))
        if len(self._lines) > MAX_BUFFER_LINES:
            self._lines[:] = self._lines[-MAX_BUFFER_LINES:]
        if len(self._lines) != self._last_rendered_idx:
            self._render_output()

    # ─────────────────────── Time Parsing ───────────────────────
    @staticmethod
    def _parse_time_filter(expr: str):
        """Parse a time-filter expression into (start_datetime, end_datetime).

        Supports:
        - ISO-format ranges: "YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM"
        - Relative shortcuts: "15m", "2h", "1d"

        Returns (None, None) on parse failure or empty input.
        """
        expr = expr.strip()
        if not expr:
            return None, None
        if " to " in expr:
            try:
                lhs, rhs = expr.split(" to ", 1)
                return datetime.fromisoformat(lhs.strip()), datetime.fromisoformat(rhs.strip())
            except Exception:  # noqa: BLE001
                return None, None
        unit = expr[-1]
        if unit in "smhd":
            try:
                val = int(expr[:-1])
                delta_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
                return datetime.now() - timedelta(**{delta_map[unit]: val}), None
            except Exception:  # noqa: BLE001
                return None, None
        return None, None

    # ─────────────────────── Shutdown ───────────────────────
    async def on_unmount(self):
        """Lifecycle handler called on application shutdown: stops and joins
        the Observer if it is running."""
        if self._observer:
            self._observer.stop()
            self._observer.join()

def main():
    """Entrypoint for CLI execution via `poetry run CentralizedLogViewer` / `poetry run clv` or installed script."""
    LogViewer().run()

# ─────────────────────────── Entrypoint ─────────────────────────
if __name__ == "__main__":
    main()
