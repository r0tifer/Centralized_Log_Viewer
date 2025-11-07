# AGENTS.md — CLV Internal Module Guidelines

## Purpose
This file defines **local development rules** for the `clv/` package — the modular implementation of Centralized Log Viewer (CLV).  
It supplements the **root AGENTS.md** by explaining how each internal component should behave, communicate, and evolve.

---

## Module Ownership & Responsibilities

| Module | Responsibility | Key Notes |
|--------|----------------|-----------|
| **app.py** | Application shell and orchestrator | - Owns main layout and lifecycle. <br> - Handles global keybindings, routing, and message coordination. <br> - Should not define widget visuals or logic directly. |
| **storage.py** | State persistence and config IO | - Reads/writes JSON state files. <br> - Provides safe defaults when config is invalid. <br> - Must remain headless (no UI dependencies). |
| **widgets/query_bar.py** | Query, time, severity, and action controls | - Emits `ActionTriggered`, `TimeWindowChanged`, and `SeverityChanged` messages. <br> - No logic beyond UI validation. |
| **widgets/segmented.py** | Generic segmented control | - Self-contained visual component. <br> - Should be reusable across other widgets. |
| **widgets/advanced_drawer.py** | Advanced filters and secondary options | - Optional drawer for extended filtering and plugin-provided UI. <br> - Should expose show/hide events to `app.py`. |
| **widgets/filter_chip.py** | Active filter chips | - Renders filter tags. <br> - Emits dismissal events handled by the app. |

---

## CSS & Visual Design

- Each widget owns its own **`DEFAULT_CSS`** block.
- The app-level stylesheet should **only define layout**, not widget visuals.
- Avoid defining the same CSS selector in more than one module.
- Prefer **semantic class names** and **ID scoping** for maintainability.
- When removing programmatic style overrides, ensure CSS reproduces the desired layout before deletion.

---

## Message Contracts

All cross-module communication must occur through **Textual messages** or **public methods** — never through shared globals or direct widget state mutation.

### Core Message Types
| Origin | Message | Purpose |
|--------|----------|----------|
| QueryBar | `ActionTriggered` | Run, Clear, or Save was pressed. |
| QueryBar | `TimeWindowChanged` | Time preset changed. |
| QueryBar | `SeverityChanged` | Severity filter changed. |
| FilterChip | `Dismissed` | Filter chip was removed. |
| AdvancedDrawer | `Closed` | Drawer hidden by user action. |

When adding new message types:
1. Define them inside the emitting widget.
2. Document their purpose and payload.
3. Handle them in `app.py` using `on_message(...)`.

---

## Plugin Integration Points

`clv/` is designed for future extensibility through plugins.  
Do **not** hardcode external integrations; use hooks instead.

### Reserved namespaces
- `clv/plugins/sources/` — new log source providers.
- `clv/plugins/filters/` — new filter stages.
- `clv/plugins/exporters/` — output/export pipelines.

Each plugin should subclass an abstract interface defined in `clv/plugins/__init__.py`:
- `LogSourceProvider`
- `FilterStage`
- `Exporter`

The app will later load them dynamically via a registry.

---

## Coding Standards

- Follow **single-responsibility design**: one purpose per module.
- Keep imports **acyclic** (no circular dependencies).
- Maintain **headless safety**: widgets can load without needing a terminal UI active.
- When adding dependencies, justify them with performance or UX value.
- Ensure **unit tests** cover message emission and event behavior.

---

## Extensibility Checklist (for Contributors)

Before adding or changing a module:
1. Confirm that your feature belongs in this layer (`widgets`, `services`, `storage`, `plugins`).
2. Avoid tight coupling to other widgets — use messages.
3. Keep CSS local to your module.
4. If new data flows are introduced, document them here.
5. Update both this file and the root `AGENTS.md` if you change architectural boundaries.

---

## Non-Goals (for `clv/` package)

- Network log aggregation or remote tailing.  
- Background daemons or system services.  
- Heavy GUI frameworks or external windowing systems.  
- Any code that violates the “fast, lightweight, terminal-native” ethos.

---

## Quick Reference

- **Main entry point:** `clv/app.py`
- **Reusable widgets:** `clv/widgets/`
- **Persistent state:** `clv/storage.py`
- **Future extensions:** `clv/plugins/`
- **Testing priority:** interactions between QueryBar, FilterChip, and AdvancedDrawer.

---

> 🧭 **North Star:**  
> Each `clv/` module should stand alone — clean boundaries, minimal imports, and predictable communication.  
> Together they form a responsive, extensible TUI that anyone can extend without forking the core.
