# AGENTS.md — CLV Plugin Development Guidelines

## Purpose
This document defines how to build and integrate **plugins** into the Centralized Log Viewer (CLV).  
Plugins extend CLV’s core functionality without modifying the main codebase.

---

## Philosophy

CLV’s plugin system enables third-party developers to add new log sources, filters, and exporters — safely and predictably.

- **Isolation:** Plugins must never modify core behavior directly.  
- **Safety:** Plugins are sandboxed through defined interfaces.  
- **Extensibility:** Core should discover and integrate plugins dynamically.  
- **Minimal coupling:** Plugins depend only on public APIs.

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

Each plugin should register itself by defining an `__all__` list or `register()` function.

Example:

```python
__all__ = ["MySource"]
```

or

```python
def register():
    return MySource()
```

---

## Security and Safety

- Plugins must never perform network calls or subprocess execution without user consent.
- All file reads and writes must be **confined to configured directories**.
- Sensitive information (e.g., passwords, tokens) must not be logged or transmitted.

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
  `requires_clv = ">=2.0,<3.0"`. Comma-separated comparators (`>=`, `<=`, `>`,
  `<`, `==`, `!=`) are supported and checked against `clv.__version__`.
  Omitting it means "any version".
- A plugin failing its constraint is skipped and reported in
  `PluginRegistry.errors`, which the Advanced drawer surfaces.

## Failure Handling

Loading never raises. An import error, an unsatisfied `requires_clv`, a class
that cannot be instantiated, an object that implements no interface, or a
`FilterStage` that throws mid-render is recorded in `PluginRegistry.errors` and
skipped — a stage that raises is disabled for that pass while the remaining
stages keep running. A third-party plugin must never prevent CLV from starting
or break a render.

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

- Network aggregation or remote log collection.  
- Kernel-level or privileged operations.  
- Background daemons or telemetry.

---

> 🧭 **Goal:**  
> The plugin system empowers developers to extend CLV responsibly — adding sources, filters, or exporters — without sacrificing the project’s speed, security, or simplicity.