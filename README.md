# Centralized Log Viewer

An interactive terminal-based log viewer built with [Textual](https://textual.textualize.io/). This application automatically discovers all `*.log` files inside a configurable directory (default: `logs/`) and provides a powerful UI for viewing, filtering, and tailing logs in real time.

---

## ✨ Features

- 📁 **Recursive log discovery** from one or more configured directories
- 🌲 **Tree-based navigation** of directory structure and `.log` files
- 🔍 **Regex-based filtering** of log lines in real time
- 🕒 **Time range filters** (e.g., `15m`, `2h`, or `2024-05-01 10:00 to 2024-05-01 12:00`)
- 🎨 **Color-coded log levels** for ERROR, WARNING, INFO, DEBUG
- ⚙️ **Log Severity filter** to focus on specific severities
- 📜 **Live log tailing** with auto-scroll toggle
- ↕️ **Adjustable tree and log pane sizes** with keyboard shortcuts
- 🖱️ **Toggle mouse capture** on demand

---

## 🚀 Getting Started

### 🔧 Prerequisites

- Python 3.9 or newer
- [Poetry](https://python-poetry.org/) (recommended for dependency management)

### 📦 Installation

```bash
# Clone the repository
git clone https://git.deeptree.tech/ADVTCH/Python/src/branch/main/log_viewer
cd textual-log-viewer

# Install dependencies
poetry install
```

---

## 🛠 Configuration

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
```

---

## 🧭 Usage

```bash
# Run via poetry
poetry run python -m main
```

Or, if you have the script installed:

```bash
python main.py
```

### 🎹 Keyboard Shortcuts

| Key              | Action                     |
|------------------|----------------------------|
| `↑` / `↓`        | Navigate tree              |
| `Enter`          | Open highlighted file      |
| `[` / `]`        | Shrink/Expand tree         |
| `+` / `-`        | Show more/fewer lines      |
| `/`              | Regex filter               |
| `T`              | Time range filter          |
| `A`              | Toggle auto-scroll         |
| `M`              | Toggle mouse capture       |
| `Q`              | Quit application           |

---

## 🧪 Development

To run in development mode:

```bash
poetry run textual run main.py --dev
```

---

## 👤 Author

**Michael Levesque**  
📧 michael.levesque@yourdomain.com  
🔗 [GitHub Profile](https://git.deeptree.tech/ADVTCH)

