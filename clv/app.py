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
from time import monotonic
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional, Sequence
from xml.dom import minidom

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Button, Footer, Input, Label, Static, Switch, Tree
from textual.widgets._tree import TreeNode

from .plugins import (
    Exporter,
    FilterContext,
    PluginError,
    PluginRegistry,
    ProviderSource,
    load_plugins,
)
from .services import SourceManager, persist_log_sources, persist_setting
from .services.backend import LOCAL
from .services.clipboard import prepare_payload
from .services.clustering import (
    COUNT_PREFIX,
    Cluster,
    ClusterStream,
    cluster_entries,
    describe as describe_clusters,
    summarise,
)
from .services.config import LogConfig, get_config_file, load_config, user_config_path
from .services.discovery import DiscoveredFile, DiscoveryReport, discover
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
from .services.marks import MarkSet, mark_key
from .services.parsing import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_WARN,
    LogEntry,
    level_matches,
)
from .services.query import (
    NORMALISED_FIELD_KEYS,
    collect_field_names,
    entry_matches,
)
from .services.refs import SourceRef, format_ref, identity, parse_ref, ref_key
from .services.rotation import RotatedSet, describe_set, group_rotated
from .services.session import ORIGIN_FIELD, SourceSession
from .services.timeline import Timeline, build_timeline
from .services.timeline import EMPTY as EMPTY_TIMELINE
from .services.watch import (
    WatchIndex,
    WatchNotifier,
    WatchRule,
    describe_rules,
    notifying,
    toggled,
)
from .storage import SavedView, SessionState, StateStore
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
from .widgets.severity import SEVERITY_COLORS
from .widgets.timeline import TimelineBar
from .widgets.view_dialogs import SaveViewDialog, ViewPickerDialog, ViewRequest
from .widgets.watch_dialog import WatchRulesDialog

#: What `n`/`N` step between when there is no query and no severity bucket to
#: take the definition from. Stepping every entry would just duplicate the down
#: arrow; WARN and above is what someone scanning a tail is looking for, and
#: warnings are included because they are usually what precedes the failure.
NOTABLE_LEVELS: frozenset[str] = frozenset({LEVEL_WARN, LEVEL_ERROR, LEVEL_CRITICAL})

#: ``SEVERITY_COLORS`` is imported from `clv/widgets/severity.py` rather than
#: defined here: the timeline colours its buckets with the same palette the log
#: pane colours its lines with, and a widget may not import `clv.app`. The name
#: is still reachable as `clv.app.SEVERITY_COLORS`, which is where everything
#: that already used it looks.

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

#: Watch rules shown as individual chips before they collapse into a count.
MAX_WATCH_CHIPS = 3

SOURCES_PANEL_MIN_WIDTH = 20
SOURCES_PANEL_MAX_WIDTH = 120
SOURCES_PANEL_STEP = 4

ICON_FOLDER = "📂"
ICON_FILE = "📄"
#: Replaces the file icon on a starred log rather than sitting beside it, so
#: starring never changes a row's width.
ICON_STAR = "⭐"
STARRED_GROUP = f"{ICON_STAR} Starred"
#: Saved views sit above the starred group, in the same repeated-shortcuts
#: spirit: both are things the operator chose to keep, and both would otherwise
#: cost a walk down the hierarchy on every launch.
ICON_VIEW = "📑"
VIEWS_GROUP = f"{ICON_VIEW} Views"
#: A rotated set: several files, one log. Distinct from the folder icon
#: because expanding it lists members rather than a directory's contents.
ICON_ROTATED = "🗂"
#: A source a plugin supplies rather than one found on disk. Its own group and
#: its own icon, because none of the things that assume a file apply to it.
ICON_PROVIDER = "🔌"
PROVIDERS_GROUP = f"{ICON_PROVIDER} Providers"
#: Marks a source that is in the merged set. A prefix rather than a replacement
#: for the file icon: membership is a second fact about a log, unlike starring,
#: which replaces the icon precisely so a row never changes width.
ICON_MERGED = "⧉"
#: The merged set, repeated as a group. Below the starred group: a star is a
#: standing favourite, while membership here is the working set for the next
#: `u`, and the two are read in that order.
MERGED_GROUP = f"{ICON_MERGED} Merged"


class MergedSetNode:
    """Marker carried by the tree row that opens the merged view.

    The other groups are headings — selecting "Starred" means nothing, so they
    carry no data and the selection handler ignores them. This one *is* an
    action: it is the only way back into a merged set with the mouse, and after
    a restart the set is the thing an operator is looking for rather than any
    single member. Its children stay individually selectable, because opening
    one member on its own is also a reasonable thing to want.
    """

    __slots__ = ()


#: Singleton, so the handler and the tree lookups can both test identity —
#: sturdier than matching on a label that carries a count.
MERGED_VIEW = MergedSetNode()

#: Marks a cell of a row that *acts* rather than selects, and says which act.
#: Carried as segment metadata on the label, which is the same mechanism Tree
#: uses to tell its own expand chevron from the rest of the line.
ACTION_META = "clv-action"

#: The verbs a merged row offers, as glyphs narrow enough that three of them
#: still fit beside the name in a tree panel at its minimum width.
ICON_NAME_SET = "✎"
ICON_CLEAR_SET = "✕"

#: How that cell is painted, so it reads as a control rather than decoration.
ACTION_STYLE = Style(color="#95c8f5", bold=True)

#: Groups that sit above the configured roots, in the order they are built.
#: Used to place the merged group correctly when it appears mid-session,
#: without rebuilding the tree around it.
TREE_GROUP_LABELS: tuple[str, ...] = (VIEWS_GROUP, PROVIDERS_GROUP, STARRED_GROUP)

#: Width of the source column in a merged view, per breakpoint. Content, not
#: layout — the column is part of the line the pane renders, so CSS never sees
#: it — but it still has to shrink before the log text does.
MERGED_COLUMN_WIDTHS: dict[str, int] = {"-compact": 8, "-narrow": 14, "-wide": 20}

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
    "open_views": "Search",
    "save_view": "Search",
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
    "toggle_mark": "Navigation",
    "next_mark": "Navigation",
    # TimelineBar owns the bucket keys, bound on the widget for the reason
    # LogView's cursor keys are: they only mean anything while the bar has
    # focus. Folded into the overlay by build_help_sections, like those.
    "bucket_left": "Navigation",
    "bucket_right": "Navigation",
    "bucket_home": "Navigation",
    "bucket_end": "Navigation",
    "apply_bucket": "Navigation",
    "toggle_auto_scroll": "View",
    "toggle_structured": "View",
    "toggle_timeline": "View",
    "toggle_clustering": "View",
    "watch_rules": "View",
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
    "toggle_merge": "Sources",
    "open_merged": "Sources",
    "clear_merged": "Sources",
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


