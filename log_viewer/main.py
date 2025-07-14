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
from typing import Dict, List, Optional

# ───────────── Import Third Party Librarys ─────────────
from rich.text import Text
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ───────────── Import Textual Librarys ─────────────
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.scroll_view import ScrollView
from textual.reactive import reactive
from textual.widgets    import Checkbox, Footer, Header, Input, RichLog, Static, Tree, Select

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
    CSS_PATH = "log_viewer.css" if (Path(__file__).parent / "log_viewer.css").exists() else None
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_tree", "Reload tree"),
        Binding("enter", "open_highlighted", "Open / tail log"),
        Binding("[", "shrink_tree", "Narrow tree"),
        Binding("]", "expand_tree", "Widen tree"),
        Binding("+", "more_lines", "Taller pane"),
        Binding("-", "fewer_lines", "Shorter pane"),
        Binding("m", "toggle_mouse", "Toggle mouse"),
    ]

    current_log: reactive[Optional[Path]] = reactive(None)
    regex_filter: reactive[str] = reactive("")
    time_filter: reactive[str] = reactive("")
    severity_filter: reactive[str] = reactive("")
    auto_scroll: reactive[bool] = reactive(True)
    show_lines: reactive[int] = reactive(DEFAULT_SHOW_LINES)
    mouse_enabled: reactive[bool] = reactive(True)

    _lines: List[str]
    _queue: SimpleQueue[str]
    _observer: Optional[Observer] = None
    _tree_width: int = DEFAULT_TREE_WIDTH
    _last_rendered_idx: int = 0
    _last_filter_sig: tuple[str, str, str, int] = ("", "", "", 0)

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
                    with Horizontal(classes="filter_row"):
                        yield Static("Log Severity:", classes="f_label")
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
                        yield RichLog(id="output", auto_scroll=True)
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
        self._apply_mouse_mode()
        self._drain_queue()
        self.set_focus(self.query_one("#tree"))
        self.set_interval(1 / REFRESH_HZ, self._drain_queue, name="flush")

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

        regex_raw = self.regex_filter.strip()
        severity_raw = self.severity_filter.strip()
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

    def _apply_mouse_mode(self) -> None:
        """Enable or disable mouse capture based on ``mouse_enabled``."""
        enabled = self.mouse_enabled
        try:
            if hasattr(self.screen, "set_mouse_capture"):
                self.screen.set_mouse_capture(enabled)
            elif hasattr(self, "set_mouse_capture"):
                self.set_mouse_capture(enabled)
            elif hasattr(self, "capture_mouse") and hasattr(self, "release_mouse"):
                (self.capture_mouse if enabled else self.release_mouse)()
            elif hasattr(self, "mouse"):
                self.mouse = enabled
            elif hasattr(self, "ENABLE_MOUSE_SUPPORT"):
                self.ENABLE_MOUSE_SUPPORT = enabled
        except Exception:
            pass


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

    def action_toggle_mouse(self) -> None:
        """Toggle mouse capture on/off at runtime."""
        self.mouse_enabled = not self.mouse_enabled
        self._apply_mouse_mode()

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
        """
        Setup the tree's root label and return list of (Path, Tree.Node) tuples to populate.
        
        If there's one log root, we use its full path as the tree root label.
        If there are multiple, we label the root as "Log Roots" and add each log root path as a branch.
        """
        root = tree.root 
        
        # Clear root children if any
        if hasattr(root, "clear"):
            root.clear()
        else:
            for child in list(root.children):
                child.remove()

        search_roots: list[tuple[Path, Tree.Node]] = []

        if len(LOG_ROOTS) == 1:
            base = LOG_ROOTS[0]
            root.label = str(base)  # Full path
            search_roots.append((base, root))
        else:
            root.label = "Log Roots"
            for base in LOG_ROOTS:
                node = root.add(str(base), data=base)  # Show full path
                search_roots.append((base, node))

        return search_roots

    async def _populate_tree(self):
        """Build the tree, supporting one or many LOG_ROOTS dynamically."""
        tree: Tree = self.query_one("#tree")
        search_roots = self._build_root_tree_nodes(tree)
        dir_nodes: dict[Path, Tree.Node] = {base: node for base, node in search_roots}

        # Gather all .log files under each root (async)
        all_file_lists = await asyncio.to_thread(
            lambda: [
                (base, sorted(
                    base.rglob("*.log"),
                    key=lambda p: str(p.relative_to(base)).lower()
                ))
                for base, _ in search_roots
            ]
        )

        total_dirs = set()
        total_files = 0

        for base, files in all_file_lists:
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
        if select.value == "other":
            return self.query_one("#severity_other", Input).value
        return select.value or ""

    def on_input_changed(self, event: Input.Changed):
        """Handle changes to any input fields and refresh output."""
        if event.input.id == "severity_other" and self.query_one("#severity_select", Select).value != "other":
            return
        self.regex_filter = self.query_one("#regex").value
        self.time_filter = self.query_one("#time").value
        self.severity_filter = self._current_severity()
        self._render_output()

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
        if file_path.exists():
            try:
                with file_path.open("r", encoding="utf-8", errors="ignore") as fp:
                    self._lines.extend(fp.readlines()[-MAX_BUFFER_LINES:])
            except Exception:
                self._lines.append(f"ERROR: failed to read {file_path}\n")

        # ── Start a new watchdog tailer
        self._queue = SimpleQueue()
        handler = TailHandler(file_path, self._queue)
        observer = Observer()
        observer.schedule(handler, str(file_path.parent), recursive=False)
        observer.start()
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
    """Entrypoint for CLI execution via `poetry run log-viewer` or installed script."""
    LogViewer().run()

# ─────────────────────────── Entrypoint ─────────────────────────
if __name__ == "__main__":
    main()
