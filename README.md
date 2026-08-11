# Centralized Log Viewer (CLV)

CLV is a fast, Textual-powered TUI that gives Linux operators a Windows Event
Viewer–inspired experience. Point it at any number of folders and/or individual
files, and it discovers, tails, filters and colorizes them — the same way on a
desktop terminal and on a headless 80-column SSH session.

---

## Feature Highlights

- 🔎 **Search that works on real logs.** The query is a regex matched against
  the *whole line*, across every format CLV recognises — and lines it cannot
  parse are still searchable rather than silently dropped. Smart case: a
  lowercase query is case-insensitive, an uppercase character opts back in.
- 🧬 **Multi-format parsing.** syslog (RFC 3164 and 5424), ISO-8601/bracketed
  levels, Python `logging`, JSON lines, and Common Log Format access logs.
  Anything else is kept as a raw line with its text intact.
- 🧵 **Stack traces stay attached.** A line no format recognises inherits the
  timestamp and severity of the entry above it, so a traceback survives a
  "show me only errors" filter along with the ERROR that produced it.
- 🪶 **Bounded memory, whatever the file size.** Opening a source seeks
  backwards from the end of the file; a 160 MB log opens in ~2 ms using under a
  megabyte. Tailing reads only what was appended.
- 🧭 **Any file, not just `*.log`.** Name folders or files; every readable text
  file counts — including UTF-16 exports from Windows and PowerShell, and
  `.ods` spreadsheets, which are unpacked into tab-separated rows. Binary
  files are detected by content and skipped, and include/exclude globs are
  yours to set.
- 📐 **Responsive layout.** Breakpoints at 90 and 130 columns reflow the
  controls; every control stays on screen and keyboard-reachable down to 80
  columns.
- 📤 **Get the view out.** `Ctrl+E` writes the filtered entries as JSON Lines,
  CSV or raw text — the whole filtered set, not just the lines on screen. `y`
  copies what is on screen to your local clipboard through the terminal, so it
  works over SSH and tmux where a mouse selection does not.
- 🧩 **Plugins.** `LogSourceProvider`, `FilterStage` and `Exporter` interfaces,
  loaded from `clv/plugins/` or from installed packages via the `clv.plugins`
  entry point group. A broken plugin is reported, never fatal.
- ⭐ **Starred logs.** Press `*` on any log to star it. Starred logs are
  repeated in a group at the top of the tree, so a favourite buried several
  folders deep is one keystroke away. Star exactly one and CLV opens it on
  launch.
- 💾 **Session state that persists.** Filters, toggles, drawer settings and
  stars come back on restart. The source you merely had open does not: without
  a star, CLV opens on its discovery summary, so every launch starts from a
  known state rather than silently resuming a tail.

---

## Installation

### Install script (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/r0tifer/Centralized_Log_Viewer/main/install.sh | bash
```

Downloads the release build for your architecture (x86_64 or aarch64), verifies
it against `SHA256SUMS` — and against the maintainer's GPG signature when one is
published — then installs the program tree and a `clv` launcher on your PATH.
Without root it installs under `~/.local`.

```bash
# Pin a version, choose locations, or require a specific signing key
install.sh --version v2.1.0
install.sh --prefix ~/bin --libdir ~/opt/clv
install.sh --gpg-fpr <fingerprint>     # fail unless SHA256SUMS is signed by this key
```

### Prebuilt packages

Download release assets from
[GitHub Releases](https://github.com/r0tifer/Centralized_Log_Viewer/releases):

```bash
# Debian/Ubuntu
sudo dpkg -i centralized-log-viewer_X.Y.Z-1_amd64.deb

