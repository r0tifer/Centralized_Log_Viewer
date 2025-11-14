from __future__ import annotations

import asyncio
import configparser
import csv
import io
import itertools
import json
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal, Optional
from xml.dom import minidom

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import messages
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Button, Footer, Label, RichLog, Static, Switch, Tree
from textual.widgets import Input
from textual.widget import MountError
from textual.widgets._tree import TOGGLE_STYLE, TreeNode

from .services import SourceManager, persist_log_sources
from .storage import SessionState, StateStore
from .widgets.add_source_dialog import AddSourceDialog
from .widgets.advanced_drawer import AdvancedFiltersDrawer
from .widgets.custom_time_dialog import CustomTimeRangeDialog
from .widgets.filter_chip import FilterChip
from .widgets.query_bar import QueryBar


SEVERITY_SEGMENTS: list[tuple[str, str, str]] = [
    ("all", "All", ""),
    ("info", "Info", r"INFO"),
    ("warn", "Warn", r"WARN|WARNING"),
    ("error", "Error", r"ERROR"),
    ("debug", "Debug", r"DEBUG"),
]

SEVERITY_COLORS = {
    "ERROR": "#f87171",
    "WARN": "#facc15",
    "WARNING": "#facc15",
    "INFO": "#22c55e",
    "DEBUG": "#a855f7",
}

REGEX_SAMPLE_LIMIT = 2000
STRUCTURED_PAYLOAD_MAX_CHARS = 8_192
CSV_ROWS_DEFAULT = 20
CSV_COLS_DEFAULT = 10
CSV_ROWS_MIN = 1
CSV_COLS_MIN = 1
CSV_ROWS_MAX = 5_000
CSV_COLS_MAX = 200

LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?) - (?P<level>\w+) - (?P<message>.*)$"
)

SOURCES_PANEL_DEFAULT_WIDTH = 38
SOURCES_PANEL_MIN_WIDTH = 24
SOURCES_PANEL_MAX_WIDTH = 80
SOURCES_PANEL_STEP = 2

DEFAULT_SETTINGS_TEMPLATE = (
    "[log_viewer]\n"
    "log_dirs = /var/log\n"
    "max_buffer_lines = 500\n"
    "default_show_lines = 200\n"
    "refresh_hz = 2\n"
    "min_show_lines = 10\n"
    "show_step = 10\n"
    "csv_max_rows = 20\n"
    "csv_max_cols = 10\n"
)


@dataclass
class LogConfig:
    log_dirs: list[Path]
    max_buffer_lines: int
    default_show_lines: int
    refresh_hz: int
    min_show_lines: int
    show_step: int
    csv_max_rows: int
    csv_max_cols: int


@dataclass(frozen=True)
class DiscoverySummary:
    """Summary data describing discovered log sources."""

    source_count: int
    folder_count: int
    log_count: int


def get_xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".config"


def get_config_file() -> Optional[Path]:
    xdg_conf = _ensure_user_settings_file()
    if xdg_conf:
        return xdg_conf

    dev_conf = Path(__file__).resolve().parents[1] / "settings.conf"
    if dev_conf.exists():
        return dev_conf

    return None


def _ensure_user_settings_file() -> Optional[Path]:
    """Ensure the per-user settings file exists; copy template defaults if needed."""

    target = get_xdg_config_home() / "clv" / "settings.conf"
    template = Path(__file__).resolve().parents[1] / "settings.conf"

    if target.exists():
        return target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return template if template.exists() else None

    if template.exists():
        try:
            shutil.copyfile(template, target)
            return target
        except OSError:
            # Fall back to writing defaults below
            pass

    try:
        target.write_text(DEFAULT_SETTINGS_TEMPLATE, encoding="utf-8")
        return target
    except OSError:
        return template if template.exists() else None


def load_config() -> LogConfig:
    config = configparser.ConfigParser()
    path = get_config_file()
    if path:
        config.read(path)
    viewer = config["log_viewer"] if "log_viewer" in config else {}

    raw_dirs = viewer.get("log_dirs", "logs") if hasattr(viewer, "get") else "logs"
    if isinstance(raw_dirs, str):
        raw_dirs = [part.strip() for part in raw_dirs.split(",") if part.strip()]

    log_dirs: list[Path] = []
    for entry in raw_dirs:
        path = Path(entry).expanduser()
        if not path.is_absolute():
            continue
        try:
            path = path.resolve()
        except FileNotFoundError:
            pass
        log_dirs.append(path)

    if not log_dirs:
        log_dirs = [Path.cwd() / "logs"]

    def _get_int(option: str, default: int) -> int:
        if hasattr(viewer, "getint"):
            try:
                return viewer.getint(option, default)
            except ValueError:
                return default
        return default

    def _clamp(value: int, *, default: int, minimum: int, maximum: int) -> int:
        if not isinstance(value, int):
            return default
        return max(minimum, min(value, maximum))

    return LogConfig(
        log_dirs=log_dirs,
        max_buffer_lines=_get_int("max_buffer_lines", 500),
        default_show_lines=_get_int("default_show_lines", 200),
        refresh_hz=_get_int("refresh_hz", 2),
        min_show_lines=_get_int("min_show_lines", 10),
        show_step=_get_int("show_step", 10),
        csv_max_rows=_clamp(
            _get_int("csv_max_rows", CSV_ROWS_DEFAULT),
            default=CSV_ROWS_DEFAULT,
            minimum=CSV_ROWS_MIN,
            maximum=CSV_ROWS_MAX,
        ),
        csv_max_cols=_clamp(
            _get_int("csv_max_cols", CSV_COLS_DEFAULT),
            default=CSV_COLS_DEFAULT,
            minimum=CSV_COLS_MIN,
            maximum=CSV_COLS_MAX,
        ),
    )


