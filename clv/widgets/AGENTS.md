# AGENTS.md — CLV Widgets Guidelines

## Purpose
This file defines **component-level design rules** for widgets inside `clv/widgets/`.  
Widgets are the building blocks of Centralized Log Viewer (CLV). Each one is **self-contained**, visually consistent, and communicates through messages — never by modifying shared state directly.

---

## Widget Design Principles

1. **Single Responsibility**
   - Each widget performs a distinct UI function (e.g., query input, segmented control, drawer, chip).
   - Avoid combining multiple independent functions into a single widget.

2. **Encapsulation**
   - Visuals (`DEFAULT_CSS`) and behavior (`on_mount`, `watch_*`, etc.) must live within the widget.
   - Widgets should not depend on CSS or logic from other widgets.

3. **Loose Coupling**
   - Communicate using **Textual messages** or simple public methods.
   - Never reach into another widget’s state tree.

4. **Scalability**
   - Widgets must render cleanly at different terminal sizes.
   - Ensure minimum widths and dynamic scaling; never let a control squish invisible.

5. **Keyboard + Mouse Parity**
   - All clickable elements must have keyboard access.
   - Avoid mouse-only interactions.

---

## Current Widgets

| Widget | Purpose | Message Types | Notes |
|--------|----------|----------------|-------|
| **QueryBar** | Main query and filter input row | `ActionTriggered`, `TimeWindowChanged`, `SeverityChanged` | Coordinates query, time, severity, and actions (Run/Clear/Save). |
| **SegmentedButtons** | Multi-button selection group | `ValueChanged` | Used by QueryBar for Severity; reusable elsewhere. |
| **AdvancedFiltersDrawer** | Secondary filter options | `SettingsChanged`, `ViewToggleChanged`, `RescanRequested`, `ScanSSHConfigRequested`, `Closed` | Optional drawer for plugin or advanced UI elements. Capped at `max-height: 16`: a new **row** pushes what follows below the fold, where it lays out and paints nothing — join an existing row, or add to `#drawer-actions`, which is horizontal and costs no rows. |
| **FilterChip** | Displays active filters | `Dismissed` | Allows quick removal of active filters. |
| **HelpOverlay** | Lists every keybinding | *(dismiss only)* | Modal; sections are built by the app from `BINDINGS`, so it cannot go stale. |
| **ExportDialog** | Format + destination for `Ctrl+E` | *(dismisses with `ExportRequest`)* | Modal; states the entry count, offers "marked lines only", and overwriting takes a second press of Export. |
| **LogView** | The log pane, with a line cursor | `CursorMoved`, `EntrySelected` | `ScrollView` + Line API. Rows are entry-indexed, not line-indexed, because one entry can wrap or render as a whole panel. Append is O(new); the row cap trims in batches so the rebuild is amortised. Owns the cursor keys as widget-scoped `BINDINGS`. |
| **DetailPane** | Properties of the selected entry | *(none — driven by `show()`)* | Never renders a blank property list: four formats carry no fields, so each no-field case explains itself. |
| **GotoDialog** | Where in time `g` moves the cursor | *(dismisses with the typed string)* | Modal; does no parsing of its own, so it cannot disagree with `filtering.parse_moment` about what `-15m` means. |
| **AddSourceDialog** | A path to add, or the way to the machine list | *(dismisses with the typed string, or the `REMOTE_HOSTS` sentinel)* | Modal; the template the other dialogs copy. The sentinel carries NUL, which `services/refs` already excludes from a ref string — so no path anyone can type collides with it. The dialog does not know what the app does with it. |
| **RemoteHostsDialog** | Add, edit, test and remove `[ssh:<name>]` hosts | *(dismisses with the host tuple, or `None` for unchanged)* | Modal; holds a working copy so Escape genuinely cancels and one confirm is one write. No password field and no sudo toggle — the schema refuses both. Probing is an injected callable, so the widget imports nothing from `clv.plugins`. |
| **SSHConfigImportDialog** | Pick which `~/.ssh/config` aliases become hosts | *(dismisses with the host tuple, or `None`)* | Modal; records in, records out — reads no file and writes none. Nothing is ticked until somebody ticks it, because a forty-alias config describes a fleet and three boxes that no longer exist. Only `log_dirs` is editable: it is the only field OpenSSH cannot answer. |

---

## CSS Guidelines

- Each widget defines its own `DEFAULT_CSS` block.
- Use **semantic selectors** and **scoped IDs** (`#query-bar`, `.chip`, etc.).
- Avoid repeating selectors across widgets.
- Use Textual’s **layout properties** (`width`, `height`, `margin`, `padding`) instead of runtime style overrides.
- Include comments for non-trivial CSS rules.

### Example Pattern

```python
class MyWidget(Widget):
    DEFAULT_CSS = '''
    MyWidget {
        height: auto;
        padding: 1;
        background: $surface;
    }
    '''
```

---

## Message Design

### Naming Convention
- Use action verbs and describe events (`ActionTriggered`, `ValueChanged`, `Closed`, `Dismissed`).
- Messages should carry only the minimal payload (e.g., selected value, query string).

### Example Pattern

```python
class MyWidget(Widget):
    class ValueChanged(Message):
        def __init__(self, sender: MyWidget, value: str) -> None:
            super().__init__(sender)
            self.value = value
```

---

## Testing Expectations

- **Unit Tests:** Ensure messages fire correctly and state changes are valid.
- **Snapshot Tests:** Validate visual layout boundaries.
- **Manual Tests:** Verify no control is rendered off-screen or clipped at small terminal sizes.

---

## Future-Proofing for Extensibility

- All widgets should be import-safe by external modules or plugins.
- Plugin developers can reuse `SegmentedButtons`, `FilterChip`, or `LabeledField` to maintain a consistent look.
- Widgets must never require app-level imports (`from clv.app import …`).

---

## Quick Reference

| Best Practice | Description |
|----------------|--------------|
| Keep CSS local | No global style definitions in widgets. |
| Use messages, not globals | Communicate cleanly with parent app. |
| Support resize | Layout must remain readable under terminal scaling. |
| Test focus navigation | Ensure keyboard-only operation is complete. |
| Write docstrings | Every class and message must be documented. |

---

> 🧭 **Goal:**  
> Widgets are the **modular UI backbone** of CLV. Each should be reusable, predictable, and cleanly styled — empowering both core and plugin developers to build upon them safely.