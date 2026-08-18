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
| **services/refs.py** | Source identity | - Defines `SourceRef`, the surface CLV requires of a source. <br> - `parse_ref` / `format_ref` are the persistence boundary; `normalize_ref` is the user-input one. See the identity rule below. <br> - Owns `identity` / `ref_key`, the one canonical form. <br> - Two implementations: `Path`, and `RemoteRef` (`ssh:<node>/<path>`). The remote type lives **here, not in the SSH plugin** — `parse_ref` decodes `session.json` before any plugin is imported. It is pure identity and raises rather than performing IO. |
| **services/config.py** | Settings, and what CLV could not honour | - `[log_viewer]` plus one `[ssh:<name>]` per remote host; per-host settings fall back to the global ones. <br> - Never raises and never goes quiet: a bad section is skipped and recorded as a `ConfigIssue`, which `app.py` prints beside the plugin errors. Own type, not `PluginError` — services may not import plugins. <br> - `enable_ssh` defaults false and gates connecting, not parsing. <br> - **No password option, no sudo option.** Both are refused by name; that absence is the enforcement point for those requirements. |
| **services/backend.py** | Source IO, and what it costs | - Defines `SourceBackend`; `LocalBackend`'s behaviour is what `os` did before, and `RemoteBackend` is the second implementation. <br> - Every method is marked `@cheap` or `@blocking`; `cheap_only()` makes a blocking call from `poll()` raise. See the seam rule below. <br> - `identity` is opaque and may be `None`; `walk` is lazy and yields files only. <br> - `classify` takes a **batch** and returns bytes, never a verdict: it is what stops the discovery sniff being a round trip per file, and the rule stays in `reader` / `compressed`. |
| **plugins/sources/ssh.py** | The SSH transport | - Owns the connection, the capability probe and `RemoteBackend`. Lives under `plugins/` because a plugin may not spawn a subprocess without consent and a *network* subprocess raises that bar. <br> - `register()` returns `[]` on purpose: this is a backend, not a `LogSourceProvider`, and a remote log must be an ordinary source rather than a `ProviderSource`. <br> - Every argv carries `BatchMode=yes`; `StrictHostKeyChecking` and `UserKnownHostsFile` appear nowhere. <br> - Every operator-supplied byte goes through `quote_all` before it enters a script. <br> - Two hint tables, not one: `_FAILURE_HINTS` is the *transport* failing (host key, credentials, DNS) and `_REMOTE_HINTS` is the *command* failing (a missing utility, a path the SSH user cannot read). Merging them would report a file mode as an authentication problem. <br> - Reconnection is bounded — `RECONNECT_BACKOFF`, then it stops and names `Ctrl+R`. Never a reconnect per poll tick. |
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

**Narrowing a source is `refs.is_source_ref`, never `isinstance(data, Path)`.**
The tree is typed on `object` and carries four different things — refs, saved
views, provider sources, and the merged-set sentinel — and everything that walks
it looking for a *source* goes through that one predicate. It is a **closed
union of ref types**, not a duck test, and that distinction is load-bearing: a
provider source is deliberately not a ref, which is what keeps
`journal:unit/sshd.service` out of the starred set and out of anyone's
`session.json`. `plugins/__init__.py`'s `ProviderSource` docstring explains the
same rule from the other side; the two must move together.

Two related answers live in `app.py` rather than here, because they are about
the *tree* rather than about identity: `_is_file_node` tells a file node from a
folder node without asking a remote host on the event loop (it reads the
discovery report, which already knew), and `_source_facts` says which machine a
source is on.
They must change together with the prose claim in `plugins/__init__.py`'s
`ProviderSource` docstring, which cites them by name. Changing them in
isolation makes that docstring false; widening them structurally (an
`isinstance` against the protocol) is what would let a `journal:` source into
someone's `session.json`, which is the thing that docstring exists to prevent.

---

## The filesystem seam

**IO against a source goes through a `SourceBackend`. `os` and `pathlib` are
`LocalBackend`'s business and nobody else's.** `refs.py` says what a source is;
`services/backend.py` says who reads it. Nothing else in `clv/services/` may
call `os.walk`, `os.access`, `os.scandir` or `path.open("rb")` — a guard test in
`tests/test_backend.py` fails if one comes back, because such a call works
perfectly on local files and silently reads *this* machine when handed a remote
ref.

**Costs are declared, not assumed.** This is the part that is a type rather than
a convention:

| Mark | Means | Where it may be called |
|---|---|---|
| `@cheap` | A `stat`-sized operation | Anywhere, including `poll()` |
| `@blocking` | May be a network round trip | A worker thread only |

`stat`, `identity` and `reachability` are **guaranteed cheap on every backend** — a backend that
cannot honour that is a reason to change the reader, and `blocking_methods`
refuses to build capabilities for one that tries. A backend whose honest `stat`
*is* a round trip satisfies this by asking **who wants to know**: `in_cheap_only()`
reports whether the caller is inside the guard, so `RemoteBackend.stat` serves a
cache there and goes to the wire outside it, where a worker and the contract
suite both live. Reading that flag is not a licence to block under it. `BackendCapabilities.blocking`
is derived from the marks, so it cannot drift from the code, and an unmarked
method is refused outright. `SourceBuffer.poll` runs inside `cheap_only()`, so a
blocking call there raises `BlockingCallError` rather than freezing the UI at
`refresh_hz`. That exception is deliberately **not** caught: it is a design
error, not a source that went away.

`reachability` is the third, and it is cheap by *nature* rather than by
concession: it reports state the backend already holds and is forbidden to
probe. A source that cannot be reached says so through it, because
`SourceBuffer.poll` deliberately swallows `OSError` — a source that vanished
mid-session is not worth taking the pane down for — and that is exactly right
for a rotated local file and exactly wrong for a dropped link, which would
otherwise render as a log that had simply gone quiet. A reader that knows *why*
it stopped reports it in band on `TailRead.problem` instead, once.

When adding a method to the protocol: mark it, add it to `PROTOCOL_METHODS`, and
put it in `GUARANTEED_CHEAP` only if every conceivable backend can answer it
without a round trip.

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