def parse_timerange(shortcut: str) -> tuple[datetime, datetime]:
    now = datetime.now()
    shortcut = shortcut.lower().strip()
    if shortcut == "all":
        epoch = datetime.fromtimestamp(0)
        return epoch, now
    if shortcut.endswith("m"):
        minutes = int(shortcut[:-1])
        return now - timedelta(minutes=minutes), now
    if shortcut.endswith("h"):
        hours = int(shortcut[:-1])
        return now - timedelta(hours=hours), now
    if shortcut.endswith("d"):
        days = int(shortcut[:-1])
        return now - timedelta(days=days), now
    raise ValueError("Unsupported shortcut. Use values like '15m', '1h', '1d'.")


def parse_datetime_range(range_str: str) -> Optional[tuple[datetime, datetime]]:
    if "to" not in range_str:
        return None
    try:
        start_str, end_str = [segment.strip() for segment in range_str.split("to", 1)]
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str)
        return start, end
    except ValueError:
        return None


def parse_log_line(line: str) -> Optional[tuple[datetime, str, str]]:
    match = LOG_LINE_RE.match(line)
    if not match:
        return None
    timestamp_str = match.group("timestamp")
    level = match.group("level")
    message = match.group("message")
    timestamp: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            timestamp = datetime.strptime(timestamp_str, fmt)
            break
        except ValueError:
            continue
    if timestamp is None:
        return None
    return timestamp, level.upper(), message


