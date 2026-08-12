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
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Button, Footer, Input, Label, Static, Switch, Tree
from textual.widgets._tree import TreeNode

from .plugins import Exporter, FilterContext, PluginError, PluginRegistry, load_plugins
from .services import SourceManager, persist_log_sources
from .services.clipboard import prepare_payload
from .services.config import LogConfig, get_config_file, load_config, user_config_path
from .services.discovery import DiscoveryReport, discover
from .services.export import (
    BUILTIN_FORMATS,
    builtin_format,
    default_stem,
    describe_formats,
    write_atomically,
)
from .services.filtering import (
    FilterSpec,
    QueryError,
    TimeWindow,
    align_moments,
    compile_query,
    describe_empty_result,
    filter_entries,
    parse_absolute_window,
    parse_moment,
    parse_relative_window,
)
from .services.parsing import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_WARN,
    LogEntry,
    LogParser,
    level_matches,
)
from .services.reader import AnyReader, open_reader
from .storage import SessionState, StateStore
from .widgets.add_source_dialog import AddSourceDialog
from .widgets.advanced_drawer import AdvancedFiltersDrawer, AdvancedSettings
from .widgets.custom_time_dialog import CustomTimeRangeDialog
from .widgets.detail_pane import DetailPane
from .widgets.export_dialog import ExportChoice, ExportDialog, ExportRequest
from .widgets.filter_chip import FilterChip, FilterChips
from .widgets.goto_dialog import GotoDialog
from .widgets.help_overlay import HelpOverlay, HelpSection
from .widgets.log_view import LogView
from .widgets.query_bar import QueryBar

#: What `n`/`N` step between when there is no query and no severity bucket to
#: take the definition from. Stepping every entry would just duplicate the down
#: arrow; WARN and above is what someone scanning a tail is looking for, and
#: warnings are included because they are usually what precedes the failure.
NOTABLE_LEVELS: frozenset[str] = frozenset({LEVEL_WARN, LEVEL_ERROR, LEVEL_CRITICAL})

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
#: line together. Measured, not guessed: the row needs 147 columns, so this
#: leaves one to spare. It moved from 136 when the Star button was added to the
#: action row, and tests/test_query_bar.py checks that nothing sits off-screen
#: at exactly this width — so widening that row again fails the build until the
#: number is re-measured.
BREAKPOINT_MERGE = 148

SOURCES_PANEL_MIN_WIDTH = 20
SOURCES_PANEL_MAX_WIDTH = 120
SOURCES_PANEL_STEP = 4

ICON_FOLDER = "📂"
ICON_FILE = "📄"
#: Replaces the file icon on a starred log rather than sitting beside it, so
#: starring never changes a row's width.
ICON_STAR = "⭐"
STARRED_GROUP = f"{ICON_STAR} Starred"

#: Help categories, in the order the overlay lists them. A category with no
#: bindings is skipped, so a group can be declared before the item that fills
#: it: Navigation is empty until a line cursor exists to navigate with.
HELP_CATEGORY_ORDER: tuple[str, ...] = (
    "Help",
    "Search",
    "Navigation",
    "View",
    "Sources",
    "Session",
    "Other",
)

#: Which category each binding belongs to, keyed by action name. Kept parallel
#: to ``BINDINGS`` rather than as a ``Binding`` argument so binding
#: construction stays exactly what Textual documents. An action missing here
#: still appears in the overlay under "Other" — help is generated, so it can
#: never go stale — and ``test_help_overlay`` fails on the omission so the
#: fallback stays a safety net rather than a destination.
BINDING_CATEGORIES: dict[str, str] = {
    "show_help": "Help",
    "focus_query": "Search",
    "clear_query": "Search",
    "cycle_time": "Search",
    "cycle_severity": "Search",
    "toggle_advanced": "Search",
    # LogView owns the cursor keys. They are bound on the widget rather than
    # the app so they cannot fight the source tree or a text input, and they
    # are folded into the overlay by build_help_sections rather than written
    # out by hand — help stays generated.
    "cursor_up": "Navigation",
    "cursor_down": "Navigation",
    "cursor_page_up": "Navigation",
    "cursor_page_down": "Navigation",
    "cursor_home": "Navigation",
    "cursor_end": "Navigation",
    "select_cursor": "Navigation",
    "toggle_detail": "Navigation",
    "next_match": "Navigation",
    "previous_match": "Navigation",
    "goto_timestamp": "Navigation",
    "toggle_auto_scroll": "View",
    "toggle_structured": "View",
    "export_view": "View",
    "copy_view": "View",
    "toggle_pane": "View",
    "shrink_sources_panel": "View",
    "expand_sources_panel": "View",
    "more_lines": "View",
    "fewer_lines": "View",
    "toggle_copy_mode": "View",
    "add_source": "Sources",
    "toggle_star": "Sources",
    "reload_sources": "Sources",
    "save_session": "Session",
    "quit_app": "Session",
}


