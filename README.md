# Centralized Log Viewer (CLV)

Centralized Log Viewer (CLV) is a fast, Textual-powered TUI that gives Linux users a Windows Event Viewer–inspired experience. CLV was born out of the need to have a lightweight, extensable (planned), and easy to deploy Log Viewer. Enter CLV. With CLV Linux User are provided an easy to configure settings file that accepts parent log folders and/or signle log folders. With Rich Text support, CLV can read and "Pretty" format a large number of file extensions. Not just `.log`! As an example: XML, CSV, TXT, and JSON. When CLV is opened or when a log source is add via the `Add Source` button, it discovers log sources, applies regex/time/severity filters, and renders colorized tails without surprises whether you are in a desktop terminal or on a headless box.

---

## Feature Highlights

- 🔍 **Zero-latency filtering:** Query bar validates regex as you type, pairs it with severity chips, and cycles through preset or custom time windows.
- 🧱 **Advanced controls drawer:** Toggle auto-scroll, structured rendering, exclude paths, and other secondary filters without covering the main log pane.
- 🪵 **Rich log rendering:** Severity-colored lines, structured payload preview (JSON/XML/CSV), copy-friendly mode, and bounded ring buffers to cap RAM.
- 🧭 **Persistent state:** Session state (sources, filters, toggles) is saved automatically through `clv/storage.py`, so restarts pick up exactly where you left off.
- 🧩 **Composable widgets:** Query bar, segmented buttons, filter chips, and drawers live under `clv/widgets/`, keeping the UI modular and easy to extend.
- 🛠️ **Upcoming plugins:** `clv/plugins/` is being built out to host future log-source providers, filter stages, and exporters so new functionality can live outside the core.

---

## Installation

### Prebuilt packages (recommended)

Download the latest release assets from the [GitHub Releases](https://github.com/r0tifer/clv_dev/releases) page and install the package for your distro:

```bash
# Debian/Ubuntu
curl -LO https://github.com/r0tifer/clv_dev/releases/download/vX.Y.Z/centralized-log-viewer_X.Y.Z-1_amd64.deb
sudo dpkg -i centralized-log-viewer_X.Y.Z-1_amd64.deb

# RHEL/Fedora/openSUSE
curl -LO https://github.com/r0tifer/clv_dev/releases/download/vX.Y.Z/centralized-log-viewer-X.Y.Z-1.x86_64.rpm
sudo rpm -Uvh centralized-log-viewer-X.Y.Z-1.x86_64.rpm
```

These packages install the PyInstaller-built tree under `/opt/centralized-log-viewer` and install a `clv` launcher into `/usr/local/bin`.

### Tarball

Every release also ships `centralized-log-viewer-linux-x86_64.tar.gz`. Extract it anywhere and run `./clv/clv`.

### From source (developers)

```bash
git clone https://github.com/r0tifer/clv_dev.git
cd clv_dev
python -m pip install -e .
python -m clv  # or: clv
```

---

## Configuration & Settings Priority

`settings.conf` is resolved in the following order:

1. `${XDG_CONFIG_HOME:-~/.config}/clv/settings.conf` – automatically created after the first run; this is the persistent user config.
2. `settings.conf` in the repository root – used as a development fallback when the XDG file is missing.

Key options:

| Option | Purpose | Default |
| --- | --- | --- |
| `log_dirs` | Comma-separated absolute directories to scan recursively for `*.log`. | `./logs` (resolved relative to the working directory) |
| `max_buffer_lines` | Lines kept per source before dropping old entries. | `500` |
| `default_show_lines` / `min_show_lines` / `show_step` | Controls visible lines in the log panel. | `200 / 10 / 10` |
| `refresh_hz` | Polling frequency for new log data. | `2` |
| `csv_max_rows`, `csv_max_cols` | Structured payload preview limits. | `20 / 10` |

Update the config, save, and restart CLV to apply the changes. Invalid or missing values fall back to safe defaults.

---

## Usage

After installing:

```bash
clv                # launches the Textual TUI
clv --help         # list CLI flags
python -m clv      # module entry point (mirrors clv script)
```

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `/` | Focus query input (regex) |
| `Tab` / `Shift+Tab` | Move focus between controls |
| `Ctrl+Enter` | Apply filters |
| `Esc` | Clear query |
| `A` | Add log source dialog |
| `[` / `]` | Resize source tree |
| `+` / `-` | Adjust visible log lines |
| `Ctrl+L` | Toggle copy mode |
| `Ctrl+S` | Persist session |
| `Q` | Quit application |

Mouse interactions are fully supported, but every action has a keyboard path for headless use.

---

## Development Notes

- Core application lives in `clv/app.py`; avoid adding new code under `centralized_log_viewer/` (legacy shim only).
- Widgets own their visuals/CSS in `clv/widgets/` and should not depend on one another’s internals.
- Session/state logic is centralized in `clv/storage.py`; services like log discovery live under `clv/services/`.
- Run `python -m pip install -e .` (or `poetry install` if you prefer) and then `python -m clv` to hack on the app. Textual’s `--dev` flag is supported: `python -m textual run clv/app.py --dev`.

---

## Plugin Roadmap (Coming Soon)

Work is underway on a plugin interface (see `clv/plugins/`) featuring:

- **LogSourceProvider** – discover and stream logs from new backends.
- **FilterStage** – inject custom filtering or transformation logic.
- **Exporter** – push the currently viewed session to downstream tools.

Plugins will be loadable from a `clv/plugins/` directory or via Python entry points (`clv.plugins.*`), empowering teams to add organization-specific behavior without patching core CLV code.

Stay tuned to the AGENTS.md file and release notes as the plugin API stabilizes.