def filter_log_lines(
    lines: Iterable[str],
    *,
    level: Optional[str] = None,
    regex: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[str]:
    pattern = re.compile(regex) if regex else None
    filtered: list[str] = []
    for raw in lines:
        parsed = parse_log_line(raw)
        if parsed is None:
            if pattern is None and level is None and start is None and end is None:
                filtered.append(raw)
            continue
        timestamp, severity, message = parsed
        if level and severity != level.upper():
            continue
        if pattern and not pattern.search(message):
            continue
        if start and timestamp < start:
            continue
        if end and timestamp > end:
            continue
        filtered.append(raw)
    return filtered


class FilterChips(Container):
    COLUMN_ORDER: tuple[str, ...] = (
        "query",
        "regex-status",
        "time",
        "severity",
        "auto",
        "actions",
    )
    KEY_TO_SLOT: dict[str, str] = {
        "regex": "query",
        "time": "time",
        "severity": "severity",
    }
    EXTRA_SLOT = "extra"

    DEFAULT_CSS = """
    /* Active filters chip bar — clearer separation from QueryBar */
    FilterChips {
        layout: grid;
        grid-columns: 3fr 2fr 2fr 1fr 1fr 1fr;
        grid-rows: auto auto;
        grid-gutter: 0 1;
        padding: 0 2 1 2;
        background: $surface 6%;
        border-bottom: solid $surface 12%;
        height: auto;
        min-height: 0;
        max-height: 6;
        overflow-y: auto;
    }

    FilterChips > .chip-slot {
        layout: vertical;
        align: left top;
    }

    FilterChips > .chip-slot > FilterChip {
        margin-bottom: 1;
    }

    FilterChips > .chip-slot > FilterChip:last-child {
        margin-bottom: 0;
    }

    FilterChips > #chip-slot-extra {
        column-span: 6;
        margin-top: 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._slots: dict[str, Container] = {}
        for slot in self.COLUMN_ORDER:
            self._slots[slot] = Container(id=f"chip-slot-{slot}", classes="chip-slot")
        self._slots[self.EXTRA_SLOT] = Container(
            id="chip-slot-extra", classes="chip-slot extra-slot"
        )

    def compose(self) -> ComposeResult:
        for slot in self.COLUMN_ORDER:
            yield self._slots[slot]
        yield self._slots[self.EXTRA_SLOT]

    def _clear_slots(self) -> None:
        for slot in self._slots.values():
            slot.remove_children()

    def _resolve_slot(self, key: str) -> Container:
        slot_name = self.KEY_TO_SLOT.get(key)
        if slot_name and slot_name in self._slots:
            return self._slots[slot_name]
        if self.EXTRA_SLOT in self._slots:
            return self._slots[self.EXTRA_SLOT]
        return self._slots[self.COLUMN_ORDER[0]]

    def update_chips(self, chips: list[FilterChip]) -> None:
        self._clear_slots()
        has_chips = bool(chips)
        self.display = has_chips
        if not has_chips:
            return
        for chip in chips:
            slot = self._resolve_slot(getattr(chip, "key", ""))
            slot.mount(chip)


class LogTree(Tree[Path]):
    """Tree widget with themed icons for log sources."""

    COMPONENT_CLASSES = Tree.COMPONENT_CLASSES | {
        "tree--icon-root",
        "tree--icon-branch",
        "tree--icon-leaf",
        "tree--focus-indicator",
    }

    ICON_ROOT = "🌲"
    ICON_BRANCH = "📂"
    ICON_LEAF = "📄"
    FOCUS_ARROW = "›"

    def __init__(
        self,
        *args,
        base_path: Path | None = None,
        role: Literal["directory", "files", "placeholder"] = "directory",
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.base_path: Path | None = base_path
        self.role: Literal["directory", "files", "placeholder"] = role
        self.show_guides = True
        self.guide_depth = 4

    def render_label(
        self,
        node: TreeNode[Path],
        base_style: Style,
        style: Style,
    ) -> Text:
        """Render labels with toggle and icon styling."""

        node_label = node._label.copy()
        node_label.stylize(style)

        is_cursor = node is self.cursor_node
        indicator_style = base_style
        if is_cursor:
            indicator_style = base_style + self.get_component_rich_style(
                "tree--focus-indicator",
                partial=True,
            )
            indicator = (f"{self.FOCUS_ARROW} ", indicator_style)
        else:
            indicator = ("  ", base_style)

        if node._allow_expand:
            toggle_symbol = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            toggle = (f"{toggle_symbol} ", base_style + TOGGLE_STYLE)
        else:
            toggle = ("  ", base_style)

        if node.is_root:
            icon_name = "tree--icon-root"
            icon_symbol = self.ICON_ROOT
        elif node._allow_expand:
            icon_name = "tree--icon-branch"
            icon_symbol = self.ICON_BRANCH
        else:
            icon_name = "tree--icon-leaf"
            icon_symbol = self.ICON_LEAF

        icon_style = base_style + self.get_component_rich_style(icon_name, partial=True)
        icon = (f"{icon_symbol} ", icon_style)

        return Text.assemble(indicator, toggle, icon, node_label)

class LogViewerApp(App[None]):
    CSS = """
    /* Root must be vertical so fractional heights propagate */
    Screen { layout: vertical; }

    #main-content {
        height: 1fr;
        min-height: 1;
    }

    #chip-bar { min-height: 0; max-height: 4; overflow-y: auto; padding: 0 2; }

    /* Make columns stretch to the remaining screen height */
    #sources-panel,
    #viewer-panel {
        height: 1fr;
    }

    #sources-panel {
        width: 38;
        min-width: 24;
        border-right: solid $surface 15%;
        padding: 0 2 0 1;
        background: $surface 2%;
    }

    #sources-panel:focus-within {
        border-right: solid $accent 60%;
        background: $surface 6%;
    }

    /* Let the tree area actually consume vertical space */
    #tree-panel {
        layout: vertical;
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        padding-right: 1;
        scrollbar-gutter: stable;
    }

    #tree-panel > .log-tree {
        height: auto;
        min-height: 3;
        margin-bottom: 0;
    }

    #tree-panel > .log-tree:last-child {
        margin-bottom: 0;
    }

    /* ---- Query Bar layout + controls (mirrors widget DEFAULT_CSS) ---- */

    /* Top band rows */
    #query-grid { layout: vertical; }
    #query-grid > .row { layout: horizontal; height: auto; margin: 0; padding: 0; align: left middle; width: 1fr; }
    #query-grid > .row > LabeledField { width: 1fr; margin-right: 1; }
    #query-grid > .row > LabeledField:last-child { margin-right: 0; }
    #time-row { align: left top; }
    #time-row > #time-controls { layout: horizontal; align: right top; height: auto; width: auto; }
    #time-spacer { width: 1fr; min-width: 0; }
    #time-controls > #auto-scroll-field { margin-right: 1; }
    #auto-scroll-field { padding-top: 0; }
    #actions-field { layout: horizontal; align: right top; height: auto; min-height: 3; padding: 1 0 0 0; }
    #actions-field Button { margin-left: 1; height: 3; min-height: 3; padding: 0 2; }
    #actions-field Button:first-child { margin-left: 0; }
    #actions-field Button#add-source { margin-left: 1; }

    /* LabeledField sizing so children don't collapse */
    LabeledField { width: 1fr; min-width: 16; }
    LabeledField > .field-label { color: $text-muted; }
    LabeledField > .field-control { height: auto; min-height: 3; width: 1fr; }

    /* Inputs span their grid cell */
    QueryBar Input { height: 3; width: 1fr; border: tall $surface 25%; background: $surface 8%; }

    /* Radios behave like buttons */
    QueryBar RadioSet { layout: horizontal; }
    QueryBar #time-field { width: auto; }
    QueryBar #time-field .field-control { width: auto; layout: horizontal; align: left middle; content-align: left middle; height: auto; min-height: 3; padding: 0 1 0 1; margin: 0; }
    QueryBar #time-field RadioSet { width: auto; align: left middle; }
    QueryBar RadioButton {
        border: round $surface 25%;
        background: $surface 12%;
        color: $text;
        padding: 0 2;
        height: 3;
        align: center middle;
        margin-right: 1;
    }
    QueryBar RadioSet RadioButton:last-child { margin-right: 0; }
    QueryBar RadioButton.-checked { background: $accent 38%; border: round $accent 40%; color: $text; }
    QueryBar RadioButton:hover,
    QueryBar RadioButton:focus { background: $surface 18%; outline: wide $accent 30%; }

    /* Severity segmented pill group */
    QueryBar #severity-field SegmentedButtons { height: 3; width: 1fr; }

    /* Switch shouldn't stretch full width */
    #auto-scroll-field .field-control { width: auto; }

    .log-tree {
        background: $surface 6%;
        border: round $surface 12%;
        padding: 0 0 1 0;
    }

    .log-tree:focus {
        border: round $accent 50%;
        background: $surface 10%;
        outline: heavy $accent 60%;
    }

    .empty-tree {
        color: $text-muted;
        padding: 1 0;
    }

    LogTree {
        color: #dce3f7;
    }

    LogTree > .tree--guides {
        color: #587ca7;
    }

    LogTree > .tree--guides-hover {
        color: #7aa3d1;
    }

    LogTree > .tree--guides-selected {
        color: #95c8f5;
    }

    LogTree > .tree--highlight-line {
        background: #2c384f;
    }

    LogTree > .tree--cursor {
        background: #3d4f6a;
        color: #ffffff;
        text-style: bold;
    }

    LogTree > .tree--label {
        color: #eef3ff;
    }

    LogTree > .tree--focus-indicator {
        color: $accent;
        text-style: bold;
    }

    LogTree > .tree--icon-root {
        color: #81e09a;
    }

    LogTree > .tree--icon-branch {
        color: #7fc4ff;
    }

    LogTree > .tree--icon-leaf {
        color: #f6d38d;
    }

    #viewer-panel {
        width: 1fr;
        min-width: 40;
        padding: 0 1 0 1;
    }

    #main-content RichLog {
        border: solid $surface 20%;
        height: 1fr;
    }

    .panel-title {
        padding: 0 0 1 0;
        text-style: bold;
    }

    /* Toast notifications reflect severity with solid background colors */
    Toast {
        border: none;
        background: $surface 12%;
        color: $text;
    }

    Toast.-information {
        background: #14532d;
        color: #f0fdf4;
    }

    Toast.-warning {
        background: #713f12;
        color: #fefce8;
    }

    Toast.-error {
        background: #7f1d1d;
        color: #fee2e2;
    }

    LogViewerApp.-copy-mode #query-bar,
    LogViewerApp.-copy-mode #chip-bar,
    LogViewerApp.-copy-mode #advanced-drawer,
    LogViewerApp.-copy-mode #sources-panel,
    LogViewerApp.-copy-mode Footer,
    LogViewerApp.-copy-mode Label.panel-title {
        display: none;
    }

    LogViewerApp.-copy-mode #viewer-panel {
        width: 1fr;
        padding: 0;
    }

    LogViewerApp.-copy-mode #main-content RichLog {
        border: none;
    }


    """
    BINDINGS = [
        Binding("/", "focus_query", "Focus query", show=False),
        Binding("ctrl+enter", "run_query", "Apply filters", show=False),
        Binding("enter", "run_query", "Apply filters", show=False),
        Binding("escape", "clear_field", "Clear query", show=False),
        Binding("a", "open_add_source_dialog", "Add source", show=False),
        Binding("[", "shrink_sources_panel", "Narrow sources", show=False),
        Binding("]", "expand_sources_panel", "Widen sources", show=False),
        Binding("+", "more_lines", "Show more lines", show=False),
        Binding("-", "fewer_lines", "Show fewer lines", show=False),
        Binding("ctrl+l", "toggle_copy_mode", "Copy mode", show=False),
        Binding("ctrl+s", "save_session", "Persist session", show=False),
        Binding("q", "quit_app", "Quit", show=False),
        Binding("t", "cycle_time", "Cycle time", show=False),
        Binding("s", "cycle_severity", "Cycle severity", show=False),
    ]

    state = reactive(SessionState())

    def __init__(self) -> None:
        super().__init__()
        self._persist_state = False
        self._store = StateStore()
        self._config = load_config()
        config_path = get_config_file()
        if config_path is None:
            config_path = get_xdg_config_home() / "clv" / "settings.conf"
        self._settings_path = config_path
        self._sources_panel_width = SOURCES_PANEL_DEFAULT_WIDTH
        self._show_step = max(1, self._config.show_step)
        self._show_lines = self._clamp_show_lines(self._config.default_show_lines)
        self._copy_mode_active = False
        self._sources: list[Path] = []
        self._source_manager = SourceManager([], [])
        self._selected_source: Optional[Path] = None
        self._discovery_summary: DiscoverySummary | None = None
        self._suppress_tree_selection = True
        self._raw_lines: deque[str] = deque(maxlen=self._config.max_buffer_lines)
        self._tail_timer: Timer | None = None
        self._tail_offset: int = 0
        self._tail_remainder: str = ""
        self.query_bar = QueryBar()
        self.chip_bar = FilterChips(id="chip-bar")
        self.advanced_drawer = AdvancedFiltersDrawer()
        self.log_panel = RichLog(id="log-stream")
        self.log_panel.auto_scroll = self.state.auto_scroll
        self._is_shutting_down: bool = False

    def compose(self) -> ComposeResult:
        yield self.query_bar
        yield self.chip_bar
        yield self.advanced_drawer
        with Horizontal(id="main-content"):
            with Vertical(id="sources-panel"):
                yield Label("Log Sources", classes="panel-title")
                yield Vertical(id="tree-panel")
            with Vertical(id="viewer-panel"):
                yield Label("Log Output", classes="panel-title")
                yield self.log_panel
        yield Footer()

    async def on_mount(self) -> None:
        self.state = self._store.load()
        self._initialize_sources()
        discovery_summary = await self._populate_tree()
        self._apply_state()
        self._refresh_chips()
        # ---- Force vertical stretch so Log panes never get squished ----
        screen = self.screen
        screen.styles.layout = "vertical"
        screen.styles.height = "100%"
        main = self.query_one("#main-content")
        main.styles.layout = "horizontal"
        main.styles.height = "1fr"

        self.query_bar.styles.height = "auto"
        self.query_bar.styles.max_height = 12
        self.query_bar.styles.overflow_y = "hidden"

        self.chip_bar.styles.max_height = 4
        self.chip_bar.styles.height = "auto"
        self.chip_bar.styles.overflow_y = "auto"

        self.query_one("#sources-panel").styles.height = "1fr"
        self.query_one("#viewer-panel").styles.height = "1fr"
        tree_panel = self.query_one("#tree-panel")
        tree_panel.styles.height = "1fr"
        tree_panel.styles.overflow_y = "auto"
        tree_panel.styles.overflow_x = "hidden"
        tree_panel.styles.scrollbar_gutter = "stable"
        self._apply_sources_panel_width()
        self.log_panel.clear()
        self._write_discovery_summary(discovery_summary)
        selected = False
        if self.state.selected_source:
            selected = self._select_source(Path(self.state.selected_source))
        if not selected:
            if self.state.selected_source:
                self._update_state(selected_source="")
            self.log_panel.clear()
            self._write_discovery_summary(discovery_summary)
        self._suppress_tree_selection = False
        self._persist_state = True
        self._store.save(self.state)

    def _apply_state(self) -> None:
        self.query_bar.set_query_value(self.state.query)
        self.query_bar.set_severity(self.state.severity)
        if (
            self.state.time_window == "range"
            and self.state.custom_start
            and self.state.custom_end
        ):
            self.query_bar.apply_custom_time_range(
                self.state.custom_start,
                self.state.custom_end,
                emit=False,
            )
        else:
            self.query_bar.select_time(self.state.time_window)
        switch = self.query_bar.query_one("#auto-scroll-toggle", Switch)
        switch.value = self.state.auto_scroll
        self.log_panel.auto_scroll = self.state.auto_scroll
        self.query_bar.set_pretty_rendering(self.state.pretty_rendering)
        self._sync_regex_validation()

    def _initialize_sources(self) -> None:
        directories: list[Path] = []
        files: list[Path] = []
        for entry in self._config.log_dirs:
            try:
                if entry.is_dir():
                    directories.append(entry)
                elif entry.is_file():
                    files.append(entry)
            except OSError:
                continue
        self._source_manager = SourceManager(directories, files)
        self._sources = []

    def _tree_panel(self) -> Vertical:
        return self.query_one("#tree-panel", Vertical)

    def _ensure_tree_focus(self) -> None:
        if not self.is_mounted:
            return
        try:
            panel = self._tree_panel()
        except NoMatches:
            return
        tree: LogTree | None = None
        for candidate in panel.query(LogTree):
            tree = candidate
            break
        if tree is None:
            return
        tree.focus()
        cursor = tree.cursor_node
        if cursor is None:
            tree.select_node(tree.root)
        tree.scroll_to_node(tree.cursor_node or tree.root)

    def _apply_sources_panel_width(self) -> None:
        if not self.is_mounted:
            return
        try:
            panel = self.query_one("#sources-panel", Vertical)
        except Exception:
            return
        panel.styles.width = self._sources_panel_width
        panel.refresh(layout=True)

    def _adjust_sources_panel_width(self, delta: int) -> None:
        new_width = self._sources_panel_width + delta
        self._sources_panel_width = max(
            SOURCES_PANEL_MIN_WIDTH,
            min(SOURCES_PANEL_MAX_WIDTH, new_width),
        )
        self._apply_sources_panel_width()

    def _clamp_show_lines(self, value: int) -> int:
        minimum = max(1, self._config.min_show_lines)
        maximum = max(minimum, self._config.max_buffer_lines)
        return max(minimum, min(value, maximum))

    @staticmethod
    def _clear_node(node: TreeNode[Path]) -> None:
        if hasattr(node, "clear") and callable(node.clear):
            node.clear()
        else:
            for child in list(node.children):
                child.remove()

    @staticmethod
    def _format_root_label(base: Path) -> str:
        return str(base)

    def _populate_directory_tree(
        self,
        tree: LogTree,
        base: Path,
        *,
        sources: set[Path],
        dir_accumulator: set[Path],
    ) -> int:
        root = tree.root
        self._clear_node(root)
        root.label = self._format_root_label(base)
        root.data = base

        dir_nodes: dict[Path, TreeNode[Path]] = {base: root}

        try:
            candidates = list(base.rglob("*"))
        except (OSError, PermissionError):
            candidates = []

        files: list[Path] = []
        for candidate in candidates:
            try:
                if candidate.is_file() and os.access(candidate, os.R_OK):
                    files.append(candidate)
            except OSError:
                continue

        files.sort(key=lambda p: str(p.relative_to(base)).lower())

        count = 0
        for file_path in files:
            if base not in dir_accumulator:
                dir_accumulator.add(base)
            count += 1
            sources.add(file_path)
            rel = file_path.relative_to(base)
            node = root
            current = base
            for part in rel.parts[:-1]:
                current = current / part
                dir_accumulator.add(current)
                if current not in dir_nodes:
                    dir_nodes[current] = node.add(part, data=current)
                node = dir_nodes[current]
            node.add_leaf(rel.name, data=file_path)

        if count:
            root.expand()
        return count

    def _populate_file_tree(self, tree: LogTree, sources: set[Path], files: list[Path]) -> int:
        root = tree.root
        self._clear_node(root)
        count = 0
        for file_path in files:
            root.add_leaf(str(file_path), data=file_path)
            sources.add(file_path)
            count += 1
        if count:
            root.expand()
        return count

    async def _populate_tree(self) -> DiscoverySummary:
        panel = self._tree_panel()
        for child in list(panel.children):
            await child.remove()

        sources: set[Path] = set()
        discovered_dirs: set[Path] = set()
        total_files = 0

        session_dirs = self._source_manager.directories
        for base in session_dirs:
            tree = LogTree(self._format_root_label(base), classes="log-tree", base_path=base, role="directory")
            await panel.mount(tree)
            total_files += self._populate_directory_tree(
                tree,
                base,
                sources=sources,
                dir_accumulator=discovered_dirs,
            )

        standalone_count = 0
        session_files = self._source_manager.files
        if session_files:
            file_tree = LogTree("Individual Logs", classes="log-tree", role="files")
            await panel.mount(file_tree)
            standalone_count = self._populate_file_tree(file_tree, sources, session_files)
            total_files += standalone_count
            for file_path in session_files:
                discovered_dirs.add(file_path.parent)

        if not panel.children:
            await panel.mount(Static("No log sources configured.", classes="empty-tree"))

        self._ensure_tree_focus()
        self._sources = sorted(sources, key=lambda p: str(p).lower())
        configured_sources = len(session_dirs) + len(session_files)
        summary = DiscoverySummary(
            source_count=configured_sources,
            folder_count=len(discovered_dirs),
            log_count=total_files,
        )
        self._discovery_summary = summary
        return summary

    def _highlight_source(self, path: Path) -> None:
        """Move tree focus to the node representing *path* if present."""

        try:
            target = path.resolve()
        except OSError:
            target = path

        panel = self._tree_panel()
        for tree in panel.query(LogTree):
            node_path = self._find_node_path(tree.root, target)
            if not node_path:
                continue
            *ancestors, node = node_path
            for ancestor in ancestors:
                ancestor.expand()
            tree.focus()
            tree.select_node(node)
            tree.scroll_to_node(node)
            return

    @staticmethod
    def _find_node_path(node: TreeNode[Path], target: Path) -> list[TreeNode[Path]]:
        if isinstance(node.data, Path):
            try:
                node_path = node.data.resolve()
            except OSError:
                node_path = node.data
            if node_path == target:
                return [node]
        for child in node.children:
            path = LogViewerApp._find_node_path(child, target)
            if path:
                return [node, *path]
        return []

    def _write_discovery_summary(self, summary: DiscoverySummary) -> None:
        lines = [
            "Summary",
            f"Total log sources: {summary.source_count}",
            f"Folders containing logs: {summary.folder_count}",
            f"Total logs: {summary.log_count}",
            "",
            "Select a log from the tree to begin.",
        ]
        for line in lines:
            self._write_log_line(line)

    def _clear_selected_source_state(self) -> None:
        """Reset any selected source and persist the cleared session state."""

        if self._tail_timer is not None:
            self._tail_timer.stop()
            self._tail_timer = None
        self._selected_source = None
        self._raw_lines.clear()
        self._tail_offset = 0
        self._tail_remainder = ""
        if self.state.selected_source:
            self._update_state(selected_source="")

    def _select_source(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            return False
        if not resolved.exists() or not resolved.is_file():
            return False
        if resolved not in self._sources:
            self._sources.append(resolved)
            self._sources.sort(key=lambda p: str(p).lower())
        self._selected_source = resolved
        try:
            raw_text = resolved.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            self._show_message(f"Failed to read {resolved}: {exc}", "error")
            return False
        lines = raw_text.splitlines()
        self._raw_lines.clear()
        for line in lines[-self._config.max_buffer_lines :]:
            self._raw_lines.append(line)
        try:
            self._tail_offset = resolved.stat().st_size
        except OSError:
            self._tail_offset = 0
        self._tail_remainder = ""
        self._update_state(selected_source=str(resolved))
        self._sync_regex_validation()
        self._render_log()
        self._restart_tail_timer()
        return True

    def _sync_regex_validation(self) -> None:
        sample = list(self._raw_lines)[-REGEX_SAMPLE_LIMIT:]
        self.query_bar.validate_regex(sample)

    def _restart_tail_timer(self) -> None:
        if self._tail_timer is not None:
            self._tail_timer.stop()
            self._tail_timer = None
        if not self._selected_source:
            return
        interval = max(0.25, 1 / max(self._config.refresh_hz, 1))
        self._tail_timer = self.set_interval(interval, self._poll_tail)

    def _poll_tail(self) -> None:
        if not self._selected_source:
            return
        path = self._selected_source
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._tail_offset:
            self._tail_offset = 0
        if size == self._tail_offset:
            return
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(self._tail_offset)
                chunk = handle.read()
                self._tail_offset = handle.tell()
        except OSError:
            return
        if not chunk:
            return
        text = self._tail_remainder + chunk
        lines = text.splitlines()
        if text.endswith(("\n", "\r")):
            remainder = ""
        else:
            remainder = lines.pop() if lines else text
        self._tail_remainder = remainder
        for line in lines:
            self._raw_lines.append(line)
        self._sync_regex_validation()
        self._render_log()

    def _render_log(self) -> None:
        self.log_panel.clear()
        if not self._selected_source:
            if self._discovery_summary:
                self._write_discovery_summary(self._discovery_summary)
            else:
                self._write_log_line("Select a log from the tree to begin.")
            return

        lines = list(self._raw_lines)
        if not lines:
            self._write_log_line("No log entries found in the selected source.")
            return

        filtered = self._apply_filters(lines)
        if not filtered:
            self._write_log_line("No log lines match the current filters.")
            return

        visible = filtered[-self._show_lines :]

        for line in visible:
            renderable = self._renderable_for_line(line)
            self._write_log_line(renderable)
        if self.state.auto_scroll:
            self.log_panel.scroll_end(animate=False)

    def _apply_filters(self, lines: list[str]) -> list[str]:
        level = None if self.state.severity == "all" else self.state.severity
        regex = self.state.query or None
        start: Optional[datetime] = None
        end: Optional[datetime] = None
        if self.state.time_window == "range" and self.state.custom_start and self.state.custom_end:
            parsed = parse_datetime_range(f"{self.state.custom_start} to {self.state.custom_end}")
            if parsed:
                start, end = parsed
        elif self.state.time_window and self.state.time_window not in {"", "all"}:
            try:
                start, end = parse_timerange(self.state.time_window)
            except ValueError:
                start = end = None
        return filter_log_lines(lines, level=level, regex=regex, start=start, end=end)

    def _renderable_for_line(self, line: str) -> RenderableType:
        if self.state.pretty_rendering:
            structured = self._format_structured_line(line)
            if structured is not None:
                return structured
        return self._colorize_text(line)

    def _format_structured_line(self, line: str) -> RenderableType | None:
        parsed = parse_log_line(line)
        if not parsed:
            return None
        _, severity, message = parsed
        payload = message.strip()
        if not payload or len(payload) > STRUCTURED_PAYLOAD_MAX_CHARS:
            return None
        formatted = (
            self._format_json_payload(payload)
            or self._format_xml_payload(payload)
            or self._format_csv_payload(payload)
        )
        if not formatted:
            return None
        renderable, label = formatted
        header = self._colorize_text(line)
        panel = Panel(
            renderable,
            title=label,
            border_style=SEVERITY_COLORS.get(severity, "#94a3b8"),
            padding=(0, 1),
        )
        return Group(header, panel)

    def _format_json_payload(self, payload: str) -> tuple[RenderableType, str] | None:
        if not payload.startswith(("{", "[")):
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        return Syntax(pretty, "json", theme="ansi_dark"), "JSON"

    def _format_xml_payload(self, payload: str) -> tuple[RenderableType, str] | None:
        if not payload.startswith("<"):
            return None
        try:
            dom = minidom.parseString(payload)
        except Exception:  # pragma: no cover - defensive
            return None
        pretty = dom.toprettyxml(indent="  ")
        cleaned = "\n".join(line for line in pretty.splitlines() if line.strip())
        return Syntax(cleaned, "xml", theme="ansi_dark"), "XML"

    def _format_csv_payload(self, payload: str) -> tuple[RenderableType, str] | None:
        if "," not in payload:
            return None
        reader = csv.reader(io.StringIO(payload))
        max_rows = self._config.csv_max_rows
        max_cols = self._config.csv_max_cols
        try:
            rows = list(itertools.islice(reader, max_rows))
        except csv.Error:
            return None
        if not rows:
            return None
        max_len = max((len(row) for row in rows), default=0)
        column_count = min(max_len, max_cols)
        if column_count == 0:
            return None
        table = Table(box=None, show_header=True, show_edge=False, pad_edge=False)
        headers = [f"Col {index + 1}" for index in range(column_count)]
        for header in headers:
            table.add_column(header, overflow="fold")
        truncated_col = False
        for row in rows:
            padded = list(row[:column_count])
            if len(row) > column_count:
                truncated_col = True
            if len(padded) < column_count:
                padded.extend([""] * (column_count - len(padded)))
            table.add_row(*padded)
        if truncated_col:
            table.add_row(*(["..."] * column_count))
        return table, "CSV preview"

    def _colorize_text(self, line: str) -> Text:
        parsed = parse_log_line(line)
        styled = Text(line)
        if parsed:
            _, severity, _ = parsed
            color = SEVERITY_COLORS.get(severity)
            if color:
                styled.stylize(color)
        return styled

    def _refresh_chips(self) -> None:
        # Skip visual updates if the app is shutting down or the chip bar isn't attached
        if getattr(self, "_is_shutting_down", False):
            return
        if not hasattr(self, "chip_bar") or not self.chip_bar or not self.chip_bar.is_attached:
            return

        chips: list[FilterChip] = []

        # Optional: keep the query chip compact
        def _elide(text: str, limit: int = 64) -> str:
            return text if len(text) <= limit else text[: limit - 1] + "…"

        # Query chip
        if getattr(self.state, "query", ""):
            # If you previously used key="regex", consider switching to "query" for clarity
            chips.append(FilterChip(f"Query: {_elide(self.state.query)}", key="query"))

        # Severity chip
        if getattr(self.state, "severity", "all") != "all":
            chips.append(
                FilterChip(f"Severity: {self.state.severity.title()}", key="severity")
            )

        # Time chips
        tw = getattr(self.state, "time_window", "")
        if tw and tw not in {"", "all"}:
            if (
                tw == "range"
                and getattr(self.state, "custom_start", "")
                and getattr(self.state, "custom_end", "")
            ):
                chips.append(
                    FilterChip(
                        f"Time: {self.state.custom_start} ⟷ {self.state.custom_end}",
                        key="time",
                    )
                )
            elif tw != "range":
                chips.append(FilterChip(f"Time: {tw}", key="time"))

        # Mount chips only if the chip bar is still attached; ignore teardown races
        try:
            self.chip_bar.update_chips(chips)
        except MountError:
            return

    def watch_state(self, old_state: SessionState, new_state: SessionState) -> None:  # type: ignore[override]
        if not getattr(self, "_persist_state", False):
            return
        tracked_changed = any(
            getattr(old_state, field) != getattr(new_state, field)
            for field in SessionState.PERSISTED_FIELDS
        )
        if not tracked_changed:
            return
        self._store.save(new_state)

    def action_focus_query(self) -> None:
        self.set_focus(self.query_bar.query_one("#query-input"))

    def action_cycle_time(self) -> None:
        value = self.query_bar.cycle_time_preset()
        self._update_state(time_window=value)
        self._render_log()

    def action_cycle_severity(self) -> None:
        value = self.query_bar.cycle_severity()
        self._update_state(severity=value)
        self._render_log()

    def action_run_query(self) -> None:
        self._render_log()
        self._show_message("Filters applied")

    def action_clear_field(self) -> None:
        query_input = self.query_bar.query_one("#query-input")
        query_input.value = ""
        self.query_bar.validate_regex([])
        self._update_state(query="")
        self._render_log()

    def action_save_session(self) -> None:
        new_paths = self._source_manager.added_paths
        if not new_paths:
            self._show_message("No new log sources to save.", "warning")
            return
        try:
            persist_log_sources(self._settings_path, self._source_manager.all_sources())
        except Exception as exc:  # noqa: BLE001
            self._show_message(f"Failed to save session: {exc}", "error")
            return
        self._source_manager.clear_added()
        self._config.log_dirs = self._source_manager.all_sources()
        self._show_message(f"Saved {len(new_paths)} new log source(s) to settings.")

    def action_open_add_source_dialog(self) -> None:
        if not self.is_mounted:
            return
        self.run_worker(
            self._prompt_add_source(),
            name="add-source-dialog",
            group="dialogs",
            exit_on_error=False,
        )

    def action_shrink_sources_panel(self) -> None:
        self._adjust_sources_panel_width(-SOURCES_PANEL_STEP)

    def action_expand_sources_panel(self) -> None:
        self._adjust_sources_panel_width(SOURCES_PANEL_STEP)

    def action_more_lines(self) -> None:
        updated = self._clamp_show_lines(self._show_lines + self._show_step)
        if updated == self._show_lines:
            self._announce_line_window()
            return
        self._show_lines = updated
        self._render_log()
        self._announce_line_window()

    def action_fewer_lines(self) -> None:
        updated = self._clamp_show_lines(self._show_lines - self._show_step)
        if updated == self._show_lines:
            self._announce_line_window()
            return
        self._show_lines = updated
        self._render_log()
        self._announce_line_window()

    def action_toggle_copy_mode(self) -> None:
        self._copy_mode_active = not self._copy_mode_active
        self.set_class(self._copy_mode_active, "-copy-mode")
        if self._copy_mode_active:
            try:
                self.set_focus(self.log_panel)
            except Exception:
                pass
            message = "Copy mode enabled. UI chrome hidden for selection."
        else:
            message = "Copy mode disabled. Controls restored."
        try:
            self.notify(message, severity="information", title="", markup=False)
        except Exception:
            pass

    def action_quit_app(self) -> None:
        self.exit()

    async def _prompt_add_source(self) -> None:
        dialog = AddSourceDialog()

        result = await self.push_screen(dialog, wait_for_dismiss=True)
        await self._process_add_source_result(result)

    async def _process_add_source_result(self, result: str | None) -> None:
        if result is None:
            self._show_message("Add log source canceled.")
            return

        if not result.strip():
            self._show_message("No path entered.", "warning")
            return

        addition = self._source_manager.add(result)

        if addition.success:
            summary = await self._populate_tree()
            if not self._selected_source:
                self.log_panel.clear()
                self._write_discovery_summary(summary)
            if addition.path:
                self._highlight_source(addition.path)
        elif addition.path and self._source_manager.contains(addition.path):
            self._highlight_source(addition.path)

        if addition.messages:
            non_info = [m for m in addition.messages if m.severity != "info"]
            info = [m for m in addition.messages if m.severity == "info"]
            for message in [*non_info, *info]:
                self._show_message(message.text, message.severity)
        elif not addition.success:
            self._show_message(
                "Unable to add log source. Check the path and permissions.",
                "error",
            )

        if addition.success:
            self._show_message("Log Source successfully added.")

    def _update_state(self, **changes) -> None:
        # Always update the state first
        self.state = replace(self.state, **changes)

        # Skip visual updates if we are shutting down or the UI isn't fully attached
        if self._is_shutting_down:
            return
        if not self.is_mounted:
            return
        if not hasattr(self, "chip_bar") or not self.chip_bar.is_attached:
            return

        try:
            self._refresh_chips()
        except MountError:
            # If teardown races detach the target, ignore the visual update
            pass

    def _show_message(self, text: str, severity: Literal["info", "warning", "error"] = "info") -> None:
        normalized = severity if severity in {"info", "warning", "error"} else "info"
        toast_severity = {
            "info": "information",
            "warning": "warning",
            "error": "error",
        }[normalized]

        # Toast notification for visibility (bottom-right)
        try:
            self.notify(text, severity=toast_severity, title="", markup=False)
        except Exception:  # pragma: no cover - defensive
            pass

        label_map = {
            "info": ("SUCCESS", "#22c55e"),
            "warning": ("WARNING", "#facc15"),
            "error": ("ERROR", "#f87171"),
        }
        label, color = label_map[normalized]
        safe_text = escape(text)
        message = f"[{color}]{label}: {safe_text}[/{color}]"
        self._write_log_line(message)

    def _announce_line_window(self) -> None:
        if not self.is_mounted:
            return
        try:
            self.notify(
                f"Showing last {self._show_lines} line(s).",
                severity="information",
                title="",
                markup=False,
            )
        except Exception:
            pass

    def _write_log_line(self, payload: RenderableType | str) -> None:
        """Write to the log widget, preserving Rich renderables when provided."""

        if isinstance(payload, str):
            self.log_panel.write(payload)
            return
        if isinstance(payload, Text):
            text = payload.copy()
            self.log_panel.write(text)
            return
        self.log_panel.write(payload)

    async def on_tree_node_selected(self, event: Tree.NodeSelected[Path]) -> None:
        if self._suppress_tree_selection:
            event.stop()
            return
        if isinstance(event.node.data, Path) and event.node.data.is_file():
            self._select_source(event.node.data)

    async def on_exit_app(self, message: messages.ExitApp) -> None:
        """Persist a cleared selection before the app exits."""

        self._is_shutting_down = True
        self._clear_selected_source_state()

    async def on_query_bar_action_triggered(self, message: QueryBar.ActionTriggered) -> None:
        if message.action_id == "add-source":
            self.action_open_add_source_dialog()
        elif message.action_id == "run-query":
            self.action_run_query()
        elif message.action_id == "clear-query":
            self.action_clear_field()
        elif message.action_id == "save-session":
            self.action_save_session()

    async def on_query_bar_time_window_changed(self, message: QueryBar.TimeWindowChanged) -> None:
        if message.value == "range" and message.start and message.end:
            self._update_state(
                time_window="range",
                custom_start=message.start,
                custom_end=message.end,
            )
        elif message.value == "range":
            self._update_state(time_window="range", custom_start="", custom_end="")
        else:
            self._update_state(time_window=message.value, custom_start="", custom_end="")
        self._render_log()

    async def on_query_bar_custom_range_requested(self, message: QueryBar.CustomRangeRequested) -> None:
        """Kick off the Custom Time dialog flow in a worker (don't await here)."""
        self.run_worker(
            self._prompt_custom_time_range(),
            name="custom-time-range-dialog",
            group="dialogs",
            exit_on_error=False,
        )
    
    async def _prompt_custom_time_range(self) -> None:
        """Open the custom time dialog, await result, and apply the range."""
        dialog = CustomTimeRangeDialog(
            initial_start=self.state.custom_start,
            initial_end=self.state.custom_end,
        )
        # Must be awaited from a worker when wait_for_dismiss=True
        result = await self.push_screen(dialog, wait_for_dismiss=True)

        if result is None:
            # User canceled. If a custom range was already active, re-assert it visually.
            if (
                self.state.time_window == "range"
                and self.state.custom_start
                and self.state.custom_end
            ):
                self.query_bar.apply_custom_time_range(
                    self.state.custom_start,
                    self.state.custom_end,
                    emit=False,
                )
            return

        start, end = result
        # Let QueryBar fire TimeWindowChanged so the normal handler runs.
        self.query_bar.apply_custom_time_range(start, end, emit=True)


    async def on_query_bar_severity_changed(self, message: QueryBar.SeverityChanged) -> None:
        self._update_state(severity=message.value)
        self._render_log()

    async def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[override]
        if event.input.id == "query-input":
            self._update_state(query=event.value)
            self._sync_regex_validation()
            self._render_log()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "toggle-advanced":
            if self.advanced_drawer.visible:
                self.advanced_drawer.hide()
            else:
                self.advanced_drawer.show()
        elif event.button.id and event.button.id.startswith("dismiss-"):
            key = event.button.id.removeprefix("dismiss-")
            if key == "regex":
                self._update_state(query="")
                self.query_bar.set_query_value("")
            elif key == "severity":
                self._update_state(severity="all")
                self.query_bar.set_severity("all")
            elif key == "time":
                self._update_state(time_window="all", custom_start="", custom_end="")
                self.query_bar.select_time("all")
            self._render_log()
        elif event.button.id == "close-advanced":
            self.advanced_drawer.hide()

    async def on_switch_changed(self, event: Switch.Changed) -> None:
        # Persist Auto-scroll when the user clicks the toggle
        if event.switch.id == "auto-scroll-toggle":
            self._update_state(auto_scroll=event.value)
            self.log_panel.auto_scroll = event.value
        elif event.switch.id == "pretty-structured-toggle":
            self._update_state(pretty_rendering=event.value)
            self._render_log()
    
    def run(self) -> None:  # pragma: no cover - entry point convenience
        super().run()

def run() -> None:  # pragma: no cover - script entry point
    LogViewerApp().run()
