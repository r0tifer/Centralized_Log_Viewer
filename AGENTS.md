
# AGENTS.md — Centralized Log Viewer

## Mission & Product North Star
Centralized Log Viewer (CLV) is the Linux counterpart to **Windows Event Viewer**. Our objective is a **fast, lightweight, minimal-dependency** TUI that provides a consistent, discoverable interface **both** in a desktop terminal emulator and on a **headless** server terminal.

**We prioritize:**
- **Speed & Responsiveness** over features that add latency or RAM churn.
- **Zero-surprise UX**: identical layouts and behaviors across environments.
- **Low friction**: minimal dependencies, trivial install, predictable defaults.

---

## Status & Migration Notes
- The original monolithic entry point **`centralized_log_viewer/main.py` and supporting files** are **retired**.
- They are replaced by the **modular CLV app** in `clv/app.py` (with supporting modules under `clv/`).
- All new feature work, bug fixes, and maintenance must target the `clv` package.
- If you touch anything in `centralized_log_viewer/`, it should be **for removal or thin compatibility shims only** (and those should be short‑lived).

### Rationale
- Clear separation of concerns, testability, and composability.
- Modern Textual component model, smaller surface of side effects, easier UX iteration.

### Developer Actions
1. **Stop** adding code to `centralized_log_viewer/`.
2. **Port** any still‑useful logic to discrete, testable modules under `clv/`.
3. **Delete** dead code aggressively; protect users by keeping the CLI entry points stable.

---

## Architecture Overview

### Top‑Level Modules (non‑exhaustive)
- **Application shell**: `clv/app.py`
  - orchestrates layout, routing of user actions, tailing and filtering, persistence, and rendering.
- **State & persistence**: `clv/storage.py`
  - small JSON‑backed session store with strong defaults.
- **UI components**:
  - `clv/widgets/query_bar.py` — query & filter controls (regex, time, severity, auto‑scroll) with validation & actions.
  - `clv/widgets/advanced_drawer.py` — advanced, secondary controls (exclude paths, source filters, symlinks, etc.).
  - `clv/widgets/segmented.py` — segmented button control (e.g., severity selection).
  - `clv/widgets/filter_chip.py` — dismissible chips reflecting active filters.

### Key Data Flows
1. **Source Discovery → Tail → Buffer**
   - Discover `.log` files from configured paths, stream new content, and cap memory via ring buffers.
