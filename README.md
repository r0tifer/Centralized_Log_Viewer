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
- 🌐 **Anywhere includes another machine.** Name a host in `settings.conf` — or
  press `R` and add it — and its folders appear in the same tree as the ones on
  this disk, discovered recursively, tailed, filtered, starred and merged. It
  reads over `ssh` with the setup you already have: your agent, your keys, your
  `~/.ssh/config` with its `ProxyJump` and `known_hosts`. There is no password
  option and no `sudo` option, nothing is installed on the remote and nothing is
  left running there, and none of it happens until you turn `enable_ssh` on —
  see [Remote sources over SSH](#remote-sources-over-ssh).
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
- 📊 **See when it started.** `b` draws a one-row histogram of the filtered set
  above the log, coloured by the worst severity in each bucket. It is a control
  rather than a picture: `←`/`→` and `Enter` narrow the time window to the spike
  you are pointing at, which is an ordinary custom range you can dismiss like
  any other filter.
- 🔖 **Mark the lines that matter.** `m` bookmarks the line under the cursor,
  `M` steps between the marks, and `Ctrl+E` can export just those. Marks are
  keyed by content rather than position, so they survive filtering and tailing —
  and they are session-only, never written to disk.
- 🧹 **Collapse the noise.** `c` folds repeated lines into one `×147` row by
  normalising the volatile tokens — IDs, IPs, durations, paths — out of them.
  Nothing is hidden: `Enter` expands a cluster in place and every line inside
  is still selectable, markable and exportable.
- 📤 **Get the view out.** `Ctrl+E` writes the filtered entries as JSON Lines,
  CSV or raw text — the whole filtered set, not just the lines on screen. `y`
  copies the selected line — or the whole visible view — to your local clipboard
  through the terminal, so it works over SSH and tmux where a mouse selection
  does not.
- ⧉ **Merge several logs into one stream.** `x` adds a log to the merged set,
  `u` opens the set as one timestamp-ordered pane with a source column. Filters,
  navigation, marks, the detail pane and export all work there exactly as they
  do on a single file. A set may span machines: with SSH configured, local and
  remote logs interleave in one pane, and `node:` says which machine each line
  came from.
- 🧩 **Plugins.** `LogSourceProvider`, `FilterStage` and `Exporter` interfaces,
  loaded from `clv/plugins/` or from installed packages via the `clv.plugins`
  entry point group. A broken plugin is reported, never fatal. A plugin is
  **trusted code** — it runs with your privileges, in CLV's process, and can
  read every log CLV can open; install one the way you would install any other
  program. `clv/plugins/AGENTS.md` has the trust model in full.
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

The binaries are built on AlmaLinux 8, so they need **glibc 2.28 or newer** — RHEL/Rocky/Alma 8+, Debian 11+, Ubuntu 20.04+, and anything more
recent. The build fails rather than ships if that floor creeps upward. Nothing
is bundled that a system tool will be made to load: CLV strips its own library
path before running `journalctl`, so a bundle built on one distribution cannot
break a binary belonging to another.

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
| `cluster_lookback` | How far back, in entries, `c` may reach to fold a repeated line into a cluster. A bound, not a taste: it keeps one cluster from spanning a session. | `200` |
| `enable_journald` | Offer the systemd journal as a source. Off by default: reading it runs `journalctl`, and CLV spawns no subprocess unasked. The drawer's switch writes this line for you. | `false` |
| `enable_ssh` | Read log folders on machines named in `[ssh:<name>]` sections. Off by default, and for a stronger version of the same reason: a remote source spawns a *network* subprocess. With it false nothing connects, however many hosts are configured. | `false` |

Invalid values fall back to safe defaults; the app never fails to start because
of a malformed settings file. Most discovery options are also editable at
runtime in the **Advanced** drawer.

### Remote host sections

One `[ssh:<name>]` section per machine, alongside `[log_viewer]`. Everything
except `log_dirs` is optional, and every option left out falls back to the
global value above. See [Remote sources over SSH](#remote-sources-over-ssh) for
what these do and why two options you might look for are absent.

| Option | Purpose | Default |
| --- | --- | --- |
| *(the section name)* | CLV's name for the machine: what the tree shows, what `node:` matches, and the address connected to unless `host` overrides it. | *(required)* |
| `host` | The address or `~/.ssh/config` alias to connect to, when it differs from the name. | *(the section name)* |
| `user` | SSH user. Left out, `ssh` decides — from `~/.ssh/config` or your local username. | *(unset)* |
| `port` | SSH port. Outside 1–65535 the host is skipped and reported, never clamped: clamping would connect somewhere you never named. | `22` |
| `identity_file` | A private key to offer. An unreadable one warns and keeps the host, since ssh-agent may already hold the key. | *(unset)* |
| `log_dirs` | Folders and/or files **on that machine**, comma separated. Must be absolute; a relative entry is ambiguous and refused. | *(required)* |
| `enabled` | Whether this host is read at all. `space` toggles it in the `R` dialog. | `true` |
| `include_globs` / `exclude_globs` | Per-host overrides of the global glob lists. An empty value means "explicitly no filtering", which is not the same as leaving the line out. | *(inherit)* |
| `max_files` | Per-host discovery budget, so one noisy machine cannot consume the whole allowance and truncate the others. | *(inherit)* |
| `max_buffer_lines` | Per-host history budget. The pressure valve for a slow link. | *(inherit)* |
| `correct_clock_skew` | Apply this host's measured clock offset when ordering a merged view. Skew is always *reported*; correcting it is opt-in, and the pane says when it is on. | `false` |

The `R` dialog edits the first seven; the rest are file-only. That costs them
nothing — writeback is key-level and never regenerates a section, so options the
dialog does not show, comments you wrote, and even a line this version refuses
all survive an edit untouched.

**Two options do not exist**, and their absence is enforced by the schema rather
than by convention: there is no password key of any spelling, and no `sudo` key
of any spelling. Writing one is reported as unsupported and the value never
reaches CLV's model of the host. See the section on
[authentication](#authentication-your-agent-and-your-keys-and-nothing-else) for
what to do instead.

---

### Upgrading

Your settings file is created once, from the template shipped with whichever
build you first installed. That template is two thirds prose — every option is
introduced by the comment block explaining what it does — so a file created two
years ago is still handing you two-year-old documentation, even though every
release since may have added options.

Nothing on the **launch path** ever rewrites that file. CLV does not edit your
configuration behind your back while starting up. What it does instead is tell
you, once per version, which settings your file does not carry, in the discovery
summary.

Closing the gap is an explicit action:

```bash
clv --upgrade-config            # fold your settings into the newer template
clv --print-default-config      # or just read the newer template
clv --version                   # which build you are actually running
```

`--upgrade-config` rewrites `~/.config/clv/settings.conf` from the shipped
template and:

- **keeps every value you set** — written into the new template, so it arrives
  surrounded by the current prose rather than the old;
- **keeps every `[ssh:<name>]` host**, copied across byte for byte, including a
  host CLV itself cannot parse;
- **keeps options this version no longer documents**, under a
  `# --- Carried over from your previous settings file` banner, rather than
  silently dropping them;
- **does not keep comments you wrote yourself** inside `[log_viewer]`. The new
  template is the base, and there is nowhere sensible to reattach a note about
  an option whose surrounding prose has been rewritten.

Because of that last point it always saves the previous file first, as
`~/.config/clv/settings.conf.bak-<timestamp>`. It is also a no-op when there is
nothing to do: the file carries a `config_version` marker, and a file already at
the current version is not even touched.

`install.sh` runs `--upgrade-config` for you after installing, so an upgrade
picks this up without a second command. Skip it with:

```bash
./install.sh --no-config-upgrade      # or CLV_NO_CONFIG_UPGRADE=1
```

Under `sudo`, the installer runs the upgrade as the invoking user rather than as
root, so it updates your settings file and not `/root`'s.

None of this is required. Every setting your file does not carry is already in
effect at its default, and remote hosts are managed from `R` and the Advanced
drawer without touching the file at all. If your machines are already named in
`~/.ssh/config`, **Scan SSH config** in the Advanced drawer lists the aliases
that are not configured yet and writes the ones you pick — it imports the alias
and a `log_dirs` line, leaving `ssh` to apply your own `Host` block.

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

### Merging sources

The name promises centralized logs, and until now it delivered centralized
*discovery* — you still read one file at a time. `x` on any log in the tree adds
it to the **merged set**; `u` opens the set as one timestamp-ordered stream:

```
⭐ Starred
  ⭐ logs/auth.log            alpha.log   2026-08-11 10:00:00 INFO  request accepted
⧉ Merged (2 sources)   ←     beta.log    2026-08-11 10:00:01 INFO  upstream connect
  ⧉ logs/alpha.log     click alpha.log   2026-08-11 10:00:02 ERROR upstream timeout
  ⧉ logs/beta.log      to open beta.log  2026-08-11 10:00:03 WARN  retrying
📂 /var/log
   ⧉📄 alpha.log
   ⧉📄 beta.log
    📄 auth.log
```

The set is repeated as a group below the starred logs, so it is one keystroke
away however deep its members are buried; each member also carries a `⧉` where
it sits in the folder tree. The row carries its verbs as separate click
targets, so a click can say which one it meant:

| On the `⧉ Merged` row | | Keyboard |
| --- | --- | --- |
| `⧉` | Open the set as one stream | `u` |
| `✎` | Save the set under a name | `V` |
| `✕` | Empty the set, to start another | `X` |
| the name | Expand / collapse its members | `Enter` |

Selecting one member below it opens just that log, which is also sometimes what
you want. Every group in the tree — Views, Providers, Starred, Merged — arrives
collapsed, because a shortcut that unfolds itself pushes the rest of the tree
off screen. A source column names the origin of every row
(abbreviated as the terminal narrows), and the status line names the set.
Adding or removing a source edits those rows in place — it never re-runs
discovery, and it never collapses folders you had opened. **Every other feature works exactly as it does on a single log** — filters,
`n`/`N` navigation, `g`, marks, the detail pane and `Ctrl+E`. That is the point:
merging is not a mode with its own reduced feature set. The origin travels as a
field, so `source:beta.log` is a query you can write, and it shows up in the
detail pane's property list for free.

Some specifics worth knowing:

- **`max_buffer_lines` applies per source**, so three merged logs cost three
  buffers and no member is crowded out by a louder one. The merged stream is a
  *view* over those buffers, never a fourth copy of the lines.
- **Lines without a timestamp are never dropped.** They are anchored directly
  after the last timestamped line from their own source — which is what keeps a
  stack trace attached to the error above it — and the status line counts them,
  because they were placed by inference rather than by their own clock.
- **Mixed timezone-aware and naive timestamps merge** rather than refusing to.
  If any source is naive the offsets are dropped, matching what the time-window
  filter already does; if every source is aware they are kept, so two time zones
  order correctly.
- **Only members you merged are polled**, at the same rate one source was. A
  merged view does not multiply the poll frequency by the member count.
- A member that disappears from disk is reported and the rest keep going.
- The set persists in `session.json` and can be captured in a saved view.

**Naming a set.** A merged set is not limited to one: `✎` on the row (or `V`)
saves it as a named view, `✕` empties the working set so you can build the
next one, and `v` (or the **Views** group at the top of the tree) switches
between them. Renaming and deleting a saved set live in that picker — `r` and
`d`. Clearing the working set never touches what you saved. So `web tier` and `db tier` can be two different
groups of logs, each one keystroke away, and applying one moves the merged
group in the tree to match.

The set has to be **open** when you save — press `u` first. A view saved while
a single log is on screen deliberately records no set, so a view about one file
never drags someone else's merged group around with it. Note also that a view
is a filter bundle first: it captures your query, severity and time window
alongside the set, so save it with the filters you want to come back to.

**A merged set can span machines.** With remote sources configured, a local log
and a log on another host open in the same timestamp-ordered pane — comparing
one path across a fleet is what the feature is for.

**`Ctrl+X` builds that set in one press.** On any log, it gathers the same path
from every machine the last scan found it on and opens the result, rather than
making you press `x` on five leaves inside five separately collapsed host trees.
It reports what it did: how many sources it merged, which walked hosts do not
have that path, and which hosts it could not reach at all — three different
facts, kept apart, because a host that was unreachable has told CLV nothing
about its files and saying "not on web03" would be a confident answer with no
evidence behind it. When the members share a basename, the pane's source column
switches to naming the *machine*, since that is the part that differs.

Two things are worth knowing before you read causation out of the interleaving:

- **Ordering across machines is only as trustworthy as their clocks**, and CLV
  says so rather than hiding it. Clock skew between hosts is measured and
  reported beside the merged view's `anchored` count; correcting for it is
  opt-in per host, and when it is on the pane states that timestamps are being
  adjusted. The raw line is never rewritten either way.
- **`node:` is the machine CLV read a line from; `host:` is what the line says
  about itself.** Syslog, access logs and journald all normalise into `host`,
  so it keeps meaning exactly what it always did and no saved query changes
  meaning. `node:web01 status>=500` is the query you want across a fleet.

**Managing hosts.** `R` opens the host dialog: add, edit, enable, disable and
remove machines, with a **Test connection** that makes one bounded probe and
reports what it found. Changes are written back into `settings.conf` in place,
so the comments, per-host budgets and glob overrides you have written there
survive an edit untouched. The next section covers setup, the authentication
model and what a non-GNU remote gives up.

### Remote sources over SSH

A root may live on another machine. CLV connects with `ssh`, reads the files
there, and puts them in the same tree as the ones on this disk — discovered
recursively, opened, tailed, filtered, starred, merged. A remote log is not a
second-class source type; that is the whole point, and it is why this is core
plumbing rather than a plugin bolted on the side.

**It is off until you say otherwise.** With `enable_ssh = false` — the default —
nothing connects and no `ssh` process is spawned, however many hosts are
configured. Reading the journal spawns a subprocess; reading a remote host
spawns a *network* subprocess, which raises the bar for asking rather than
lowering it.

#### Setting a host up

Three routes, and none of them requires the other:

- **The dialog.** `R` lists your machines: add, edit, enable, disable, remove,
  and **Test connection**, which makes one bounded probe and reports what it
  found. Also reachable from the Add Source dialog (`a`).
- **Scan SSH config.** If your machines are already named in `~/.ssh/config`,
  the Advanced drawer's **Scan SSH config** lists the `Host` aliases that are
  not configured here yet and writes the ones you pick — the alias and a
  `log_dirs` line, nothing else, leaving `ssh` to apply your own `Host` block.
- **The file**, one `[ssh:<name>]` section per machine:

```ini
[log_viewer]
enable_ssh = true

[ssh:web01]
log_dirs = /var/log, /srv/app/logs
```

That is a complete host. The name after `ssh:` is CLV's name for the machine:
it is what the source tree shows, what `node:` matches in a query, and the
address connected to — so if the name is already a `Host` alias in your
`~/.ssh/config`, there is nothing else to write. Add `host =` only when CLV's
name for a machine is not the address to reach it at.

#### Authentication: your agent and your keys, and nothing else

CLV inherits `~/.ssh/config` wholesale — aliases, `ProxyJump`, per-host keys,
`known_hosts`, agent forwarding. A host you can already reach by hand is a host
CLV can read.

**There is no password option, and there will not be one.** No password field
exists in the settings schema, in any dialog, in session state, or in memory.
Every invocation carries `BatchMode=yes`, which turns every interactive prompt
into a clean failure with a usable reason instead of a process waiting on a
stdin nobody is reading. A connection that would need you to type something is
reported as unreachable, with the reason. Load your key with `ssh-add`, or point
`identity_file` at one.

**Host key verification is never disabled.** `StrictHostKeyChecking=no` and
`UserKnownHostsFile` appear in no argv CLV builds, not behind a flag and not for
testing — there is a test asserting they never will. The first connection to a
machine you have not trusted yet therefore fails, and says:

> the host key is not trusted. Connect once by hand to verify it: CLV never
> disables host key checking.

Do exactly that — `ssh web01` once, check the fingerprint, accept it — and CLV
works from then on. A key that has *changed* gets a different and louder message,
because that is a different fact.

**There is no `sudo` option either**, anywhere, local or remote. CLV reads as the
configured SSH user and never escalates. A log that user cannot read is reported,
naming the host and the path, with the fix: add the user to a group that can read
it (`adm`, `systemd-journal`) or set an ACL on the file. Reading a log by becoming
someone else is not something CLV will do on your behalf.

#### The connection itself

One multiplexed connection per host (`ControlMaster=auto`), so per-file commands
cost a round trip rather than a handshake. The socket lives in a `0700` directory
under `$XDG_RUNTIME_DIR`, is named from a hash rather than the host name (a long
FQDN overflows `sun_path`, and the failure mode is silent loss of multiplexing),
and is torn down explicitly with `ssh -O exit` when the host is reconfigured, when
`enable_ssh` goes off, and at shutdown. `ControlPersist` is 60 seconds as a
backstop for a CLV that was killed rather than quit — **a persisted multiplex
socket is a live authenticated connection any local process running as you can
ride**, so leaving one behind is a real exposure rather than an untidiness.

Nothing is installed on the remote host and nothing is left running there. CLV
issues bounded commands and one `tail -F` per open source, and that `tail` is
killed when the connection closes — including the case where an idle log means
it would otherwise never notice.

When a host goes away, the pane says so. A dropped link is not a log that went
quiet, and CLV will not render it as one: there is a toast, a status-line
segment, and an explanation in the empty pane. Reconnection is bounded — six
attempts at 1, 2, 5, 15, 30 and 60 seconds — and then it stops and tells you,
naming `Ctrl+R`. `Ctrl+R` resets the backoff and keeps the connections that are
still good.

#### Non-GNU remotes, and what degrades

`find -printf`, `stat -c` and `dd iflag=skip_bytes` are GNU extensions. BusyBox
and BSD do not have all of them, so capability is **probed at connect, never
assumed**, and the host gets one of four command profiles:

| Profile | `find -printf` | `stat` format | Ranged read |
| --- | --- | --- | --- |
| `gnu` | yes | `%d %i %s %Y` | `dd iflag=skip_bytes` |
| `busybox` | no | `%d %i %s %Y` | `dd iflag=skip_bytes` |
| `bsd` | no | `%d %i %z %m` | `tail -c +N \| head -c M` |
| `posix` | no | *(none)* | `tail -c +N \| head -c M` |

Only the last row loses anything you would notice. Rotation detection compares
`(device, inode)`, so a host with no readable `stat` degrades to comparing size
and modification time — which misfires on a log rotated inside the same second,
and CLV reports rotation conservatively rather than pretending. Everything else
is a different argv for the same result. Alpine containers are a first-class
target, not an edge case, and there is an opt-in test suite that runs against
one.

**Test connection** in the `R` dialog tells you which profile a host got, along
with its measured clock skew.

#### Over a slow link

Discovery of a remote root is **one** command, not one per file — that is the
specific cost that makes reading 400 remote files unusable, and there is a test
that counts commands so it cannot come back. Opening a source reads a bounded
tail; tailing transfers only appended bytes. `poll()` never makes a round trip
at all, so the UI does not stall twice a second per merged source.

Two per-host settings are the pressure valve, and both fall back to the global
value when absent:

- `max_files` — one noisy machine should not consume the whole file allowance
  and truncate the others.
- `max_buffer_lines` — five merged remote sources pull five times the history
  over the link on open. Lower it for a slow connection.

#### Known limitations

Honest rather than absent:

- **Plain-text export carries no machine column.** JSON Lines and CSV both carry
  `node`; the plain-text format deliberately does not, because prefixing a host
  onto a raw line would put text in an export that no log on any machine
  contained.
- **A clipboard copy carries no provenance.** `y` emits `entry.raw` only, which
  is exactly what an all-local merged copy has always done.
- **The `R` editor offers seven of the twelve host options.** `correct_clock_skew`,
  both glob overrides and both budgets are file-only, because twelve fields do
  not fit beside a list, a hint and a button row at 80 columns. Editing is
  key-level, so leaving them alone in the dialog preserves them exactly.

#### `sshfs` is a legitimate alternative

If the machine is already mounted, or you would rather mount it, CLV has always
been able to read a mounted remote folder and needs none of the code above to do
it — you get full fidelity, real inodes, and every feature, for no configuration
at all. What you pay is a per-file round trip during discovery, which is the
specific thing this feature exists to avoid on a tree of a few hundred files.
Neither answer is wrong. This one exists for people who do not want a mount.

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

### The systemd journal

On Linux the OS event log *is* the journal — binary, so no amount of file
discovery will ever find it. CLV reads it through a plugin that shells out to
`journalctl -o json --follow`, and offers:

| Source | |
| --- | --- |
| **System journal** | everything, as one stream |
| **This boot** / **Previous boot** | `--boot 0`, `--boot -1` |
| One node per `.service` unit | the closest thing to Event Viewer's *Windows Logs → Application / Security / System* |

Journal sources appear in a **Providers** group in the tree and behave like any
other source from there on: the same filters, cursor, marks, detail pane and
export. Because the plugin normalises the journal's own fields, field queries
work immediately — `unit:sshd.service`, `host:web01`, `pid:991`, and the raw
`_SYSTEMD_UNIT` spelling too.

**It is off by default, and turning it on is a decision you make explicitly.**
Reading the journal means running a subprocess, and CLV does not spawn one
unless asked — which is also why the journal ships as a plugin rather than as
part of core. With `enable_journald` false, nothing is spawned and nothing is
enumerated. Enable it either by setting `enable_journald = true` in
`settings.conf`, or with the **Journal (systemd)** switch in the Advanced
drawer, which turns it on and writes that line back to your settings file so the
choice is made once rather than every launch.

On a machine without systemd — or without `journalctl` on `PATH` — CLV runs
normally, the switch is disabled, and the drawer says why.

Two details worth knowing. A severity bucket is pushed down to
`journalctl --priority` where that actually filters something (`error`, `warn`,
`info`), so debug records are not carried across a pipe just to be discarded;
`debug` and `trace` map to priority 7, which is everything, so nothing is pushed
down and CLV does not pretend otherwise. And the initial read is bounded by
`--lines`, the journal's equivalent of the backwards seek CLV does on a file.

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

### The severity timeline

`b` opens a one-row histogram above the log: event volume over the time your
filtered lines cover, each column coloured by the **worst** severity in it.

```
▁▁▂▁▁▃▂▁▁▁█▆▃▁▁▁▂▁▁·······▁▂▁▁▁▂▁▁▃▁▁▁▂▁▁▁▁▂▁▁▁▁▁▁▂▁▁▃▂▁▁▁▁▁
2026-08-07 09:14:20–09:14:30 · 61 events · ERROR
```

Volume is the height of the block, not the colour, so the shape of a spike
reads on a monochrome terminal. A column where nothing happened is a `·` rather
than a gap, so the axis does not look like it stopped.

It is a **control, not a picture**. `←`/`→` move the selection, `Home`/`End`
jump to either end, and `Enter` narrows the time window to the selected bucket
— which is an ordinary custom range, so it appears as a `Time:` chip and is
dismissed like any other filter. Clicking a column does both at once. The bar
then re-buckets over the narrower window, so pressing `Enter` again drills in
further.

The histogram is built from the **filtered** set, so it answers "when did the
thing I am looking at happen" rather than "when did anything happen". It is
built from the buffer only — no second read of the file — and while it is
hidden it is not maintained at all. A source whose lines carry no timestamp
says so in the caption instead of drawing an empty rectangle.

Whether the bar is open is remembered between runs, along with a **Timeline**
switch in the Advanced drawer. Which bucket you had selected is not.

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

### Noise reduction

`c` collapses repeated lines into one row with a count:

```
  2026-08-07 09:25:01 - ERROR - connection refused for 10.0.0.5:5432 after 1.25s
  2026-08-07 09:25:01 - ERROR - connection refused for 10.0.0.6:5433 after 0.90s
  2026-08-07 09:25:02 - ERROR - connection refused for 10.0.0.7:5433 after 2.10s
  … 144 more like it …
  2026-08-07 09:27:14 - WARN  - disk almost full

  ▸ ×147 09:25:01→09:27:09  2026-08-07 09:25:01 - ERROR - connection refused …
  2026-08-07 09:27:14 - WARN  - disk almost full
```

Two lines collapse together when they look the same once the volatile tokens
are normalised away — quoted strings, timestamps, UUIDs, IPv6 and IPv4
addresses (with ports), hex, paths, floats and integers, applied in that order.
So `request 8821 took 12ms` and `request 8822 took 47ms` are one event, and the
line that is genuinely different stays on its own. Severity is part of the
match: a WARN and an ERROR that read alike stay apart, as do identical lines
from two different logs in a merged view.

**Collapsing never hides a line.** It is a display transform, not a filter.
`Enter` on a cluster row expands it in place and gives back exactly the lines
that went into it, byte-identical and in order; every one of them is then
selectable, markable and exportable like any other. `Enter` again closes it.
Marking a *collapsed* cluster is refused with a note asking you to expand it
first — one keystroke should not mark a hundred and forty-seven lines behind a
single gutter dot.

The row shows the count, the span from first to last, and the cluster's
severity in its colour. `Ctrl+E` gains a **Clustered** option that writes one
row per group with `cluster.count`, `cluster.first` and `cluster.last` as
fields; expanded output remains the default.

Clusters only form within `cluster_lookback` entries (200 by default, see
[Configuration](#configuration)), so one cluster cannot quietly span a whole
session and swallow an event from an hour ago. Whether clustering is on is
remembered between runs; which clusters you had open is not.

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
| CSV | A fixed, rectangular table of the same columns, plus a `node` column, with the remaining parsed fields as one JSON column. |
| Plain text | The raw lines, byte-identical to what is on screen. |

Three things worth knowing:

- It exports the **whole filtered set**, not just the lines that fit on screen.
  The dialog states the count before it writes, so `+`/`-` never changes what an
  export contains.
- **A merged export names its machines.** The `node` column (CSV) and the `node`
  field (JSON Lines) carry the machine each line was read from, and the default
  filename names one of them — `web01-syslog-20260817-142530`. `node` is empty
  for a local source, which has no machine to name. Plain text is left alone:
  it is the raw lines and nothing CLV added.
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
| `a` | Add a log source (or open the remote host list from the same dialog) |
| `t` / `s` | Cycle time window / severity |
| `f` | Toggle the Advanced drawer |
| `*` | Star / unstar the log under the cursor |
| `x` | Add / remove the log under the cursor from the merged set |
| `Ctrl+X` | Merge this path across every host that has it, then open it |
| `u` | Open the merged set as one timestamp-ordered stream |
| `X` | Empty the merged set |
| `↑` / `↓` | Move the line cursor |
| `PgUp` / `PgDn` | Move the line cursor a screen at a time |
| `Home` / `End` | First / last line (`End` also resumes following) |
| `n` / `N` | Next / previous match (or warning-and-worse, with no query) |
| `g` | Go to a timestamp |
| `m` / `M` | Mark the cursor line / jump to the next mark |
| `v` / `V` | Open saved views / save the current filters as a view |
| `W` | Manage watch rules (live highlights and alerts) |
| `d` | Show / hide the event detail pane |
| `b` | Show / hide the severity timeline (then `←` `→` `Enter` to filter to a bucket) |
| `c` | Collapse / expand repeated lines (then `Enter` on a cluster row) |
| `w` | Follow new lines (auto-scroll) on/off |
| `o` | Structured output on/off |
| `Ctrl+B` | Switch between tree and log pane (compact widths) |
| `[` / `]` | Narrow / widen the source tree |
| `+` / `-` | Show more / fewer lines |
| `Ctrl+E` | Export the filtered entries to a file |
| `y` | Copy the selected line, or the visible lines, to the clipboard (OSC 52) |
| `Ctrl+L` | Copy mode (hides all chrome) |
| `R` | Add, edit, test and remove remote hosts (SSH); also reachable from `a` |
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

`LogSourceProvider` plugins are wired too: whatever `discover()` returns appears
in a **Providers** group in the tree, and selecting one opens it like any other
source. Implement `discover()` and `open()` and CLV wraps your iterator; for a
source that tails, implement the optional `open_reader(path, *, max_lines)`
instead and return an object with `prime()`, `poll()`, `path`, `RELOAD_NOTICE`
and — if it holds anything — `close()`, which CLV calls on every source switch
and at shutdown. The shipped [`journald`](clv/plugins/sources/journald.py)
provider is the worked example.

Provider sources are **not** filesystem paths, and CLV does not pretend they
are: starring, include/exclude globs and rotated-set grouping all test for a
real `Path` and so skip them. That is deliberate — a provider identifier in
your `session.json` would be a path that does not exist.

---

## Development

```bash
python -m pip install -e .
python -m pip install pytest
python -m pytest            # 791 tests
python -m textual run clv/app.py --dev
```