def build_help_sections(
    bindings: Iterable[Binding],
    categories: dict[str, str] | None = None,
) -> list[HelpSection]:
    """Group *bindings* into the overlay's sections, in declaration order.

    Pure and app-free so the grouping can be tested without running the app,
    and so the overlay widget never has to reach back into ``clv.app``.
    """

    lookup = BINDING_CATEGORIES if categories is None else categories
    grouped: dict[str, list[tuple[str, str]]] = {}
    for binding in bindings:
        category = lookup.get(binding.action, "Other")
        grouped.setdefault(category, []).append(
            (binding.key, binding.description or binding.action)
        )

    ordered = [name for name in HELP_CATEGORY_ORDER if name in grouped]
    # A category invented by a caller's map is still listed, after the known
    # ones, rather than silently dropping the bindings it holds.
    ordered += [name for name in grouped if name not in HELP_CATEGORY_ORDER]
    return [HelpSection(name, tuple(grouped[name])) for name in ordered]


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

    /* Holds the log pane and the detail pane. The split direction is the only
       thing decided here — how much room the detail pane takes is its own CSS,
       keyed off the breakpoint class mirrored onto it. */
    #log-area {
        layout: vertical;
        width: 1fr;
        height: 1fr;
    }

    LogViewerApp.-wide #log-area { layout: horizontal; }

    #log-stream {
        width: 1fr;
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

    /* 80 columns cannot hold both panes at a readable width, so the detail
       pane takes the viewer and the log goes behind it. Esc-free by design:
       `d` closes it and the log is back. */
    LogViewerApp.-compact #log-area.-detail #log-stream { display: none; }

    /* Copy mode strips the chrome so a mouse selection grabs log text only —
       which includes the detail pane, whose property table is not log text. */
    LogViewerApp.-copy-mode #query-bar,
    LogViewerApp.-copy-mode #chip-bar,
    LogViewerApp.-copy-mode #advanced-drawer,
    LogViewerApp.-copy-mode #sources-panel,
    LogViewerApp.-copy-mode #status-bar,
    LogViewerApp.-copy-mode DetailPane,
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
        # First in the list on purpose. The footer fills from the left and
        # drops from the right, so the binding that documents all the others
        # is the one that must never be the entry that falls off.
        Binding("question_mark", "show_help", "Help", show=True),
        Binding("/", "focus_query", "Query", show=True),
        Binding("escape", "clear_query", "Clear", show=True),
        Binding("a", "add_source", "Add source", show=True),
        # Ordered before the filter bindings deliberately. The footer drops
        # entries from the right as it runs out of room, and at 80 columns
        # Star was being truncated to a bare "*".
        Binding("asterisk", "toggle_star", "Star", show=True),
        Binding("t", "cycle_time", "Time", show=True),
        Binding("s", "cycle_severity", "Severity", show=True),
        Binding("f", "toggle_advanced", "Advanced", show=True),
        # The auto-scroll and structured switches are only shown when the query
        # bar merges its rows, so they need a keyboard path that does not
        # depend on terminal width.
        Binding("w", "toggle_auto_scroll", "Follow", show=True),
        Binding("o", "toggle_structured", "Structured", show=False),
        Binding("d", "toggle_detail", "Detail pane", show=False),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "previous_match", "Previous match", show=False),
        Binding("g", "goto_timestamp", "Go to timestamp", show=False),
        Binding("ctrl+b", "toggle_pane", "Switch pane", show=True),
        Binding("[", "shrink_sources_panel", "Narrower", show=False),
        Binding("]", "expand_sources_panel", "Wider", show=False),
        Binding("+", "more_lines", "More lines", show=False),
        Binding("-", "fewer_lines", "Fewer lines", show=False),
        Binding("ctrl+l", "toggle_copy_mode", "Copy mode", show=True),
        # Hidden, like every binding added after the footer filled up at 80
        # columns; `?` is how they are found. Note that Textual's Input binds
        # ctrl+e to end-of-line, so this fires everywhere except inside the
        # query input — deliberately not `priority`, since stealing a
        # text-editing key from the input is the worse trade.
        Binding("ctrl+e", "export_view", "Export view", show=False),
        Binding("y", "copy_view", "Copy to clipboard", show=False),
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
        self._reader: AnyReader | None = None
        self._parser = LogParser()
        self._entries: deque[LogEntry] = deque(maxlen=self._config.max_buffer_lines)
        self._tail_timer: Timer | None = None

        self._show_lines = self._config.default_show_lines
        self._sources_panel_width = self._config.tree_width
        self._copy_mode = False
        self._breakpoint = ""
        self._merged = False
        self._plugins: PluginRegistry = PluginRegistry()

        #: Set when the cursor moved off the last line and suspended follow, so
        #: the status line can say *why* it is paused rather than just that it
        #: is. Cleared by anything that resumes following.
        self._follow_suspended_by_cursor = False
        #: (position, total, label) from the last `n`/`N`, mirrored into the
        #: status bar. The query bar has its own copy, but #match-count is
        #: hidden at the compact breakpoint and this is not.
        self._match_position: tuple[int, int, str] | None = None
        #: Rows `n`/`N` step between, cached per render. Recomputing per cursor
        #: move would re-run the query regex over every visible line on every
        #: arrow keypress; the set only changes when the pane is rebuilt.
        self._navigation_cache: tuple[list[int], str] | None = None

        self.query_bar = QueryBar()
        self.chip_bar = FilterChips(id="chip-bar")
        self.advanced_drawer = AdvancedFiltersDrawer()
        self.log_panel = LogView(
            id="log-stream",
            wrap=True,
            max_rows=self._config.max_buffer_lines,
        )
        self.detail_pane = DetailPane(id="detail-pane")

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
                with Container(id="log-area"):
                    yield self.log_panel
                    yield self.detail_pane
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
        # Nothing is opened implicitly: the viewer starts on the discovery
        # summary rather than resuming whatever happened to be open last.
        # A starred log is different — it was chosen for this.
        await self._rescan()
        self._open_starred_on_launch()

        self._refresh_chips()
        self._sync_star_button()
        self._sync_detail_pane()
        # Filled at mount rather than only when the drawer opens, so the plugin
        # and exporter lines are correct the first time it is seen.
        self._refresh_plugin_status()
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
            clipboard=self.state.clipboard_osc52,
            detail_pane=self.state.detail_pane,
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

        targets = [self, self.query_bar, self.advanced_drawer, self.detail_pane]
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

        # Starred logs are repeated in a group at the top. With the tree
        # collapsed by default, a favourite would otherwise cost a walk down
        # the hierarchy on every launch.
        starred = self._starred_paths()
        present = sorted(
            (item.path for item in report.files if _resolve(item.path) in starred),
            key=lambda p: str(p).lower(),
        )
        if present:
            group = tree.root.add(STARRED_GROUP, data=None, expand=True)
            for path in present:
                group.add_leaf(f"{ICON_STAR} {_compact_path(path)}", data=path)

        # One branch per configured root, then a folder hierarchy beneath it.
        by_root: dict[Path, list] = {}
        for item in report.files:
            by_root.setdefault(item.root, []).append(item)

        for root in sorted(by_root, key=lambda p: str(p).lower()):
            items = by_root[root]
            if len(items) == 1 and items[0].path == root:
                # The operator named this single file directly.
                tree.root.add_leaf(self._leaf_label(root, str(root)), data=root)
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
                parent.add_leaf(
                    self._leaf_label(item.path, item.path.name), data=item.path
                )

        tree.focus()

    def _starred_paths(self) -> set[Path]:
        return {_resolve(Path(entry)) for entry in self.state.starred}

    def _leaf_label(self, path: Path, text: str) -> str:
        icon = ICON_STAR if _resolve(path) in self._starred_paths() else ICON_FILE
        return f"{icon} {text}"

    def _highlight_source(self, path: Path, *, select: bool = True) -> None:
        """Reveal *path* in the tree.

        ``select=False`` moves the cursor without selecting, because
        ``select_node`` posts NodeSelected and would open the log. Starring a
        file should bookmark it, not open it.
        """

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
        def _reveal() -> None:
            if select:
                tree.select_node(node)
            else:
                tree.move_cursor(node)
            tree.scroll_to_node(node)

        self.call_after_refresh(_reveal)

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

        reader = open_reader(resolved, max_lines=self._config.max_buffer_lines)
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
        self._sync_star_button()
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
            notice = self._reader.RELOAD_NOTICE.format(name=self._reader.path.name)
            self._notify(notice, "warning")
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
        # Which line the cursor was on, so a filter change does not send it
        # back to the top. Captured before the pane is cleared.
        previous_entry = self.log_panel.cursor_entry
        previous_index = self.log_panel.cursor
        # The visible set is about to change, so both the navigation targets
        # and any position into them are stale.
        self._navigation_cache = None
        self._match_position = None
        self.log_panel.clear()

        if self._selected_source is None:
            if self._report is not None:
                self._show_discovery_summary(self._report)
            self._sync_detail_pane()
            return

        if not self._entries:
            self.log_panel.write(Text("No log entries in the selected source.", style="dim"))
            self._sync_detail_pane()
            return

        try:
            result = self._visible_entries(self._entries)
        except QueryError as exc:
            self.log_panel.write(Text(f"Invalid query: {exc}", style="bold #f87171"))
            self._sync_detail_pane()
            return

        if not result.entries:
            message = describe_empty_result(result.stats, self._filter_spec())
            self.log_panel.write(Text(message, style="dim"))
            self._sync_detail_pane()
            return

        for entry in result.entries[-self._show_lines :]:
            self.log_panel.write_entry(self._renderable_for(entry), entry)

        self._restore_cursor(previous_entry, previous_index)
        if scroll_end or self.state.auto_scroll:
            self.log_panel.scroll_end(animate=False)
        self._update_status()

    def _restore_cursor(self, entry: LogEntry | None, index: int) -> None:
        """Put the cursor back where it was, or as near as the new view allows.

        Resetting to the top on every keystroke in the query box would make the
        cursor useless while filtering, which is exactly when it is wanted. The
        selected line wins when it survived; when it did not, the nearest
        surviving line does — never the top.
        """

        if entry is None or index < 0:
            return
        if self.log_panel.move_cursor_to_entry(entry, near=index):
            return
        self.log_panel.clamp_cursor(index)

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
            self.log_panel.write_entry(self._renderable_for(entry), entry)

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

        if self.state.auto_scroll:
            follow = "following"
        elif self._follow_suspended_by_cursor:
            # Say *why* it stopped. Incoming lines fighting a cursor the
            # operator just moved is the failure mode this avoids, and a bare
            # "paused" would look like the app had decided on its own.
            follow = "paused — cursor moved, End resumes"
        else:
            follow = "paused"

        parts = [str(self._selected_source), detail]
        if self._match_position is not None:
            position, total, label = self._match_position
            parts.append(f"{label} {position} of {total}")
        parts.append(follow)
        status.update(" · ".join(parts))

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

    # --- match navigation ---------------------------------------------------

    def _navigation_targets(self) -> tuple[list[int], str]:
        """Rows `n`/`N` step between, and what to call one in a notification.

        With a query active these are the query's matches. Because the query
        *filters* rather than highlights, that is normally every line on
        screen — the value `n` adds there is the "3 of 47" position readout and
        the wrap notice, not a different destination. With `Invert match` on it
        genuinely differs, and with no query at all it falls back to severity,
        which is the case that earns the key.
        """

        if self._navigation_cache is not None:
            return self._navigation_cache
        targets = self._compute_navigation_targets()
        self._navigation_cache = targets
        return targets

    def _compute_navigation_targets(self) -> tuple[list[int], str]:
        spec = self._filter_spec()
        rows = self.log_panel.entry_rows()

        if spec.query:
            try:
                pattern = compile_query(
                    spec.query, case_sensitive=spec.case_sensitive, regex=spec.regex
                )
            except QueryError:
                return [], "match"
            if pattern is not None:
                return [
                    index for index, entry in rows if pattern.search(entry.raw)
                ], "match"

        if spec.severity != "all":
            return [
                index for index, entry in rows if level_matches(entry.level, spec.severity)
            ], f"{spec.severity} entry"

        return [
            index for index, entry in rows if entry.level in NOTABLE_LEVELS
        ], "warning or worse"

    def action_next_match(self) -> None:
        self._step_match(1)

    def action_previous_match(self) -> None:
        self._step_match(-1)

    def _step_match(self, direction: int) -> None:
        if self._selected_source is None:
            self._notify("Open a log before navigating matches.", "warning")
            return

        targets, label = self._navigation_targets()
        if not targets:
            self._notify(f"No {label} to jump to.", "warning")
            self._sync_match_position()
            return

        cursor = self.log_panel.cursor
        if direction > 0:
            following = [index for index in targets if index > cursor]
            target = following[0] if following else targets[0]
            wrapped = not following
        else:
            preceding = [index for index in targets if index < cursor]
            target = preceding[-1] if preceding else targets[-1]
            wrapped = not preceding

        self.log_panel.move_cursor(target)
        self._sync_match_position()
        self._update_status()
        if wrapped:
            # Say it rather than stopping dead at the end: silently refusing to
            # move looks like a broken key.
            position = targets.index(target) + 1
            self._notify(
                f"Wrapped to the {'first' if direction > 0 else 'last'} {label} "
                f"({position} of {len(targets)})."
            )

    def _sync_match_position(self) -> None:
        """Report where the cursor sits within the navigation targets.

        Recomputed on every cursor move, not only on `n`/`N`, so arrowing onto
        a match updates the counter instead of leaving a stale one on screen.
        """

        targets, label = self._navigation_targets()
        cursor = self.log_panel.cursor
        if cursor >= 0 and cursor in targets:
            position = targets.index(cursor) + 1
            self._match_position = (position, len(targets), label)
        else:
            self._match_position = None
        self.query_bar.set_match_position(
            None if self._match_position is None else self._match_position[0]
        )

    def action_goto_timestamp(self) -> None:
        self.run_worker(self._prompt_goto(), group="dialogs", exit_on_error=False)

    def action_toggle_detail(self) -> None:
        """Show or hide the event detail pane."""
        self._set_detail_pane(not self.state.detail_pane)
        self._notify(
            "Detail pane open." if self.state.detail_pane else "Detail pane closed."
        )

    def action_toggle_structured(self) -> None:
        """Flip structured rendering of JSON/XML/CSV payloads."""
        self._set_structured(not self.state.pretty_rendering)
        self._notify(
            "Structured output on."
            if self.state.pretty_rendering
            else "Structured output off."
        )

    def _star_target(self) -> Optional[Path]:
        """The log that starring would act on.

        The tree cursor wins while the tree has focus — that is what the
        operator is pointing at. Otherwise it is the log on screen, so the
        toolbar button stars what you are reading rather than something the
        cursor happens to be resting on.
        """

        try:
            tree = self.query_one("#source-tree", LogTree)
        except NoMatches:
            tree = None

        def cursor_file() -> Optional[Path]:
            if tree is None or tree.cursor_node is None:
                return None
            data = tree.cursor_node.data
            return data if isinstance(data, Path) and data.is_file() else None

        if tree is not None and tree.has_focus:
            target = cursor_file()
            if target is not None:
                return target
        if self._selected_source is not None:
            return self._selected_source
        return cursor_file()

    def _sync_star_button(self) -> None:
        """Show whether the star target is starred, or disable when there is none."""

        if self._is_shutting_down or not self.is_mounted:
            return
        target = self._star_target()
        starred = None if target is None else str(_resolve(target)) in self.state.starred
        self.query_bar.set_star_state(starred)

    async def action_toggle_star(self) -> None:
        """Star or unstar the log the star target resolves to."""

        data = self._star_target()
        if data is None:
            self._notify("Open a log, or move the tree cursor to one, to star it.", "warning")
            return

        key = str(_resolve(data))
        starred = set(self.state.starred)
        if key in starred:
            starred.discard(key)
            message = f"Unstarred {data.name}."
        else:
            starred.add(key)
            message = f"Starred {data.name}."
        self._update_state(starred=tuple(sorted(starred)))

        # Rebuild from the report already in hand rather than re-walking the
        # filesystem, then put the cursor back where the operator left it.
        if self._report is not None:
            await self._build_tree(self._report)
            self._highlight_source(data, select=False)
        self._sync_star_button()
        self._notify(message)

    def _open_starred_on_launch(self) -> None:
        """Open a starred log when exactly one of them is available.

        Several stars are a set of favourites rather than an instruction about
        what to open, so the summary stays put and the group is there to jump
        from. A star pointing at something no longer discoverable is reported
        and kept: rotated logs come back.
        """

        report = self._report
        if report is None or not self.state.starred:
            return

        discovered = {_resolve(item.path) for item in report.files}
        starred = [Path(entry) for entry in self.state.starred]
        present = [path for path in starred if _resolve(path) in discovered]

        for path in starred:
            if _resolve(path) not in discovered:
                self._notify(f"Starred log is not available: {path}", "warning")

        if len(present) == 1:
            self._select_source(present[0], announce=False)

    def action_show_help(self) -> None:
        """Open the binding list. Tailing continues behind it."""

        # `?` reaches this action from the overlay too, where it closes rather
        # than reopening; guard anyway so it can never stack two overlays.
        if isinstance(self.screen, HelpOverlay):
            return
        # LogView's cursor keys are bound on the widget, not the app, so they
        # have to be handed in explicitly — still generated from Binding
        # objects, so a key added there can no more go missing from the overlay
        # than one added here.
        bindings = list(self.BINDINGS) + list(LogView.BINDINGS)
        self.push_screen(HelpOverlay(build_help_sections(bindings)))

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

    def action_copy_view(self) -> None:
        """Put the visible lines on the local clipboard via OSC 52.

        The counterpart to `Ctrl+L`, not a replacement for it: copy mode needs a
        local terminal selection, which is exactly what is unavailable over tmux
        or SSH, and this path needs a terminal that honours OSC 52. Whichever
        one an operator's setup supports, one of them works.
        """

        if self._selected_source is None:
            self._notify("Open a log before copying.", "warning")
            return
        if not self.state.clipboard_osc52:
            self._notify(
                "Clipboard copy is off — enable it in the Advanced drawer, "
                "or use Ctrl+L copy mode.",
                "warning",
            )
            return

        try:
            result = self._visible_entries(self._entries)
        except QueryError as exc:
            self._notify(f"Cannot copy while the query is invalid: {exc}", "error")
            return

        # The lines on screen, filter and window included — the same slice
        # _render_log writes. Ctrl+E is the path for the whole filtered set.
        visible = result.entries[-self._show_lines :]
        payload = prepare_payload(
            [entry.raw for entry in visible],
            max_bytes=self._config.clipboard_max_bytes,
        )
        if payload.empty:
            self._notify(
                payload.summary
                if not payload.truncated
                else "That line is larger than clipboard_max_bytes; nothing copied.",
                "warning",
            )
            return

        try:
            self.copy_to_clipboard(payload.text)
        except Exception as exc:  # noqa: BLE001 - terminal-dependent
            # Guarded so a terminal that rejects the sequence cannot leave a
            # half-written escape on screen or take the app down with it.
            self._notify(f"Clipboard copy failed: {exc}", "error")
            return
        self._notify(payload.summary, "warning" if payload.truncated else "info")

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

    def action_export_view(self) -> None:
        """Write the entries the filters kept to a file."""
        self.run_worker(self._prompt_export(), group="dialogs", exit_on_error=False)

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

    async def _prompt_goto(self) -> None:
        """Move the cursor to the first entry at or after a moment in time."""

        if self._selected_source is None:
            self._notify("Open a log before jumping to a timestamp.", "warning")
            return

        typed = await self.push_screen(GotoDialog(), wait_for_dismiss=True)
        if typed is None:
            return

        moment = parse_moment(typed)
        if moment is None:
            self._notify(
                f"Could not read {typed!r} as a time. Try 2026-08-07 09:25:01 or -15m.",
                "warning",
            )
            return

        rows = self.log_panel.entry_rows()
        # Entries with no timestamp cannot answer a question about time, so they
        # are skipped — and counted, because a pane that quietly ignored half a
        # source would be the same silent loss the severity filter is careful to
        # avoid.
        undated = sum(1 for _, entry in rows if entry.timestamp is None)

        def at_or_after(entry: LogEntry) -> bool:
            if entry.timestamp is None:
                return False
            stamp, target_moment = align_moments(entry.timestamp, moment)
            return stamp >= target_moment

        target = next((index for index, entry in rows if at_or_after(entry)), None)

        skipped = f" ({undated} with no timestamp skipped)" if undated else ""
        if target is None:
            self._notify(
                f"No entry at or after {moment:%Y-%m-%d %H:%M:%S}{skipped}.", "warning"
            )
            return

        self.log_panel.move_cursor(target)
        self._update_status()
        self._notify(f"Moved to {moment:%Y-%m-%d %H:%M:%S} or later{skipped}.")

    def _exporter_choices(self) -> list[ExportChoice]:
        """Built-ins first, then whatever the plugin registry supplied.

        Plugin exporters are marked ``needs_path=False``: ``Exporter.export``
        receives the entries and a :class:`FilterContext` and nothing else, so
        the destination is theirs to choose and the dialog's path input does not
        apply to them.
        """

        choices = [
            ExportChoice(f"builtin:{fmt.key}", fmt.label, fmt.extension)
            for fmt in BUILTIN_FORMATS
        ]
        choices += [
            ExportChoice(
                f"plugin:{index}", f"{_plugin_name(exporter)} (plugin)", needs_path=False
            )
            for index, exporter in enumerate(self._plugins.exporters)
        ]
        return choices

    async def _prompt_export(self) -> None:
        if self._selected_source is None:
            self._notify("Open a log before exporting.", "warning")
            return

        try:
            # The whole filtered set, deliberately not the `_show_lines` window:
            # an export is the answer to "save what I filtered", not "save what
            # happens to fit on screen".
            result = self._visible_entries(self._entries)
        except QueryError as exc:
            self._notify(f"Cannot export while the query is invalid: {exc}", "error")
            return

        entries = list(result.entries)
        if not entries:
            self._notify("Nothing to export — no entries match the filters.", "warning")
            return

        dialog = ExportDialog(
            self._exporter_choices(),
            entry_count=len(entries),
            default_name=default_stem(self._selected_source),
        )
        request = await self.push_screen(dialog, wait_for_dismiss=True)
        if request is None:
            self._notify("Export canceled.")
            return
        self._run_export(request, entries)

    def _run_export(self, request: ExportRequest, entries: list[LogEntry]) -> None:
        if request.key.startswith("builtin:"):
            self._export_builtin(request, entries)
        else:
            self._export_via_plugin(request, entries)

    def _export_builtin(self, request: ExportRequest, entries: list[LogEntry]) -> None:
        fmt = builtin_format(request.key.split(":", 1)[1])
        if fmt is None or request.path is None:  # pragma: no cover - defensive
            self._notify("Unknown export format.", "error")
            return
        try:
            written = write_atomically(request.path, entries, fmt.writer)
        except OSError as exc:
            # Permissions, a missing directory, a full disk: reported, never a
            # traceback, and the destination is left as it was.
            self._notify(f"Export failed: {exc}", "error")
            return
        self._notify(f"Exported {_plural(written, 'entry', 'entries')} to {request.path}.")

    def _export_via_plugin(self, request: ExportRequest, entries: list[LogEntry]) -> None:
        exporter = self._exporter_at(request.key)
        if exporter is None:  # pragma: no cover - defensive
            self._notify("That exporter is no longer available.", "error")
            return
        name = _plugin_name(exporter)
        try:
            outcome = exporter.export(entries, self._plugin_context())
        except Exception as exc:  # noqa: BLE001 - third-party code
            # Same contract as a FilterStage that raises: recorded, surfaced,
            # and survivable. An export must never take down the app.
            self._plugins.errors.append(PluginError(name, f"raised: {exc}"))
            self._refresh_plugin_status()
            self._notify(f"Exporter {name} failed: {exc}", "error")
            return

        if outcome is None or not getattr(outcome, "ok", False):
            detail = getattr(outcome, "detail", "") or "reported a failure"
            self._notify(f"Exporter {name}: {detail}", "warning")
            return
        detail = outcome.detail or f"exported {_plural(len(entries), 'entry', 'entries')}"
        destination = f" → {outcome.destination}" if outcome.destination else ""
        self._notify(f"Exporter {name}: {detail}{destination}")

    def _exporter_at(self, key: str) -> Optional[Exporter]:
        try:
            index = int(key.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        exporters = self._plugins.exporters
        return exporters[index] if 0 <= index < len(exporters) else None

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

    def on_tree_node_highlighted(self, _event: Tree.NodeHighlighted[Path]) -> None:
        # The star target follows the cursor while the tree has focus, so the
        # button has to follow it too.
        self._sync_star_button()

    def on_descendant_focus(self, _event) -> None:
        # Focus moving between the tree and the rest of the UI changes which
        # log the star button would act on.
        self._sync_star_button()

    def on_query_bar_action_triggered(self, message: QueryBar.ActionTriggered) -> None:
        if message.action_id == "toggle-star":
            # Async action, so it runs as a worker rather than blocking here.
            self.run_worker(self.action_toggle_star(), group="star", exit_on_error=False)
            return
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
        elif message.field == "clipboard":
            self._set_clipboard_enabled(message.value)
        elif message.field == "detail_pane":
            self._set_detail_pane(message.value)
        else:
            self._set_structured(message.value)

    def on_log_view_cursor_moved(self, message: LogView.CursorMoved) -> None:
        """Follow mode and the detail pane both track the cursor.

        Moving the cursor off the last line suspends following, because the
        alternative is incoming lines dragging the view out from under whoever
        just pointed at something. `End` (or `w`) puts it back.
        """

        if self.state.auto_scroll and not message.at_end:
            self._set_auto_scroll(False, cursor_suspended=True)
            self._notify("Auto-scroll paused — End resumes following.")
        elif message.at_end and self._follow_suspended_by_cursor:
            self._set_auto_scroll(True)
            self._notify("Following new lines.")
        if self.state.detail_pane:
            self.detail_pane.show(message.entry)
        self._sync_match_position()
        self._update_status()

    def on_log_view_entry_selected(self, message: LogView.EntrySelected) -> None:
        """Enter on a line opens the detail pane on it."""

        if not self.state.detail_pane:
            self._set_detail_pane(True)
        self.detail_pane.show(message.entry)

    # --- view state -----------------------------------------------------------
    #
    # Auto-scroll and structured output each have two controls: one in the query
    # bar and a mirror in the Advanced drawer, exactly one of which is visible
    # at a time. Both funnel through here so the state has a single owner and
    # the two copies cannot drift apart.

    def _set_auto_scroll(self, value: bool, *, cursor_suspended: bool = False) -> None:
        self._update_state(auto_scroll=value)
        self.log_panel.auto_scroll = value
        # Resuming clears the reason, whichever control resumed it.
        self._follow_suspended_by_cursor = cursor_suspended and not value
        if value:
            self.log_panel.scroll_end(animate=False)
        self._sync_view_toggles()
        self._update_status()

    def _set_detail_pane(self, value: bool) -> None:
        self._update_state(detail_pane=value)
        self._sync_view_toggles()
        self._sync_detail_pane()

    def _set_structured(self, value: bool) -> None:
        self._update_state(pretty_rendering=value)
        self._sync_view_toggles()
        self._render_log()

    def _set_clipboard_enabled(self, value: bool) -> None:
        """Terminal capability, not a filter: nothing needs re-rendering."""
        self._update_state(clipboard_osc52=value)
        self._notify(
            "Clipboard copy (y) enabled."
            if value
            else "Clipboard copy off — Ctrl+L copy mode still works."
        )

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
            clipboard=self.state.clipboard_osc52,
            detail_pane=self.state.detail_pane,
        )

    def _sync_detail_pane(self) -> None:
        """Show or hide the detail pane and refresh what it is showing.

        The `-detail` class on the container is what the compact breakpoint
        keys off to hide the log behind the pane; the pane's own visibility is
        its `-visible` class.
        """

        # is_running, not is_mounted: rendering is unit-tested without a screen,
        # and query_one needs one. Same guard _update_status uses.
        if self._is_shutting_down or not self.is_running:
            return
        visible = self.state.detail_pane
        self.detail_pane.set_class(visible, "-visible")
        try:
            self.query_one("#log-area", Container).set_class(visible, "-detail")
        except NoMatches:
            pass
        if visible:
            self.detail_pane.show(self.log_panel.cursor_entry)

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
        # Read-only, so an operator can see what Ctrl+E offers without opening
        # the dialog.
        self.advanced_drawer.set_export_status(
            describe_formats(
                [fmt.label for fmt in BUILTIN_FORMATS]
                + [_plugin_name(exporter) for exporter in self._plugins.exporters]
            )
        )

    async def on_unmount(self) -> None:
        self._is_shutting_down = True
        self._stop_tail()
        if self._persist_state:
            # Persist as-is: the selected source is deliberately kept so the
            # next launch reopens it.
            self._store.save(self.state)


def _plugin_name(plugin: object) -> str:
    """A plugin's own name, without trusting it to have set one."""
    name = getattr(plugin, "name", "") or ""
    return name if isinstance(name, str) and name else type(plugin).__name__


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _compact_path(path: Path) -> str:
    """``deeper/a.log`` — enough to tell identically named logs apart."""
    parent = path.parent.name
    return f"{parent}/{path.name}" if parent else path.name


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