2. **Filter Application**
   - Regex & severity, plus time windows (`15m`, `1h`, `6h`, `24h`, custom range`).
3. **Render**
   - Colorize by severity, write to the log panel, auto‑scroll as needed.
4. **Persist**
   - Session state (selected source, filters, toggles) saved to JSON on change.

---

## Extensibility & Ownership Model

CLV follows a **modular, multi-component architecture** designed for long-term extensibility. Each component owns its visuals, behavior, and communication interface.

### Ownership by Module

| Responsibility | Module | Description |
|----------------|---------|--------------|
| **App Shell & Layout** | `clv/app.py` | Root grid layout, orchestration, routing, global bindings, persistence hooks. |
| **Persistence / State** | `clv/storage.py` | Manages session storage and configuration IO. |
| **Query & Filters Bar** | `clv/widgets/query_bar.py` | Owns the query, time, severity, auto-scroll, and action controls. |
| **Segmented Buttons** | `clv/widgets/segmented.py` | Reusable segmented control component. |
| **Advanced Drawer** | `clv/widgets/advanced_drawer.py` | Secondary filters and configuration options. |
| **Filter Chip** | `clv/widgets/filter_chip.py` | Dismissible tags representing active filters. |

### Extensibility Goals

- Support plug-in architecture under `clv/plugins/`:
  - **Sources:** Extend discovery and log ingestion (`LogSourceProvider`).
  - **Filters:** Extend filtering behavior (`FilterStage`).
  - **Exporters:** Add export and integration pipelines (`Exporter`).
- Encourage external developers to contribute modules without modifying core files.
- Provide plugin discovery and validation for safe extensibility.

---

## CSS & Styling Guidelines

- Each widget owns its **`DEFAULT_CSS`** block.
- App-level CSS focuses only on **layout, grid, and theming**.
- Avoid cross-module styling collisions; namespace if necessary.
- Remove programmatic sizing once stable CSS is applied.

---

## Plugin Architecture (Planned)

| Plugin Type | Interface | Purpose |
|--------------|------------|----------|
| **LogSourceProvider** | `discover()`, `open(path)` | Adds new log discovery or ingestion backends. |
| **FilterStage** | `apply(line, context)` | Adds new filtering or transformation logic. |
| **Exporter** | `export(session_state, lines)` | Enables export or integration workflows. |

Plugins will be loadable via:
- Local folder drop-ins under `clv/plugins/`
- Python package entry points (`clv.plugins.*`)

---

## Module Separation Principles

| Layer | Owns | Must Not Do |
|--------|------|--------------|
| **App (`clv/app.py`)** | Layout, coordination, persistence hooks | Define widget visuals or logic directly |
| **Widgets (`clv/widgets/…`)** | Self-contained UI behavior + CSS | Depend on other widgets’ internals |
| **Storage/Services (`clv/storage.py`, `clv/services/…`)** | Data IO, config, and session management | Directly manipulate UI |
| **Plugins (`clv/plugins/…`)** | External extensions | Break interface contracts |

---

## North Star

> CLV must remain **fast**, **clear**, and **modular** — a foundation that users can extend without breaking its simplicity or speed.

---

## Original Guidance (Preserved for Reference)

# AGENTS.md — Centralized Log Viewer

## Mission & Product North Star
Centralized Log Viewer (CLV) is the Linux counterpart to **Windows Event Viewer**. Our objective is a **fast, lightweight, minimal-dependency** TUI that provides a consistent, discoverable interface **both** in a desktop terminal emulator and on a **headless** server terminal.

**We prioritize:**
- **Speed & Responsiveness** over features that add latency or RAM churn.
- **Zero-surprise UX**: identical layouts and behaviors across environments.
- **Low friction**: minimal dependencies, trivial install, predictable defaults.

---

## Status & Migration Notes
- The original monolithic entry point **`centralized_log_viewer/main.py` and supporting files** are **retired**.
- They are replaced by the **modular CLV app** in `clv/app.py` (with supporting modules under `clv/`).
- All new feature work, bug fixes, and maintenance must target the `clv` package.
- If you touch anything in `centralized_log_viewer/`, it should be **for removal or thin compatibility shims only** (and those should be short‑lived).

### Rationale
- Clear separation of concerns, testability, and composability.
- Modern Textual component model, smaller surface of side effects, easier UX iteration.

### Developer Actions
1. **Stop** adding code to `centralized_log_viewer/`.
2. **Port** any still‑useful logic to discrete, testable modules under `clv/`.
3. **Delete** dead code aggressively; protect users by keeping the CLI entry points stable.

---

## Architecture Overview

### Top‑Level Modules (non‑exhaustive)
- **Application shell**: `clv/app.py`
  - orchestrates layout, routing of user actions, tailing and filtering, persistence, and rendering.
- **State & persistence**: `clv/storage.py`
  - small JSON‑backed session store with strong defaults.
- **UI components**:
  - `clv/widgets/query_bar.py` — query & filter controls (regex, time, severity, auto‑scroll) with validation & actions.
  - `clv/widgets/advanced_drawer.py` — advanced, secondary controls (exclude paths, source filters, symlinks, etc.).
  - `clv/widgets/segmented.py` — segmented button control (e.g., severity selection).
  - `clv/widgets/filter_chip.py` — dismissible chips reflecting active filters.

### Key Data Flows
1. **Source Discovery → Tail → Buffer**
   - Discover `.log` files from configured paths, stream new content, and cap memory via ring buffers.
2. **Filter Application**
   - Regex & severity, plus time windows (`15m`, `1h`, `6h`, `24h`, custom range`).
3. **Render**
   - Colorize by severity, write to the log panel, auto‑scroll as needed.
4. **Persist**
   - Session state (selected source, filters, toggles) saved to JSON on change.

---

## Product Requirements (non‑negotiable)

### 1) Identity: “Linux Windows‑Event‑Viewer”
- CLV **is** the Linux version of Windows Event Viewer: a focused, operator‑friendly viewer with live tailing, filterable panes, and ergonomic navigation.

### 2) Lightweight & Fast
- Target **instant launch** and **low CPU** when idle.
- Avoid heavy dependency chains; prefer the standard library and Textual‑native approaches.
- Keep the rendering loop lean; batch updates and avoid unnecessary reflows.

### 3) Minimal Dependencies
- Rely on Python 3.11+ and Textual only (plus Rich, which ships with Textual).
- Any new dependency must justify:
  - **Size/complexity cost vs. value**
  - **Long‑term maintenance & security posture**
  - **Availability on common enterprise distros**

