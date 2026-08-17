# AGENTS.md — CLV Internal Module Guidelines

## Purpose
This file defines **local development rules** for the `clv/` package — the modular implementation of Centralized Log Viewer (CLV).  
It supplements the **root AGENTS.md** by explaining how each internal component should behave, communicate, and evolve.

---

## Module Ownership & Responsibilities

| Module | Responsibility | Key Notes |
|--------|----------------|-----------|
| **app.py** | Application shell and orchestrator | - Owns main layout and lifecycle. <br> - Handles global keybindings, routing, and message coordination. <br> - Should not define widget visuals or logic directly. |
| **storage.py** | State persistence and config IO | - Reads/writes JSON state files. <br> - Provides safe defaults when config is invalid. <br> - Must remain headless (no UI dependencies). <br> - `from_dict` dispatches on the **text** of a field's annotation; changing one silently drops the field on load. |
| **services/refs.py** | Source identity | - Defines `SourceRef`, the surface CLV requires of a source. <br> - `parse_ref` / `format_ref` are the persistence boundary; `normalize_ref` is the user-input one. See the identity rule below. <br> - Owns `identity` / `ref_key`, the one canonical form. |
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

## Source identity

**A source is a `SourceRef`. `Path` is one implementation and no longer the
assumed one.** The surface is declared in `services/refs.py`, derived by
counting every source-facing call rather than by guessing, and a source may not
be assumed to have a member the protocol does not list.

Two boundaries, and keeping them apart is the whole rule:

| Boundary | Functions | Rule |
|---|---|---|
| **Persistence** — a string CLV itself wrote | `parse_ref` / `format_ref` | Exact inverses. Never expand `~`, never prepend the working directory, never resolve. The stored form is already canonical. |
| **User input** — a string a person typed | `normalize_ref` | Expands and absolutises, exactly as the old `normalize_path` did, and leaves a registered scheme alone. |

Comparison is a third thing again: `identity` (a ref) and `ref_key` (its string
form) are what star and merge membership are decided by. `format_ref` does
**not** resolve — a view saved on a symlink records the symlink.

`str(ref)` is load-bearing in three places at once: it is what lands in
`session.json`, the value of `ORIGIN_FIELD` on every merged entry (so it is
what the operator types after `source:`), and half of a `marks.mark_key`. It
may contain neither `,` nor NUL. Two hosts with the same path must produce two
strings, or one saved query matches both machines.

Two guard tests in `tests/test_refs.py` hold this: one fails if a `Path(...)`
reappears in a function that reads persisted state, the other if a boundary
function stops naming its helper. The seam is one call wide and rots back in
one line, which is why it is tested rather than documented alone.

**What Phase 6 of `SSH_TODO.md` still owes.** Five sites narrow a source with
`isinstance(data, Path)` — `app.py`'s `_sync_merged_tree`, `_star_target`,
`on_tree_node_selected` and `_find_node`, plus `rotation.RotatedSet.__contains__`
— and they are correct today only because `Path` is the sole implementation.
They must change together with the prose claim in `plugins/__init__.py`'s
`ProviderSource` docstring, which cites them by name. Changing them in
isolation makes that docstring false; widening them structurally (an
`isinstance` against the protocol) is what would let a `journal:` source into
someone's `session.json`, which is the thing that docstring exists to prevent.

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

`plugins.load_plugins()` loads them at startup into a `PluginRegistry`.
`FilterStage`s run on every render and `Exporter`s are invoked from the `Ctrl+E`
dialog; `LogSourceProvider` is loaded and reported but not yet consulted by
discovery.

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

- Collection infrastructure: unattended collection, remote agents or daemons,
  store-and-forward pipelines, or privileged operations anywhere. Reading a
  remote root on demand over the operator's own SSH is **in** scope as of
  2026-08-16 — see [SSH_TODO.md](../SSH_TODO.md); becoming the thing that
  gathers logs for you is not, and that is the line.  
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
