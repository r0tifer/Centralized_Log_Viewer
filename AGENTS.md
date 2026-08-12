# AGENTS.md — Centralized Log Viewer

## Mission & Product North Star

Centralized Log Viewer (CLV) is the Linux counterpart to **Windows Event
Viewer**: a **fast, lightweight, minimal-dependency** TUI that behaves
identically in a desktop terminal emulator and on a **headless** server
terminal.

**We prioritize:**
- **Speed & responsiveness** over features that add latency or RAM churn.
- **Zero-surprise UX**: identical layout and behavior across environments.
- **Low friction**: minimal dependencies, trivial install, predictable defaults.

---

## Product Requirements (non-negotiable)

### 1) Point it anywhere
An operator names **folders and/or individual files**. Folders are searched
recursively. CLV is **not** restricted to `*.log` — any readable text file is a
valid source. Filtering by name is the user's decision (`include_globs` /
`exclude_globs`), not a hard-coded rule. Files are excluded for
**readability** reasons (binary content, compressed archives), never for
naming reasons.

"Readable text" is judged on **characters, not bytes**. UTF-16 encodes ASCII
with a NUL beside every character, so a byte-level NUL test rejects the
Windows and PowerShell exports operators most often point CLV at. Encoding is
sniffed from the byte-order mark (`reader.detect_encoding`) and BOM-less files
are read as UTF-8, as before — statistical charset guessing is **not** done,
because guessing wrong puts a core dump on screen.

A small number of **container documents** are readable text that no byte test
can recognise, because the text is inside an archive. These are declared by
suffix in `documents.FORMATS` (currently `.ods`) and bypass the binary check.
Anything needing a third-party parser does not qualify — that is why PDFs are
in `DEFAULT_EXCLUDE_GLOBS` rather than supported.

### 2) Never silently lose a line
Every physical line becomes a `LogEntry` with its raw text preserved. A line no
format recognises is still **searchable**. Severity and time filters may hide a
line that demonstrably lacks a level or timestamp — and when they do, the UI
must **say so and count it**. An empty pane always explains itself.

### 3) Bounded work, whatever the file size
Opening a source seeks **backwards from the end**; nothing ever reads a whole
file. Tailing reads only appended bytes and renders only new lines. Memory is
capped by `max_buffer_lines` regardless of on-disk size. Discovery runs off the
UI thread and is capped by `max_files`.

**Container documents are the one exception**, and they invert two of these
rules on purpose. A deflated archive has no cheap tail, so `DocumentReader`
extracts the document whole, bounded by a **line** budget rather than a byte
one, and re-extracts on change instead of tailing. It also keeps the **first**
lines rather than the last: a spreadsheet's header row names its columns, and
a document has no "newest" end the way a log does.

### 4) Nothing off-screen, ever
Layout must scale cleanly from 80 columns up. Every control stays on screen and
keyboard-reachable at every supported width. Horizontal groups are built from
`1fr` children so they **divide** available width rather than demanding a fixed
amount. Fixed-width control clusters in a shared row are how the previous build
pushed its action buttons off the edge — do not reintroduce them.

### 5) Single UX for desktop & headless
No mouse-only affordances; every action has a keyboard path.

### 6) Minimal dependencies
Python 3.11+, Textual, and Rich (which ships with Textual). A new dependency
must justify its size, maintenance burden, and availability on enterprise
distros.

---

## Architecture

| Layer | Location | Owns | Must not |
| --- | --- | --- | --- |
| **App shell** | `clv/app.py` | Layout, routing, lifecycle, breakpoints | Parse, filter, read files, or define widget visuals |
| **Services** | `clv/services/` | Parsing, filtering, discovery, reading, config, source management | Touch the UI or import Textual |
| **Widgets** | `clv/widgets/` | Self-contained UI + own `DEFAULT_CSS` | Depend on other widgets' internals or import `clv.app` |
| **Plugins** | `clv/plugins/` | Extension interfaces + loader | Break interface contracts |
| **State** | `clv/storage.py` | JSON session persistence (atomic) | Depend on the UI |

