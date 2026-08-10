"""CLV application shell.

This module owns layout, routing and lifecycle. Parsing, filtering, discovery,
reading and configuration all live in ``clv/services``; plugins live in
``clv/plugins``. Styling is CSS only — nothing here assigns ``.styles.*`` at
runtime, so the stylesheet is the single place layout is decided.

Responsiveness is handled by breakpoint classes (``-compact``/``-narrow``/
``-wide``) set on the app in :meth:`LogViewerApp.on_resize`. Widgets key their
own CSS off those classes. This is implemented directly rather than via
Textual's ``HORIZONTAL_BREAKPOINTS`` so the app behaves the same across the
supported Textual range.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal, Optional
from xml.dom import minidom

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Button, Footer, Input, Label, RichLog, Static, Switch, Tree
from textual.widgets._tree import TreeNode

from .plugins import FilterContext, PluginRegistry, load_plugins
from .services import SourceManager, persist_log_sources
from .services.config import LogConfig, get_config_file, load_config, user_config_path
from .services.discovery import DiscoveryReport, discover
from .services.filtering import (
    FilterSpec,
    QueryError,
    TimeWindow,
    describe_empty_result,
    filter_entries,
    parse_absolute_window,
    parse_relative_window,
)
from .services.parsing import LogEntry, LogParser
from .services.reader import SourceReader
from .storage import SessionState, StateStore
from .widgets.add_source_dialog import AddSourceDialog
from .widgets.advanced_drawer import AdvancedFiltersDrawer, AdvancedSettings
from .widgets.custom_time_dialog import CustomTimeRangeDialog
from .widgets.filter_chip import FilterChip, FilterChips
from .widgets.query_bar import QueryBar

SEVERITY_COLORS: dict[str, str] = {
    "CRITICAL": "#fb7185",
    "ERROR": "#f87171",
    "WARN": "#facc15",
    "NOTICE": "#38bdf8",
    "INFO": "#22c55e",
    "DEBUG": "#a855f7",
    "TRACE": "#94a3b8",
}

STRUCTURED_PAYLOAD_MAX_CHARS = 8_192

#: Terminal widths at which the layout changes shape.
BREAKPOINT_COMPACT = 90
BREAKPOINT_NARROW = 130
#: Width at which the time presets, toggles and action buttons fit on a single
#: line together. Measured against their natural widths (48 + 21 + 55 columns
#: plus padding), not guessed, and asserted in tests/test_query_bar.py.
BREAKPOINT_MERGE = 136

SOURCES_PANEL_MIN_WIDTH = 20
SOURCES_PANEL_MAX_WIDTH = 120
SOURCES_PANEL_STEP = 4

ICON_FOLDER = "📂"
ICON_FILE = "📄"


class LogTree(Tree[Path]):
    """Source tree.

    A single tree holds every configured root. The previous build mounted one
    Tree per root inside a scrolling container, which required manual cursor
    hand-off between trees and manual scroll synchronisation against private
    node internals. One tree scrolls itself.
    """

    COMPONENT_CLASSES = Tree.COMPONENT_CLASSES | {
        "tree--icon-branch",
        "tree--icon-leaf",
    }

    DEFAULT_CSS = """
    LogTree {
        background: $surface 6%;
        border: round $surface 12%;
        height: 1fr;
        padding: 0 1;
        color: #dce3f7;
    }

    LogTree:focus {
        border: round $accent 50%;
        background: $surface 10%;
    }

    LogTree > .tree--guides { color: #587ca7; }
    LogTree > .tree--guides-hover { color: #7aa3d1; }
    LogTree > .tree--guides-selected { color: #95c8f5; }
    LogTree > .tree--highlight-line { background: #2c384f; }
    LogTree > .tree--cursor { background: #3d4f6a; color: #ffffff; text-style: bold; }
    LogTree > .tree--label { color: #eef3ff; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.show_guides = True
        self.guide_depth = 3
        self.show_root = False


class LogViewerApp(App[None]):
    """Centralized Log Viewer."""

    CSS = """
    Screen { layout: vertical; }

    #main-content {
        layout: horizontal;
        height: 1fr;
        min-height: 3;
    }

    #sources-panel {
        layout: vertical;
        width: 38;
        min-width: 20;
        height: 1fr;
        padding: 0 1;
        background: $surface 2%;
        border-right: solid $surface 15%;
    }

    #sources-panel:focus-within {
        border-right: solid $accent 60%;
        background: $surface 6%;
    }

    #tree-panel {
        layout: vertical;
        height: 1fr;
    }

    #viewer-panel {
        layout: vertical;
        width: 1fr;
        min-width: 24;
        height: 1fr;
        padding: 0 1;
    }

    #log-stream {
        height: 1fr;
        border: solid $surface 20%;
        scrollbar-gutter: stable;
    }

    .panel-title {
        height: 1;
        padding: 0 0 1 0;
        text-style: bold;
    }

    .empty-tree {
        color: $text-muted;
        padding: 1 0;
    }

    #status-bar {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        background: $surface 8%;
    }

    /* Compact: the source tree costs too much of a narrow screen to keep
       beside the log, so it collapses to a toggle (ctrl+b). */
    LogViewerApp.-compact #sources-panel {
        width: 100%;
        border-right: none;
    }

    LogViewerApp.-compact #viewer-panel { display: none; }
    LogViewerApp.-compact.-viewer-focused #sources-panel { display: none; }
    LogViewerApp.-compact.-viewer-focused #viewer-panel { display: block; width: 100%; }

    /* Copy mode strips the chrome so a mouse selection grabs log text only. */
    LogViewerApp.-copy-mode #query-bar,
    LogViewerApp.-copy-mode #chip-bar,
    LogViewerApp.-copy-mode #advanced-drawer,
    LogViewerApp.-copy-mode #sources-panel,
    LogViewerApp.-copy-mode #status-bar,
    LogViewerApp.-copy-mode Footer,
    LogViewerApp.-copy-mode .panel-title {
        display: none;
    }

    LogViewerApp.-copy-mode #viewer-panel { width: 1fr; padding: 0; display: block; }
    LogViewerApp.-copy-mode #log-stream { border: none; }

    Toast { border: none; }
    Toast.-information { background: #14532d; color: #f0fdf4; }
    Toast.-warning { background: #713f12; color: #fefce8; }
    Toast.-error { background: #7f1d1d; color: #fee2e2; }
    """

    BINDINGS = [
        Binding("/", "focus_query", "Query", show=True),
        Binding("escape", "clear_query", "Clear", show=True),
        Binding("a", "add_source", "Add source", show=True),
        Binding("t", "cycle_time", "Time", show=True),
        Binding("s", "cycle_severity", "Severity", show=True),
        Binding("f", "toggle_advanced", "Advanced", show=True),
        # The auto-scroll and structured switches are only shown when the query
        # bar merges its rows, so they need a keyboard path that does not
        # depend on terminal width.
        Binding("w", "toggle_auto_scroll", "Follow", show=True),
        Binding("o", "toggle_structured", "Structured", show=False),
        Binding("ctrl+b", "toggle_pane", "Switch pane", show=True),
        Binding("[", "shrink_sources_panel", "Narrower", show=False),
        Binding("]", "expand_sources_panel", "Wider", show=False),
        Binding("+", "more_lines", "More lines", show=False),
        Binding("-", "fewer_lines", "Fewer lines", show=False),
        Binding("ctrl+l", "toggle_copy_mode", "Copy mode", show=True),
        Binding("ctrl+s", "save_session", "Save sources", show=True),
        Binding("ctrl+r", "reload_sources", "Reload", show=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    state: reactive[SessionState] = reactive(SessionState())

    def __init__(self, *, config: LogConfig | None = None, store: StateStore | None = None) -> None:
        super().__init__()
        self._store = store or StateStore()
        self._config = config or load_config()
        self._settings_path = get_config_file() or user_config_path()
        self._persist_state = False
        self._is_shutting_down = False

        self._source_manager = SourceManager([], [])
        self._report: DiscoveryReport | None = None
        self._selected_source: Optional[Path] = None
        self._reader: SourceReader | None = None
        self._parser = LogParser()
        self._entries: deque[LogEntry] = deque(maxlen=self._config.max_buffer_lines)
        self._tail_timer: Timer | None = None

        self._show_lines = self._config.default_show_lines
        self._sources_panel_width = self._config.tree_width
        self._copy_mode = False
        self._breakpoint = ""
        self._merged = False
        self._plugins: PluginRegistry = PluginRegistry()

        self.query_bar = QueryBar()
        self.chip_bar = FilterChips(id="chip-bar")
        self.advanced_drawer = AdvancedFiltersDrawer()
        self.log_panel = RichLog(
            id="log-stream",
            wrap=True,
            markup=False,
            max_lines=self._config.max_buffer_lines,
        )

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield self.query_bar
        yield self.advanced_drawer
        yield self.chip_bar
        with Horizontal(id="main-content"):
            with Vertical(id="sources-panel"):
                yield Label("Log Sources", classes="panel-title")
                yield Vertical(id="tree-panel")
            with Vertical(id="viewer-panel"):
                yield Label("Log Output", classes="panel-title")
                yield self.log_panel
        yield Static("", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        self.state = self._store.load()
        self._plugins = load_plugins()
        self._apply_breakpoint(self.size.width)
        self._sources_panel_width = self.state.tree_width or self._config.tree_width
        self._apply_panel_width()

        self._source_manager = SourceManager(*self._split_roots(self._config.log_dirs))
        self._apply_state_to_widgets()
        # Deliberately no source is opened here: the viewer starts on the
        # discovery summary so a launch always begins from a known state
        # rather than silently resuming and tailing whatever was open last.
        await self._rescan()

        self._refresh_chips()
        self._update_status()
        self._persist_state = True

    @staticmethod
    def _split_roots(roots: Iterable[Path]) -> tuple[list[Path], list[Path]]:
        directories: list[Path] = []
        files: list[Path] = []
        for entry in roots:
            try:
                if entry.is_dir():
                    directories.append(entry)
                elif entry.is_file():
                    files.append(entry)
                else:
                    # Keep unresolved entries so discovery can report them.
                    directories.append(entry)
            except OSError:
                continue
        return directories, files

    def _apply_state_to_widgets(self) -> None:
        """Push loaded session state into the controls."""

        self.query_bar.set_query_value(self.state.query)
        self.query_bar.set_severity(self.state.severity)
        self.query_bar.set_auto_scroll(self.state.auto_scroll)
        self.query_bar.set_pretty_rendering(self.state.pretty_rendering)
        if self.state.time_window == "range" and self.state.custom_start and self.state.custom_end:
            self.query_bar.apply_custom_time_range(
                self.state.custom_start, self.state.custom_end, emit=False
            )
        else:
            self.query_bar.select_time(self.state.time_window)

        self.log_panel.auto_scroll = self.state.auto_scroll
        self.advanced_drawer._settings = AdvancedSettings(
            include_globs=self.state.include_globs,
            exclude_globs=self.state.exclude_globs,
            follow_symlinks=self.state.follow_symlinks,
            skip_binary=self.state.skip_binary,
            max_buffer_lines=self._config.max_buffer_lines,
            case_sensitive=self.state.case_sensitive,
            use_regex=self.state.use_regex,
            invert_match=self.state.invert_match,
        )
        # Seed the drawer's mirrored view switches from the restored session,
        # so they are correct the first time the drawer is opened.
        self.advanced_drawer.sync_view_toggles(
            auto_scroll=self.state.auto_scroll,
            structured=self.state.pretty_rendering,
        )

    # --- responsiveness -----------------------------------------------------

    def on_resize(self, event) -> None:
        self._apply_breakpoint(event.size.width)

    def _apply_breakpoint(self, width: int) -> None:
        """Set the breakpoint class that widget CSS keys off.

        The class is mirrored onto the widgets that have responsive rules:
        Textual scopes ``DEFAULT_CSS`` to the widget subtree, so a selector
        rooted at the app node would never match from inside a widget.
        """

        if width < BREAKPOINT_COMPACT:
            breakpoint_name = "-compact"
        elif width < BREAKPOINT_NARROW:
            breakpoint_name = "-narrow"
        else:
            breakpoint_name = "-wide"

        # Merging the time presets, toggles and actions onto one line needs
        # more room than "-wide" guarantees, so it gets its own threshold
        # rather than riding along with the size class.
        merged = width >= BREAKPOINT_MERGE
        if breakpoint_name == self._breakpoint and merged == self._merged:
            return

        targets = [self, self.query_bar, self.advanced_drawer]
        for name in ("-compact", "-narrow", "-wide"):
            active = name == breakpoint_name
            for target in targets:
                target.set_class(active, name)
        # The drawer needs it too: it shows the mirrored view toggles only when
        # the query bar is not showing its own.
        self.query_bar.set_class(merged, "-merged")
        self.advanced_drawer.set_class(merged, "-merged")

        self._breakpoint = breakpoint_name
        self._merged = merged
        self._apply_panel_width()
        self._sync_compact_pane()

    # --- discovery ----------------------------------------------------------

    def _discovery_settings(self):
        return self.advanced_drawer.settings.to_discovery(self._config.discovery)

    async def _rescan(self) -> None:
        """Re-run discovery off the UI thread and rebuild the tree."""

        roots = self._source_manager.all_sources()
        settings = self._discovery_settings()

        # The walk touches the filesystem and can be slow on a large tree, so
        # it runs in a thread rather than blocking the event loop.
        worker = self.run_worker(
            lambda: discover(roots, settings), thread=True, name="discover", exit_on_error=False
        )
        report = await worker.wait()
        if report is None:
            report = DiscoveryReport()
        self._report = report
        await self._build_tree(report)
        if self._selected_source is None:
            self._show_discovery_summary(report)
        self._update_status()

    async def _build_tree(self, report: DiscoveryReport) -> None:
        panel = self.query_one("#tree-panel", Vertical)
        # Removal is deferred, so it must be awaited before mounting a
        # replacement — otherwise the second rescan collides with the tree the
        # first one left registered.
        await panel.remove_children()

        if not report.files:
            await panel.mount(Static("No log sources found.", classes="empty-tree"))
            return

        tree: LogTree = LogTree("Sources", id="source-tree")
        await panel.mount(tree)

        # One branch per configured root, then a folder hierarchy beneath it.
        by_root: dict[Path, list] = {}
        for item in report.files:
            by_root.setdefault(item.root, []).append(item)

        for root in sorted(by_root, key=lambda p: str(p).lower()):
            items = by_root[root]
            if len(items) == 1 and items[0].path == root:
                # The operator named this single file directly.
                tree.root.add_leaf(f"{ICON_FILE} {root}", data=root)
                continue

            # Every folder starts collapsed. Expanding the whole hierarchy up
            # front buries the configured roots under hundreds of rows on a
            # tree of any size; the operator opens the branch they want.
            # _highlight_source still expands ancestors on demand.
            root_node = tree.root.add(f"{ICON_FOLDER} {root}", data=root, expand=False)
            folders: dict[Path, TreeNode[Path]] = {root: root_node}
            for item in items:
                parent = root_node
                current = root
                for part in item.relative.parts[:-1]:
                    current = current / part
                    if current not in folders:
                        folders[current] = parent.add(
                            f"{ICON_FOLDER} {part}", data=current, expand=False
                        )
                    parent = folders[current]
                parent.add_leaf(f"{ICON_FILE} {item.path.name}", data=item.path)

        tree.focus()

    def _highlight_source(self, path: Path) -> None:
        try:
            tree = self.query_one("#source-tree", LogTree)
        except NoMatches:
            return
        target = _resolve(path)
        node = _find_node(tree.root, target)
        if node is None:
            return

        # Expand outermost-first: expanding an inner node while its own parent
        # is still collapsed leaves it off the rendered line list.
        ancestors: list[TreeNode[Path]] = []
        parent = node.parent
        while parent is not None:
            ancestors.append(parent)
            parent = parent.parent
        for ancestor in reversed(ancestors):
            ancestor.expand()

        # Selecting has to wait for the tree to rebuild its line list, which
        # expand() has just invalidated. Selecting immediately reads a stale
        # line for the target and silently parks the cursor on the root -- only
        # visible once folders stopped being expanded up front.
        def _select() -> None:
            tree.select_node(node)
            tree.scroll_to_node(node)

        self.call_after_refresh(_select)

    # --- source selection and tailing --------------------------------------

    def _select_source(self, path: Path, *, announce: bool = True) -> bool:
        resolved = _resolve(path)
        if not resolved.is_file():
            if announce:
                self._notify(f"{resolved} is not a readable file.", "error")
            return False

        self._stop_tail()
        self._parser.reset()
        self._entries = deque(maxlen=self._config.max_buffer_lines)

        reader = SourceReader(resolved, max_lines=self._config.max_buffer_lines)
        try:
            initial = reader.prime()
        except OSError as exc:
            self._notify(f"Failed to read {resolved}: {exc}", "error")
            return False

        self._reader = reader
        self._selected_source = resolved
        self._entries.extend(self._parser.feed(initial.lines))
        self._show_lines = min(self._config.default_show_lines, self._config.max_buffer_lines)

        self._sync_regex_validation()
        self._render_log(scroll_end=True)
        self._start_tail()
        self._update_status()
        self._sync_compact_pane()
        return True

    def _start_tail(self) -> None:
        """Tail regardless of auto-scroll.

        Auto-scroll controls whether the viewport follows new lines; it must
        not decide whether new lines are read at all. Turning it off to read
        back through history used to stop ingestion entirely.
        """

        self._stop_tail()
        if self._reader is None:
            return
        interval = 1 / max(1, self._config.refresh_hz)
        self._tail_timer = self.set_interval(interval, self._poll_tail)

    def _stop_tail(self) -> None:
        if self._tail_timer is not None:
            self._tail_timer.stop()
            self._tail_timer = None

    def _poll_tail(self) -> None:
        if self._reader is None or self._is_shutting_down:
            return
        try:
            result = self._reader.poll()
        except OSError:
            return

        if result.rotated:
            self._parser.reset()
            self._entries.clear()
            self._entries.extend(self._parser.feed(result.lines))
            self._render_log(scroll_end=self.state.auto_scroll)
            self._notify(f"{self._reader.path.name} was rotated; reloaded.", "warning")
            return

        if not result.lines:
            return

        new_entries = self._parser.feed(result.lines)
        overflowing = len(self._entries) + len(new_entries) > (self._entries.maxlen or 0)
        self._entries.extend(new_entries)
        self._sync_regex_validation()

        if overflowing:
            # The ring buffer dropped old lines, so the visible window shifted:
            # a full redraw is the only correct option.
            self._render_log()
        else:
            self._append_entries(new_entries)
        self._update_status()

    # --- filtering and rendering -------------------------------------------

    def _filter_spec(self) -> FilterSpec:
        settings = self.advanced_drawer.settings
        return FilterSpec(
            query=self.state.query,
            severity=self.state.severity,
            window=self._time_window(),
            case_sensitive=True if settings.case_sensitive else None,
            regex=settings.use_regex,
            invert=settings.invert_match,
        )

    def _time_window(self) -> TimeWindow:
        if self.state.time_window == "range":
            window = parse_absolute_window(self.state.custom_start, self.state.custom_end)
            return window or TimeWindow()
        try:
            return parse_relative_window(self.state.time_window)
        except ValueError:
            return TimeWindow()

    def _plugin_context(self) -> FilterContext:
        return FilterContext(spec=self._filter_spec(), source=self._selected_source)

    def _visible_entries(self, entries: Iterable[LogEntry]):
        """Apply plugin stages, then the user's filters."""

        context = self._plugin_context()
        staged = self._plugins.apply_filters(list(entries), context)
        return filter_entries(staged, context.spec)

    def _render_log(self, *, scroll_end: bool = False) -> None:
        if self._is_shutting_down:
            return
        self.log_panel.clear()

        if self._selected_source is None:
            if self._report is not None:
                self._show_discovery_summary(self._report)
            return

        if not self._entries:
            self.log_panel.write(Text("No log entries in the selected source.", style="dim"))
            return

        try:
            result = self._visible_entries(self._entries)
        except QueryError as exc:
            self.log_panel.write(Text(f"Invalid query: {exc}", style="bold #f87171"))
            return

        if not result.entries:
            message = describe_empty_result(result.stats, self._filter_spec())
            self.log_panel.write(Text(message, style="dim"))
            return

        for entry in result.entries[-self._show_lines :]:
            self.log_panel.write(self._renderable_for(entry))

        if scroll_end or self.state.auto_scroll:
            self.log_panel.scroll_end(animate=False)
        self._update_status()

    def _append_entries(self, entries: list[LogEntry]) -> None:
        """Incremental render for tailed lines.

        Tailing used to clear and rewrite the whole pane on every poll. Only
        the new lines need rendering, so cost is proportional to what arrived.
        """

        if self._is_shutting_down or not entries:
            return
        try:
            result = self._visible_entries(entries)
        except QueryError:
            return
        for entry in result.entries:
            self.log_panel.write(self._renderable_for(entry))

    def _renderable_for(self, entry: LogEntry) -> RenderableType:
        if self.state.pretty_rendering:
            structured = self._structured_renderable(entry)
            if structured is not None:
                return structured
        return self._colorize(entry)

    def _colorize(self, entry: LogEntry) -> Text:
        text = Text(entry.raw)
        color = SEVERITY_COLORS.get(entry.level or "")
        if color:
            # Continuations are dimmed so an inherited level does not read as
            # a fresh entry at that severity.
            text.stylize(f"dim {color}" if entry.continuation else color)
        return text

    def _structured_renderable(self, entry: LogEntry) -> RenderableType | None:
        payload = (entry.message or "").strip()
        if not payload or len(payload) > STRUCTURED_PAYLOAD_MAX_CHARS:
            return None
        formatted = (
            self._format_json(payload) or self._format_xml(payload) or self._format_csv(payload)
        )
        if formatted is None:
            return None
        renderable, label = formatted
        return Group(
            self._colorize(entry),
            Panel(
                renderable,
                title=label,
                border_style=SEVERITY_COLORS.get(entry.level or "", "#94a3b8"),
                padding=(0, 1),
            ),
        )

    @staticmethod
    def _format_json(payload: str) -> tuple[RenderableType, str] | None:
        if not payload.startswith(("{", "[")):
            return None
        try:
            parsed = json.loads(payload)
        except ValueError:
            return None
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        return Syntax(pretty, "json", theme="ansi_dark"), "JSON"

    @staticmethod
    def _format_xml(payload: str) -> tuple[RenderableType, str] | None:
        if not payload.startswith("<"):
            return None
        try:
            dom = minidom.parseString(payload)
        except Exception:  # noqa: BLE001 - minidom raises broadly
            return None
        pretty = "\n".join(
            line for line in dom.toprettyxml(indent="  ").splitlines() if line.strip()
        )
        return Syntax(pretty, "xml", theme="ansi_dark"), "XML"

    def _format_csv(self, payload: str) -> tuple[RenderableType, str] | None:
        if "," not in payload:
            return None
        max_rows = self._config.csv_max_rows
        max_cols = self._config.csv_max_cols
        try:
            rows = list(itertools.islice(csv.reader(io.StringIO(payload)), max_rows))
        except csv.Error:
            return None
        if not rows:
            return None
        column_count = min(max((len(row) for row in rows), default=0), max_cols)
        if column_count == 0:
            return None

        table = Table(box=None, show_header=True, show_edge=False, pad_edge=False)
        for index in range(column_count):
            table.add_column(f"Col {index + 1}", overflow="fold")
        truncated = False
        for row in rows:
            if len(row) > column_count:
                truncated = True
            padded = list(row[:column_count])
            padded.extend([""] * (column_count - len(padded)))
            table.add_row(*padded)
        if truncated:
            table.add_row(*(["…"] * column_count))
        return table, "CSV preview"

    def _show_discovery_summary(self, report: DiscoveryReport) -> None:
        self.log_panel.clear()
        for line in report.summary_lines():
            self.log_panel.write(Text(line))
        self.log_panel.write(Text(""))
        if report.files:
            self.log_panel.write(Text("Select a log from the tree to begin.", style="dim"))
        else:
            self.log_panel.write(
                Text(
                    "Press 'a' to add a folder or file, or edit "
                    f"{self._settings_path}",
                    style="dim",
                )
            )
        if self._plugins.errors:
            self.log_panel.write(Text(""))
            for error in self._plugins.errors:
                self.log_panel.write(Text(f"Plugin problem — {error}", style="#facc15"))

    # --- status and chips ---------------------------------------------------

    def _update_status(self) -> None:
        # is_running is the reliable guard: the status bar only exists once the
        # app has a screen, and rendering is unit-tested without one.
        if self._is_shutting_down or not self.is_running:
            return
        try:
            status = self.query_one("#status-bar", Static)
        except NoMatches:
            return

        if self._selected_source is None:
            report = self._report
            found = report.file_count if report else 0
            status.update(f"No source selected · {found} file(s) discovered")
            return

        try:
            result = self._visible_entries(self._entries)
            shown = min(len(result.entries), self._show_lines)
            total = result.stats.total
            detail = f"{shown} shown / {result.stats.matched} matched / {total} buffered"
        except QueryError:
            detail = "invalid query"

        follow = "following" if self.state.auto_scroll else "paused"
        status.update(f"{self._selected_source} · {detail} · {follow}")

    def _refresh_chips(self) -> None:
        if self._is_shutting_down or not self.chip_bar.is_attached:
            return

        chips: list[FilterChip] = []
        if self.state.query:
            label = self.state.query
            if len(label) > 40:
                label = label[:39] + "…"
            chips.append(FilterChip(f"Query: {label}", key="query"))
        if self.state.severity != "all":
            chips.append(FilterChip(f"Severity: {self.state.severity.title()}", key="severity"))
        if self.state.time_window not in {"", "all"}:
            if self.state.time_window == "range" and self.state.custom_start:
                text = f"{self.state.custom_start} → {self.state.custom_end}"
            else:
                text = self.state.time_window
            chips.append(FilterChip(f"Time: {text}", key="time"))
        settings = self.advanced_drawer.settings
        if settings.invert_match:
            chips.append(FilterChip("Inverted", key="invert"))
        if settings.include_globs:
            chips.append(FilterChip(f"Include: {settings.include_globs}", key="include"))

        self.chip_bar.update_chips(chips)

    def _sync_regex_validation(self) -> None:
        self.query_bar.validate_entries(list(self._entries))

    # --- state --------------------------------------------------------------

    def _update_state(self, **changes) -> None:
        self.state = replace(self.state, **changes)
        if self._is_shutting_down or not self.is_mounted:
            return
        self._refresh_chips()

    def watch_state(self, old: SessionState, new: SessionState) -> None:  # type: ignore[override]
        if not self._persist_state:
            return
        if any(
            getattr(old, name) != getattr(new, name) for name in SessionState.PERSISTED_FIELDS
        ):
            self._store.save(new)

    def _notify(self, text: str, severity: Literal["info", "warning", "error"] = "info") -> None:
        """Surface a message as a toast.

        Messages deliberately do not go into the log pane any more: they used
        to be interleaved with log lines, which meant copy mode copied them.
        """

        mapped = {"info": "information", "warning": "warning", "error": "error"}.get(
            severity, "information"
        )
        try:
            self.notify(text, severity=mapped, title="", markup=False)
        except Exception:  # noqa: BLE001 - notification is best effort
            pass

    # --- actions ------------------------------------------------------------

    def action_focus_query(self) -> None:
        self.set_focus(self.query_bar.query_one("#query-input", Input))

    def action_clear_query(self) -> None:
        self.query_bar.set_query_value("")
        self._update_state(query="")
        self._sync_regex_validation()
        self._render_log()

    def action_cycle_time(self) -> None:
        self.query_bar.cycle_time_preset()

    def action_cycle_severity(self) -> None:
        self.query_bar.cycle_severity()

    def action_toggle_auto_scroll(self) -> None:
        """Flip auto-scroll from the keyboard."""
        self._set_auto_scroll(not self.state.auto_scroll)
        self._notify(
            "Following new lines." if self.state.auto_scroll else "Auto-scroll paused."
        )

    def action_toggle_structured(self) -> None:
        """Flip structured rendering of JSON/XML/CSV payloads."""
        self._set_structured(not self.state.pretty_rendering)
        self._notify(
            "Structured output on."
            if self.state.pretty_rendering
            else "Structured output off."
        )

    def action_toggle_advanced(self) -> None:
        self.advanced_drawer.toggle()
        self._refresh_plugin_status()

    def action_toggle_pane(self) -> None:
        """At the compact breakpoint, swap between tree and viewer."""
        self.toggle_class("-viewer-focused")
        if self.has_class("-viewer-focused"):
            self.set_focus(self.log_panel)
        else:
            try:
                self.query_one("#source-tree", LogTree).focus()
            except NoMatches:
                pass

    def _sync_compact_pane(self) -> None:
        """Show the pane that matters: the tree until a source is picked, the
        log once one is. Only meaningful at the compact breakpoint, where the
        two panes cannot share the screen."""

        self.set_class(self._selected_source is not None, "-viewer-focused")

    def action_shrink_sources_panel(self) -> None:
        self._adjust_panel_width(-SOURCES_PANEL_STEP)

    def action_expand_sources_panel(self) -> None:
        self._adjust_panel_width(SOURCES_PANEL_STEP)

    def _adjust_panel_width(self, delta: int) -> None:
        self._sources_panel_width = max(
            SOURCES_PANEL_MIN_WIDTH,
            min(SOURCES_PANEL_MAX_WIDTH, self._sources_panel_width + delta),
        )
        self._apply_panel_width()
        self._update_state(tree_width=self._sources_panel_width)

    def _apply_panel_width(self) -> None:
        # The one place a style is set at runtime: the width is user state, not
        # a layout decision, and CSS has no variable to carry it. At the
        # compact breakpoint the panes don't share the screen, so the stored
        # width must not override the full-width CSS.
        try:
            panel = self.query_one("#sources-panel", Vertical)
        except NoMatches:
            return
        if self._breakpoint == "-compact":
            panel.styles.width = "100%"
        else:
            panel.styles.width = self._sources_panel_width

    def action_more_lines(self) -> None:
        self._set_show_lines(self._show_lines + self._config.show_step)

    def action_fewer_lines(self) -> None:
        self._set_show_lines(self._show_lines - self._config.show_step)

    def _set_show_lines(self, value: int) -> None:
        updated = max(
            self._config.min_show_lines, min(value, self._config.max_buffer_lines)
        )
        if updated == self._show_lines:
            return
        self._show_lines = updated
        self._render_log()
        self._notify(f"Showing up to {self._show_lines} lines.")

    def action_toggle_copy_mode(self) -> None:
        self._copy_mode = not self._copy_mode
        self.set_class(self._copy_mode, "-copy-mode")
        if self._copy_mode:
            self.set_focus(self.log_panel)
        self._notify(
            "Copy mode on — chrome hidden." if self._copy_mode else "Copy mode off."
        )

    def action_add_source(self) -> None:
        self.run_worker(self._prompt_add_source(), group="dialogs", exit_on_error=False)

    def action_save_session(self) -> None:
        new_paths = self._source_manager.added_paths
        if not new_paths:
            self._notify("No new log sources to save.", "warning")
            return
        try:
            persist_log_sources(self._settings_path, self._source_manager.all_sources())
        except OSError as exc:
            self._notify(f"Failed to save settings: {exc}", "error")
            return
        self._source_manager.clear_added()
        self._notify(f"Saved {len(new_paths)} source(s) to {self._settings_path}.")

    async def action_reload_sources(self) -> None:
        selected = self._selected_source
        added = list(self._source_manager.added_paths)

        self._stop_tail()
        self._config = load_config()
        self._settings_path = get_config_file() or user_config_path()
        self._entries = deque(maxlen=self._config.max_buffer_lines)
        self._source_manager = SourceManager(*self._split_roots(self._config.log_dirs))
        for path in added:
            self._source_manager.add(str(path))

        await self._rescan()
        if selected is not None and selected.is_file():
            self._select_source(selected, announce=False)
        self._notify("Sources reloaded.")

    def action_quit_app(self) -> None:
        self.exit()

    # --- dialogs ------------------------------------------------------------

    async def _prompt_add_source(self) -> None:
        result = await self.push_screen(AddSourceDialog(), wait_for_dismiss=True)
        if result is None:
            self._notify("Add log source canceled.")
            return
        if not result.strip():
            self._notify("No path entered.", "warning")
            return

        addition = self._source_manager.add(result)
        for message in addition.messages:
            self._notify(message.text, message.severity)
        if not addition.messages and not addition.success:
            self._notify("Unable to add log source. Check the path and permissions.", "error")

        if addition.success:
            await self._rescan()
            if addition.path:
                self._highlight_source(addition.path)
        elif addition.path and self._source_manager.contains(addition.path):
            self._highlight_source(addition.path)

    async def _prompt_custom_range(self) -> None:
        dialog = CustomTimeRangeDialog(
            initial_start=self.state.custom_start,
            initial_end=self.state.custom_end,
        )
        result = await self.push_screen(dialog, wait_for_dismiss=True)
        if result is None:
            self.query_bar.restore_time_selection()
            return
        start, end = result
        self.query_bar.apply_custom_time_range(start, end, emit=True)

    # --- events -------------------------------------------------------------

    def on_tree_node_selected(self, event: Tree.NodeSelected[Path]) -> None:
        data = event.node.data
        if isinstance(data, Path) and data.is_file():
            self._select_source(data)

    def on_query_bar_action_triggered(self, message: QueryBar.ActionTriggered) -> None:
        handlers = {
            "add-source": self.action_add_source,
            "run-query": self._render_log,
            "clear-query": self.action_clear_query,
            "save-session": self.action_save_session,
        }
        handler = handlers.get(message.action_id)
        if handler is not None:
            handler()

    def on_query_bar_severity_changed(self, message: QueryBar.SeverityChanged) -> None:
        self._update_state(severity=message.value)
        self._render_log()

    def on_query_bar_time_window_changed(self, message: QueryBar.TimeWindowChanged) -> None:
        if message.value == "range" and message.start and message.end:
            self._update_state(
                time_window="range", custom_start=message.start, custom_end=message.end
            )
        else:
            self._update_state(time_window=message.value, custom_start="", custom_end="")
        self._render_log()

    def on_query_bar_custom_range_requested(self, _message: QueryBar.CustomRangeRequested) -> None:
        self.run_worker(self._prompt_custom_range(), group="dialogs", exit_on_error=False)

    def on_advanced_filters_drawer_settings_changed(
        self, message: AdvancedFiltersDrawer.SettingsChanged
    ) -> None:
        settings = message.settings
        self._update_state(
            include_globs=settings.include_globs,
            exclude_globs=settings.exclude_globs,
            follow_symlinks=settings.follow_symlinks,
            skip_binary=settings.skip_binary,
            case_sensitive=settings.case_sensitive,
            use_regex=settings.use_regex,
            invert_match=settings.invert_match,
        )
        if message.needs_rescan:
            # Discovery is comparatively expensive; only re-walk when the rules
            # that decide what is discovered actually changed.
            self.run_worker(self._rescan(), group="discovery", exit_on_error=False)
        else:
            self._sync_regex_validation()
            self._render_log()

    def on_advanced_filters_drawer_rescan_requested(
        self, _message: AdvancedFiltersDrawer.RescanRequested
    ) -> None:
        self.run_worker(self._rescan(), group="discovery", exit_on_error=False)

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[override]
        if event.input.id != "query-input":
            return
        self._update_state(query=event.value)
        self._sync_regex_validation()
        self._render_log()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        # Only the query bar's switches reach here; the drawer stops its own
        # and reports them as ViewToggleChanged.
        if event.switch.id == "auto-scroll-toggle":
            self._set_auto_scroll(event.value)
        elif event.switch.id == "pretty-structured-toggle":
            self._set_structured(event.value)

    def on_advanced_filters_drawer_view_toggle_changed(
        self, message: AdvancedFiltersDrawer.ViewToggleChanged
    ) -> None:
        if message.field == "auto_scroll":
            self._set_auto_scroll(message.value)
        else:
            self._set_structured(message.value)

    # --- view state -----------------------------------------------------------
    #
    # Auto-scroll and structured output each have two controls: one in the query
    # bar and a mirror in the Advanced drawer, exactly one of which is visible
    # at a time. Both funnel through here so the state has a single owner and
    # the two copies cannot drift apart.

    def _set_auto_scroll(self, value: bool) -> None:
        self._update_state(auto_scroll=value)
        self.log_panel.auto_scroll = value
        if value:
            self.log_panel.scroll_end(animate=False)
        self._sync_view_toggles()
        self._update_status()

    def _set_structured(self, value: bool) -> None:
        self._update_state(pretty_rendering=value)
        self._sync_view_toggles()
        self._render_log()

    def _sync_view_toggles(self) -> None:
        """Push the canonical view state onto both sets of controls."""

        if self._is_shutting_down or not self.is_mounted:
            return
        try:
            # prevent(), not a flag: Switch.Changed is posted asynchronously,
            # so a flag would already be cleared when the handler ran and the
            # echo would come back as a fresh user action.
            with self.prevent(Switch.Changed):
                self.query_bar.set_auto_scroll(self.state.auto_scroll)
                self.query_bar.set_pretty_rendering(self.state.pretty_rendering)
        except NoMatches:
            pass
        self.advanced_drawer.sync_view_toggles(
            auto_scroll=self.state.auto_scroll,
            structured=self.state.pretty_rendering,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if (event.button.id or "") == "toggle-advanced":
            self.action_toggle_advanced()

    def on_filter_chip_dismissed(self, message: FilterChip.Dismissed) -> None:
        self._dismiss_chip(message.key)

    def _dismiss_chip(self, key: str) -> None:
        if key == "query":
            self.action_clear_query()
            return
        if key == "severity":
            self.query_bar.set_severity("all")
            self._update_state(severity="all")
        elif key == "time":
            self.query_bar.select_time("all")
            self._update_state(time_window="all", custom_start="", custom_end="")
        elif key == "invert":
            self.advanced_drawer._settings = replace(
                self.advanced_drawer.settings, invert_match=False
            )
            self._update_state(invert_match=False)
        elif key == "include":
            self.advanced_drawer._settings = replace(
                self.advanced_drawer.settings, include_globs=""
            )
            self._update_state(include_globs="")
            self.run_worker(self._rescan(), group="discovery", exit_on_error=False)
            return
        self._render_log()

    def _refresh_plugin_status(self) -> None:
        parts = []
        if self._plugins.total:
            parts.append(
                f"{len(self._plugins.sources)} source, "
                f"{len(self._plugins.filters)} filter, "
                f"{len(self._plugins.exporters)} exporter plugin(s) loaded"
            )
        else:
            parts.append("No plugins loaded")
        if self._plugins.errors:
            parts.append(f"{len(self._plugins.errors)} failed: " + "; ".join(
                str(error) for error in self._plugins.errors[:3]
            ))
        self.advanced_drawer.set_plugin_status(" · ".join(parts))

    async def on_unmount(self) -> None:
        self._is_shutting_down = True
        self._stop_tail()
        if self._persist_state:
            # Persist as-is: the selected source is deliberately kept so the
            # next launch reopens it.
            self._store.save(self.state)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _find_node(node: TreeNode[Path], target: Path) -> Optional[TreeNode[Path]]:
    if isinstance(node.data, Path) and _resolve(node.data) == target:
        return node
    for child in node.children:
        found = _find_node(child, target)
        if found is not None:
            return found
    return None


def run() -> None:  # pragma: no cover - script entry point
    LogViewerApp().run()