# RHEL/Fedora/openSUSE
sudo rpm -Uvh centralized-log-viewer-X.Y.Z-1.x86_64.rpm
```

Both install the PyInstaller tree under `/opt/centralized-log-viewer` with a
`clv` launcher in `/usr/local/bin`. A
`centralized-log-viewer-linux-<arch>.tar.gz` tarball also ships with every
release as a universal fallback.

### From source (developers)

```bash
git clone https://github.com/r0tifer/Centralized_Log_Viewer.git
cd Centralized_Log_Viewer
python -m pip install -e .
python -m clv   # or: clv
```

CLV creates `~/.config/clv/settings.conf` on first run, whichever method you
use.

---

## Configuration

`settings.conf` is resolved in this order:

1. `${XDG_CONFIG_HOME:-~/.config}/clv/settings.conf` — created on first run.
2. `settings.conf` in the repository root — development fallback.

| Option | Purpose | Default |
| --- | --- | --- |
| `log_dirs` | Folders **and/or files** to monitor, comma separated. Folders are searched recursively. | `/var/log` |
| `include_globs` | Only list files matching these globs. Empty means every text file. | *(empty)* |
| `exclude_globs` | Never list files matching these globs. | archives, binary journals, PDFs |
| `follow_symlinks` | Follow symlinked directories (cycles are detected). | `false` |
| `skip_binary` | Skip files whose first block decodes to NUL characters. UTF-16 text and extractable documents (`.ods`) are exempt. | `true` |
| `max_files` | Stop discovery after this many files. | `5000` |
| `max_buffer_lines` | Lines held in memory per source. | `5000` |
| `default_show_lines` / `min_show_lines` / `show_step` | Visible-line window and its `+`/`-` step. | `500 / 10 / 50` |
| `refresh_hz` | Poll frequency for new content. | `2` |
| `tree_width` | Starting width of the source tree, in columns. | `38` |
| `csv_max_rows` / `csv_max_cols` | Structured payload preview limits. | `20 / 10` |
| `clipboard_max_bytes` | Most log text one `y` clipboard copy may carry. Oversized copies are truncated at a line boundary and say so. | `65536` |

Invalid values fall back to safe defaults; the app never fails to start because
of a malformed settings file. Most discovery options are also editable at
runtime in the **Advanced** drawer.

---

## Usage

```bash
clv              # launch the TUI
python -m clv    # module entry point
```

### Getting help

Press `?` for an overlay listing every keybinding, grouped by what it does. The
footer only has room for the first handful at narrow widths, so the overlay is
the complete list — it is generated from the bindings themselves and cannot
fall out of date. Dismiss it with `?`, `Esc` or `q`.

One wrinkle worth knowing: while the cursor is in the query input, `?` types a
literal question mark, because it is a valid regex character. Press `Esc` first
if the input has focus. Tailing continues while the overlay is open.

### Exporting

`Ctrl+E` writes the entries the filters kept to a file. Three formats ship:

| Format | What it contains |
| --- | --- |
| JSON Lines | One object per entry — raw line, timestamp, level, message, detected format, continuation flag and every parsed field. The only lossless option. |
| CSV | A fixed, rectangular table of the same columns, with the parsed fields as one JSON column. |
| Plain text | The raw lines, byte-identical to what is on screen. |

Two things worth knowing:

- It exports the **whole filtered set**, not just the lines that fit on screen.
  The dialog states the count before it writes, so `+`/`-` never changes what an
  export contains.
- Writing is atomic (a sibling temp file, then a rename), and overwriting an
  existing file takes a second press of Export. A permission error is reported
  as a notification, not a traceback.

Any `Exporter` plugin you have installed is listed in the same dialog, below the
built-ins. The Advanced drawer shows the full list read-only, so you can see
what is available without opening the dialog.

One wrinkle: while the cursor is in the query input, `Ctrl+E` moves it to the end
of the line — that binding belongs to the input. Press `Tab` or click the log
pane first.

### Copying to the clipboard

`y` copies the lines currently on screen — filters and the visible-line window
included — to your **local** clipboard using an OSC 52 escape sequence.

`y` and `Ctrl+L` solve the same problem from opposite ends and both are kept:

| | Needs | Works over SSH / tmux |
| --- | --- | --- |
| `y` | A terminal that honours OSC 52 | Yes |
| `Ctrl+L` copy mode | A local mouse selection | No |

A copy larger than `clipboard_max_bytes` is truncated at a line boundary,
keeping the newest lines, and the notification says how many were dropped —
there is no silent partial copy. If your terminal renders the sequence as
garbage, turn it off with the **Clipboard (OSC 52)** switch in the Advanced
drawer; the setting is remembered, and `Ctrl+L` remains.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `?` | Show every keybinding |
| `/` | Focus the query input |
| `Enter` | Apply filters |
| `Esc` | Clear the query |
| `a` | Add a log source |
| `t` / `s` | Cycle time window / severity |
| `f` | Toggle the Advanced drawer |
| `*` | Star / unstar the log under the cursor |
| `w` | Follow new lines (auto-scroll) on/off |
| `o` | Structured output on/off |
| `Ctrl+B` | Switch between tree and log pane (compact widths) |
| `[` / `]` | Narrow / widen the source tree |
| `+` / `-` | Show more / fewer lines |
| `Ctrl+E` | Export the filtered entries to a file |
| `y` | Copy the visible lines to the clipboard (OSC 52) |
| `Ctrl+L` | Copy mode (hides all chrome) |
| `Ctrl+S` | Save added sources to `settings.conf` |
| `Ctrl+R` | Reload configuration and rescan |
| `q` | Quit |

Every action has a keyboard path; mouse is fully supported but never required.

---

## How filtering behaves

Understanding two rules explains everything the pane does:

1. **The query never drops what it cannot parse.** It matches raw line text, so
   unstructured lines are searchable like any other.
2. **Severity and time filters only hide lines that demonstrably lack what you
   asked for** — and when they do, the empty pane says so, e.g. *"No matches —
   12 have no detected severity (nothing in this source declares a level)."*

---

## Architecture

| Layer | Location | Responsibility |
| --- | --- | --- |
| App shell | `clv/app.py` | Layout, routing, lifecycle. No parsing or IO. |
| Services | `clv/services/` | `parsing`, `filtering`, `discovery`, `reader`, `config`, `sources`, `export`, `clipboard`. UI-free and independently testable. |
| Widgets | `clv/widgets/` | Self-contained UI components owning their own CSS. |
| Plugins | `clv/plugins/` | Extension interfaces and the loader. |
| State | `clv/storage.py` | JSON session persistence (atomic writes). |

Styling is CSS-only: no module assigns `.styles.*` at runtime except for the
user-adjustable tree width. Responsive behavior comes from breakpoint classes
(`-compact` / `-narrow` / `-wide`) that the app sets and widget CSS keys off.

---

## Writing a plugin

Drop a module into `clv/plugins/filters/` (or `sources/` / `exporters/`), or
publish one from an installed package under the `clv.plugins` entry point group.

```python
from dataclasses import replace
from clv.plugins import FilterStage

class Redact(FilterStage):
    name = "redact-secrets"
    requires_clv = ">=2.0,<3.0"     # optional

    def apply(self, entry, context):
        if "password" not in entry.raw:
            return entry                # keep unchanged
        return replace(entry, raw=entry.raw.replace("password", "******"))

def register():
    return Redact()
```

Return `None` from `apply` to drop a line. A plugin that fails to import, fails
its version check, or raises at runtime is disabled and reported in the
Advanced drawer — it cannot take the app down.

`Exporter` plugins are reachable from the UI: `Ctrl+E` lists them alongside the
three built-in formats and hands the selected one the whole filtered set. An
exporter chooses its own destination (`export` receives the entries and a
`FilterContext`, not a path), and one that raises is reported and skipped like
any other plugin failure.

---

## Development

```bash
python -m pip install -e .
python -m pip install pytest
python -m pytest            # 290 tests
python -m textual run clv/app.py --dev
```
