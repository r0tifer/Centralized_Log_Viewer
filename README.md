# Centralized Log Viewer (CLV)

An interactive terminal-based log viewer built with [Textual](https://textual.textualize.io/). This application automatically discovers all `*.log` files inside a configurable directory (default: `logs/`) and provides a powerful UI for viewing, filtering, and tailing logs in real time.

---

## Features

- 📁 **Recursive log discovery** from one or more configured directories
- 🌲 **Tree-based navigation** of directory structure and `.log` files
- ➕ **Session-aware log sources** — add extra files or folders on the fly and persist them
- 🔍 **Regex-based filtering** of log lines in real time
- 🕒 **Time range filters** (e.g., `15m`, `2h`, or `2024-05-01 10:00 to 2024-05-01 12:00`)
- 🎨 **Color-coded log levels** for ERROR, WARNING, INFO, DEBUG
- 📊 **Structured payload preview** for JSON/XML/CSV (CSV limits configurable via `settings.conf`)
- ⚙️ **Log Severity filter** to focus on specific severities
- 📜 **Live log tailing** with auto-scroll toggle
- ↕️ **Adjustable tree and log pane sizes** with keyboard shortcuts
- 🖨️ **Copy mode** that hides surrounding chrome for clean clipboard grabs

---

## 🚀 Getting Started

### 🔧 Prerequisites

- Python 3.9 or newer
- [Poetry](https://python-poetry.org/) (recommended for dependency management)

### Installation

```bash
# Clone the repository
git clone https://git.deeptree.tech/ADVTCH/Python/src/branch/main/log_viewer
cd centralized-log-viewer

# Install dependencies
poetry install
```

---

## Configuration

The app uses a `settings.conf` file in the root directory:

```ini
[log_viewer]

# Comma-separated list of folder paths to scan for .log files.
# Each folder is searched recursively for files ending in ".log".
# Example: log_dirs = logs, /var/log/custom, ./debug_output
log_dirs = /var/log

# Maximum number of log lines to keep in memory per file.
# Older lines are discarded once this limit is reached.
# Helps prevent excessive memory usage when tailing large files.
max_buffer_lines = 500

# Number of lines to display when a log file is first opened.
# Can be adjusted at runtime with + / - or the corresponding key bindings.
default_show_lines = 40

# Frequency (in Hz) that the viewer checks for new log content.
# Lower values reduce CPU usage; higher values increase responsiveness.
refresh_hz = 4

# Default width (in columns) of the file tree pane on the left.
# Can be resized using the [ and ] keys.
default_tree_width = 30

# Minimum number of log lines allowed in the view window.
# Prevents the view from becoming too narrow when reducing visible lines.
min_show_lines = 10

# Number of lines to add or remove when increasing or decreasing the view size.
# Used when pressing + or - to adjust visible log content.
show_step = 10

# Structured CSV rendering limits. Increase these if you want larger CSV payloads
# (detected inside log messages) to be rendered in the UI; lower them to keep
# the UI snappy when logs contain very large tables.
csv_max_rows = 20
csv_max_cols = 10
```

---

## Usage

```bash
# Run via poetry
poetry run CentralizedLogViewer  # or: poetry run clv

# Or run the module directly
python -m log_viewer.main
```

### Keyboard Shortcuts

| Key              | Action                     |
|------------------|----------------------------|
| `↑` / `↓`        | Navigate tree              |
| `Enter`          | Open highlighted file      |
| `[` / `]`        | Shrink/Expand tree         |
| `+` / `-`        | Show more/fewer lines      |
| `A`              | Add Log Source modal       |
| `Ctrl+S`         | Save session to settings   |
| `Ctrl+L`         | Toggle copy mode           |
| `/`              | Regex filter               |
| `T`              | Time range filter          |
| `Space`          | Toggle auto-scroll checkbox|
| `Q`              | Quit application           |

> **Tip:** Shift-click inside the log pane while copy mode is active to capture only the log output.

### Managing Log Sources at Runtime

- Use the **Add Log Source** button in the top toolbar or press `A` to open the modal.
- Enter an absolute path to a directory (all `*.log` files will be indexed) or to a specific log file.
- If the viewer lacks permission to read the path, a warning explains what failed and reminds you to re-launch the app with elevated privileges.

### Saving Your Session

When you are satisfied with the extra sources you've added, press **Save Session** in the toolbar or hit `Ctrl+S`. The new paths are appended to `settings.conf` so they'll be loaded the next time the viewer starts. If you change your mind, simply skip saving—the temporary additions apply for the current session only.

### Copy Mode

Press `Ctrl+L` to toggle copy mode. The tree, filters, and metadata panels collapse, giving you a clean log pane for clipboard captures. Exit copy mode with `Ctrl+L` again or by clicking outside the log pane.

---

## Development

To run in development mode:

```bash
poetry run textual run main.py --dev
```

---
