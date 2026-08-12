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
- 🔬 **Select a line, see the whole event.** Arrow keys move a cursor through
  the log; `Enter` opens a detail pane showing the raw line beside its parsed
  timestamp, canonical severity, detected format and every field the parser
  recovered — host, tag, PID, HTTP status, or the flattened keys of a JSON
  payload.
- 🪶 **Bounded memory, whatever the file size.** Opening a source seeks
  backwards from the end of the file; a 160 MB log opens in ~2 ms using under a
  megabyte. Tailing reads only what was appended. Compressed members are the
  honest exception — see [Compressed and rotated logs](#compressed-and-rotated-logs).
- 🧭 **Any file, not just `*.log`.** Name folders or files; every readable text
  file counts — including UTF-16 exports from Windows and PowerShell, and
  `.ods` spreadsheets, which are unpacked into tab-separated rows. Binary
  files are detected by content and skipped, and include/exclude globs are
  yours to set.
- 🗂 **Compressed and rotated logs.** `.gz`, `.bz2` and `.xz` are read directly,
  and `app.log` + `app.log.1` + `app.log.2.gz` are presented as **one source**
  spanning all three, oldest lines first. Only the live member is tailed; the
  rotated-out ones are read once, and only as far back as your buffer needs.
- 📐 **Responsive layout.** Breakpoints at 90 and 130 columns reflow the
  controls; every control stays on screen and keyboard-reachable down to 80
  columns.
- 🔖 **Mark the lines that matter.** `m` bookmarks the line under the cursor,
  `M` steps between the marks, and `Ctrl+E` can export just those. Marks are
  keyed by content rather than position, so they survive filtering and tailing —
  and they are session-only, never written to disk.
- 📤 **Get the view out.** `Ctrl+E` writes the filtered entries as JSON Lines,
  CSV or raw text — the whole filtered set, not just the lines on screen. `y`
  copies the selected line — or the whole visible view — to your local clipboard
  through the terminal, so it works over SSH and tmux where a mouse selection
  does not.
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
| `skip_binary` | Skip files whose first block decodes to NUL characters. UTF-16 text, extractable documents (`.ods`) and compressed members are exempt. | `true` |
| `max_files` | Stop discovery after this many files. | `5000` |
| `group_rotated` | Present a rotated log's members as one source. Overridden by the drawer's **Group rotated** switch once you touch it. | `true` |
| `max_buffer_lines` | Lines held in memory per source. | `5000` |
| `default_show_lines` / `min_show_lines` / `show_step` | Visible-line window and its `+`/`-` step. | `500 / 10 / 50` |
| `refresh_hz` | Poll frequency for new content. | `2` |
| `tree_width` | Starting width of the source tree, in columns. | `38` |
| `csv_max_rows` / `csv_max_cols` | Structured payload preview limits. | `20 / 10` |
| `clipboard_max_bytes` | Most log text one `y` clipboard copy may carry. Oversized copies are truncated at a line boundary and say so. | `65536` |
| `watch_rate_limit` | Seconds a watch rule waits before notifying again; matches inside the window are counted and reported together. | `60` |
| `watch_bell` | Ring the terminal bell when a watch rule notifies. | `false` |

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

### Compressed and rotated logs

`.gz`, `.bz2` and `.xz` files are read directly — no `zcat`, no unpacking to a
temp file, and nothing written anywhere. `.zst` is still excluded, because
there is no decompressor for it in the Python standard library and adding one
would be CLV's first new runtime dependency.

More usefully, the members of a rotated log are presented as **one source**:

```
📂 /var/log
   🗂 syslog (4 files)
      📄 syslog
      📄 syslog.1
      📄 syslog.2.gz
      📄 syslog.3.gz
   📄 auth.log
```

Selecting the `syslog` set reads all four in order, oldest lines first, into one
pane — so a query, a time window or a `g` jump crosses a rotation boundary
without you opening anything else. The status line names the member the cursor
is currently in. Every member is still listed underneath and can still be opened
on its own.

Recognised names, after any compression suffix: `app.log.1`, `app.log-20260811`,
`app.log.2026-08-11`. A gap in the numbering is fine. Turn grouping off with
`group_rotated` in `settings.conf` or the **Group rotated** switch in the
Advanced drawer.

**On bounded memory.** CLV's usual promise is that nothing reads a whole file:
opening a source seeks backwards from the end. A deflate stream has no cheap
backwards seek, so a compressed member is the exception, and it is worth being
precise about what is and is not bounded:

- **Memory is bounded**, by `max_buffer_lines`. Lines stream through a
  fixed-size buffer, so a 400 MB decompressed member costs no more than a small
  one.
- **Work is proportional to the member**, not to what you see. Reading the last
  500 lines of a `.gz` means decompressing it to get there. A second cap on
  decompressed bytes stops a decompression bomb, degrading to "here is what was
  read" rather than exhausting the machine.
- **The budget is shared across the set and spent newest-first.** Members are
  read back from the live one until `max_buffer_lines` is met, then no further.
  A set whose newest member already fills the buffer opens as fast as a single
  file, and CLV says how many members it actually read.
- **Only the live member tails.** Nothing appends to `syslog.2.gz`, so it is
  read once and never polled again.

### Inspecting an event

The log pane has a cursor. Arrow keys move it a line at a time, `PgUp`/`PgDn` a
screen at a time, `Home`/`End` to either end, and a mouse click selects the line
you click on.

`Enter` on the selected line opens the **detail pane**, which lists:

| Property | |
| --- | --- |
| The raw line | Exactly as it appears in the file |
| `Timestamp` | Normalised, or `—` when the line carries none |
| `Level` | The canonical severity, or `—` |
| `Format` | Which format matched — down to *unrecognised* |
| `Continuation` | Whether the timestamp and level were inherited from the line above |
| …then every field | `host`, `tag`, `pid`, `status`, or a JSON payload's keys flattened to dotted paths |

`d` opens and closes the pane without moving the cursor, and there is a **Detail
pane** switch in the Advanced drawer. Whether it is open is remembered between
runs; which line you had selected is not.

Not every line has fields — four of the formats CLV recognises carry none, and
an unrecognised line carries nothing but its text. The pane says which case it
is rather than showing you an empty list.

Where the pane goes depends on the width: beside the log from 130 columns,
below it between 90 and 130, and in place of the log at 80, where the two
cannot share the screen. Press `d` to get the log back.

**Moving the cursor pauses follow mode.** Otherwise incoming lines would drag
the view out from under the line you just pointed at. The status bar says so —
*"paused — cursor moved, End resumes"* — and `End` or `w` starts following
again.

### Marking lines

`m` marks the line under the cursor and `M` steps between the marks, wrapping
with a notification. Marked lines carry a `●` in the gutter — a glyph, not just
a colour, so it reads on a monochrome terminal — and the status bar keeps a
count. `Ctrl+E` then offers **Marked lines only**, which is the point: mark the
three lines that matter while reading a five-thousand-line buffer, then export
exactly those.

A mark is recorded as the source path plus a digest of the line's text, not as
a position. That is what lets it survive the ring buffer evicting lines above
it, and lets a line a filter hid come back still marked. Two consequences worth
knowing: identical lines in one source share a mark, and once a line has rotated
out of the buffer entirely its mark is dropped.

**Marks are never written to disk.** They are session-only, and they stay that
way on purpose: the digest is derived from log content, and `session.json`
records paths and settings, never anything about what a log contained. Closing
CLV forgets them.

### Saved views

A filter set you had to think about is worth keeping. `V` names the one that is
active; `v` opens the list of saved ones.

```
V  →  name it "5xx on web01"      (query, severity, time window, search
                                   options, globs and the open log)
v  →  Enter applies it            r renames · d deletes (twice) · Esc closes
```

Saved views also appear as a group at the top of the source tree, above the
starred logs, so applying one is a single click or a couple of arrow keys.

Applying a view puts everything back at once — one repaint, not one per field —
and reopens the log it was saved against. If that log has since gone, the
filters are applied anyway and a notification says which source was missing: a
rotated-away file should not turn a view into a dead end.

Views live in `session.json`, so they survive restarts. Like everything else
there, a view records **settings and one path only** — never a log line, never
what it matched.

### Watch rules

Tailing means waiting for something. A watch rule says what, so you can stop
reading every line: `W` opens the manager, `a` adds a rule, `Enter` edits one,
`space` enables or disables it, `d` twice deletes it.

A rule is a name, a pattern in the same syntax the query box uses (so
`tag:kernel oom-killer` works), and an action:

| Action | Effect |
| --- | --- |
| Highlight only | Matching lines are drawn with a distinct background |
| Notify only | A notification, nothing visual |
| Highlight + notify | Both (the default) |

The highlight is a **background**, deliberately: severity is carried in the
text colour, so a watched INFO line reads as watched rather than as an error.

**Notifications are rate limited.** The first match for a rule is reported at
once; anything else inside the window is counted and reported together — *"Watch
'oom-killer' matched 143 lines."* The window is `watch_rate_limit` in
`settings.conf` (60 seconds by default), and a rule matching every line
therefore costs one message a minute rather than one per line. Set
`watch_bell = true` if you want the terminal bell as well; it is off by default.

Opening a log highlights the lines already in the buffer that match, but says
nothing about them — you asked to be told about what happens next, not about
what happened before you asked. Enabled rules appear as chips beside the filter
chips, and dismissing a chip disables that rule without deleting it. The
Advanced drawer has a switch for the whole set and a count of what is live.

Rules persist in `session.json`, the same as saved views: a pattern is
something you typed, not something a log contained. Everything runs in-process
for the life of the session — no daemon, no desktop notification service, no
subprocess.

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

`y` copies to your **local** clipboard using an OSC 52 escape sequence. It
copies the **selected line** when the cursor is on one, and the lines currently
on screen — filters and the visible-line window included — when it is not.
Nothing is selected until you move the cursor, so `y` on a freshly opened log
still copies the view.

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
| `Enter` | Apply filters (in the query input) · open the detail pane (in the log pane) |
| `Esc` | Clear the query |
| `a` | Add a log source |
| `t` / `s` | Cycle time window / severity |
| `f` | Toggle the Advanced drawer |
| `*` | Star / unstar the log under the cursor |
| `↑` / `↓` | Move the line cursor |
| `PgUp` / `PgDn` | Move the line cursor a screen at a time |
| `Home` / `End` | First / last line (`End` also resumes following) |
| `n` / `N` | Next / previous match (or warning-and-worse, with no query) |
| `g` | Go to a timestamp |
| `m` / `M` | Mark the cursor line / jump to the next mark |
| `v` / `V` | Open saved views / save the current filters as a view |
| `W` | Manage watch rules (live highlights and alerts) |
| `d` | Show / hide the event detail pane |
| `w` | Follow new lines (auto-scroll) on/off |
| `o` | Structured output on/off |
| `Ctrl+B` | Switch between tree and log pane (compact widths) |
| `[` / `]` | Narrow / widen the source tree |
| `+` / `-` | Show more / fewer lines |
| `Ctrl+E` | Export the filtered entries to a file |
| `y` | Copy the selected line, or the visible lines, to the clipboard (OSC 52) |
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

### Field queries

The parser recovers more than a timestamp and a level from each line — a
hostname, a program tag, an HTTP status, every key of a JSON payload — and the
query box can ask about any of it:

```
tag:sshd host:web01 status>=500 timeout|refused
└──────────── field terms ─────┘ └─── regex ──┘
```

Terms combine with **and**. Anything that is not a term is the regex it has
always been, matched against the whole raw line.

| Operator | Means | Example |
| --- | --- | --- |
| `key:value` | contains, smart-case (case-sensitive only if you type a capital) | `host:web` |
| `key:` | the field is present at all | `pid:` |
| `key=value` | exactly equal, case-sensitive | `host=web01` |
| `key!=value` | not equal | `tag!=cron` |
| `key>value` `key>=value` `key<value` `key<=value` | numeric when both sides are numbers, alphabetical otherwise | `status>=500` |

Quote a value to keep spaces or colons inside it: `msg:"disk full"`.

Which names work depends on the source. The parser's own vocabulary — `host`,
`tag`, `pid`, `msgid`, `ident`, `user`, `request`, `status`, `size` — is always
available, and every key a JSON line carries is added as soon as one is read.
Start typing a name and the field list drops down under the input: `Tab` takes
the first suggestion, `↓` steps into the list, `Esc` dismisses it. The Advanced
drawer keeps a one-line reminder of the syntax under Search options.

**Nothing you already search for changes.** A word that is not a known field
name is text, so `sshd:` and `kernel: oom-killer` search for exactly what they
always did, and so does a timestamp like `10:30:00`. The cost of that guarantee
is that a mistyped field name (`hsot:web01`) is searched for as text rather
than reported — which is why the completions exist.

A line that has no such field is hidden and **counted**, like every other
filter here: *"No matches — 214 carry no 'status' field (this source's format
does not report it)."* If you are filtering a syslog with `status>=500`, that
sentence is the answer.

### Navigating what you filtered to

`n` and `N` move the line cursor forward and back through the lines worth
stopping at, wrapping at either end with a notification rather than going quiet.
What counts as "worth stopping at" depends on what is active:

| Active | `n` steps between |
| --- | --- |
| A query | Its matches — and since the query *filters*, that is every visible line. What `n` adds here is the position: *"match 12 of 47"*, in the hit counter and the status bar. |
| A severity bucket | Entries in that bucket. |
| Neither | **WARN and above.** Stepping every entry would only duplicate the down arrow; warnings are included because they usually precede the failure. |

`g` asks for a time and moves the cursor to the first entry **at or after** it.
It takes an absolute timestamp (`2026-08-07 09:25:01`) or an offset from now
(`-15m`, `-6h`, `-2d`; a bare `15m` means the past). Entries with no parsed
timestamp cannot answer a question about time, so they are skipped — and the
notification says how many, rather than quietly ignoring part of the source.

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
