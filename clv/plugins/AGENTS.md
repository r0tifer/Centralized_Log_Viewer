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

### 2. FilterStage

Processes or filters log lines before display.

```python
from clv.plugins import FilterStage

class RedactFilter(FilterStage):
    name = "RedactSensitiveData"

    def apply(self, line, context):
        return line.replace("password", "******")
```

### 3. Exporter

Saves or transmits log data to an external destination.

```python
from clv.plugins import Exporter

class JsonExporter(Exporter):
    name = "JSON Exporter"

    def export(self, session_state, lines):
        with open("export.json", "w") as f:
            f.write("\n".join(lines))
```

---

## Plugin Discovery

The app dynamically discovers plugins using:

1. **Local scan** — checks the `clv/plugins/` folder.  
2. **Entry points** — scans installed packages that expose a `clv.plugins` namespace.

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
- Use `requires_clv = ">=1.0,<2.0"` in plugin metadata to indicate compatibility.  
- The CLV app will warn users about incompatible versions.

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