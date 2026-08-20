# AGENTS.md — CLV Plugin Development Guidelines

## Purpose
This document defines how to build and integrate **plugins** into the Centralized Log Viewer (CLV).  
Plugins extend CLV’s core functionality without modifying the main codebase.

---

## Philosophy

CLV’s plugin system enables third-party developers to add new log sources, filters, and exporters — safely and predictably.

- **Isolation:** Plugins must never modify core behavior directly.  
- **Failure isolation:** A plugin that raises is reported and skipped, never
  fatal. This is a guarantee about *CLV's* behaviour, not about the plugin's —
  see [Trust model](#trust-model).  
- **Extensibility:** Core should discover and integrate plugins dynamically.  
- **Minimal coupling:** Plugins depend only on public APIs.

---

## Trust model

Read this before you install a plugin, and before you write documentation that
describes what one can do.

### What a plugin can do

**A plugin is trusted code.** It is Python, executed at CLV's privilege, inside
CLV's process. Four consequences, none of which CLV can change:

- It can read anything the operator can read — every log CLV has open, every
  file under `$HOME`, `~/.ssh`, `settings.conf`, the environment.
- It can write, spawn a subprocess, and open a socket. The
  [conventions](#conventions-for-plugin-authors) below ask it not to; nothing
  stops it.
- `import` runs its module-level code **before any interface check runs**. By
  the time CLV can tell whether an object implements `FilterStage`, the module
  has already executed.
- The interfaces bound what CLV *asks* of a plugin. They do not bound what a
  plugin *can do*. `FilterStage.apply` is where CLV calls in; it is not a wall.

Installing a plugin is the same act of trust as installing any other program.
Judge it the same way: by who wrote it and whether you read it.

### What isolation does and does not do

**Today there is none.** Every plugin runs in CLV's process. A plugin that
hangs, leaks memory or spins the CPU cannot be stopped — CLV can catch an
exception, and that is the whole of the containment that exists.

When isolation arrives it will be **failure containment, not safety**: a
subprocess host can be killed on crash, hang or timeout, which is the first
time in CLV's history a plugin can be *stopped*. It will not make an untrusted
plugin safe. The child runs as the operator, with the operator's filesystem and
the operator's network. Everything in *What a plugin can do* stays true of an
isolated plugin except the ability to take the viewer down with it.

Any wording that would let this section be summarised as "plugins are contained,
therefore plugins are safe" is wrong. `tests/test_plugin_docs.py` enforces the
mechanical half of that: the word this paragraph is refusing to use may not
appear anywhere in this file, in any casing. The honest statements never need
it, so the rule costs nothing and closes the door on the hedged version.

### Reviewing a third-party plugin

Before you enable one, read it in this order. It is ordered by how much damage
the thing you are looking at can do before you notice it:

1. **The imports.** `subprocess`, `socket`, `http`, `urllib`, `ctypes`,
   `shutil`, `os.system` — and anything reaching into `clv.services.*` rather
   than the published interfaces. An import is also the file's first chance to
   run code.
2. **Module-level code**, and `discover()`. Both run at startup, before you have
   selected anything. `register()` runs at import too.
3. **Anything touching a write path, a subprocess or a socket**, wherever it
   appears. Check what it writes, where, and whether log content goes into it —
   log content is sensitive, and a plugin that copies it into a cache or a temp
   file has leaked it whether or not that was the intent.

If the plugin ships a `requires_clv` constraint, a name, and tests, that is
evidence of care. It is not evidence of safety.

---

## Plugin Structure

Each plugin is a Python module or package located in one of the following:

1. Local development folder: `clv/plugins/`
2. Installed entry point: via Python package (declared in `pyproject.toml`)

### Example Structure

```
clv/
  plugins/
    sources/
      journald_source.py
    filters/
      redact_filter.py
    exporters/
      json_exporter.py
```

Each plugin must define a class implementing one of the **Abstract Base Classes (ABCs)** below.

---

## Plugin Interfaces

### 1. LogSourceProvider

Provides a new source of logs to tail or read.

```python
from clv.plugins import LogSourceProvider

class MySource(LogSourceProvider):
    name = "My Custom Source"

    def discover(self):
        # Return a list of available sources
        return ["/var/log/custom.log"]

    def open(self, path):
        # Yield lines from the log source
        with open(path, "r") as f:
            for line in f:
                yield line
```

Sources are wired: whatever `discover()` returns appears in a **Providers**
group in the source tree, and selecting one opens it like any other source.
Return `ProviderSource(path, label)` records rather than bare identifiers when
you have a better name than the identifier's last component.

**A provider source is not a file, and CLV does not treat it as one.** Starring,
include/exclude globs and rotated-set grouping all test for a real `Path` and
skip yours. That is deliberate rather than an omission: a provider identifier
persisted into `session.json` would be a path that does not exist.

#### Tailing a live source

`open()` returns an iterator, which is enough for a finite list of lines and
nothing more: it cannot express tailing, cannot be asked to stop, and has
nowhere to put cleanup. For a live stream, implement the optional
`open_reader()` instead:

```python
    def open_reader(self, path, *, max_lines):
        return MyReader(path, max_lines=max_lines)   # or None to use open()
```

The returned object must expose `path`, `prime()`, `poll()` and
`RELOAD_NOTICE`, and should expose `close()` if it holds anything — CLV calls
it on every source switch and again at shutdown. Optionally expose
`set_severity(bucket) -> bool` to filter at the source, returning whether that
required a restart.

Returning `None` (the default) means "use `open()`", so a provider written
before this existed keeps working unchanged.

**Discovery must not ask the filesystem where a package is.** Drop-ins are
found by importing each subpackage and walking its own `__path__`. A frozen
build (PyInstaller) has the modules inside an archive and no
`clv/plugins/sources/` directory on disk, so a `Path.is_dir()` check skips
every drop-in — and reports nothing, because loading no plugins is a valid
state rather than an error. That combination cost the shipped binary its
journal support entirely; see
`test_drop_ins_are_found_without_the_folder_existing_on_disk`.

**A subprocess must not inherit a frozen build's environment.** PyInstaller
puts its own `_internal` directory on `LD_LIBRARY_PATH` so the bundled
interpreter finds the libraries shipped beside it. Children inherit that, so a
*system* binary loads the bundle's libcrypto/libssl instead of the system's and
dies whenever the build machine's distribution differs from the user's — which
for a released binary is the normal case. Run system tools with
`journald.child_environment()`, which restores `*_ORIG` or strips the bundle's
own entry and leaves anything the operator set alone.

**Subprocesses need consent.** A plugin must not run one because it was
installed. The shipped `journald` provider is the pattern: a `settings.conf`
opt-in, read fresh on every `discover()`, returning no sources at all until it
is true — and reporting, never raising, where the tool it needs is absent.

### 2. FilterStage

Transforms or drops entries before they reach the pane.

`apply` receives a `LogEntry` (frozen dataclass: `raw`, `timestamp`, `level`,
`message`, `format_name`, `continuation`, `fields`) and a `FilterContext`
(`spec`, `source`). Return an entry to keep it, or `None` to drop it. Use
`dataclasses.replace` to modify — entries are immutable.

`fields` is the structure the parser recovered from the line: a **read-only
mapping of string to string**, empty for a line no format matched. Key names
are normalised across formats, so `entry.fields.get("host")` means the same
thing whether the line came from syslog or from an access log; the full
vocabulary is documented in the `clv.services.parsing` module docstring. Values
are never coerced — an HTTP status is `"500"`, not `500`.

Three things to know before using it:

- A continuation line (a stack trace frame, say) inherits its parent's
  timestamp and level but **not** its fields, so `fields` is empty there.
- It is a `mappingproxy`. `copy.deepcopy` and therefore `dataclasses.asdict`
  cannot handle one; call `dict(entry.fields)` if you need a plain dict.
- To add fields, pass a new mapping to `replace`. Do not try to mutate the one
  you were given — it is read-only by design.

```python
class TagUnknownHosts(FilterStage):
    name = "TagUnknownHosts"

    def apply(self, entry, context):
        if "host" in entry.fields:
            return entry
        return replace(entry, fields={**entry.fields, "host": "unknown"})
```

```python
from dataclasses import replace
from clv.plugins import FilterStage

class RedactFilter(FilterStage):
    name = "RedactSensitiveData"

    def apply(self, entry, context):
        if "password" not in entry.raw:
            return entry
        return replace(entry, raw=entry.raw.replace("password", "******"))
```

Dropping is the same method:

```python
class DropDebug(FilterStage):
    name = "DropDebug"

    def apply(self, entry, context):
        return None if entry.level == "DEBUG" else entry
```

Stages run *before* the user's query, severity and time filters.

### 3. Exporter

Saves or transmits the entries the filters kept.

**Reachable from the UI:** `Ctrl+E` opens the export dialog, which lists every
loaded `Exporter` below CLV's three built-in formats (JSON Lines, CSV and plain
text — those live in `clv.services.export`, not here, so that a built-in cannot
fail to load).

`export` receives the sequence of `LogEntry` objects that passed the plugin
stages and the user's filters, plus the `FilterContext`, and returns an
`ExportResult`. Three points follow from that:

- The sequence is the **whole filtered set**, not the `_show_lines` window the
  pane happens to be showing. Do not assume it is small.
- There is no destination argument. An exporter picks its own path and reports it
  back as `ExportResult.destination`; the dialog disables its path input for
  plugin exporters and says so. Confine writes to somewhere the operator would
  expect, and never to a temp or cache directory — log content is sensitive.
- Raising is survivable but visible: the exception is recorded in
  `PluginRegistry.errors`, surfaced as a notification and shown in the Advanced
  drawer. Returning `ExportResult(ok=False, detail=...)` is the way to report a
  failure you expected.

```python
import json
from pathlib import Path
from clv.plugins import Exporter, ExportResult

class JsonExporter(Exporter):
    name = "JSON Exporter"

    def export(self, entries, context):
        destination = Path("export.json")
        destination.write_text(
            json.dumps([entry.raw for entry in entries], indent=2),
            encoding="utf-8",
        )
        return ExportResult(ok=True, detail=f"{len(entries)} lines", destination=destination)
```

---

## Plugin Discovery

The app dynamically discovers plugins using:

1. **Local scan** — modules directly under `clv/plugins/` and in the
   `sources/`, `filters/` and `exporters/` subpackages. Modules whose name
   starts with `_` are skipped.
2. **Entry points** — installed distributions advertising the `clv.plugins`
   entry point group.

### How a module says what it exports

Three strategies, tried in order. The first that produces anything wins.

1. **`register()`** — returns one plugin, or any iterable of them (a list, a
   tuple, a set, or a generator).

   ```python
   def register():
       return MySource()
   ```

   Returning `None` or an empty list is a *deliberate* decline — it is how a
   plugin says "not on this machine", the way the shipped `journald` provider
   would if its tool were missing. It is never reported as a problem.

2. **`__all__`** — a list of plugin class or instance names.

   ```python
   __all__ = ["MySource"]
   ```

3. **A namespace scan.** If the module defines neither of the above, CLV looks
   for concrete `Plugin` subclasses **that module itself defined**. A class
   imported from elsewhere is not collected, and neither is a subclass that has
   not implemented its interface method — that one would otherwise be
   instantiated into a confusing `TypeError`.

A module that defines no plugin and says nothing about why is **reported**:
`defines no plugin — add register() or __all__`. Writing the class and
forgetting the boilerplate is the most likely first mistake, and it used to
produce zero plugins, zero errors and no clue.

### What an entry point may point at

Four legal target shapes, all handled:

| Target | Example |
| --- | --- |
| A module | `mypkg.plugin` — its `register()` / `__all__` / namespace is read |
| A plugin class | `mypkg:MyFilter` — CLV instantiates it |
| A zero-argument factory | `mypkg:make_plugin` — called once |
| A plugin instance | `mypkg:INSTANCE` |

A callable that requires arguments is reported as such rather than rejected with
a message about interfaces.

---

## Conventions for plugin authors

These are **conventions, not protections**. Each says who is trusting whom, and
what CLV actually enforces — which in all three cases is nothing. CLV does not
inspect a plugin's imports, intercept its file access, or filter what it logs.
A reviewer enforces these by reading the code; the operator enforces them by
choosing what to install.

- **Never perform a network call or spawn a subprocess without user consent.**
  *Not enforced.* The shipped `journald` provider is the pattern to copy: a
  `settings.conf` opt-in, read fresh on every `discover()`, offering nothing at
  all until it is true.
- **Confine file reads and writes to configured directories.** *Not enforced.*
  A plugin runs with the operator's full filesystem access; see
  [Trust model](#trust-model).
- **Never log or transmit sensitive information** (passwords, tokens, and log
  content itself). *Not enforced.* This is the convention most worth honouring
  and the one CLV has least ability to check.

---

## Plugin Testing

| Type | What to Test | Tools |
|------|---------------|-------|
| **Unit** | Validate `discover()`, `apply()`, `export()` methods | pytest |
| **Integration** | Verify plugin registration and runtime behavior | textual + pytest |
| **Static** | Lint for unsafe imports and access | ruff, mypy |

---

## Versioning & Compatibility

- Follow **semantic versioning** for each plugin.
- Set `requires_clv` on the plugin class to declare compatibility, e.g.
  `requires_clv = ">=2.0,<3.0"`. Omitting it means "any version".
- A plugin failing its constraint is skipped and reported in
  `PluginRegistry.errors`, which the Advanced drawer surfaces.

### The constraint grammar

A PEP 440 subset, hand-rolled — `packaging` is not a dependency and will not
become one. Comma-separated pieces are ANDed and whitespace is ignored.

| Form | Meaning |
| --- | --- |
| `>=` `<=` `>` `<` `==` `!=` | ordered comparison; a bare version means `==` |
| `==2.6.*`, `!=2.6.*` | release-prefix match |
| `~=2.6` | compatible release — `>=2.6, ==2.*` |
| `~=2.6.1` | `>=2.6.1, ==2.6.*` |
| `^2.0.0` | Poetry's caret, a documented alias — `>=2.0.0,<3.0.0` |

Versions may carry a prerelease (`a`/`b`/`rc`, with `alpha`/`beta`/`pre`
normalised), `.postN` and `.devN`. They order as
`1.0.dev1 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 < 1.0.post1` — so `2.6.0rc1` is
correctly **older** than `2.6.0`, which the previous comparator got backwards by
stripping the letters and reading it as `2.6.1`.

**One deliberate divergence from PEP 440: prereleases are always considered.**
Strict PEP 440 excludes a prerelease from a range that does not name one, which
would make `requires_clv = ">=2.6"` unsatisfied on a running `2.7.0rc1` and
silently disable every installed plugin on any release-candidate build.

An **unparseable** constraint — `~~2.6`, `>=abc`, a bare `~=2` — is reported
against your plugin by name. It is never a silent "unsatisfied": a typo and a
genuine incompatibility must not look the same from the outside.

## Failure Handling

Loading never raises. An import error, an unreadable or unsatisfied
`requires_clv`, a class that cannot be instantiated, an object that implements
no interface, a module that exports nothing, or a `FilterStage` that throws
mid-render is recorded in `PluginRegistry.errors` and skipped. A third-party
plugin must never prevent CLV from starting or break a render.

**A plugin disabled at runtime stays disabled for the session.** A `FilterStage`
that raises is taken out of service and its failure recorded **once**; the
remaining stages keep running and the pane keeps rendering. It is not retried on
the next render — doing that cost one identical error per render pass, and a
plugin that raised on one entry will raise on the next.

`PluginRegistry.errors` deduplicates identical `(origin, message)` pairs into a
single entry with a count, and caps the number of distinct problems it stores,
reporting how many it dropped. A broken plugin can no longer bury the discovery
summary in the log panel.

---

## Developer Workflow

1. Create your plugin module in `clv/plugins/` or as a separate package.  
2. Implement one of the ABCs (`LogSourceProvider`, `FilterStage`, or `Exporter`).  
3. Add minimal tests.  
4. Add documentation to this folder’s `README.md` if distributing internally.  
5. Submit PRs with a short demo (e.g., asciinema or screenshot).

---

## Plugin Review Criteria

- ✅ Conforms to ABCs  
- ✅ Does not alter core logic or CSS  
- ✅ Has tests and docstrings  
- ✅ Respects CLV’s minimal dependency policy  
- ✅ Passes linting and security checks

---

## Non-Goals

Still non-goals:

- Collection infrastructure: unattended collection, an agent or daemon on a
  remote host, store-and-forward pipelines, or spooling. A source plugin may
  read a remote log on demand over a connection the operator already has; it may
  not install anything, leave anything running, or cache content to disk.  
- Kernel-level or privileged operations. No `sudo`, `doas` or `pkexec`, at
  either end of a connection, not behind a setting.  
- Credentials of any kind — no password field, no key generation, no agent
  management, and never a disabled host key check.  
- Background daemons or telemetry. A plugin may not run unattended, and CLV
  reports nothing anywhere. The opt-in subprocess host planned in
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phase 13 is not a daemon: it lives and
  dies with the viewer, it is started only for a plugin whose author asked for
  it, and it exists to make a plugin *killable*, not to make it long-lived.

### Reversed

Kept rather than deleted, so the argument is on the record rather than erased —
the rule stated at the head of [TODO.md](../../TODO.md).

- **"Extension is in-process only."** *Reversed 2026-08-14* by
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phase 13. The objection was that a
  process boundary buys less than it appears to, and that objection stands
  unchanged — see [Trust model](#trust-model). What changed is that failure
  containment turned out to be worth having on its own: a plugin that hangs or
  leaks currently cannot be stopped at all. Isolation is opt-in per plugin, is
  refused outright for per-entry kinds, and is described only as what it is —
  see [What isolation does and does not do](#what-isolation-does-and-does-not-do).
- **"CLV has no plugin distribution mechanism."** *Reversed 2026-08-14* by
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phases 3 and 15. The objection was to
  CLV running a hosted index — a server, a namespace and a moderation queue,
  where every listing is a trust signal CLV would be issuing. That objection
  stands and **no index is planned**. What is planned is a user plugin
  directory, an explicit enable-list, and manifests a `clv plugin install` can
  verify from a path, a tarball or a URL that anyone may host.
- **"A provider source cannot be starred or merged."** *Reversed 2026-08-19* by
  [SSH_TODO.md](../../SSH_TODO.md) Phase 9. The objection was that a provider
  source is not a file — nothing on disk answers to `journal:unit/sshd.service`
  — so putting one in `session.json` would record a path that does not exist.
  Two thirds of that stands and is now enforced by name rather than by accident:
  glob filtering and rotated-set grouping both refuse a `JournalRef` explicitly,
  because a journal has no directory and nothing that rotates. The third was
  never an argument, only an implementation: a persisted **identifier** is not a
  persisted path, and a journal unit is exactly the source an operator wants
  starred and compared across a fleet. `ProviderSource` is still not a ref — it
  is the tree node's payload, and its `path` carries the identity.

- **"Network aggregation or remote log collection."** *Reversed 2026-08-16* by
  [SSH_TODO.md](../../SSH_TODO.md). The objection was to CLV becoming collection
  infrastructure, and it stands — it is the first bullet above. What changed is
  that reading a folder on a host the operator can already `ssh` into needs none
  of that. A source plugin may now open a network connection, under the same
  consent rule every subprocess already lives under: a `settings.conf` opt-in,
  read fresh on every `discover()`, offering nothing at all until it is true.
  A *network* subprocess raises that bar rather than lowering it.

---

## The SSH transport is a module here, not a plugin

`sources/ssh.py` lives in this package and its `register()` returns `[]`. That
is not a stub — it is the point.

A `LogSourceProvider` hands back a `ProviderSource`, which is deliberately *not*
a path: glob filtering and rotated-set grouping cannot see one. That is right
for a journal unit, which has no directory to walk and nothing that rotates. It
is exactly wrong for `/var/log/syslog` on `web01`, which has both — and a remote
log that cannot be starred or merged has not met the goal `SSH_TODO.md` sets.

**Starring and merging used to be on that list and no longer are.** The
exclusion was enforced by a provider source not being a `SourceRef` at all,
which was cheap rather than right: a journal unit is the clearest case of a
source someone wants starred, and comparing one unit across a fleet is the
workflow remote sources exist for. `JournalRef` is a ref, so both work. What
still cannot reach the starred set is a provider source whose `path` is not a
concrete ref type — the union in `refs.SOURCE_REF_TYPES` is closed, and it is a
union of types rather than a duck test precisely so that stays true.

So the module supplies a **`SourceBackend`** instead. A remote root reaches
`SourceManager` as an ordinary root, builds the same nested folder tree, and is
read by the same `SourceReader`; `app.build_backends` wires the resolver in.

What keeps it in this package is consent, not layering — the same reason the
journal is here. Reading a remote source spawns `ssh`, a plugin may not spawn a
subprocess without the operator asking, and `enable_ssh` is read fresh on every
call rather than once at import. Nothing connects, and no socket exists, until
it is true.

---

> 🧭 **Goal:**  
> The plugin system empowers developers to extend CLV responsibly — adding sources, filters, or exporters — without sacrificing the project’s speed, security, or simplicity.