### Services
- `parsing.py` — multi-format line parsing, canonical severity vocabulary,
  continuation carry-forward. Whatever structure a matcher captured beyond the
  timestamp and level is carried on `LogEntry.fields`, under key names
  normalised across formats — `host` means the same thing whether it came from
  syslog or from an access log. Values are strings and are never coerced; a
  continuation inherits timestamp and level but never fields.
- `filtering.py` — `FilterSpec` → `FilterResult` with per-reason hidden counts.
  Also owns time parsing: `parse_relative_window` / `parse_absolute_window` for
  the presets, `parse_moment` for the single point `g` jumps to, and
  `align_moments` so an aware JSON stamp and a naive syslog one can be ordered
  rather than refused.
- `discovery.py` — walks roots into a `DiscoveryReport`; pure and synchronous
  so callers can thread it. Every skip is attributed to exactly one of
  **`unsupported file type`** (CLV cannot display it), **`filtered out`** (the
  operator's own globs hid it), or **`unreadable`** (the read failed). Keep
  these apart: only the first is CLV's verdict on the file, and merging them
  back into one "excluded" count is what made the number unactionable. A
  *named* source that is skipped is also listed by path — a file the operator
  typed out must never disappear into a tally.
- `reader.py` — BOM-based encoding detection, bounded backwards reads,
  incremental tailing, rotation and truncation recovery. `open_reader()` picks
  between `SourceReader` (streams) and `DocumentReader` (container documents);
  both expose `path` / `prime()` / `poll()` and a `RELOAD_NOTICE` template.
- `documents.py` — stdlib-only text extraction for container formats.
- `config.py` — settings resolution, validation, clamping.
- `sources.py` — session source management and settings persistence.
- `export.py` — the three built-in output formats (JSON Lines, CSV, plain text)
  and the atomic write behind `Ctrl+E`. Core rather than drop-in plugins so a
  built-in cannot fail to load and the drawer's plugin count keeps meaning
  "installed plugins"; `clv/plugins/exporters/` is still a live extension point.
  Does **not** import `clv.plugins` — that dependency already runs the other way.
- `clipboard.py` — assembles and size-caps the payload `y` hands to
  `App.copy_to_clipboard`. OSC 52 has no continuation form, so an oversized
  payload is truncated at a line boundary and reported, never chunked and never
  silently dropped.

### Data flow
```
config.load_config ─→ SourceManager ─→ discovery.discover (thread)
                                              ↓
                                        DiscoveryReport ─→ tree
                                              ↓
   reader.SourceReader.prime/poll ─→ parsing.LogParser.feed ─→ deque[LogEntry]
                                              ↓
        plugins.apply_filters ─→ filtering.filter_entries ─→ LogView
                                                               ↓
                                                cursor ─→ DetailPane
```

---

## Styling rules

- **CSS is the only place layout is decided.** No module assigns `.styles.*` at
  runtime. The single exception is the user-adjustable source-tree width, which
  is user state rather than a layout decision.
- Each widget owns its `DEFAULT_CSS`. App CSS covers the shell only and must
  not restate widget-internal rules.
- **`DEFAULT_CSS` is scoped to its widget.** A selector rooted at
  `LogViewerApp` will *not* match from inside a widget's own CSS. Responsive
  rules therefore key off a breakpoint class the app **mirrors onto the
  widget** (`QueryBar.-compact`, not `LogViewerApp.-compact QueryBar`).
- Breakpoints: `-compact` (<90 cols), `-narrow` (<130), `-wide` (≥130).

---

## Message contracts

| Origin | Message | Purpose |
| --- | --- | --- |
| `QueryBar` | `ActionTriggered` | Run / Clear / Save / Add Source fired |
| `QueryBar` | `TimeWindowChanged` | Time window changed (carries bounds for a custom range) |
| `QueryBar` | `SeverityChanged` | Severity bucket changed |
| `QueryBar` | `CustomRangeRequested` | Open the custom range dialog |
| `LogView` | `CursorMoved` | The line cursor moved; carries whether it is on the last entry |
| `LogView` | `EntrySelected` | `Enter` on a line — open the detail pane on it |
| `SegmentedButtons` | `ValueChanged` / `Reselected` | Segment activated / re-activated |
| `FilterChip` | `Dismissed` | Revert the named filter |
| `AdvancedFiltersDrawer` | `SettingsChanged` | Full before/after snapshot; `needs_rescan` says whether discovery must re-run |
| `AdvancedFiltersDrawer` | `ViewToggleChanged` | Auto-scroll / structured / clipboard flipped from a drawer switch |
| `ExportDialog` | dismiss value | `ExportRequest(key, path)`, or `None` when canceled |
| `AdvancedFiltersDrawer` | `RescanRequested` / `Closed` | Explicit rescan / dismissal |

### Controls with two homes

Auto-scroll and structured output appear in the query bar when it is wide
enough to merge its rows, and in the Advanced drawer's **View** section when it
is not — exactly one copy is ever visible. The app owns the state; both
controls funnel through `_set_auto_scroll` / `_set_structured`, and
`_sync_view_toggles` pushes the result back to both.

Mirroring uses `prevent(Switch.Changed)`, **not** a boolean guard. Textual posts
`Switch.Changed` asynchronously, so a flag cleared at the end of the sync method
is already back to `False` by the time the handler runs, and the echo arrives
looking like a fresh user action.

The **Clipboard (OSC 52)** switch is the counter-example: it has one home, so it
lives in the drawer's own `#output-options` container rather than in
`#view-toggles`, which is hidden once the query bar is wide enough to show its
copies. A single-home control placed in that container disappears above 148
columns.

Cross-module communication goes through messages or public methods — never
shared globals or reaching into another widget's tree.

---

## Plugins

Three interfaces in `clv/plugins/__init__.py`:

| Interface | Method | Purpose |
| --- | --- | --- |
| `LogSourceProvider` | `discover()`, `open(path)` | New ingestion backends |
| `FilterStage` | `apply(entry, context) -> LogEntry \| None` | Transform or drop entries |
| `Exporter` | `export(entries, context) -> ExportResult` | Send the current view somewhere |

Loaded from `clv/plugins/{sources,filters,exporters}/` drop-ins (via a
`register()` function or `__all__`) and from the `clv.plugins` entry point
group. Optional `requires_clv` constraints are enforced.

**Loading never raises.** Import failures, bad version constraints, and stages
that throw at runtime are recorded in `PluginRegistry.errors`, surfaced in the
Advanced drawer, and skipped. A third-party plugin must never stop CLV starting
or break a render.

---

## Testing

- Services are UI-free and unit-tested directly.
- Widget and app behavior is tested headlessly via `App.run_test()`.
- `tests/conftest.py` redirects `HOME` and `XDG_CONFIG_HOME` per test. **Never
  remove this** — without it the suite reads and writes the developer's real
  session and settings files.
- Layout regressions are caught by asserting widget `region` bounds at a given
  terminal size rather than by eyeballing screenshots.

Run: `python -m pytest` (343 tests).

---

## Security & Privacy

- Read only what the operator configured or explicitly selected.
- Local only: no network, no telemetry, no exfiltration.
- Treat log contents as sensitive; never copy them into caches or temp files.
  Session state stores paths and filter settings, never log content.

---

## Non-Goals (for now)

- Network collection, multi-node aggregation, remote tailing.
- Heavy parsing DSLs or schema-aware pipelines.
- Background daemons or privileged operations.

---

## Quick Reference

- **Start here**: `clv/app.py`, then `clv/services/`.
- **Primary UI**: source tree (left), log output (right); at `-compact` they
  swap via `Ctrl+B`. The log pane (`LogView`) carries a line cursor, and the
  `DetailPane` beside/below it renders whatever that cursor is on.
- **Keep it fast**: bounded reads, incremental renders, threaded discovery.
- **Keep it honest**: never drop a line silently; explain every empty pane.
- **Keep it consistent**: identical keyboard and mouse paths everywhere.
