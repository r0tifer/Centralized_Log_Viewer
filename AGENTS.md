# AGENTS.md — Centralized Log Viewer

## Mission & Product North Star

Centralized Log Viewer (CLV) is the Linux counterpart to **Windows Event
Viewer**: a **fast, lightweight, minimal-dependency** TUI that behaves
identically in a desktop terminal emulator and on a **headless** server
terminal.

**We prioritize:**
- **Speed & responsiveness** over features that add latency or RAM churn.
- **Zero-surprise UX**: identical layout and behavior across environments.
- **Low friction**: minimal dependencies, trivial install, predictable defaults.

---

## Product Requirements (non-negotiable)

### 1) Point it anywhere
An operator names **folders and/or individual files**. Folders are searched
recursively. CLV is **not** restricted to `*.log` — any readable text file is a
valid source. Filtering by name is the user's decision (`include_globs` /
`exclude_globs`), not a hard-coded rule. Files are excluded for
**readability** reasons (binary content, compressed archives), never for
naming reasons.

**Anywhere includes another machine.** A root may live on a host the operator
names in `settings.conf`, read over SSH with their own credentials — documented
for operators under *Remote sources over SSH* in [README.md](README.md), with the
reasoning behind each decision in [SSH_TODO.md](SSH_TODO.md). A remote folder is a
root like any other: discovered
recursively, listed under the same folder hierarchy, starrable, mergeable. It is
not a second-class source type, and a source is therefore a `SourceRef` rather
than a `pathlib.Path` — see the identity rule in [clv/AGENTS.md](clv/AGENTS.md).
What remains refused is stated under Non-Goals, and the line is on-demand reads
versus collection infrastructure, not local versus remote.

"Readable text" is judged on **characters, not bytes**. UTF-16 encodes ASCII
with a NUL beside every character, so a byte-level NUL test rejects the
Windows and PowerShell exports operators most often point CLV at. Encoding is
sniffed from the byte-order mark (`reader.detect_encoding`) and BOM-less files
are read as UTF-8, as before — statistical charset guessing is **not** done,
because guessing wrong puts a core dump on screen.

A small number of **container documents** are readable text that no byte test
can recognise, because the text is inside an archive. These are declared by
suffix in `documents.FORMATS` (currently `.ods`) and bypass the binary check.
Anything needing a third-party parser does not qualify — that is why PDFs are
in `DEFAULT_EXCLUDE_GLOBS` rather than supported.

### 2) Never silently lose a line
Every physical line becomes a `LogEntry` with its raw text preserved. A line no
format recognises is still **searchable**. Severity and time filters may hide a
line that demonstrably lacks a level or timestamp — and when they do, the UI
must **say so and count it**. An empty pane always explains itself.

### 3) Bounded work, whatever the file size
Opening a source seeks **backwards from the end**; nothing ever reads a whole
file. Tailing reads only appended bytes and renders only new lines. Memory is
capped by `max_buffer_lines` regardless of on-disk size. Discovery runs off the
UI thread and is capped by `max_files`.

**Two exceptions exist, both because deflate has no cheap tail**, and both
state so in their own module docstring rather than leaving it to be discovered:

*Container documents* invert two of these rules on purpose. `DocumentReader`
extracts the document whole, bounded by a **line** budget rather than a byte
one, and re-extracts on change instead of tailing. It also keeps the **first**
lines rather than the last: a spreadsheet's header row names its columns, and
a document has no "newest" end the way a log does.

*Compressed members* (`compressed.py`) are read forward under a line budget and
a **decompressed-byte cap**. Memory stays bounded by the budget; work is
proportional to the member, which is the part that cannot be fixed and must
therefore be said out loud. They keep the **last** lines, unlike a document —
this is a log, and its newest content is at the end. A rotated set spends one
shared budget newest-member-first, so older members are often never opened at
all, and only the live member is ever polled.

**Distance does not relax any of this**, and adds two clauses of its own.

