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
| **AdvancedFiltersDrawer** | Secondary filter options | `Closed`, (future) `Changed` | Optional drawer for plugin or advanced UI elements. |
| **FilterChip** | Displays active filters | `Dismissed` | Allows quick removal of active filters. |

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