class LogTree(Tree[object]):
    """Source tree.

    A single tree holds every configured root. The previous build mounted one
    Tree per root inside a scrolling container, which required manual cursor
    hand-off between trees and manual scroll synchronisation against private
    node internals. One tree scrolls itself.

    Typed on ``object`` rather than ``Path`` since Item 9: the saved-views group
    hangs :class:`~clv.storage.SavedView` records off its nodes, and selection
    dispatches on what the node carries. Everything that walks the tree for a
    file already tests ``isinstance(data, Path)``, so a view node is invisible
    to it.
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

    class ActionRequested(Message):
        """An action marker on a row was clicked, and which one."""

        def __init__(self, node: TreeNode[object], action: str) -> None:
            super().__init__()
            self.node = node
            self.action = action

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.show_guides = True
        self.guide_depth = 3
        self.show_root = False

    async def _on_click(self, event: events.Click) -> None:
        """Let one cell of a row mean "act" while the rest means "select".

        A group row has two jobs — expand its contents, and open what it
        stands for — and answering both to the same click meant opening the
        merged view while collapsing the list under it. Splitting them needs a
        click target narrower than a row, which `Tree` does not offer: it
        dispatches from segment metadata (that is how the expand chevron is
        told apart), so a marker carrying metadata of its own is the same
        mechanism rather than a new one.

        Textual dispatches `_on_click` to every class in the MRO, most-derived
        first, and stops at the first handler to call `prevent_default()`.
        That, not `stop()`, is what keeps `Tree` from also selecting the row
        and toggling it: `stop()` only ends the bubble to parent widgets, so
        the base class ran anyway and the view opened as the group shut.
        Anything without the marker falls through untouched, which is how the
        rest of the row keeps behaving like the group heading it is.
        """

        action = event.style.meta.get(ACTION_META)
        if not action:
            return
        event.prevent_default()
        event.stop()
        line = event.style.meta.get("line")
        node = self.get_node_at_line(line) if line is not None else None
        if node is not None:
            self.post_message(self.ActionRequested(node, action))


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
    LogViewerApp.-copy-mode TimelineBar,
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
        Binding("b", "toggle_timeline", "Severity timeline", show=False),
        Binding("c", "toggle_clustering", "Collapse repeated lines", show=False),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "previous_match", "Previous match", show=False),
        Binding("g", "goto_timestamp", "Go to timestamp", show=False),
        Binding("m", "toggle_mark", "Mark / unmark this line", show=False),
        Binding("M", "next_mark", "Jump to next mark", show=False),
        Binding("v", "open_views", "Saved views", show=False),
        Binding("V", "save_view", "Save current filters as a view", show=False),
        Binding("W", "watch_rules", "Watch rules", show=False),
        Binding("x", "toggle_merge", "Add / remove from the merged set", show=False),
        Binding("u", "open_merged", "Open the merged view", show=False),
        Binding("X", "clear_merged", "Empty the merged set", show=False),
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
        #: What the loaded providers offered on the last scan. Kept apart from
        #: the report because these are not files and must not end up anywhere
        #: that assumes they are.
        self._provider_sources: list[ProviderSource] = []
        #: Readers and buffers, owned by a UI-free service. A single open log is
        #: a session of one, so nothing below has a single-source path of its
        #: own to keep in step — see clv/services/session.py.
        self._session = SourceSession(max_lines=self._config.max_buffer_lines)
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
        #: Marked lines, keyed by content. Session-only and never written to
        #: disk — see clv/services/marks.py for why that is a constraint rather
        #: than a gap.
        self._marks = MarkSet()
        #: Which lines matched which watch rules, evaluated once per line so a
        #: re-render is a lookup rather than a rule sweep.
        self._watch_index = WatchIndex()
        #: Coalesces watch notifications. Drained from the tail poll, so no
        #: second timer exists to keep in step with the first.
        self._watch_notifier = WatchNotifier(window=self._config.watch_rate_limit)
        #: Field names present in the buffer, offered as query completions.
        self._field_names: frozenset[str] = frozenset()
        #: Names a query term may use: the parser's normalised vocabulary plus
        #: whatever this source turned out to carry. Anything else stays a
        #: regex, which is what keeps `sshd:` searching for text.
        self._known_fields: frozenset[str] = NORMALISED_FIELD_KEYS

        self.query_bar = QueryBar()
        self.chip_bar = FilterChips(id="chip-bar")
        self.advanced_drawer = AdvancedFiltersDrawer()
        self.log_panel = LogView(
            id="log-stream",
            wrap=True,
            max_rows=self._config.max_buffer_lines,
        )
        self.detail_pane = DetailPane(id="detail-pane")
        self.timeline_bar = TimelineBar(id="timeline-bar")
        #: The histogram the bar is showing, kept so a tail poll can fold new
        #: lines into it instead of rebuilding it from the whole buffer.
        self._timeline: Timeline = EMPTY_TIMELINE
        #: Clustering of the currently rendered set, or None when `c` is off.
        #: Kept so a tailed line joins the run it belongs to rather than
        #: re-clustering the buffer on every poll.
        self._clusters: ClusterStream | None = None
        #: Which clusters the operator opened, keyed by content the way marks
        #: are: the buffer is a bounded deque, so anything positional starts
        #: pointing at a different cluster as lines are evicted. Session-only
        #: and never persisted — a cluster key is derived from log content.
        self._expanded_clusters: set[str] = set()
        #: Stream row index -> row index in the pane, so a cluster that grew on
        #: this poll can be redrawn without rebuilding the pane.
        self._cluster_rows: dict[int, int] = {}

    # --- the session, and the three names the app knows it by ---------------

    @property
    def _selected_source(self) -> Optional[Path]:
        """The log the pane is showing, or None.

        A property over the session rather than an attribute of the shell, so
        "which source" has exactly one answer and the merged case cannot grow a
        second one behind its back. Settable because a caller with lines of its
        own still has to be able to say where they came from.
        """

        return self._session.primary_path

    @_selected_source.setter
    def _selected_source(self, value: Optional[Path]) -> None:
        self._session.set_primary_path(value)

    @property
    def _entries(self):
        """Every buffered entry the filters will see."""

        return self._session.entries

    @_entries.setter
    def _entries(self, value) -> None:
        self._session.set_entries(value)

    @property
    def _reader(self):
        buffer = self._session.primary
        return buffer.reader if buffer is not None else None

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
                # Above #log-area rather than inside it: that container turns
                # horizontal at -wide so the detail pane can sit beside the log,
                # and a bar placed in it would become a third column.
                yield self.timeline_bar
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
        self._sync_timeline()
        self._sync_watch_rules()
        # Filled at mount rather than only when the drawer opens, so the plugin
        # and exporter lines are correct the first time it is seen.
        self._refresh_plugin_status()
        self._sync_journald_status()
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
            group_rotated=self.state.group_rotated,
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
            watch_rules=self._watching,
            timeline=self.state.timeline,
            clustering=self.state.clustering,
        )

    @property
    def _watching(self) -> bool:
        """Whether any watch rule is live — what the drawer's switch shows.

        Any rather than all: the switch answers "is anything being watched",
        and flipping it off must be able to quieten a partly-enabled set.
        """

        return any(rule.enabled for rule in self.state.watch_rules)

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

        # Both halves run in the thread. The walk touches the filesystem and
        # can be slow on a large tree; a provider may *shell out*, which is
        # slower still — enumerating units on a multi-gigabyte journal is
        # seconds of work. Asking providers on the event loop froze the UI for
        # exactly as long as they took, and when one hit its own timeout the
        # tree quietly came back short. Neither is discovery's judgement to
        # make: a provider that raises is still recorded and skipped.
        worker = self.run_worker(
            # `backends=LOCAL` is passed rather than left to the default, so the
            # seam is visible at the call site that owns the walk. Phase 3 of
            # SSH_TODO.md is where this stops being a constant.
            lambda: (
                discover(roots, settings, backends=LOCAL),
                self._plugins.discover_sources(),
            ),
            thread=True,
            name="discover",
            exit_on_error=False,
        )
        found = await worker.wait()
        report, provider_sources = found if found else (None, [])
        if report is None:
            report = DiscoveryReport()
        self._report = report
        self._provider_sources = provider_sources
        self._refresh_plugin_status()
        # After the providers have run, so the drawer reports what they found
        # rather than what they were about to look for.
        self._sync_journald_status()
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

        if not report.files and not self.state.views and not self._provider_sources:
            await panel.mount(Static("No log sources found.", classes="empty-tree"))
            return

        tree: LogTree = LogTree("Sources", id="source-tree")
        await panel.mount(tree)

        # Every group starts collapsed, like the configured roots below them.
        # These are shortcuts to things buried deeper in the tree, and a
        # shortcut that arrives already unfolded is not a shortcut: a hundred
        # journal units expanded on launch pushes the actual roots off screen,
        # which is the opposite of what a group at the top is for. One
        # keystroke opens the one you want.
        #
        # Saved views first: they are filter bundles rather than files, they
        # are few, and they are the fastest way back into a piece of work.
        # Above the starred group because a view usually names a starred log.
        if self.state.views:
            group = tree.root.add(VIEWS_GROUP, data=None, expand=False)
            for view in self.state.views:
                group.add_leaf(f"{ICON_VIEW} {view.name}", data=view)

        # Provider sources next: few, named rather than pathed, and nothing
        # below this point in the tree can hold one — a folder hierarchy is
        # exactly what they do not have.
        if self._provider_sources:
            group = tree.root.add(PROVIDERS_GROUP, data=None, expand=False)
            for source in self._provider_sources:
                group.add_leaf(f"{ICON_PROVIDER} {source.name}", data=source)

        # Starred logs are repeated in a group at the top. With the tree
        # collapsed by default, a favourite would otherwise cost a walk down
        # the hierarchy on every launch.
        starred = self._starred_paths()
        present = sorted(
            (item.path for item in report.files if identity(item.path) in starred),
            key=lambda p: str(p).lower(),
        )
        if present:
            group = tree.root.add(STARRED_GROUP, data=None, expand=False)
            for path in present:
                group.add_leaf(f"{ICON_STAR} {_compact_path(path)}", data=path)

        # The merged set, below the stars: a star is a standing favourite, this
        # is the working set for the next `u`. Listed whether or not discovery
        # found each member — they were chosen one keystroke at a time, and one
        # that has since rotated away should be visible enough to press `x` on
        # rather than silently absent.
        if self.state.merged:
            group = tree.root.add(self._merged_label(), data=MERGED_VIEW, expand=False)
            for path in self._merged_display_paths():
                group.add_leaf(f"{ICON_MERGED} {_compact_path(path)}", data=path)

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
            folders: dict[Path, TreeNode[object]] = {root: root_node}
            for folder, entries in self._by_folder(items).items():
                parent = root_node
                current = root
                for part in folder.parts:
                    current = current / part
                    if current not in folders:
                        folders[current] = parent.add(
                            f"{ICON_FOLDER} {part}", data=current, expand=False
                        )
                    parent = folders[current]
                self._add_files(parent, entries)

        tree.focus()

    @staticmethod
    def _by_folder(items: Sequence[DiscoveredFile]) -> dict[Path, list[Path]]:
        """Group one root's files by their folder, relative to that root.

        Insertion order is the report's order, which is already sorted by
        path — so the tree is built in the same sequence as before.
        """

        folders: dict[Path, list[Path]] = {}
        for item in items:
            relative = item.relative.parent
            folders.setdefault(Path() if relative == Path(".") else relative, []).append(
                item.path
            )
        return folders

    def _add_files(self, parent: TreeNode[object], paths: Sequence[Path]) -> None:
        """Add one folder's files, folding rotated members into single nodes.

        Grouping happens per folder rather than per root: ``app.log.1`` is a
        rotation of the ``app.log`` beside it, never of one two directories
        away that happens to share a name.
        """

        if not self.advanced_drawer.settings.group_rotated:
            for path in paths:
                parent.add_leaf(self._leaf_label(path, path.name), data=path)
            return

        sets, singles = group_rotated(paths)
        for rotated in sets:
            # A branch, not a leaf: the set is the source, and its members stay
            # individually openable underneath it. Collapsed, because the whole
            # point of the node is not having to look at the members.
            node = parent.add(
                f"{ICON_ROTATED} {rotated.name} ({len(rotated)} files)",
                data=rotated,
                expand=False,
            )
            for member in rotated.members:
                node.add_leaf(self._leaf_label(member.path, member.name), data=member.path)
        for path in singles:
            parent.add_leaf(self._leaf_label(path, path.name), data=path)

    def _starred_paths(self) -> set[Path]:
        return {identity(parse_ref(entry)) for entry in self.state.starred}

    def _leaf_label(self, path: Path, text: str) -> str:
        icon = ICON_STAR if identity(path) in self._starred_paths() else ICON_FILE
        # Merge membership prefixes rather than replaces: a starred log can be
        # merged too, and the star already owns the icon slot.
        merged = ICON_MERGED if identity(path) in self._merged_paths else ""
        return f"{merged}{icon} {text}"

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
        target = identity(path)
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
        resolved = identity(path)
        if not resolved.is_file():
            if announce:
                self._notify(f"{resolved} is not a readable file.", "error")
            return False

        self._stop_tail()
        try:
            # Only commits on success, so a source that will not open does not
            # also cost the one that was working.
            self._session.open_single(resolved)
        except OSError as exc:
            self._notify(f"Failed to read {resolved}: {exc}", "error")
            return False

        self._show_lines = min(self._config.default_show_lines, self._config.max_buffer_lines)
        self._after_source_change()
        return True

    def _select_rotated_set(self, rotated_set: RotatedSet) -> bool:
        """Open a whole rotated log as one source.

        The one path in CLV that is not instant: older members have to be
        decompressed from the front, so this says what it read rather than
        going quiet for a second and hoping nobody notices.
        """

        self._stop_tail()
        try:
            buffer = self._session.open_rotated(rotated_set)
        except OSError as exc:
            self._notify(f"Failed to read {rotated_set.name}: {exc}", "error")
            return False

        self._show_lines = min(self._config.default_show_lines, self._config.max_buffer_lines)
        self._after_source_change()
        reader = buffer.reader
        self._notify(describe_set(rotated_set, getattr(reader, "members_read", 0)))
        return True

    # --- the merged set ------------------------------------------------------

    def action_toggle_merge(self) -> None:
        """Add or remove the source under the tree cursor from the merged set."""

        target = self._star_target()
        if target is None:
            self._notify("Move to a log in the tree to merge it.", "warning")
            return

        stored = ref_key(target)
        current = list(self.state.merged)
        if stored in current:
            current.remove(stored)
            action = "Removed from"
        else:
            current.append(stored)
            action = "Added to"
        # Sorted as strings, not as refs: the persisted order is lexicographic
        # over the stored form, which is stable across a mixed local and remote
        # set. `SourceRef` deliberately declares no ordering of its own.
        self._update_state(merged=tuple(sorted(current)))
        # Edited in place rather than rebuilt. Membership says nothing about
        # what is on disk, so a rescan would be a filesystem walk per keystroke
        # — and rebuilding the tree collapses every folder the operator had
        # opened, which is a heavy price for adding one indicator.
        self._sync_merged_tree()
        self._notify(
            f"{action} the merged set ({len(current)} source(s)). Press u to open it."
        )

    def action_open_merged(self) -> None:
        """Open every source in the merged set as one timestamp-ordered stream."""

        # parse_ref, not identity: the stored form is what the set was built
        # from, and resolving here would open a symlink's target instead of the
        # member the operator chose.
        paths = [parse_ref(entry) for entry in self.state.merged]
        if not paths:
            self._notify("No sources merged yet — press x on a log to add one.", "warning")
            return
        if len(paths) == 1:
            # One member is not a merge, and opening it as one would show a
            # source column with one value in it.
            self._select_source(paths[0])
            return

        self._stop_tail()
        opened, failed = self._session.open_many(paths)
        for path, reason in failed:
            self._notify(f"{path.name} could not be opened: {reason}", "warning")
        if not opened:
            self._notify("None of the merged sources could be opened.", "error")
            return

        self._show_lines = min(self._config.default_show_lines, self._config.max_buffer_lines)
        self._after_source_change()
        anchored = self._session.anchored
        detail = (
            f" · {anchored} line(s) with no timestamp anchored to their own source"
            if anchored
            else ""
        )
        self._notify(f"Merged {len(opened)} sources.{detail}")

    def action_clear_merged(self) -> None:
        """Empty the merged set, so the next one starts from nothing.

        The verb that was missing: naming a set has always been possible
        through saved views, but building a *second* one meant pressing `x`
        off every member of the first. What is already saved is untouched —
        this empties the working set, not the views that recorded it.
        """

        if not self.state.merged:
            self._notify("The merged set is already empty.", "warning")
            return
        count = len(self.state.merged)
        self._update_state(merged=())
        self._sync_merged_tree()
        self._notify(
            f"Cleared {_plural(count, 'source', 'sources')} from the merged set. "
            "Saved views that name a set are unaffected."
        )

    @property
    def _merged_paths(self) -> set[Path]:
        return {identity(parse_ref(entry)) for entry in self.state.merged}

    def _merged_label(self) -> Text:
        """The group's heading: a marker that opens, and a name that expands.

        The leading glyph carries the action metadata and is painted to look
        like a control; the rest of the row is an ordinary group heading. One
        row, two jobs, and a click can tell which one it meant — which it could
        not when the whole row did both and opening the view collapsed the list
        of what was in it.

        The count is the difference between a heading and a control: "Merged"
        alone reads as a category, while "2 sources" says there is something
        assembled here to open.
        """

        def marker(glyph: str, action: str) -> tuple[str, Style]:
            return glyph, Style.from_meta({ACTION_META: action}) + ACTION_STYLE

        count = _plural(len(self.state.merged), "source", "sources")
        return Text.assemble(
            marker(ICON_MERGED, "open"),
            f" Merged ({count})  ",
            marker(ICON_NAME_SET, "save"),
            " ",
            marker(ICON_CLEAR_SET, "clear"),
        )

    def _merged_display_paths(self) -> list[Path]:
        """The merged set in the order the group lists it."""

        return sorted(
            (parse_ref(entry) for entry in self.state.merged),
            key=lambda path: (path.name.lower(), str(path).lower()),
        )

    def _sync_merged_tree(self) -> None:
        """Make the tree's merged group and indicators match the current set.

        Reconciles from state rather than applying a delta, so every way the
        set can change goes through one path: `x` on a source, and applying a
        saved view that carries a set of its own. A delta cannot express the
        second — a view replaces the whole set at once — and having two ways
        to update the same rows is how one of them ends up stale.

        In place, never a rebuild: `_build_tree` creates folders shut, so
        rebuilding would collapse everything the operator had expanded. The
        group node itself is kept and only its children are swapped, so a
        collapsed group stays collapsed.
        """

        try:
            tree = self.query_one("#source-tree", LogTree)
        except NoMatches:
            return

        merged = self._merged_paths
        group = self._merged_group(tree)

        if merged:
            if group is None:
                # Appearing mid-session, so it has to be placed rather than
                # appended: below the other groups, above the configured roots.
                #
                # Open, unlike the ones built at startup: this one appeared
                # because the operator just pressed `x`, and seeing the source
                # land in it is the confirmation that the keystroke worked.
                group = tree.root.add(
                    self._merged_label(),
                    data=MERGED_VIEW,
                    expand=True,
                    before=self._group_count(tree),
                )
            else:
                group.remove_children()
            # The count is part of what makes the row read as a control.
            group.set_label(self._merged_label())
            for path in self._merged_display_paths():
                group.add_leaf(f"{ICON_MERGED} {_compact_path(path)}", data=path)
        elif group is not None:
            # An empty group is a row that explains nothing.
            group.remove()
            group = None

        # Every other node carrying a path — the file in its folder, and any
        # copy of it in the starred group — gains or loses the indicator.
        for node in _walk_nodes(tree.root):
            if node.parent is group or not isinstance(node.data, Path):
                continue
            plain = node.label.plain
            if plain.startswith(ICON_MERGED):
                plain = plain[len(ICON_MERGED) :]
            member = identity(node.data) in merged
            node.set_label(f"{ICON_MERGED}{plain}" if member else plain)

    @staticmethod
    def _merged_group(tree: LogTree) -> Optional[TreeNode[object]]:
        return next(
            (node for node in tree.root.children if node.data is MERGED_VIEW), None
        )

    @staticmethod
    def _group_count(tree: LogTree) -> int:
        """How many group rows lead the tree, so a new one lands after them."""

        count = 0
        for node in tree.root.children:
            if node.data is None and node.label.plain in TREE_GROUP_LABELS:
                count += 1
            else:
                break
        return count

    def _merged_name(self) -> str:
        """What to call the merged set — in the status line and in an export."""

        names = [parse_ref(entry).name for entry in self.state.merged]
        if len(names) <= 2:
            return "+".join(names) or "merged"
        return f"{names[0]}+{len(names) - 1}-more"

    def _select_provider_source(self, source: ProviderSource) -> bool:
        """Open a source a plugin supplied.

        Everything past the reader is identical to a file: the same buffer, the
        same filters, the same cursor. What is different is that the failure
        modes belong to third-party code, so opening is guarded the way a
        filter stage is and a provider that throws costs only its own source.
        """

        self._stop_tail()
        reader = self._plugins.open_source(
            source, max_lines=self._config.max_buffer_lines
        )
        if reader is None:
            self._refresh_plugin_status()
            self._notify(f"{source.name} could not be opened — see the drawer.", "error")
            return False

        try:
            self._session.adopt(source.path, reader)
            # The severity bucket may be answerable at the source rather than
            # after the fact; a journal follow can filter before the pipe.
            self._session.push_severity(self.state.severity)
        except Exception as exc:  # noqa: BLE001 - third-party reader
            self._plugins.errors.append(PluginError(source.provider, f"raised: {exc}"))
            self._refresh_plugin_status()
            self._notify(f"Failed to read {source.name}: {exc}", "error")
            return False

        self._show_lines = min(self._config.default_show_lines, self._config.max_buffer_lines)
        self._after_source_change()
        return True

    def _after_source_change(self) -> None:
        """Everything that has to be rebuilt when the buffer becomes new."""

        self._sync_field_names()
        self._sync_regex_validation()
        # Rules are recompiled against this source's vocabulary and the primed
        # buffer is matched for highlighting — silently; see _sync_watch_rules.
        self._watch_index.reset()
        self._sync_watch_rules()
        self._render_log(scroll_end=True)
        self._start_tail()
        self._update_status()
        self._sync_compact_pane()
        self._sync_star_button()

    def _start_tail(self) -> None:
        """Tail regardless of auto-scroll.

        Auto-scroll controls whether the viewport follows new lines; it must
        not decide whether new lines are read at all. Turning it off to read
        back through history used to stop ingestion entirely.
        """

        self._stop_tail()
        if not self._session:
            return
        interval = 1 / max(1, self._config.refresh_hz)
        self._tail_timer = self.set_interval(interval, self._poll_tail)

    def _stop_tail(self) -> None:
        if self._tail_timer is not None:
            self._tail_timer.stop()
            self._tail_timer = None

    def _poll_tail(self) -> None:
        if not self._session or self._is_shutting_down:
            return
        outcomes = self._session.poll()
        if not outcomes:
            return

        rotated = [outcome for outcome in outcomes if outcome.rotated]
        if rotated:
            # A rotated file can be a different shape entirely, so the field
            # vocabulary is rebuilt rather than extended, and the watch index
            # starts again on what is effectively a new source.
            self._field_names = frozenset()
            self._sync_field_names()
            self._watch_index.reset()
            self._sync_watch_rules()
            self._render_log(scroll_end=self.state.auto_scroll)
            for outcome in rotated:
                self._notify(outcome.notice, "warning")
            return

        new_entries = [entry for outcome in outcomes for entry in outcome.entries]
        if not new_entries:
            return

        self._sync_field_names(new_entries)
        self._sync_regex_validation()
        # Before the rows are written, so a watched line is highlighted the
        # moment it appears rather than on the render after it.
        self._poll_watch(new_entries)

        if any(outcome.overflowed for outcome in outcomes):
            # The ring buffer dropped old lines, so the visible window shifted:
            # a full redraw is the only correct option.
            self._render_log()
        elif not self._session.lands_at_the_end(new_entries):
            # A merged view where a line sorted into the middle: appending it
            # would put it in the wrong place. Tailing several live logs at
            # once does not take this path — they are all producing "now" —
            # so the incremental render survives the case it exists for.
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
            known_fields=self._known_fields,
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

    def _origin(self, entry: LogEntry) -> Optional[Path]:
        """Which source *entry* came from.

        Marks and watch answers are keyed on this rather than on "the open
        log", so that when a session holds more than one member two identical
        lines from two different logs stay two different lines.
        """

        return self._session.origin_of(entry)

    def _origins(self) -> list[Optional[Path]]:
        """Every source the open session draws from.

        One entry for an ordinary log; one per member for a merge, and one per
        rotated member for a set — which is why this asks the buffers rather
        than assuming the answer is `_selected_source`.
        """

        sources: list[Optional[SourceRef]] = [
            buffer.path for buffer in self._session.buffers
        ]
        if self._session.is_merged or any(
            entry.fields.get(ORIGIN_FIELD) for entry in self._entries
        ):
            # An ORIGIN_FIELD value is format_ref output that has been living on
            # an entry, so it comes back the same way anything else persisted
            # does — and for two hosts it is what keeps their marks apart.
            sources += [
                parse_ref(value)
                for value in {
                    entry.fields.get(ORIGIN_FIELD)
                    for entry in self._entries
                    if entry.fields.get(ORIGIN_FIELD)
                }
            ]
        return sources or [None]

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
            self._clear_timeline()
            self._sync_detail_pane()
            return

        if not self._entries:
            self.log_panel.write(Text("No log entries in the selected source.", style="dim"))
            self._clear_timeline()
            self._sync_detail_pane()
            return

        try:
            result = self._visible_entries(self._entries)
        except QueryError as exc:
            self.log_panel.write(Text(f"Invalid query: {exc}", style="bold #f87171"))
            self._clear_timeline()
            self._sync_detail_pane()
            return

        if not result.entries:
            message = describe_empty_result(result.stats, self._filter_spec())
            self.log_panel.write(Text(message, style="dim"))
            self._clear_timeline()
            self._sync_detail_pane()
            return

        # Drop marks — and cached watch answers — whose lines the ring buffer
        # has evicted, so the count in the status line stays honest as the
        # source tails and the index cannot grow without bound. Keyed per
        # entry: in a merged view the lines on screen belong to different
        # sources, and pruning one at a time would see every other source's
        # lines as missing and throw its marks away.
        live = {mark_key(self._origin(entry), entry) for entry in self._entries}
        self._marks.retain(live, sources=self._origins())
        self._watch_index.retain(live)

        # Built from the filtered set and *not* from the `_show_lines` window:
        # the histogram answers "when did the thing I am looking at happen",
        # and the answer must not change because the pane is showing fewer
        # lines than it matched.
        self._rebuild_timeline(result.entries)

        self._write_rows(result.entries)
        # Rows are created unmarked, so with an empty set there is nothing to do
        # — and an empty set is the overwhelmingly common case.
        if self._marks:
            self._sync_marks()
        if self._watch_index.active:
            self._sync_watch_highlights()

        self._restore_cursor(previous_entry, previous_index)
        if scroll_end or self.state.auto_scroll:
            self.log_panel.scroll_end(animate=False)
        self._update_status()

    # --- repeat clustering --------------------------------------------------

    def _write_rows(self, entries: Sequence[LogEntry]) -> None:
        """Fill the pane from *entries*, collapsing repeats when `c` is on.

        Without clustering this is the loop it has always been. With it, the
        rows are what the pane shows and the `_show_lines` window applies to
        *those*: collapsing is worth doing precisely because it buys history on
        screen, and slicing before it would give that back.
        """

        if not self.state.clustering:
            self._clusters = None
            self._cluster_rows = {}
            for entry in entries[-self._show_lines :]:
                self.log_panel.write_entry(self._renderable_for(entry), entry)
            return

        stream = cluster_entries(entries, lookback=self._config.cluster_lookback)
        self._clusters = stream
        self._cluster_rows = {}
        # Keys that no longer exist are dropped rather than kept forever: the
        # lines behind them have been filtered away or evicted, and an expansion
        # set that only grew would be a slow leak keyed on log content.
        live_keys = {row.key() for row in stream.rows if isinstance(row, Cluster)}
        self._expanded_clusters &= live_keys

        plan: list[tuple[int, Optional[Cluster], LogEntry]] = []
        for index, row in enumerate(stream.rows):
            if isinstance(row, Cluster):
                plan.append((index, row, row.representative))
                if row.key() in self._expanded_clusters:
                    # The members follow their header, which is what "expanded
                    # in place" means: the lines stay where they were read.
                    plan.extend((-1, None, member) for member in row.entries)
            else:
                plan.append((index, None, row))

        for stream_index, cluster, entry in plan[-self._show_lines :]:
            if cluster is None:
                self.log_panel.write_entry(self._renderable_for(entry), entry)
            else:
                self.log_panel.write_cluster(
                    self._cluster_renderable(cluster), cluster, entry
                )
            if stream_index >= 0:
                self._cluster_rows[stream_index] = len(self.log_panel.rows) - 1

    def _cluster_renderable(self, cluster: Cluster) -> RenderableType:
        """A collapsed (or opened) group, as one line.

        Deliberately never a structured panel, even with `o` on: a bordered
        payload per repeat group is the noise this feature exists to remove.
        """

        expanded = cluster.key() in self._expanded_clusters
        marker = "▾" if expanded else "▸"
        prefix = f"{marker} {COUNT_PREFIX}{cluster.count} "
        text = Text(prefix, style="bold #7aa3d1")
        if self._breakpoint != "-compact":
            # The span is what makes a count actionable — "147 of these, over
            # four seconds" is a different event from "147, over an hour". It
            # gives way first, because at 80 columns the line itself matters
            # more.
            span = self._cluster_span(cluster)
            if span:
                text.append(f"{span} ", style="#7aa3d1")
        body = self._colorize(cluster.representative)
        if self._session.is_merged:
            body = self._with_source_column(body, cluster.representative)
        return text.append_text(body)

    @staticmethod
    def _cluster_span(cluster: Cluster) -> str:
        first, last = cluster.first, cluster.last
        if first is None or last is None:
            return ""
        if first == last:
            return f"{first:%H:%M:%S}"
        return f"{first:%H:%M:%S}→{last:%H:%M:%S}"

    def action_toggle_clustering(self) -> None:
        self._set_clustering(not self.state.clustering)

    def _set_clustering(self, value: bool) -> None:
        self._update_state(clustering=value)
        if not value:
            # Nothing is remembered across an off/on: what was expanded refers
            # to clusters that no longer exist.
            self._expanded_clusters.clear()
        self._sync_view_toggles()
        self._render_log()
        if value and self._clusters is not None:
            summary = describe_clusters(self._clusters)
            self._notify(f"Collapsed repeats — {summary}." if summary else "No repeats to collapse.")

    def on_log_view_cluster_toggled(self, message: LogView.ClusterToggled) -> None:
        """Enter on a cluster row opens it, or closes it again."""

        cluster = message.cluster
        if not isinstance(cluster, Cluster):  # pragma: no cover - defensive
            return
        key = cluster.key()
        if key in self._expanded_clusters:
            self._expanded_clusters.discard(key)
        else:
            self._expanded_clusters.add(key)
        # A rebuild rather than an edit: the rows come from the filtered set,
        # and _restore_cursor puts the cursor back on the row that was toggled.
        self._render_log()

    def _sync_marks(self) -> None:
        """Set every visible row's gutter from the mark set.

        Marks are content-keyed, so they reattach themselves after a re-render:
        a line a filter hid and then brought back comes back marked, with
        nothing to restore. ``set_row_marked`` is a no-op when the value is
        unchanged, so this costs a re-strip only for rows that actually flipped.
        """

        for index, entry in self.log_panel.entry_rows():
            self.log_panel.set_row_marked(
                index, self._marks.contains(self._origin(entry), entry)
            )

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
        # A tailed line can be one an operator marked earlier and that rotated
        # back in, so the gutter has to be set as it arrives — but only when
        # there are marks at all, keeping the common path a plain append.
        # Folded into the existing grid rather than rebuilt from the buffer:
        # this is the tail path, and it must cost what arrived.
        self._extend_timeline(result.entries)
        marked = bool(self._marks)
        watching = self._watch_index.active
        for entry in result.entries:
            if self._append_clustered(entry):
                # Joined a group already on screen: the row was redrawn with a
                # higher count and there is no new row to decorate.
                continue
            self.log_panel.write_entry(self._renderable_for(entry), entry)
            if marked and self._marks.contains(self._origin(entry), entry):
                self.log_panel.set_row_marked(len(self.log_panel.rows) - 1, True)
            # A lookup, not a match: _poll_watch already asked the rules about
            # this line before it reached the pane.
            if watching and self._watch_index.watched(self._origin(entry), entry):
                self.log_panel.set_row_watched(len(self.log_panel.rows) - 1, True)

    def _append_clustered(self, entry: LogEntry) -> bool:
        """Fold one tailed *entry* into the clustering, if it is on.

        Returns True when the entry was absorbed by a row already on screen, so
        the caller writes nothing. The stream is the same object a full render
        builds, fed one entry at a time — there is no second clustering
        implementation for the tail path to disagree with.
        """

        stream = self._clusters
        if not self.state.clustering or stream is None:
            return False

        growth = stream.add(entry)
        if growth.appended:
            # A shape nobody has seen lately: an ordinary row, remembered in
            # case the next line makes it a cluster.
            self.log_panel.write_entry(self._renderable_for(entry), entry)
            self._cluster_rows[growth.index] = len(self.log_panel.rows) - 1
            return True

        row_index = self._cluster_rows.get(growth.index)
        cluster = growth.row
        if row_index is None or not isinstance(cluster, Cluster):
            # The row scrolled out of the `_show_lines` window, so there is
            # nothing on screen to update. A redraw is the honest answer.
            self._render_log()
            return True
        if cluster.key() in self._expanded_clusters:
            # An open cluster gains a *member row*, which means inserting a row
            # mid-pane — the one thing this widget deliberately cannot do. Rare
            # enough (a cluster has to be open) to pay a redraw for.
            self._render_log()
            return True
        if not self.log_panel.row_holds(row_index, cluster):
            # The pane hit its row cap and dropped a batch off the front, so
            # every index below it moved. Updating by a stale index would
            # rewrite some unrelated line, which is worse than a redraw.
            self._render_log()
            return True
        self.log_panel.update_row(row_index, self._cluster_renderable(cluster), cluster)
        return True

    # --- the severity timeline ----------------------------------------------

    def _rebuild_timeline(self, entries: Sequence[LogEntry]) -> None:
        """Bucket *entries* into the bar, at whatever width it has.

        Skipped entirely while the bar is hidden: an operator who never presses
        `b` should not pay for a histogram nobody is looking at, and the bar is
        rebuilt the moment it is shown.
        """

        if not self.state.timeline:
            self._timeline = EMPTY_TIMELINE
            return
        self._timeline = build_timeline(entries, width=self._timeline_width())
        # is_running, not is_mounted: rendering is unit-tested without a screen,
        # and a widget refresh needs one. Same guard _update_status uses.
        if self.is_running:
            self.timeline_bar.set_timeline(self._timeline)

    def _timeline_width(self) -> int:
        """How many buckets the bar has room for.

        The widget's own width once it is laid out; before that — the first
        render happens before the first resize — the terminal's, which is the
        same number minus the panels beside it and is corrected on the next
        render anyway.
        """

        width = self.timeline_bar.size.width or self.timeline_bar.container_size.width
        return max(1, width or self.size.width or 80)

    def _clear_timeline(self) -> None:
        self._timeline = EMPTY_TIMELINE
        if self.state.timeline and self.is_running:
            self.timeline_bar.clear()

    def _extend_timeline(self, entries: Sequence[LogEntry]) -> None:
        """Fold tailed lines into the existing grid, or rebuild when they miss.

        The incremental half of Item 14. At two polls a second against buckets
        minutes wide, almost everything lands in the grid; a rebuild costs one
        pass over the filtered set and happens about once per bucket.
        """

        if not self.state.timeline:
            return
        extended = self._timeline.extend(entries)
        if extended is None:
            try:
                result = self._visible_entries(self._entries)
            except QueryError:
                return
            self._rebuild_timeline(result.entries)
            return
        self._timeline = extended
        if self.is_running:
            self.timeline_bar.set_timeline(extended)

    def action_toggle_timeline(self) -> None:
        value = not self.state.timeline
        self._set_timeline(value)
        if value:
            # Only from the key. The drawer switch and a restored session both
            # go through _set_timeline without this: flipping a switch must not
            # yank focus out of the drawer, and a session that reopens with the
            # bar showing should still start on whatever normally has focus.
            self.timeline_bar.focus()

    def _set_timeline(self, value: bool) -> None:
        self._update_state(timeline=value)
        self._sync_view_toggles()
        self._sync_timeline()

    def _sync_timeline(self) -> None:
        """Show or hide the bar, and fill it when it appears."""

        if self._is_shutting_down or not self.is_running:
            return
        visible = self.state.timeline
        self.timeline_bar.set_class(visible, "-visible")
        if not visible:
            self.timeline_bar.clear()
            self._timeline = EMPTY_TIMELINE
            return
        # Rebuilt rather than restored: it was not being maintained while it
        # was hidden, which is the point of hiding it.
        try:
            result = self._visible_entries(self._entries)
        except QueryError:
            self._clear_timeline()
            return
        self._rebuild_timeline(result.entries)

    def on_timeline_bar_width_changed(self, message: TimelineBar.WidthChanged) -> None:
        """Re-bucket to the width the bar actually got.

        The bucket count is the width, so this is a rebuild rather than a
        reflow. It is also what corrects the first histogram after `b`: the bar
        is hidden until then, and a hidden widget has no width to bucket to.
        """

        if not self.state.timeline or self._is_shutting_down:
            return
        try:
            result = self._visible_entries(self._entries)
        except QueryError:
            return
        self._rebuild_timeline(result.entries)

    def on_timeline_bar_bucket_selected(self, message: TimelineBar.BucketSelected) -> None:
        """A bucket became the time window.

        Routed through the *custom range* the query bar and the state already
        have rather than through a filter of its own, so the selection shows up
        as an ordinary Time chip and is dismissed the same way. The bar then
        re-buckets over the narrower window, which is the drill-down the
        histogram exists for.
        """

        window = message.window
        if window.start is None or window.end is None:  # pragma: no cover - always bounded
            return
        start = window.start.isoformat(sep=" ", timespec="seconds")
        end = window.end.isoformat(sep=" ", timespec="seconds")
        self._update_state(time_window="range", custom_start=start, custom_end=end)
        with self.prevent(Input.Changed):
            self.query_bar.apply_custom_time_range(start, end, emit=False)
        self._refresh_chips()
        self._render_log()
        self._notify(f"Time window set to {start} → {end}.")

    def _renderable_for(self, entry: LogEntry) -> RenderableType:
        if self.state.pretty_rendering:
            structured = self._structured_renderable(entry)
            if structured is not None:
                return structured
        text = self._colorize(entry)
        if self._session.is_merged:
            return self._with_source_column(text, entry)
        return text

    def _with_source_column(self, text: Text, entry: LogEntry) -> Text:
        """Prefix a line with the source it came from.

        Only in a merged view, where the pane is the one place the answer can
        be. The width follows the breakpoint so the column gives way before the
        log text does — this is line *content*, which is why it is composed
        here and not in CSS.
        """

        width = MERGED_COLUMN_WIDTHS.get(self._breakpoint, MERGED_COLUMN_WIDTHS["-narrow"])
        origin = self._origin(entry)
        name = origin.name if origin is not None else "?"
        if len(name) > width:
            # From the left: rotated members and unit names differ at the end.
            name = "…" + name[-(width - 1) :]
        column = Text(f"{name:<{width}} ", style="#7aa3d1")
        return column.append_text(text)

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
            # The collection deduplicates and caps itself, so this can no longer
            # bury the discovery summary the operator opened CLV to read — but
            # it must still say what it is not showing.
            if self._plugins.errors.overflow_note:
                self.log_panel.write(
                    Text(
                        f"Plugin problems — {self._plugins.errors.overflow_note}",
                        style="#facc15",
                    )
                )

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

        if self._session.is_merged:
            parts = [f"{self._merged_name()} ({len(self._session)} sources)", detail]
            anchored = self._session.anchored
            if anchored:
                # Placed by inference rather than by their own timestamp, so
                # the count is on screen rather than left to be noticed.
                parts.append(f"{anchored} anchored")
        else:
            parts = [str(self._selected_source), detail]
        member = self._cursor_member()
        if member is not None:
            # One source made of several files: which one the cursor is in is
            # not deducible from anything else on screen.
            parts.append(f"in {member}")
        if self.state.clustering and self._clusters is not None:
            # Says what collapsing bought, and — when it bought nothing — that
            # it is on and found no repeats, rather than looking switched off.
            parts.append(describe_clusters(self._clusters) or "no repeats collapsed")
        marks = self._marks.count_for(*self._origins())
        if marks:
            parts.append(f"{marks} marked")
        if self._match_position is not None:
            position, total, label = self._match_position
            parts.append(f"{label} {position} of {total}")
        parts.append(follow)
        status.update(" · ".join(parts))

    def _cursor_member(self) -> Optional[str]:
        """The name of the file the cursor line came from, when that varies.

        Empty for an ordinary source: the status line already names it, and
        repeating it beside itself would be noise.
        """

        if self._session.is_merged:
            # The source column already says this, on every row.
            return None
        entry = self.log_panel.cursor_entry
        if entry is None:
            return None
        origin = self._origin(entry)
        if origin is None or origin == self._selected_source:
            return None
        return origin.name

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

        # Watch rules are not filters, but they are active state the operator
        # should be able to see and switch off from one place. Past a handful
        # they collapse into one chip: the bar is a single row, and a dozen
        # named chips would push the filters out of sight at 80 columns.
        enabled = [rule for rule in self.state.watch_rules if rule.enabled]
        if len(enabled) > MAX_WATCH_CHIPS:
            chips.append(FilterChip(f"Watching: {len(enabled)} rules", key="watch:*"))
        else:
            chips.extend(
                FilterChip(f"Watch: {rule.name}", key=f"watch:{rule.name}")
                for rule in enabled
            )

        self.chip_bar.update_chips(chips)

    def _sync_regex_validation(self) -> None:
        self.query_bar.validate_entries(list(self._entries), self._known_fields)

    def _sync_field_names(self, arrived: Iterable[LogEntry] | None = None) -> None:
        """Refresh the vocabulary field terms and completions are drawn from.

        ``arrived`` is the incremental case: tailed lines can only *add* names,
        so a poll unions instead of re-walking the buffer. The union is never
        pruned as the ring buffer evicts lines, which means a name can outlive
        the last entry carrying it — harmless, since a term naming a field no
        entry has is hidden and counted like any other, and the alternative is
        an O(buffer) rescan on every poll.
        """

        names = (
            collect_field_names(self._entries)
            if arrived is None
            else self._field_names | collect_field_names(arrived)
        )
        if names == self._field_names:
            return
        self._field_names = names
        self._known_fields = NORMALISED_FIELD_KEYS | names
        self.query_bar.set_field_names(names)

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
                parsed = spec.parse()
                pattern = compile_query(
                    parsed.text, case_sensitive=spec.case_sensitive, regex=spec.regex
                )
            except QueryError:
                return [], "match"
            # Terms without free text still define a match set, so this cannot
            # key off the pattern alone the way it did before Item 8.
            if pattern is not None or parsed.terms:
                return [
                    index
                    for index, entry in rows
                    if entry_matches(entry, parsed.terms, pattern)
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

    # --- marks --------------------------------------------------------------

    def action_toggle_mark(self) -> None:
        """Mark or unmark the cursor line."""

        entry = self.log_panel.cursor_entry
        if entry is None:
            self._notify("Move the cursor to a line to mark it.", "warning")
            return
        cluster = self.log_panel.cursor_cluster
        if isinstance(cluster, Cluster) and cluster.key() not in self._expanded_clusters:
            # A mark is a line. Marking 147 of them from one keystroke — and
            # drawing one gutter dot for all of them — is not what `m` means,
            # so the cluster has to be opened first.
            self._notify(
                f"Expand this cluster (Enter) to mark one of its {cluster.count} lines.",
                "warning",
            )
            return
        marked = self._marks.toggle(self._origin(entry), entry)
        # Content-keyed, so identical lines share one mark: resync every row
        # rather than only the cursor's, or the copies would disagree.
        self._sync_marks()
        self._update_status()
        self._notify("Marked this line." if marked else "Unmarked this line.")

    def action_next_mark(self) -> None:
        """Move the cursor to the next marked line, wrapping with a notice."""

        if self._selected_source is None:
            self._notify("Open a log before jumping between marks.", "warning")
            return

        marked = [
            index
            for index, entry in self.log_panel.entry_rows()
            if self._marks.contains(self._origin(entry), entry)
        ]
        if not marked:
            self._notify("No marked lines — press m to mark one.", "warning")
            return

        cursor = self.log_panel.cursor
        following = [index for index in marked if index > cursor]
        target = following[0] if following else marked[0]
        self.log_panel.move_cursor(target)
        self._update_status()
        position = marked.index(target) + 1
        if not following:
            self._notify(f"Wrapped to the first mark ({position} of {len(marked)}).")
        else:
            self._notify(f"Mark {position} of {len(marked)}.")

    # --- watch rules --------------------------------------------------------

    def action_watch_rules(self) -> None:
        """Add, edit, enable, disable or delete the live-alert rules."""
        self.run_worker(self._prompt_watch_rules(), group="dialogs", exit_on_error=False)

    def _sync_watch_rules(self) -> None:
        """Recompile the rules and re-read the buffer against them.

        Lines already buffered are matched so they *look* watched — a rule that
        highlighted nothing already on screen would read as broken — but they
        raise no notification. Nobody asked to be told about lines that arrived
        before the rule existed, and a source switch would otherwise open with
        a burst of toasts about history.
        """

        self._watch_index.set_rules(self.state.watch_rules, self._known_fields)
        self._watch_notifier.reset()
        if self._watch_index.active and self._session:
            self._evaluate_watches(self._entries)
        self._refresh_watch_status()

    def _evaluate_watches(self, entries: Sequence[LogEntry]) -> list[tuple[str, ...]]:
        """Ask the rules about *entries*, each keyed to the source it came from.

        Grouped by origin rather than evaluated one at a time, because
        `WatchIndex.evaluate` caches per distinct line within a source and
        calling it per entry would defeat that. In the ordinary case there is
        one group and this is the call it always was.
        """

        grouped: dict[Optional[Path], list[LogEntry]] = {}
        for entry in entries:
            grouped.setdefault(self._origin(entry), []).append(entry)
        fired: list[tuple[str, ...]] = []
        for source, group in grouped.items():
            fired += [names for _entry, names in self._watch_index.evaluate(source, group)]
        return fired

    def _refresh_watch_status(self) -> None:
        self.advanced_drawer.set_watch_status(describe_rules(self.state.watch_rules))

    def _poll_watch(self, entries: Sequence[LogEntry]) -> None:
        """Match newly arrived lines and say something, at most so often.

        Driven from the tail poll rather than a timer of its own: there is
        already a clock running at ``refresh_hz`` and a second one would only
        create ways for the two to disagree.
        """

        if not self._watch_index.active:
            return
        for names in self._evaluate_watches(entries):
            self._watch_notifier.record(notifying(names, self.state.watch_rules))

        messages = self._watch_notifier.due(monotonic())
        for message in messages:
            self._notify(message, "warning")
        if messages and self._config.watch_bell:
            # Opt-in only, and guarded: a terminal that cannot ring is not a
            # reason to lose the notification that went with it.
            try:
                self.bell()
            except Exception:  # noqa: BLE001 - terminal-dependent
                pass

    def _sync_watch_highlights(self) -> None:
        """Set every visible row's highlight from the index.

        A lookup per row, never a match: the answer was computed when the line
        arrived. This is what keeps re-rendering independent of how many rules
        are enabled.
        """

        for index, entry in self.log_panel.entry_rows():
            self.log_panel.set_row_watched(
                index, self._watch_index.watched(self._origin(entry), entry)
            )

    def _set_watch_rules(self, rules: Iterable[WatchRule]) -> None:
        self._update_state(watch_rules=tuple(rules))
        self._sync_watch_rules()
        self._sync_view_toggles()
        self._render_log()

    def _set_watch_enabled(self, value: bool) -> None:
        """The drawer's switch: all rules on, or all off."""

        if not self.state.watch_rules:
            self._notify("No watch rules yet — press W to add one.", "warning")
            return
        self._set_watch_rules(
            replace(rule, enabled=value) for rule in self.state.watch_rules
        )
        self._notify("Watch rules on." if value else "Watch rules off.")

    async def _prompt_watch_rules(self) -> None:
        rules = await self.push_screen(
            WatchRulesDialog(self.state.watch_rules, self._known_fields),
            wait_for_dismiss=True,
        )
        if rules is None:
            # Nothing changed: not worth re-indexing the buffer over a dialog
            # that was only looked at.
            return
        self._set_watch_rules(rules)
        self._notify(describe_rules(rules))

    # --- saved views --------------------------------------------------------

    def action_save_view(self) -> None:
        """Name the filters that are active and keep them."""
        self.run_worker(self._prompt_save_view(), group="dialogs", exit_on_error=False)

    def action_open_views(self) -> None:
        """Apply, rename or delete a saved view."""
        self.run_worker(self._prompt_views(), group="dialogs", exit_on_error=False)

    def _capture_view(self, name: str) -> SavedView:
        """Everything the current filter state consists of, under *name*.

        The open source is recorded by path so applying the view puts the
        filters back where they mean something. Nothing about what those
        filters *matched* is captured — see :class:`SavedView`.
        """

        settings = self.advanced_drawer.settings
        return SavedView(
            name=name,
            query=self.state.query,
            severity=self.state.severity,
            time_window=self.state.time_window,
            custom_start=self.state.custom_start,
            custom_end=self.state.custom_end,
            case_sensitive=settings.case_sensitive,
            use_regex=settings.use_regex,
            invert_match=settings.invert_match,
            include_globs=settings.include_globs,
            exclude_globs=settings.exclude_globs,
            # format_ref, not ref_key: a view records the source as it was
            # selected, and resolving it here would silently rewrite a view
            # saved on a symlink to name its target instead.
            source=format_ref(self._selected_source) if self._selected_source else "",
            # Item 9 left this out because there was no merged set to capture.
            # Recorded only when a merge is actually open, so a view saved on
            # one log does not quietly carry someone else's set around.
            merged=tuple(self.state.merged) if self._session.is_merged else (),
        )

    def _view_named(self, name: str) -> Optional[SavedView]:
        return next((view for view in self.state.views if view.name == name), None)

    async def _store_views(self, views: Iterable[SavedView]) -> None:
        """Persist *views* sorted by name, and rebuild the tree group.

        Rebuilt from the report already in hand rather than by re-walking the
        filesystem, the same way starring does it.
        """

        self._update_state(views=tuple(sorted(views, key=lambda view: view.name.lower())))
        if self._report is not None:
            await self._build_tree(self._report)

    def _default_view_name(self) -> str:
        """A name worth pressing Enter on, derived from what is filtered."""

        if self.state.query:
            query = self.state.query
            return query if len(query) <= 24 else query[:23] + "…"
        parts = [
            part
            for part in (
                self.state.severity if self.state.severity != "all" else "",
                self.state.time_window if self.state.time_window not in {"", "all"} else "",
                self._merged_name()
                if self._session.is_merged
                else (self._selected_source.name if self._selected_source else ""),
            )
            if part
        ]
        return " ".join(parts) if parts else f"View {len(self.state.views) + 1}"

    def _apply_view(self, view: SavedView) -> None:
        """Put every filter the view records back, in a single re-render.

        Field by field this would repaint the pane five times and fight the
        cursor restore on each one, so the state is assembled first, the
        controls are synced with their own messages suppressed, and exactly one
        render happens at the end — either `_select_source`'s or this method's.
        """

        settings = self.advanced_drawer.settings
        updated = replace(
            settings,
            include_globs=view.include_globs,
            exclude_globs=view.exclude_globs,
            case_sensitive=view.case_sensitive,
            use_regex=view.use_regex,
            invert_match=view.invert_match,
        )
        rescan = updated.affects_discovery(settings)
        self.advanced_drawer.sync_settings(updated)

        # prevent(), not a flag: assigning to the input posts Input.Changed
        # asynchronously, and the app's handler would render a second time.
        with self.prevent(Input.Changed):
            self.query_bar.set_query_value(view.query)
        self.query_bar.set_severity(view.severity)
        if view.time_window == "range" and view.custom_start and view.custom_end:
            self.query_bar.apply_custom_time_range(
                view.custom_start, view.custom_end, emit=False
            )
        else:
            self.query_bar.select_time(view.time_window)

        self._update_state(
            query=view.query,
            severity=view.severity,
            time_window=view.time_window,
            custom_start=view.custom_start,
            custom_end=view.custom_end,
            case_sensitive=view.case_sensitive,
            use_regex=view.use_regex,
            invert_match=view.invert_match,
            include_globs=view.include_globs,
            exclude_globs=view.exclude_globs,
        )

        missing = ""
        opened = False
        if view.merged:
            # A merged view reopens the whole set: its filters were written
            # against all of it, and half the set is a different question.
            self._update_state(merged=tuple(view.merged))
            # The tree has to follow, or the group keeps listing the set that
            # was open before this one — the same rows, quietly wrong.
            self._sync_merged_tree()
            self.action_open_merged()
            opened = True
        elif view.source:
            source = parse_ref(view.source)
            if source.is_file():
                # _select_source renders once; nothing below may render again.
                opened = self._select_source(source, announce=False)
                if opened:
                    self._highlight_source(source, select=False)
            else:
                # The filters still describe something worth seeing, so they go
                # on regardless. Refusing would make a rotated log a dead view.
                missing = view.source

        if not opened:
            self._sync_regex_validation()
            self._render_log()
        if rescan:
            self.run_worker(self._rescan(), group="discovery", exit_on_error=False)
        if missing:
            self._notify(
                f"View '{view.name}' names a source that is no longer there: {missing}",
                "warning",
            )

    async def _prompt_save_view(self) -> None:
        dialog = SaveViewDialog(
            default_name=self._default_view_name(),
            summary=self._capture_view("preview").summary(),
        )
        name = await self.push_screen(dialog, wait_for_dismiss=True)
        if name is None:
            self._notify("Save view canceled.")
            return

        replaced = self._view_named(name) is not None
        view = self._capture_view(name)
        await self._store_views(
            [existing for existing in self.state.views if existing.name != name] + [view]
        )
        self._notify(
            f"Replaced view '{name}'." if replaced else f"Saved view '{name}'."
        )

    async def _prompt_views(self) -> None:
        """Show the picker until the operator applies one or closes it.

        Rename and delete reopen it: the list the modal is holding is stale the
        moment either lands, and reopening is cheaper — in code and in
        surprises — than teaching the dialog to edit its own copy.
        """

        while True:
            if not self.state.views:
                self._notify(
                    "No saved views yet — press V to save the current filters.", "warning"
                )
                return

            request = await self.push_screen(
                ViewPickerDialog(self.state.views), wait_for_dismiss=True
            )
            if request is None:
                return
            if not await self._handle_view_request(request):
                return

    async def _handle_view_request(self, request: ViewRequest) -> bool:
        """Act on the picker's answer. True when the picker should reopen."""

        view = self._view_named(request.name)
        if view is None:  # pragma: no cover - the list came from this state
            return False

        if request.action == "apply":
            self._apply_view(view)
            self._notify(f"Applied view '{view.name}'.")
            return False
        if request.action == "delete":
            await self._store_views(
                other for other in self.state.views if other.name != view.name
            )
            self._notify(f"Deleted view '{view.name}'.")
            return True

        renamed = replace(view, name=request.new_name)
        await self._store_views(
            [
                other
                for other in self.state.views
                if other.name not in {view.name, request.new_name}
            ]
            + [renamed]
        )
        self._notify(f"Renamed '{view.name}' to '{renamed.name}'.")
        return True

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
        starred = None if target is None else ref_key(target) in self.state.starred
        self.query_bar.set_star_state(starred)

    async def action_toggle_star(self) -> None:
        """Star or unstar the log the star target resolves to."""

        data = self._star_target()
        if data is None:
            self._notify("Open a log, or move the tree cursor to one, to star it.", "warning")
            return

        key = ref_key(data)
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

        discovered = {identity(item.path) for item in report.files}
        starred = [parse_ref(entry) for entry in self.state.starred]
        present = [path for path in starred if identity(path) in discovered]

        for path in starred:
            if identity(path) not in discovered:
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
        bindings = list(self.BINDINGS) + list(LogView.BINDINGS) + list(TimelineBar.BINDINGS)
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
        """Put the selection on the local clipboard via OSC 52.

        The counterpart to `Ctrl+L`, not a replacement for it: copy mode needs a
        local terminal selection, which is exactly what is unavailable over tmux
        or SSH, and this path needs a terminal that honours OSC 52. Whichever
        one an operator's setup supports, one of them works.

        "Selection" means the cursor line when there is one, and the visible
        view when there is not. Once a line can be pointed at, copying the whole
        pane instead of the line under the cursor is the surprising answer.
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

        cursor_entry = self.log_panel.cursor_entry
        if cursor_entry is not None:
            lines = [cursor_entry.raw]
        else:
            try:
                result = self._visible_entries(self._entries)
            except QueryError as exc:
                self._notify(f"Cannot copy while the query is invalid: {exc}", "error")
                return
            # The lines on screen, filter and window included — the same slice
            # _render_log writes. Ctrl+E is the path for the whole filtered set.
            lines = [entry.raw for entry in result.entries[-self._show_lines :]]

        payload = prepare_payload(lines, max_bytes=self._config.clipboard_max_bytes)
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
        # The cap can have changed under us, so the session adopts the new one
        # before anything is read against it.
        self._session.resize(self._config.max_buffer_lines)
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

        source = self._selected_source
        marked = [
            entry for entry in entries if self._marks.contains(self._origin(entry), entry)
        ]

        dialog = ExportDialog(
            self._exporter_choices(),
            entry_count=len(entries),
            # A merged export is named for the set, not for whichever member
            # happens to be first — "auth.log-20260812" would be a lie about
            # what is in the file. The set name is a label rather than a
            # location, and goes through as one.
            default_name=default_stem(
                self._merged_name() if self._session.is_merged else source
            ),
            marked_count=len(marked),
            cluster_count=self._clusters.clustered if self._clusters is not None else 0,
        )
        request = await self.push_screen(dialog, wait_for_dismiss=True)
        if request is None:
            self._notify("Export canceled.")
            return

        if request.marked_only:
            if not marked:  # pragma: no cover - the checkbox is disabled then
                self._notify("Nothing marked to export.", "warning")
                return
            entries = marked
        if request.clustered:
            # Expanded output is the default, and clustered output is derived
            # from it rather than from a second pass: every cluster becomes one
            # ordinary entry, so the exporters never learn what a cluster is.
            entries = self._clustered_entries(entries)
        self._run_export(request, entries)

    def _clustered_entries(self, entries: list[LogEntry]) -> list[LogEntry]:
        """One entry per repeat group, for a clustered export.

        Re-clustered from whatever the export is actually writing rather than
        read off the pane: "marked lines only" narrows the set first, and a
        count taken from the pane would then be a count of lines that are not
        in the file.
        """

        stream = cluster_entries(entries, lookback=self._config.cluster_lookback)
        return [
            summarise(row) if isinstance(row, Cluster) else row for row in stream.rows
        ]

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

    def on_tree_node_selected(self, event: Tree.NodeSelected[object]) -> None:
        data = event.node.data
        if isinstance(data, Path) and data.is_file():
            self._select_source(data)
        elif isinstance(data, RotatedSet):
            self._select_rotated_set(data)
        elif isinstance(data, ProviderSource):
            self._select_provider_source(data)
        elif isinstance(data, SavedView):
            self._apply_view(data)
            self._notify(f"Applied view '{data.name}'.")

    def on_log_tree_action_requested(self, message: LogTree.ActionRequested) -> None:
        """A marker on a row was clicked. The row says what it stands for."""

        if message.node.data is not MERGED_VIEW:
            return
        if message.action == "open":
            self.action_open_merged()
        elif message.action == "save":
            self.action_save_view()
        elif message.action == "clear":
            self.action_clear_merged()

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
        # Offered to the source first: a reader that can filter before the data
        # reaches us re-primes, and what it handed over earlier answered a
        # different question. Every other reader ignores this entirely.
        if self._session.push_severity(message.value):
            self._sync_field_names()
            self._watch_index.reset()
            self._sync_watch_rules()
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
            group_rotated=settings.group_rotated,
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
        elif message.field == "watch_rules":
            self._set_watch_enabled(message.value)
        elif message.field == "timeline":
            self._set_timeline(message.value)
        elif message.field == "clustering":
            self._set_clustering(message.value)
        elif message.field == "journald":
            self._set_journald(message.value)
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

    def _set_journald(self, value: bool) -> None:
        """Turn the journal on or off, and record the choice in settings.conf.

        Flipping this switch *is* the consent the plugin rule requires, so it
        takes effect now — but consent given once should not have to be given
        again every launch, and the settings file is where CLV already writes
        an operator's decision (`Ctrl+S` does exactly this with `log_dirs`).
        The plugin re-reads the file on every scan, so no reload is needed.
        """

        try:
            persist_setting(self._settings_path, "enable_journald", str(value).lower())
        except OSError as exc:
            self._notify(f"Could not save the journal setting: {exc}", "error")
            self._sync_journald_status()
            return

        self._config = replace(self._config, enable_journald=value)
        self._notify(
            f"systemd journal enabled — written to {self._settings_path}."
            if value
            else "systemd journal disabled."
        )
        self.run_worker(self._rescan(), group="discovery", exit_on_error=False)

    def _sync_journald_status(self) -> None:
        """Show journal state in the drawer, including why it may be off."""

        from .plugins.sources.journald import JournaldProvider, availability

        available, reason = availability()
        provider = next(
            (p for p in self._plugins.sources if isinstance(p, JournaldProvider)), None
        )
        if provider is not None and self._config.enable_journald and available:
            # What the provider actually found, rather than what it is allowed
            # to look for. "Two sources and no units" is the shape of a failure
            # and looked exactly like success from out here.
            self.advanced_drawer.set_journald(
                True, available=True, reason=provider.status
            )
            return
        if available and not any(
            isinstance(plugin, JournaldProvider) for plugin in self._plugins.sources
        ):
            # The switch would write an opt-in that nothing reads. Loading no
            # plugins is not an error — it is a valid state — so nothing else
            # would ever mention it, and a control that quietly does nothing is
            # worse than one that is visibly unavailable.
            available = False
            reason = "the journald plugin is not loaded in this build"
        self.advanced_drawer.set_journald(
            self._config.enable_journald and available,
            available=available,
            reason=reason
            or ("reading /var/log/journal via journalctl" if self._config.enable_journald else ""),
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
            watch_rules=self._watching,
            timeline=self.state.timeline,
            clustering=self.state.clustering,
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
        if key.startswith("watch:"):
            # Dismissing a watch chip disables the rule rather than deleting
            # it: a chip is a way to quieten something, not to throw it away.
            name = key.split(":", 1)[1]
            if name == "*":
                self._set_watch_enabled(False)
            else:
                self._set_watch_rules(toggled(self.state.watch_rules, name, False))
                self._notify(f"Watch rule '{name}' disabled.")
            return
        if key == "severity":
            self.query_bar.set_severity("all")
            self._update_state(severity="all")
        elif key == "time":
            self.query_bar.select_time("all")
            self._update_state(time_window="all", custom_start="", custom_end="")
        elif key == "invert":
            # sync_settings, not a bare assignment: the drawer's switch has to
            # follow the setting, or dismissing the chip leaves it showing on.
            self.advanced_drawer.sync_settings(
                replace(self.advanced_drawer.settings, invert_match=False)
            )
            self._update_state(invert_match=False)
        elif key == "include":
            self.advanced_drawer.sync_settings(
                replace(self.advanced_drawer.settings, include_globs="")
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
        # Readers may hold more than a file handle — a provider-backed source
        # owns a subprocess — so shutdown releases them explicitly rather than
        # leaving it to garbage collection.
        self._session.close()
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


def _walk_nodes(node: TreeNode[object]) -> Iterator[TreeNode[object]]:
    """Every node under *node*, itself included."""

    yield node
    for child in node.children:
        yield from _walk_nodes(child)


def _find_node(node: TreeNode[Path], target: Path) -> Optional[TreeNode[Path]]:
    if isinstance(node.data, Path) and identity(node.data) == target:
        return node
    for child in node.children:
        found = _find_node(child, target)
        if found is not None:
            return found
    return None


def run() -> None:  # pragma: no cover - script entry point
    LogViewerApp().run()