*Round trips are a bounded resource too.* Discovering a remote root is **one**
command, not one per file. A per-file round trip is what makes reading 400
remote files unusable, and it is the specific cost an `sshfs` mount pays. The
budget is asserted by a test that counts commands, not by review.

*No remote IO on the event loop — ever.* `poll()` runs on a `set_interval`
timer at `refresh_hz` and must never perform a round trip; a remote source is
followed by a persistent process whose stdout is drained non-blocking, the way
`JournalReader._drain` already does. Everything one-shot — opening a source,
connecting, probing — runs in a worker thread, as discovery already does. A
frozen UI is the failure mode this clause exists to prevent, and the obvious
implementation of every remote operation is the blocking one.

**A third exception, and it is a seam rather than a format.** Every read of a
source goes through a `SourceBackend` (`services/backend.py`). A backend may
not silently substitute an unbounded read for a bounded one, nor a blocking
call for a cheap one: it **declares** its costs with `@cheap` / `@blocking`,
`BackendCapabilities.blocking` is derived from those marks rather than written
by hand, and `SourceBuffer.poll` runs inside `cheap_only()` so a declared-
blocking call raises `BlockingCallError` instead of stalling the timer.
`stat` and `identity` are cheap on every backend — that is what lets tailing ask
"did this grow?" from the event loop — and everything else belongs in a worker.

### 4) Nothing off-screen, ever
Layout must scale cleanly from 80 columns up. Every control stays on screen and
keyboard-reachable at every supported width. Horizontal groups are built from
`1fr` children so they **divide** available width rather than demanding a fixed
amount. Fixed-width control clusters in a shared row are how the previous build
pushed its action buttons off the edge — do not reintroduce them.

### 5) Single UX for desktop & headless
No mouse-only affordances; every action has a keyboard path.

### 6) Minimal dependencies
Python 3.11+, Textual, and Rich (which ships with Textual). A new dependency
must justify its size, maintenance burden, and availability on enterprise
distros.

---

## Architecture

| Layer | Location | Owns | Must not |
| --- | --- | --- | --- |
| **App shell** | `clv/app.py` | Layout, routing, lifecycle, breakpoints | Parse, filter, read files, or define widget visuals |
| **Services** | `clv/services/` | Source identity (`refs.py`), source IO (`backend.py`), parsing, filtering, discovery, reading, buffering, config, settings-file editing (`settings_file.py`), source management | Touch the UI or import Textual, or reach past `backend.py` to `os` |
| **Widgets** | `clv/widgets/` | Self-contained UI + own `DEFAULT_CSS` | Depend on other widgets' internals or import `clv.app` |
| **Plugins** | `clv/plugins/` | Extension interfaces + loader; also the two things that spawn a subprocess and so need consent — `sources/journald.py` and `sources/ssh.py` (the SSH transport and `RemoteBackend`) | Break interface contracts, or spawn anything before the operator opts in |
| **State** | `clv/storage.py` | JSON session persistence (atomic), including `SavedView` records | Depend on the UI |

### Services
- `parsing.py` — multi-format line parsing, canonical severity vocabulary,
  continuation carry-forward. Whatever structure a matcher captured beyond the
  timestamp and level is carried on `LogEntry.fields`, under key names
  normalised across formats — `host` means the same thing whether it came from
  syslog or from an access log. Values are strings and are never coerced; a
  continuation inherits timestamp and level but never fields.
- `session.py` — the readers and buffers behind the pane, and **the two things
  a source's machine contributes**. `SourceFacts` carries a remote source's node
  name, its measured timezone and its clock skew. `node` is tagged onto every
  entry as provenance (`host` is untouched and still means what the log says
  about itself); the zone and skew are applied **only in the merge sort key**,
  never to the entry, so a displayed timestamp is always exactly what the log
  said. The cross-zone branch engages only when the members' offsets actually
  disagree — a single-zone set, which every all-local merge is, takes the
  original path unchanged.
- `query.py` — the grammar behind `host:web01 status>=500`: tokenising,
  operators, and the rule that decides what is a field term at all. A token is
  one only when its key is a name the parser normalises or a key the buffer
  actually carries; everything else stays part of the regex, which is what
  keeps `sshd:` searching for text. A query with no recognised term is passed
  through byte-for-byte, so nothing anyone had saved changes meaning.