### 4) Single UX for Desktop & Headless
- The UI must function the same on a **desktop terminal** and a **headless SSH** session.
- No mouse‑only affordances; **every action must have a keyboard path**.

### 5) Primary vs. Secondary Views
- **Primary**: **Log Source** list + **Log Display** pane.
- **Secondary**: filters, input boxes, drawers, selector buttons.
- Secondary UI **must never** occlude or squeeze primary content off‑screen.

### 6) Responsive Layout (No Squish, No Off‑Screen)
- Layout must **scale cleanly** with terminal size:
  - Minimums on critical panes; never render controls off‑screen.
  - Use scroll, not overflow‐cut, for long lists.
  - Guard rails on grid/flex sizing to prevent squish.

---

## Interaction Model

### Keyboard
- Provide bindings for: **focus query**, **cycle time**, **cycle severity**, **toggle autoscroll**, **run**, **clear**, **save**.
- Enter applies filters; Escape clears query.

### Mouse
- All interactive elements (buttons, chips, radio/segments, toggles, list selections) must respond to clicks.
- No mouse‑only features. Keyboard always has parity.

### Filter UX
- Query is **regex**; validate continuously against a sample of recent lines and surface match counts.
- Time presets cycling: `15m` → `1h` → `6h` → `24h` → `Range…` (with popover for ISO start/end inputs).
- Severity segmented control: **All, Info, Warn, Error, Debug**.
- **Chips** reflect active filters and can be dismissed to revert state.

### Color & Readability
- Severity‑based colorization; never rely solely on color for meaning. Include level text in each line.

---

## Performance & Resource Budgets
- **Tail poll interval** derived from a user‑configurable Hz (clamped for sanity).
- **Ring buffer** caps in‑memory lines per source to prevent RAM bloat.
- Regex validation runs on a **bounded recent sample** to keep the UI responsive.

---

## Configuration & Defaults
- Config lives at `~/.config/clv/settings.conf` (XDG) with a development fallback.
- Important keys:
  - `log_dirs`: comma‑separated absolute paths scanned recursively for `*.log`.
  - `max_buffer_lines`: upper bound per file buffer.
  - `default_show_lines`: initial visible lines in the log view.
  - `refresh_hz`: polling frequency.
  - `min_show_lines`, `show_step`: view sizing controls.

**Principles**
- Defaults must be safe (low CPU/RAM) and helpful out‑of‑the‑box.
- Validate and sanitize config; on errors, fall back to sane defaults without crashing.

---

## CLI & Packaging
- Provide `clv` and `CentralizedLogViewer` console scripts that launch the same app.
- Keep packaging simple; target Poetry build with minimal metadata.

---

## Testing & Quality Bar
- Unit tests for parsing, filtering, and state persistence.
- Golden‑path UI tests for: query validation chip, time preset cycling, severity segmented control, and chip dismiss behaviors.
- Manual smoke on small and **very large** logs; verify no UI squish at small terminal sizes.

---

## Accessibility & Operability
- Every interactive element must have a keyboard binding or be reachable via focus.
- Tooltips and inline status (e.g., regex validity and approximate match counts) for quick feedback.
- Avoid ASCII art that reduces usable width; prefer borders that collapse gracefully.

---

## Contributing
1. Open an issue that maps to one of the product pillars above.
2. Propose changes as small, composable PRs.
3. Include before/after terminal screenshots or a short asciinema when changing layout.
4. Update this AGENTS.md if the change impacts architecture or principles.

---

## Security & Privacy
- Never read outside of configured `log_dirs` unless explicitly selected by the user.
- Reads are **local only**; no exfiltration or telemetry.
- Treat logs as sensitive; avoid writing them to caches or temp files.

---

## Deletion & Sunsetting
- Remove `centralized_log_viewer/` code once all usages are migrated.
- Keep a single compatibility stub (if needed) that forwards to `clv.app:run`.

---

## Appendix — Non‑Goals (for now)
- Network collection, multi‑node aggregation, or remote tailing.
- Heavy parsing DSLs or schema‑aware pipelines.
- Plugin systems that introduce complex lifecycle/ABI concerns.

---

## Quick Reference (for new agents)
- **Start here**: `clv/app.py` and `clv/widgets/`.
- **Primary UI anchors**: Log Sources (left), Log Display (right).
- **Keep it fast**: bounded buffers, cheap redraws, sample‑only regex validation.
- **Keep it clear**: primary views always visible; secondary UI never occludes.
- **Keep it consistent**: identical keyboard/mouse affordances; identical desktop/headless UX.