- `filtering.py` — `FilterSpec` → `FilterResult` with per-reason hidden counts.
  Also owns time parsing: `parse_relative_window` / `parse_absolute_window` for
  the presets, `parse_moment` for the single point `g` jumps to, and
  `align_moments` so an aware JSON stamp and a naive syslog one can be ordered
  rather than refused. Field terms come from `query.py`; an entry that lacks a
  field the query named is hidden into its own `hidden_missing_field` counter,
  never merged with "did not match".
- `discovery.py` — walks roots into a `DiscoveryReport`; pure and synchronous
  so callers can thread it. Every skip is attributed to exactly one of
  **`unsupported file type`** (CLV cannot display it), **`filtered out`** (the
  operator's own globs hid it), or **`unreadable`** (the read failed). Keep
  these apart: only the first is CLV's verdict on the file, and merging them
  back into one "excluded" count is what made the number unactionable. A
  *named* source that is skipped is also listed by path — a file the operator
  typed out must never disappear into a tally.
- `refs.py` — what a source *is*. `SourceRef` is the surface CLV requires of
  one, counted from every source-facing call rather than guessed; `Path` is one
  implementation and no longer the assumed one. Two boundaries live here and
  are kept apart deliberately: `parse_ref` / `format_ref` are exact inverses
  that never touch the filesystem, expand `~` or consult the working directory,
  and are what a *persisted* source goes through; `normalize_ref` expands and
  absolutises what a *person typed*. Collapsing them is what turns
  `journal:all` into `$CWD/journal:all` on the next launch. Also owns
  `identity` / `ref_key` — one canonical form, replacing the two copies that
  used to live in `app.py` and `sources.py`. Three implementations now:
  `Path`, `RemoteRef` (`ssh:web01/var/log/syslog`) for a source on another
  machine, and `JournalRef` (`journal:unit/sshd.service`) for the systemd
  journal. Neither scheme type lives in the plugin that reads it, because
  `parse_ref` decodes `session.json` before any plugin is imported — a type
  registered at import time would decode the same file differently depending on
  load order. Both are pure identity and perform no IO; a backend answers for a
  remote ref and a *provider* answers for a journal one, which is the one
  asymmetry worth knowing. `JournalRef` carries its machine **before** the colon
  (`journal@web01:unit/sshd.service`), and that is not cosmetic: templated unit
  names contain `@`, so a node inside the selector would read `unit/getty` as a
  machine. Every local journal string is byte-identical to what the provider
  built with `Path` before the type existed, which is what lets an older
  `session.json` still mean what it meant.
- `backend.py` — where a source's bytes come from, and **what asking costs**.
  `refs.py` answered what a source is; this answers who reads it. `walk`,
  `list_dir`, `kind`, `access`, `open`, `stat`, `identity`, `classify`,
  `reachability` —
  `LocalBackend`'s behaviour is what discovery and reading did before, and
  `RemoteBackend` (`plugins/sources/ssh.py`) is the second implementation. The part that is not a refactor is the blocking contract: every method
  is marked `@cheap` or `@blocking`, the marks are what
  `BackendCapabilities.blocking` is derived from, and `cheap_only()` turns a
  blocking call on the event loop into an exception rather than a freeze. Two
  members carry reasoning worth knowing: `identity` is an **opaque** comparable
  whose `None` means "this backend has no stable answer" (rotation then degrades
  to shrink-only, and says so), and `walk` yields files only, lazily, so
  `max_files` bounds work rather than merely output. `classify` is the one
  member that takes a **list**: discovery has to ask "readable? binary? a valid
  archive?" of every file it found, and asking per file is a round trip per file
  — locally the same open it always did, remotely one command per batch. It
  returns *bytes*, never a verdict, so `reader.looks_binary_block` and
  `compressed.probe_block` stay the only place the rule lives. `reachability`
  is cheap for a different reason from the others: it is not an operation but a
  report of what the last one learned, and it must never probe — the status line
  reads it on every render. It exists because `SourceBuffer.poll` swallows
  `OSError` on purpose, which is right for a rotated local file and silent for a
  dropped connection.
- `reader.py` — BOM-based encoding detection, bounded backwards reads,
  incremental tailing, rotation and truncation recovery. Tailing verifies that
  it is reading a **continuation**: metadata alone cannot say so, because
  `copytruncate` keeps the inode and a deleted-and-recreated log can be handed
  the same one back. `ANCHOR_SIZE` bytes at the read boundary are re-read as
  part of the read that was happening anyway, and a mismatch re-primes. Both
  cases previously showed a fragment of the new file and dropped the rest,
  silently. `open_reader()` picks
  between `SourceReader` (streams) and `DocumentReader` (container documents);
  both expose `path` / `prime()` / `poll()` and a `RELOAD_NOTICE` template.
- `documents.py` — stdlib-only text extraction for container formats.
- `compressed.py` — `gzip`/`bz2`/`lzma` members, bounded by a line budget and a
  decompressed-byte cap. The second stated exception to Requirement 3.
- `rotation.py` — what makes `app.log`, `app.log.1` and `app.log.2.gz` one
  source. Grouping is by name after the compression suffix is stripped; reading
  spends one shared budget newest-first, so a set whose head fills the buffer
  opens as fast as a single file. Lines come back carrying which member they
  came from, because with several files behind one source "where is this line
  from" stops having a constant answer.
- `session.py` — who owns the readers and the lines they produced, including
  the k-way merge behind `u`. A
  `SourceBuffer` is one reader plus its parser and its bounded deque; a
  `SourceSession` is the ordered set of buffers the pane is showing. **A single
  open log is a session of one**, which is the point: there is no separate
  single-source path for a feature to be written against by accident. Marks and
  watch answers key on `origin_of(entry)` rather than on "the open log", so two
  identical lines from two logs stay two lines. The tail *clock* stays in the
  app — `poll()` is called from the timer that already runs, never a second one.
  A merge is a **view** over the buffers rather than a fourth copy, cached
  against their revisions so it costs one merge per poll rather than one per
  keystroke; `max_buffer_lines` applies per member. An entry with no timestamp
  is anchored after the last timestamped line from **its own source** and
  counted, never dropped — "never silently lose a line" applied to ordering.
- `config.py` — settings resolution, validation, clamping. Two section kinds:
  `[log_viewer]` for the session, and one `[ssh:<name>]` per remote host, parsed
  into a `RemoteHost` whose per-host overrides fall back to the global ones.
  Nothing here connects to anything — `enable_ssh` (default **false**) is the
  master switch, and hosts parse whether it is on or not, because a mistake
  should be reported the launch it is made. Anything CLV cannot honour becomes a
  `ConfigIssue` rather than a raise or a silence; `app.py` prints them beside the
  plugin errors. **There is no password option and no sudo option**, and the
  schema refuses both by name with a pointer to ssh-agent and to group/ACL
  membership — the one place those requirements cannot be forgotten.
- `ssh_config.py` — reads OpenSSH's `~/.ssh/config` so its machines can be
  offered for import, and **never writes a byte back**: that file belongs to
  OpenSSH and to the operator. Only the *alias* is imported. `HostName`, `User`,
  `Port` and `IdentityFile` are shown in the picker and deliberately not
  persisted, because CLV connects by running `ssh`, which reads the same file —
  writing `host = 10.0.0.9` into `settings.conf` would stop `ssh` matching the
  `Host` block and silently lose `ProxyJump`, per-host keys and `known_hosts`.
  Keeping the alias as the destination is what makes "inherits `~/.ssh/config`
  wholesale" true rather than aspirational. It also settles the refused-key list
  structurally: `SSHConfigHost` has no field a password or a sudo flag could land
  in, however that file spells them.
- `config_upgrade.py` — the one place in CLV that rewrites the operator's
  settings file, and a narrowing of the old rule rather than an abandonment of
  it: **the launch path still never writes.** It runs on `clv --upgrade-config`
  or from `install.sh`, after copying the previous file aside. Every
  `[ssh:<name>]` section is carried across **byte-identical, including one the
  parser rejects** — regenerating sections from parsed state would silently
  delete a host with a bad port.
- `settings_file.py` — editing `settings.conf` in place without losing the
  operator's comments. `configparser` reads that file and nothing writes it
  back through `configparser`, whose `write()` discards every comment and
  blank line. Every operation is scoped to a named section, because an
  unscoped append lands at end of file — which, once one `[ssh:<name>]`
  section exists, is *inside the last host*. Editing is key-level and never
  regenerates a section, so comments, per-host budgets, options this version
  does not know about, and a refused `password =` still earning its warning
  all survive an edit untouched. Removal never touches a line above a header.
- `sources.py` — session source management and settings persistence, both
  routed through `settings_file.py`.
- `export.py` — the three built-in output formats (JSON Lines, CSV, plain text)
  and the atomic write behind `Ctrl+E`. Core rather than drop-in plugins so a
  built-in cannot fail to load and the drawer's plugin count keeps meaning
  "installed plugins"; `clv/plugins/exporters/` is still a live extension point.
  Does **not** import `clv.plugins` — that dependency already runs the other way.
- `marks.py` — the lines an operator bookmarked with `m`. Keyed by source path
  plus a digest of the line's raw text, **not** by buffer index: the buffer is a
  bounded deque, so an index-keyed mark would silently start pointing at a
  different line as lines were evicted. `MarkSet` is deliberately not
  serialisable — a digest is derived from log content, and session state holds
  paths and settings only.
- `watch.py` — the patterns `W` manages. `WatchIndex` answers "which rules did
  this line hit" **once per line**, keyed the way `marks.py` keys a bookmark, so
  a re-render is a dict lookup rather than a rule sweep over every visible row.
  `WatchNotifier` coalesces: first match immediately, everything else in the
  window counted and reported together, because a rule matching every line is
  what makes people switch a feature like this off. Both are driven from the
  existing tail poll — no second clock.
- `timeline.py` — event volume over time, bucketed to whatever width the bar
  has. The bucket count *is* the width, so a resize is a different histogram
  rather than a reflow. Buckets are a fixed grid (an origin and a step) rather
  than ranges recomputed from the data, which is what lets a tailed line find
  its bucket by arithmetic; when an arrival falls outside the grid `extend`
  says so and the caller rebuilds, rather than guessing. An entry with no
  timestamp is counted in `undated` and reported, never placed.
- `clustering.py` — what `c` collapses. A line's *shape* is its message with the
  volatile tokens normalised away, plus its level and its source — so a WARN and
  an ERROR that read alike stay apart, and a merged view never folds two logs
  together. Field *values* are deliberately not in the shape: a differing request
  ID is exactly what must not split a cluster. Clustering is a **display
  transform, never a filter** — `expand()` gives every line back, and the
  guarantee is per cluster (its own members, in order, byte-identical) rather
  than over the whole list, because gathering a run into one row is the feature.
  `ClusterStream` is one code path for both the full render and the tail, so
  "incremental" has no second implementation to drift from. `normalise` is
  memoised: clustering re-runs on every keystroke in the query box, and shaping
  a full buffer costs ~115 ms cold against ~6 ms warm.
- `clipboard.py` — assembles and size-caps the payload `y` hands to
  `App.copy_to_clipboard`. OSC 52 has no continuation form, so an oversized
  payload is truncated at a line boundary and reported, never chunked and never
  silently dropped.

### Data flow
```
config.load_config ─→ SourceManager ─→ discovery.discover (thread)
                                              ↓
                                        DiscoveryReport ─→ tree
                                              ↓
   reader.prime/poll ─→ parsing.LogParser.feed ─→ session.SourceBuffer
                                              ↓
                            session.SourceSession (one buffer, or several)
                                              ↓
        plugins.apply_filters ─→ filtering.filter_entries ─→ LogView
                                                               ↓
                                                cursor ─→ DetailPane
```

---

## Styling rules

- **CSS is the only place layout is decided.** No module assigns `.styles.*` at
  runtime. The single exception is the user-adjustable source-tree width, which
  is user state rather than a layout decision.
- Each widget owns its `DEFAULT_CSS`. App CSS covers the shell only and must
  not restate widget-internal rules.
- **`DEFAULT_CSS` is scoped to its widget.** A selector rooted at
  `LogViewerApp` will *not* match from inside a widget's own CSS. Responsive
  rules therefore key off a breakpoint class the app **mirrors onto the
  widget** (`QueryBar.-compact`, not `LogViewerApp.-compact QueryBar`).
- Breakpoints: `-compact` (<90 cols), `-narrow` (<130), `-wide` (≥130).

---

## Message contracts

| Origin | Message | Purpose |
| --- | --- | --- |
| `QueryBar` | `ActionTriggered` | Run / Clear / Save / Add Source fired |
| `QueryBar` | `TimeWindowChanged` | Time window changed (carries bounds for a custom range) |
| `QueryBar` | `SeverityChanged` | Severity bucket changed |
| `QueryBar` | `CustomRangeRequested` | Open the custom range dialog |
| `LogView` | `CursorMoved` | The line cursor moved; carries whether it is on the last entry |
| `LogView` | `EntrySelected` | `Enter` on a line — open the detail pane on it |
| `TimelineBar` | `BucketSelected` | `Enter` (or a click) on a bucket — narrow the time window to it |
| `TimelineBar` | `WidthChanged` | The bar has room for a different number of buckets than it holds |
| `LogView` | `ClusterToggled` | `Enter` on a collapsed (or opened) repeat group — the app owns which are open |
| `SegmentedButtons` | `ValueChanged` / `Reselected` | Segment activated / re-activated |
| `FilterChip` | `Dismissed` | Revert the named filter |
| `AdvancedFiltersDrawer` | `SettingsChanged` | Full before/after snapshot; `needs_rescan` says whether discovery must re-run |
| `AdvancedFiltersDrawer` | `ViewToggleChanged` | Auto-scroll / structured / clipboard / detail pane / watch rules flipped from a drawer switch |
| `ExportDialog` | dismiss value | `ExportRequest(key, path, marked_only, clustered)`, or `None` when canceled |
| `SaveViewDialog` | dismiss value | The name to save the current filters under, or `None` |
| `ViewPickerDialog` | dismiss value | `ViewRequest(action, name, new_name)`, or `None` when closed. The dialog never edits state; the app acts and reopens it |
| `WatchRulesDialog` | dismiss value | The edited rule set, or `None` when nothing changed — so a dialog that was only looked at costs no re-indexing |
| `RemoteHostsDialog` | dismiss value | The full host list, or `None` when nothing was edited. The dialog holds a working copy and hands back the whole thing, so Escape genuinely discards; the app diffs *records* against the file, which is what leaves a section the parser skipped in place |
| `SSHConfigImportDialog` | dismiss value | The `~/.ssh/config` aliases the operator ticked, or `None` when none were — never an empty tuple, so "cancelled" and "picked nothing" stay one fact |
| `AdvancedFiltersDrawer` | `ScanSSHConfigRequested` | Look in `~/.ssh/config` for machines to import. Carries nothing: the drawer does not read that file, know what a host is, or write `settings.conf` |
| `AdvancedFiltersDrawer` | `RescanRequested` / `Closed` | Explicit rescan / dismissal |

### Controls with two homes

Auto-scroll and structured output appear in the query bar when it is wide
enough to merge its rows, and in the Advanced drawer's **View** section when it
is not — exactly one copy is ever visible. The app owns the state; both
controls funnel through `_set_auto_scroll` / `_set_structured`, and
`_sync_view_toggles` pushes the result back to both.

Mirroring uses `prevent(Switch.Changed)`, **not** a boolean guard. Textual posts
`Switch.Changed` asynchronously, so a flag cleared at the end of the sync method
is already back to `False` by the time the handler runs, and the echo arrives
looking like a fresh user action.

The **Clipboard (OSC 52)** switch is the counter-example: it has one home, so it
lives in the drawer's own `#output-options` container rather than in
`#view-toggles`, which is hidden once the query bar is wide enough to show its
copies. A single-home control placed in that container disappears above 148
columns.

Cross-module communication goes through messages or public methods — never
shared globals or reaching into another widget's tree.

---

## Plugins

Three interfaces in `clv/plugins/__init__.py`:

| Interface | Method | Purpose |
| --- | --- | --- |
| `LogSourceProvider` | `discover()`, `open(path)`, optional `open_reader(path)` | New ingestion backends |
| `FilterStage` | `apply(entry, context) -> LogEntry \| None` | Transform or drop entries |
| `Exporter` | `export(entries, context) -> ExportResult` | Send the current view somewhere |

Loaded from `clv/plugins/{sources,filters,exporters}/` drop-ins (via a
`register()` function or `__all__`) and from the `clv.plugins` entry point
group. Optional `requires_clv` constraints are enforced.

**Loading never raises.** Import failures, bad version constraints, and stages
that throw at runtime are recorded in `PluginRegistry.errors`, surfaced in the
Advanced drawer, and skipped. A third-party plugin must never stop CLV starting
or break a render. The same contract covers providers: one that raises in
`discover()` or `open()` costs only its own sources, never the operator's real
ones.

`open()` returns an iterator, which cannot express tailing, cannot be asked to
stop, and has nowhere to put cleanup. A provider that follows a live stream
implements `open_reader()` instead and returns the same `path`/`prime()`/
`poll()`/`RELOAD_NOTICE` surface every core reader has, plus `close()` when it
holds something. A provider that only implements `open()` still works — core
wraps it in `IteratorReader` — which is what let this be added without breaking
anything already written against the interface.

**A provider source is not a path, but it now carries a ref.**
`ProviderSource` is its own type — the tree node's payload, holding the label
and the provider name error attribution needs — and its `path` is the identity.
For the journal that is a `JournalRef`, which is why a unit can be starred,
merged with a file, and restored. Glob filtering and rotated-set grouping still
refuse one and now do so **by name**: a journal has no directory to walk and
nothing that rotates, and both used to hold only because a provider source was
not a ref at all. What still cannot reach the starred set is a provider source
whose `path` is not a concrete ref type — `refs.SOURCE_REF_TYPES` is a closed
union of types rather than a duck test, precisely so that stays true.

**The journal is a plugin because of consent, not because of layering.**
Reading it runs `journalctl`, and a plugin may not spawn a subprocess without
the operator asking; core shipping that would put a subprocess behind a default.
`enable_journald` is read fresh on every scan, so the drawer switch takes effect
without a reload. The one cost is that the drawer's plugin count now includes a
shipped provider, which Item 3 wanted to keep meaning "plugins someone
installed" — the trade Item 12 asked for.

---

## Testing

- Services are UI-free and unit-tested directly.
- Widget and app behavior is tested headlessly via `App.run_test()`.
- `tests/conftest.py` redirects `HOME` and `XDG_CONFIG_HOME` per test. **Never
  remove this** — without it the suite reads and writes the developer's real
  session and settings files.
- Layout regressions are caught by asserting widget `region` bounds at a given
  terminal size rather than by eyeballing screenshots.

- **The default run never touches a network**, and the one suite that does is
  opt-in: `tests/containers/run.sh alpine` / `gnu`, which builds a throwaway
  sshd container, runs `pytest -m remote_integration` against it and tears it
  down. Deselected by `addopts`, never part of a gate. It is the only thing that can catch a BusyBox `find` with no `-printf`
  or a BSD `stat`, because a fake runner returns whatever fixture it was given.
- `tests/test_backend.py`'s `BackendContract` is written against the protocol and
  knows nothing about the local filesystem. Subclass it, override the `backend`
  and `workspace` fixtures, and every assertion runs against another backend —
  which is how `RemoteBackend` is held to the same behaviour as `LocalBackend`.

Run: `python -m pytest` (1469 passed, 1 skipped, 11 deselected) on **both** 3.11
and 3.14 — the local default is 3.14 and a green suite there is not evidence
that the supported floor still works.

---

## Security & Privacy

- Read only what the operator configured or explicitly selected.
- **No telemetry and no exfiltration**, absolutely. Network access is limited to
  hosts the operator names, over SSH, using their own credentials, initiated
  only by an explicit action, and never with elevated privilege. CLV reports
  nothing anywhere, to anyone, ever — that part is not narrowed and will not be.
- **No privilege escalation, anywhere.** No `sudo`, `doas` or `pkexec`, local or
  remote, not behind a setting. An unreadable file is reported, with the group
  or ACL that would fix it; it is never read by becoming someone else.
- **No credentials.** No password field in the config schema, in any dialog, in
  `SessionState`, or in memory. A connection that needs interactive input fails
  as unreachable. Host key verification is never disabled, not even for testing.
- Treat log contents as sensitive; never copy them into caches or temp files.
  Session state stores paths and filter settings, never log content — which
  is why marks (`services/marks.py`) live for the session only.

---

## Non-Goals (for now)

- **Collection infrastructure.** Unattended collection, agents or daemons on a
  remote host, store-and-forward pipelines and spooling, and privileged
  operations anywhere. CLV reads on demand, over a connection the operator
  already has, and installs nothing and leaves nothing running at either end.
- Transports other than SSH — no syslog receiver, no HTTP log API, no
  cloud-provider log service. Each of those is a different product.
- Writing to a remote host, or to any source. Read-only, always.
- Credential management: no password storage, no key generation, no agent
  management. CLV uses the SSH setup the operator already has.
- Heavy parsing DSLs or schema-aware pipelines.
- Background daemons or privileged operations. The opt-in plugin isolation host
  planned in [PLUGIN_TODO.md](PLUGIN_TODO.md) Phase 13 is neither: it lives and
  dies with the viewer, runs at the operator's own privilege and never above it,
  and exists so a plugin that hangs can be *killed*. It is not a sandbox, and
  `clv/plugins/AGENTS.md`'s trust model says so in those words.

### Reversed

Kept rather than deleted, so the argument survives rather than being erased —
the rule stated at the head of [TODO.md](TODO.md).

- **"Network collection, multi-node aggregation, remote tailing."** *Reversed
  2026-08-16* by [SSH_TODO.md](SSH_TODO.md). The objection was to CLV becoming
  collection infrastructure — an agent to install, a daemon to run, a spool to
  manage, a privilege to hold — and that objection stands unchanged; it is the
  first bullet above. What changed is that none of it is required to read a
  folder on a machine the operator can already `ssh` into. A viewer called
  *Centralized* Log Viewer that can only read the machine it runs on has
  centralised nothing. The narrowed refusal is on-demand reads versus
  infrastructure, not local versus remote.

---

## Quick Reference

- **Start here**: `clv/app.py`, then `clv/services/`.
- **Primary UI**: source tree (left), log output (right); at `-compact` they
  swap via `Ctrl+B`. The log pane (`LogView`) carries a line cursor, and the
  `DetailPane` beside/below it renders whatever that cursor is on.
- **Keep it fast**: bounded reads, incremental renders, threaded discovery.
- **Keep it honest**: never drop a line silently; explain every empty pane.
  - An **unreachable source is reported, never rendered as an empty one**. A
    pane that goes quiet because a link dropped is indistinguishable from a log
    that stopped, and the two are not the same fact.
  - An **ordering across machines is only as trustworthy as their clocks, and
    says so**. Merging was local-only, so there was one clock and one timezone;
    across hosts both assumptions fail silently and the operator reads causation
    out of a wrong interleaving.
  - **Every view of an ordering uses the same rule.** The pane, the timeline
    histogram and any future summary order the same lines the same way or they
    describe two different logs. `SourceSession.moment_mapper` is that one
    decision; a second copy of it is two places for them to disagree.
- **Keep it consistent**: identical keyboard and mouse paths everywhere.
