# TODO — Centralized Log Viewer

Planned work, in dependency order. Each phase assumes the phases above it have
landed; within a phase, items are ordered by what unblocks the most.

The ordering is deliberate: the two foundation items produce no user-visible
feature but are prerequisites for most of what follows, and doing them last
would mean reworking every feature built on top of them.

## Status

| Phase | Items | State |
| --- | --- | --- |
| 0 — Foundations | 1, 2 | ✅ **Complete** (`094716c`) |
| 1 — Close the dangling loops | 3, 4 | ✅ **Complete** (`48822ed`, `08edb3c`) |
| 2 — The line cursor and what it unlocks | 5, 6, 7 | ✅ **Complete** (`63354f9`, `1c9fd27`, `20e4ffd`) |
| 3 — Query and view power | 8, 9, 10 | ✅ **Complete** (`b882cfb`, `3fbbf75`, `bd174af`) |
| 4 — The source layer | 11, 12, 13 | Not started |
| 5 — Analysis | 14, 15 | Not started |

Completed items are kept in full rather than deleted: the "production ready
when" lists are what the tests were written against, and the reasoning behind
each constraint is worth more as a record than as a checkbox. Where what
shipped differs from what was planned, the item says so under **As shipped**.

---

## Constraints that apply to every item

These come from `AGENTS.md` and are non-negotiable. Repeated here so no item
has to relitigate them.

- **No new runtime dependency** without an explicit justification in the PR.
  Python 3.11+, Textual and Rich only. Every item below is achievable with the
  standard library.
- **Services stay UI-free.** Nothing under `clv/services/` imports Textual.
- **CSS is the only place layout is decided.** No `.styles.*` assignment at
  runtime; new widgets own their own `DEFAULT_CSS` and key responsive rules off
  the `-compact` / `-narrow` / `-wide` class the app mirrors onto them.
- **80 columns is a supported width.** Every new control must remain on screen
  and keyboard-reachable at 80 columns. Horizontal groups are built from `1fr`
  children.
- **Never silently lose a line.** New filters, clustering and merge logic must
  keep every raw line reachable, and any pane that ends up empty must explain
  itself through `describe_empty_result`.
- **Bounded work.** No feature may introduce a whole-file read of a stream
  source, or unbounded memory growth beyond `max_buffer_lines`.
- **Local only.** No network, no telemetry. Log content never lands in a cache
  or temp file; session state stores paths and settings only.
- **Every action has a keyboard path.** Mouse is supported, never required.

### Footer and keybinding policy

The footer drops entries from the right as it runs out of room — see the
comment at [app.py:372-374](clv/app.py#L372-L374). The visible bindings already
fill it at 80 columns, so **every binding added below is `show=False`** and is
discovered through the help overlay (Item 2). Only Item 2's own `?` binding was
added with `show=True`, and it replaced nothing.

`h`, `j`, `k` and `l` are deliberately left unbound throughout, so vim-style
pane navigation stays available as a later option.

### Keybinding budget (final state after all items)

Shipped rows are marked ✅. Cursor movement lives on `LogView.BINDINGS` rather
than the app — see Item 5's **As shipped** — but is listed here because it
counts against the same key budget and appears in the same help overlay.

| Key | Action | Item | Footer | |
| --- | --- | --- | --- | --- |
| `?` | Help overlay | 2 | show | ✅ |
| `Ctrl+E` | Export current view | 3 | hidden | ✅ |
| `y` | Copy (yank) selection or view | 4 | hidden | ✅ |
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | Move the line cursor | 5 | hidden | ✅ |
| `Enter` | Open detail for the cursor line | 5 | hidden | ✅ |
| `d` | Toggle detail pane | 5 | hidden | ✅ |
| `n` / `N` | Next / previous query match | 6 | hidden | ✅ |
| `g` | Go to timestamp | 6 | hidden | ✅ |
| `m` | Mark / unmark the cursor line | 7 | hidden | ✅ |
| `M` | Jump to next mark | 7 | hidden | ✅ |
| `v` | Open saved views | 9 | hidden | ✅ |
| `V` | Save current filters as a view | 9 | hidden | ✅ |
| `W` | Watch rules manager | 10 | hidden | ✅ |
| `x` | Toggle source into the merged set | 13 | hidden | |
| `u` | Open the merged (unified) view | 13 | hidden | |
| `b` | Toggle the severity timeline | 14 | hidden | |
| `c` | Toggle repeat clustering | 15 | hidden | |

No collisions with the existing bindings (`/`, `Esc`, `a`, `*`, `t`, `s`, `f`,
`w`, `o`, `Ctrl+B`, `[`, `]`, `+`, `-`, `Ctrl+L`, `Ctrl+S`, `Ctrl+R`, `q`).

---

# Phase 0 — Foundations ✅

No user-visible feature. Both items are prerequisites for most of Phases 2–5.

## 1. Carry parsed fields on `LogEntry` ✅

**Goal.** The parser already extracts `host`, `tag` and `pid` from BSD syslog
([parsing.py:250-254](clv/services/parsing.py#L250-L254)), `host`, `user`,
`request`, `status` and `size` from CLF ([parsing.py:263-267](clv/services/parsing.py#L263-L267)),
`app`, `pid` and `msgid` from RFC 5424, and every key of a JSON line — and
discards all of it, because `LogEntry` has nowhere to put it. Add
`fields: Mapping[str, str]` so that structure survives parsing.

This is the enabling change for Items 5 (detail pane property list), 8
(field-aware queries), 13 (source column on merged rows) and 15 (clustering on
field shape rather than raw text). None of them are implementable without it.

**Production ready when:**
- `LogEntry` gains `fields: Mapping[str, str] = frozen empty mapping`, default
  empty, so the dataclass stays frozen, hashable-friendly and backward
  compatible. Every existing construction site keeps working unchanged.
- Each format matcher populates `fields` from the groups it already captures.
  Key names are normalised across formats (`host` means the same thing whether
  it came from syslog or CLF) and documented in the module docstring.
- JSON lines flatten nested objects to dotted keys with a bounded depth and a
  bounded key count, so a pathological payload cannot blow up an entry.
- Values are stored as strings. No type coercion, no schema. `status` is
  `"500"`, not `500` — comparison semantics are Item 8's problem, not the
  parser's.
- Continuation carry-forward does **not** inherit fields. A stack trace line
  inherits timestamp and level because those are properties of the event; it
  has no host of its own to report and must not claim its parent's.
- No measurable parse throughput regression. Fields are built from match groups
  already computed, so this should be allocation cost only — benchmark a
  100k-line file before and after and record the numbers in the PR.

**Tests required** (`tests/test_parsing.py`):
- One test per format asserting the exact `fields` dict produced.
- A raw/unrecognised line has `fields == {}`.
- A continuation line inherits `timestamp` and `level` but **not** `fields`.
- JSON flattening: nested object → dotted keys; depth cap honoured; key-count
  cap honoured; a list value is stringified rather than exploded.
- A JSON line whose keys collide with a normalised name keeps the JSON value.
- `LogEntry` remains constructible with no `fields` argument (backward compat).

**GUI.** None. This item is invisible by design.

**README.** No user-facing change yet. Add `fields` to the `LogEntry`
description in the plugin section of `clv/plugins/AGENTS.md`, since
`FilterStage.apply` now receives it.

---

## 2. Help overlay (`?`) ✅

**Goal.** The footer cannot show more than about twelve bindings at 80 columns,
and this plan adds sixteen. Without a discoverable list, every feature below
ships hidden. A modal overlay listing every binding — including the hidden ones
— grouped by category, is the cheapest possible unblock and should land before
any new binding is added.

**Production ready when:**
- New `clv/widgets/help_overlay.py`, owning its own `DEFAULT_CSS`, opened with
  `?` and dismissed with `?`, `Esc` or `q`.
- The list is **generated from `App.BINDINGS`**, not hand-maintained. A binding
  added later appears automatically; a stale help screen is impossible by
  construction.
- Bindings are grouped (Search, Navigation, View, Sources, Session) via a
  category attached to each binding — a parallel dict keyed by action name
  keeps `Binding` construction unchanged.
- Readable and scrollable at 80×24 with every binding present.
- Opening the overlay pauses nothing: tailing continues behind it.

**Tests required** (new `tests/test_help_overlay.py`):
- `?` opens the overlay; `?`, `Esc` and `q` each close it.
- Every entry in `LogViewerApp.BINDINGS` appears in the rendered overlay —
  this is the test that keeps help complete as later items add bindings.
- Region assertion at 80×24: the overlay is fully on screen and scrollable.
- The overlay does not steal the query input's focus permanently — focus is
  restored to whatever held it on close.

**GUI.** `?` binding, `show=True`. A `Help` button is **not** added to the
query bar — the action row is already at capacity at 80 columns, which is the
constraint that caused the original layout regression.

**README.** New "Getting help" line under Usage, and a `?` row at the top of
the keyboard shortcuts table.

---

# Phase 1 — Close the dangling loops ✅

Two small items where the machinery already exists and only the user-facing
path is missing. Both are independent of Phase 2 and can ship immediately after
Phase 0.

## 3. Wire the `Exporter` interface to the UI ✅

**Goal.** `Exporter` is defined, loaded, version-checked, and counted in the
Advanced drawer's plugin status line — and cannot be invoked from anywhere in
the application. [app.py:673](clv/app.py#L673) only ever calls `apply_filters`.
Give it a keyboard path, and ship the built-in exporters that make it useful.
This is Event Viewer's "Save Filtered Log File As", which is one of the two
things operators do with a filtered view.

**Production ready when:**
- `Ctrl+E` opens an export dialog listing every registered exporter plus the
  built-ins, with a destination path input defaulting to a sensible name
  derived from the source and the current timestamp.
- Three built-in exporters ship in `clv/plugins/exporters/`: **JSON Lines**
  (full entry including `fields` from Item 1), **CSV**, and **plain text**
  (raw lines, byte-identical to what is on screen).
- Exports the **currently filtered** entries, not the raw buffer, and not just
  the `_show_lines` window. The dialog states the count it is about to write.
- Destination is validated before writing: refuses to overwrite without
  confirmation, reports permission errors as a notification rather than a
  traceback, and writes atomically (temp + `os.replace`) like `StateStore`.
- An exporter that raises is caught, recorded, and reported the same way a
  failing `FilterStage` is — an export must never take down the app.
- Export writes only where the operator pointed it. No temp copies of log
  content anywhere else.

**Tests required** (new `tests/test_exporters.py`, plus `tests/test_plugins.py`):
- Each built-in exporter round-trips a known entry list; JSON Lines preserves
  `fields`, CSV quotes correctly, plain text is byte-identical to `raw`.
- Export uses the filtered set: with a query active, only matching entries are
  written, and the count is *not* clipped to `_show_lines`.
- A raising exporter is reported and the app survives.
- Overwrite of an existing file requires confirmation.
- Write to an unwritable path surfaces a notification, not an exception.
- Headless `run_test`: `Ctrl+E` opens the dialog; Esc cancels without writing.

**GUI.** `Ctrl+E` (hidden). Export dialog modelled on
[add_source_dialog.py](clv/widgets/add_source_dialog.py). Exporter list also
shown read-only in the Advanced drawer's plugin status area, so an operator can
see what is available without opening the dialog.

**README.** New "Exporting" subsection under Usage; `Ctrl+E` row in the
shortcuts table; a note in the plugin section that the `Exporter` interface is
now reachable from the UI.

**As shipped** (`48822ed`). The three built-in formats live in
`clv/services/export.py`, **not** in `clv/plugins/exporters/` as this item
specified. The reasoning is recorded in that module's docstring and in
`AGENTS.md`: a built-in must not be able to fail to load the way third-party
code can, and the drawer's plugin count should keep meaning "plugins someone
installed". `clv/plugins/exporters/` remains a live extension point, and the
dialog lists the built-ins above whatever it supplies. `export.py` deliberately
does not import `clv.plugins`, since that dependency already runs the other way.

Item 7 later added a **Marked lines only** checkbox to the dialog; nothing in
`export.py` changed for it, because that module does not decide *which* entries
are exported.

---

## 4. OSC 52 clipboard copy ✅

**Goal.** `Ctrl+L` copy mode hides the chrome so the terminal's own mouse
selection works — which is exactly the mechanism that fails over tmux, screen
and plain SSH, the headless case the mission foregrounds. OSC 52 puts text on
the local clipboard from a keyboard path, through the terminal, with no
dependency and no helper binary.

**Production ready when:**
- `y` copies the current selection to the system clipboard via OSC 52. Before
  Item 5 lands, "selection" means the filtered visible view; after Item 5, the
  cursor line, falling back to the view when no line is selected.
- Payload is chunked and size-capped, with the cap configurable
  (`clipboard_max_bytes`). Terminals silently truncate or drop oversized OSC 52
  sequences; exceeding the cap must produce an explicit notification saying how
  much was copied, never a silent partial copy.
- `Ctrl+L` copy mode is kept as-is. It stays the fallback for terminals with
  OSC 52 disabled, and the two are documented as complementary.
- A notification confirms what was copied ("Copied 1 line" / "Copied 412
  lines").
- Emitting the escape sequence is guarded so a terminal that rejects it cannot
  corrupt the display.

**Tests required** (new `tests/test_clipboard.py`):
- Sequence construction: correct OSC 52 framing, correct base64 payload,
  correct chunking at the boundary.
- Size cap: oversized payload is truncated at a line boundary and the
  notification reports the truncation.
- `y` with no source open is a no-op with an explanatory notification, not an
  error.
- Copy respects the active filter — hidden lines are not copied.

**GUI.** `y` binding (hidden). Advanced drawer, View section: a
"Clipboard (OSC 52)" switch for operators on terminals where the sequence is
disruptive, persisted in `SessionState`.

**README.** Shortcuts table row for `y`; a paragraph under Usage explaining the
difference between `y` (works over SSH/tmux) and `Ctrl+L` (needs a local
terminal selection).

**As shipped** (`48822ed`). The payload is size-capped but **not chunked**, as
this item's first bullet asked. OSC 52 has no continuation form — a second
sequence *replaces* the clipboard rather than appending to it — so a payload
cannot be streamed in parts. `clv/services/clipboard.py` therefore truncates
deliberately, at a line boundary, keeping the newest lines, and reports exactly
what was copied. The intent of the bullet is met; the mechanism named in it does
not exist.

The other half of that bullet — "after Item 5, the cursor line, falling back to
the view when no line is selected" — was **missed** when Item 5 landed and
closed afterwards in `08edb3c`. `y` now copies the cursor line when one is
selected. Nothing is selected until the operator moves the cursor, so the
default behaviour is unchanged.

---

# Phase 2 — The line cursor and what it unlocks ✅

Item 5 is the largest single change in this plan and gates Items 6 and 7.

## 5. Selectable line cursor and event detail pane ✅

**Goal.** The defining Event Viewer interaction is *select an event, see its
full properties*. The log pane is a `RichLog` ([app.py:292](clv/app.py#L292)) —
there is no cursor, no selected line, and therefore nowhere to hang a detail
pane, a per-line copy, a bookmark, or a "filter by this field" action. This is
the biggest structural gap in the UI and unblocks four later items.

**Production ready when:**
- The log pane supports a keyboard cursor: arrow keys, PgUp/PgDn, Home/End.
  Mouse click selects the same line.
- `Enter` (or `d`) opens the detail pane below the log, showing: raw line,
  parsed timestamp, canonical level, detected format, continuation flag, and
  every key/value from `fields` (Item 1) as a property list.
- The detail pane is horizontally split at `-wide`, stacked below at `-narrow`,
  and takes the full pane at `-compact` — all decided in CSS, keyed off the
  breakpoint class the app mirrors onto the widget.
- **Auto-scroll interaction is explicit:** moving the cursor off the last line
  suspends follow mode and says so in the status line; `End` or `w` resumes it.
  Silently fighting the user's cursor with incoming lines is the failure mode
  to avoid here.
- The incremental append path (`_append_entries`) still renders only new lines.
  Tailing must not become O(buffer) per poll; if the replacement widget cannot
  append cheaply, that is a blocker, not a detail to fix later.
- Cursor position survives a re-render caused by a filter change where the
  selected line is still visible; when it is not, the cursor moves to the
  nearest surviving line rather than resetting to the top.
- Detail pane visibility persists in `SessionState`.
- No regression at 80 columns: log pane, detail pane and footer all on screen.

**Tests required** (new `tests/test_detail_pane.py`, plus
`tests/test_log_rendering.py`):
- Cursor movement across arrows / PgUp / PgDn / Home / End lands on the
  expected entry.
- `Enter` opens the detail pane; it shows every `fields` key for a syslog line,
  a CLF line and a JSON line.
- A raw unparsed line renders a detail pane that says so rather than an empty
  property list.
- Moving the cursor up suspends auto-scroll; `End` resumes it; the status line
  reflects both.
- Region assertions at 80, 100 and 140 columns for the three layout modes.
- Performance guard: appending N tailed lines writes N renderables, not the
  whole buffer.
- Cursor stability across a filter change, in both the surviving and
  non-surviving cases.

**GUI.** `Enter` and `d` (both hidden). Detail pane is a new widget with its
own CSS. Advanced drawer, View section: "Detail pane" switch mirroring `d`,
following the existing two-homes pattern — including `prevent(Switch.Changed)`
for the mirror, not a boolean guard.

**README.** New "Inspecting an event" subsection with the property list
described; shortcuts rows for `Enter` and `d`; a sentence in the filtering
section about follow mode suspending on cursor movement.

**As shipped** (`63354f9`). Three deliberate departures:

- **The replacement widget is `clv/widgets/log_view.py`**, a `ScrollView` using
  the Line API — the technique `RichLog` itself uses. Rows are entry-indexed
  rather than line-indexed, because one entry can wrap or render as a whole
  panel in structured mode. Append is O(new); the row cap drops a *batch*
  (`max_rows // 10`) rather than one row per append, so the only O(total)
  operation is amortised rather than paid on every tailed line.
- **The drawer switch shares `#output-options` with the clipboard switch**,
  under a heading renamed to "Output & panes" — not the View section this item
  asked for. Two reasons, both structural: `#view-toggles` is `display: none`
  above 148 columns, so a single-home control placed there vanishes on wide
  terminals; and giving the detail pane its own drawer section pushed "Source
  discovery" past the drawer's `max-height: 16`, where it laid out and painted
  nothing — the exact regression `test_section_headings_are_actually_painted`
  exists to catch. What the two switches share is being single-home, which is
  the property that decides where they live.
- **`Enter` and the cursor keys are bound on `LogView`, not the app.** Widget
  scope keeps them from fighting the source tree and the query input. They are
  still generated into the help overlay: `action_show_help` passes
  `LogViewerApp.BINDINGS + LogView.BINDINGS` to `build_help_sections`, and
  `test_every_binding_has_a_category` covers both, so Item 2's "a stale help
  screen is impossible by construction" survives.

---

## 6. Match navigation and jump-to-timestamp ✅

**Goal.** Today the only way to reach a query match is to scroll until you see
one. `n` / `N` step between matches; `g` jumps to a timestamp. Both are table
stakes for a log tool and trivial once a cursor exists.

**Production ready when:**
- `n` / `N` move the cursor to the next / previous entry matching the active
  query, wrapping with a notification at the ends rather than silently
  stopping.
- With no query active, `n` / `N` step between entries at or above the current
  severity selection — useful, and not a dead key.
- `g` opens a small prompt accepting an absolute timestamp or a relative offset
  (`-15m`), and moves the cursor to the nearest entry at or after it. Reuses
  the parsing already in `parse_relative_window` /
  `parse_absolute_window`.
- Entries with no timestamp are skipped by `g` and the count of skipped entries
  is reported, consistent with the "explain what is hidden" rule.
- The status line shows match position ("match 3 of 47").

**Tests required** (new `tests/test_navigation.py`):
- `n` / `N` traverse matches in order and wrap with a notification.
- With no query, `n` steps by severity.
- `g` with an absolute timestamp lands on the nearest entry at or after.
- `g` with a relative offset resolves against now.
- `g` in a source with no parsed timestamps reports the skip count.
- Match counter in the status line is correct after a filter change.

**GUI.** `n`, `N`, `g` (all hidden). The existing `#match-count` static in the
query bar ([query_bar.py:295](clv/widgets/query_bar.py#L295)) is extended to
show position within matches, not just the total — no new control.

**README.** Shortcuts rows for `n`, `N`, `g`; a short paragraph under "How
filtering behaves" on navigating matches.

**As shipped** (`1c9fd27`). One thing this item did not anticipate, recorded so
it is not read later as a bug: **the query filters rather than highlights**, so
with a query active every visible line is already a match and `n` is the next
line. What `n` adds there is the position readout and the wrap notice, not a
different destination. The case that earns the key is the fallback — no query,
severity on `all`, step between WARN and above. This is stated in
`_navigation_targets`' docstring and in the README table rather than papered
over. Making `n` step matches *within the buffer* instead would need the query
to stop filtering, which is a much larger change than this item.

Two supporting decisions: `parse_moment` and `align_moments` were added to
`clv/services/filtering.py` rather than to the dialog, built on the same
`_RELATIVE_RE` table and `fromisoformat` the existing window parsers use, so
`GotoDialog` cannot disagree with the time presets about what `-15m` means. And
the navigation target set is cached per render — recomputing it on every cursor
move would re-run the query regex over every visible line on each arrow press.

---

## 7. Bookmarks (marked lines) ✅

**Goal.** Starring works on *logs*; there is no way to mark a *line*. Marking
the three lines that matter while reading a 5000-line buffer, then stepping
between them, is how an incident actually gets pieced together.

**Production ready when:**
- `m` marks / unmarks the cursor line; `M` jumps to the next mark.
- Marks render with a distinct gutter indicator that is visible at every
  breakpoint and does not rely on colour alone.
- Marks survive filter changes and re-renders within a session, keyed by
  source path plus a content hash of the line — **not** by buffer index, which
  shifts as the bounded deque evicts.
- A marked line whose content has rotated out of the buffer is dropped silently
  on reload; a mark is a session convenience, not a promise.
- Marks are **not** persisted to `SessionState`. They reference log content,
  and `AGENTS.md` is explicit that session state stores paths and settings
  only. This constraint should be stated in the code comment, so it is not
  "fixed" later by someone who reads it as an oversight.
- Export (Item 3) gains a "marked lines only" option.

**Tests required** (new `tests/test_bookmarks.py`):
- `m` toggles a mark; `M` cycles through marks in buffer order.
- Marks survive a filter change that keeps the line visible.
- Marks survive a filter change that hides the line, and reappear when it
  returns.
- A mark on a line evicted by `max_buffer_lines` is discarded without error.
- `StateStore` round-trip contains no mark data — the privacy guard.
- Export honours "marked only".

**GUI.** `m`, `M` (hidden). Marked-line count shown in the status line. Export
dialog gains a "Marked lines only" checkbox.

**README.** New "Marking lines" subsection; shortcuts rows for `m` and `M`;
an explicit note that marks are session-only and never written to disk.

**As shipped** (`20e4ffd`). As specified, with one consequence of content
keying that this item did not call out: **identical lines in one source share a
mark**, so marking one marks every copy. That is the trade a content key makes,
and the alternative is the positional key this item already rejects. Recorded
in the `clv/services/marks.py` docstring.

The privacy constraint is enforced rather than only documented: `MarkSet` is
`__slots__`-only with no serialiser, and
`test_a_markset_offers_no_way_to_serialise_itself` asserts that alongside a
`StateStore` round-trip that contains neither the marked line nor its digest.

---

# Phase 3 — Query and view power ✅

## 8. Field-aware query syntax ✅

**Goal.** The query is a regex over the whole raw line, which is excellent for
"find this string" and useless for "show me sshd on web01 with a 5xx". With
`fields` on `LogEntry` (Item 1), `unit:sshd host:web01 status>=500` becomes
possible without giving up what already works.

**Production ready when:**
- The query bar accepts `key:value` and `key>=value` terms **alongside** free
  text. Anything not matching a field term is treated exactly as today: a regex
  over the raw line. An existing saved query must behave identically after this
  lands — that is the compatibility bar.
- Supported operators: `:` (substring, smart-case), `=` (exact), `!=`, `>`,
  `>=`, `<`, `<=`. Numeric comparison when both sides parse as numbers,
  lexicographic otherwise, and the choice is documented.
- Terms combine with implicit AND. No `OR`, no parentheses, no precedence rules
  in this item — a query DSL is a stated non-goal, and this stays deliberately
  below that line.
- An entry lacking the referenced field is **hidden and counted**, with a new
  `hidden_missing_field` counter in `FilterStats` and a matching branch in
  `describe_empty_result`. This is the same contract severity and time filters
  already honour, and skipping it would be the easiest way to break the "never
  silently lose a line" rule.
- Available field names for the current source are discoverable — offered as
  completions in the query input, sourced from the fields actually present in
  the buffer.
- A malformed term reports through the existing regex-validation path
  (`_sync_regex_validation`) rather than throwing.
- Field matching adds no measurable cost when no field term is present.

**Tests required** (`tests/test_filtering.py`, new `tests/test_field_query.py`):
- Every operator against string and numeric values.
- Mixed query: field term plus free-text regex, both applied.
- Existing plain-regex queries produce byte-identical results to before —
  parametrised over the current test corpus.
- Entry missing the field is hidden and counted in `hidden_missing_field`.
- `describe_empty_result` produces the missing-field explanation.
- Malformed term surfaces as a validation error, not an exception.
- Smart case applies to `:` and not to `=`.
- A value containing a colon or a space, quoted, parses as one term.

**GUI.** No new binding — this is the existing query input. The input's
placeholder is updated to hint the syntax, and the Advanced drawer's Search
options section gains a static one-line syntax reminder. Completions appear as
a dropdown under the query input.

**README.** New "Field queries" subsection under "How filtering behaves", with
an operator table and worked examples; a note that plain regex queries are
unchanged.

**As shipped.** Three things this item did not spell out, all forced by its own
compatibility bar:

- **A `key:value` token is a term only when the key is *known*** — one of the
  names `parsing.py` normalises, or a key the buffer actually carries. Purely
  syntactic recognition would have reinterpreted `sshd:` and `kernel:`, which
  are among the most common things anyone greps a syslog for, and "an existing
  saved query must behave identically" is the bar this item set for itself. The
  cost is that a mistyped key is searched for as text rather than reported;
  the completion dropdown exists to narrow that gap. Reinforced by the
  stronger guarantee in `parse_query`: a query with no recognised term is
  returned *unmodified*, so a plain regex never even reaches the rejoin step.
- **`invert` applies to the free-text half only.** Field terms stay positive.
  `!=`, `<` and `>` are the per-term negation, and inverting "this entry has no
  such field" has no honest answer — the entry would have to count as both
  hidden and shown.
- **`key:` with no value tests that the field is present**, rather than being
  the malformed term this item implies. It is useful (`pid:` finds everything
  from a daemon), and the alternative was flashing "invalid query" at anyone
  half-way through typing `host:web01`. A *comparison* with no value
  (`status>=`) is still an error, which is what the malformed-term test uses.

`QueryError` moved from `filtering.py` to the new `query.py` and is re-exported,
so no import site changed.

---

## 9. Saved views (Custom Views) ✅

**Goal.** `SessionState` persists exactly one filter set
([storage.py:19-64](clv/storage.py#L19-L64)). Event Viewer's headline feature is
Custom Views: named, reusable filter bundles. The interaction pattern already
exists in this codebase — starred logs — and this is the same shape applied to
filters.

**Production ready when:**
- A view captures query, severity, time window, case/regex/invert flags,
  include/exclude globs, and the merged source set once Item 13 lands.
- `V` saves the current filter state as a named view; `v` opens a picker
  listing saved views with their filter summary; selecting one applies it
  atomically (one re-render, not one per field).
- Views appear as a group at the top of the source tree, above the starred
  group, mirroring the starred-logs pattern operators already know.
- Persisted in `SessionState` as a list of named records, with the same
  defensive `from_dict` validation the existing fields get: a hand-edited or
  older state file must never prevent startup, and a single malformed view must
  not discard the rest.
- Views store **filter settings and paths only** — never log content, never
  match results.
- Rename and delete are available from the picker, both keyboard-reachable.
- A view referencing a source that no longer exists applies its filters anyway
  and reports the missing source, rather than failing.

**Tests required** (`tests/test_sources.py`, new `tests/test_saved_views.py`):
- Save / apply round-trip restores every captured field.
- Persistence across a `StateStore` reload.
- Malformed view record in the JSON is dropped; the others survive; the app
  starts.
- Applying a view triggers exactly one re-render.
- Rename and delete from the picker, via keyboard only.
- View referencing a deleted path applies filters and reports the miss.
- Tree ordering: views group above starred group above roots.

**GUI.** `v`, `V` (hidden). Picker modal in the style of the existing dialogs.
Tree group with its own header row. No query bar button — the action row is
full.

**README.** New "Saved views" section with a worked example; shortcuts rows for
`v` and `V`; a `session.json` note stating that views record filters, never log
content.

**As shipped.** Four things worth recording:

- **The picker never edits state.** It dismisses with a
  `ViewRequest(action, name, new_name)` and the app performs it, reopening the
  picker after a rename or a delete. The list a modal is holding is stale the
  instant either lands, and reopening costs one repaint against teaching the
  dialog to maintain its own copy of the truth.
- **`AdvancedFiltersDrawer.sync_settings` had to exist.** The drawer's switches
  were only ever seeded at compose time, so a view that turned on
  `case_sensitive` would have filtered correctly while the switch still read
  off. It uses `prevent(Switch.Changed, Input.Changed)` for the reason
  `sync_view_toggles` documents. `_dismiss_chip` now goes through it too — the
  Invert chip had exactly this bug already.
- **`LogTree` is `Tree[object]`, not `Tree[Path]`.** A view node carries a
  `SavedView`, and selection dispatches on the type. Everything that walks the
  tree for a file already tested `isinstance(data, Path)`, so nothing else
  changed.
- **The merged source set is not captured**, because Item 13 has not landed.
  `SavedView` gains a field when it does; nothing else about the record needs
  to move.

Views are stored sorted by name and saving over an existing name replaces it,
with the notification saying which of the two happened.

---

## 10. Watch rules and live alerts ✅

**Goal.** Tailing means watching for something. A watch rule is a saved pattern
that highlights matching lines as they arrive and raises a notification —
turning a passive tail into something an operator can leave on a second
monitor.

**Production ready when:**
- A watch rule is a named pattern (reusing Item 8's syntax) plus an action:
  highlight, notify, or both.
- Rules are managed from a `W` modal: add, edit, enable/disable, delete.
- Matching lines are highlighted distinctly from severity colouring, so a
  watched INFO line is visibly watched, not mistaken for an error.
- Notifications are rate-limited and coalesced ("12 matches for `oom-killer`
  in the last minute"). A rule matching every line must not produce a
  notification storm — this is the failure mode that makes such features get
  turned off.
- Rules evaluate only on newly tailed lines, not on every re-render, and the
  per-line cost is bounded by the number of enabled rules.
- Optional terminal bell, defaulted **off**, configurable in `settings.conf`.
- Rules persist in `SessionState` (patterns are operator input, not log
  content).
- Strictly local: a Textual notification and an optional bell. No desktop
  notification daemon, no `subprocess`, no network.

**Tests required** (new `tests/test_watch_rules.py`):
- A rule matching an appended line raises exactly one notification.
- Rate limiting: N matches within the window coalesce into one notification
  with the correct count.
- A disabled rule does not evaluate.
- Highlight styling is applied and is distinguishable from severity colour.
- Rules persist and reload; a malformed rule is dropped without loss of others.
- Rules do not evaluate on re-render — assert the evaluation count directly.

**GUI.** `W` (hidden). Rules modal. Advanced drawer gains a "Watch rules: N
active" status line beside the plugin status, with an enable-all/disable-all
switch. Active rules also surface as dismissable chips, reusing
[filter_chip.py](clv/widgets/filter_chip.py).

**README.** New "Watch rules" section; shortcuts row for `W`; `settings.conf`
table rows for the bell and rate-limit window.

**As shipped.** The interesting decisions are all about *what counts as an
event*:

- **Matching is cached per distinct line; occurrences are counted per
  arrival.** `WatchIndex` keys its answers the way `marks.py` does — source
  plus a digest of the raw text — so a re-render is a dict lookup and
  `test_re_rendering_does_not_re_evaluate_the_rules` holds by construction.
  But the cache answers "does this text match", not "how many times has this
  happened": fifty identical `connection refused` lines are fifty events, and
  an index that deduplicated them would have the notifier report one. So
  `evaluate` returns a hit for every entry handed to it and only *matching*
  work is deduplicated. Callers pass in what newly arrived; the silent pass
  over a primed buffer ignores the return value.
- **Lines already in the buffer are highlighted, never announced.** Opening a
  source or adding a rule matches what is already there so the pane is honest
  about it, and says nothing: nobody asked to be told about lines that arrived
  before the rule existed, and a source switch would otherwise open with a
  burst of toasts about history.
- **The highlight is a background, not a gutter glyph.** The gutter is two
  cells and already belongs to marks. Severity lives in the foreground colour,
  so a background plus bold is what makes a watched INFO line read as watched
  rather than as an error, and it survives a terminal with no colour at all.
- **No new timer.** The notifier is driven from the tail poll that already runs
  at `refresh_hz`; a clock of its own would only be a way for the two to
  disagree. Time is injected, so the rate limiting is unit-tested without one.
- **The enable-all switch took the spacer slot** in the drawer's "Output &
  panes" row rather than getting a section of its own — the same `max-height:
  16` constraint Item 5 recorded. Past three enabled rules the chips collapse
  into one `Watching: N rules`, because the chip bar is a single row at 80
  columns; dismissing any watch chip **disables** the rule rather than deleting
  it.
- **The dialog dismisses with `None` when nothing changed**, so opening it to
  look costs no re-indexing of the buffer.

---

# Phase 4 — The source layer

## 11. Compressed and rotated-set sources

**Goal.** `/var/log` is mostly `syslog.1` and `syslog.2.gz`, and
`.gz`/`.bz2`/`.xz`/`.zst` are all in
[`DEFAULT_EXCLUDE_GLOBS`](clv/services/discovery.py#L36-L44). An operator
investigating anything more than a few hours old has to leave CLV. `gzip`,
`bz2` and `lzma` are standard library, so this costs no dependency. Grouping
`foo.log` + `foo.log.1` + `foo.log.2.gz` into one logical source spanning weeks
is something Event Viewer cannot do at all.

**Production ready when:**
- A `CompressedReader` handles `.gz`, `.bz2` and `.xz`, selected by
  `open_reader()` alongside `SourceReader` and `DocumentReader`, exposing the
  same `path` / `prime()` / `poll()` / `RELOAD_NOTICE` contract.
- **The bounded-read exception is explicit and documented.** A deflate stream
  has no cheap backwards seek. Compressed members are read forward under a
  *line* budget, exactly as `DocumentReader` already does for container
  documents, and the rationale is written into the module docstring in the same
  style — this is a deliberate second exception to Requirement 3, not an
  oversight.
- Compressed members are treated as immutable: read once, cached in the
  bounded buffer, never re-polled. Only the live head of a rotated set tails.
- Rotation grouping detects the common patterns (`.1`, `.2.gz`,
  `-YYYYMMDD`, `.YYYY-MM-DD`) and presents the set as one tree node, expandable
  to its members. Grouping is opt-in via `group_rotated` in `settings.conf`,
  defaulting **on**.
- Entries from the set are presented oldest-to-newest across members, and the
  status line names which member the cursor is in.
- Timing is honest: opening a rotated set states how many members it will read
  and shows progress, since this is the one path that is not instant.
- Decompression is size-capped, so a decompression bomb degrades into "showing
  the first N lines" rather than exhausting memory.
- `DEFAULT_EXCLUDE_GLOBS` drops the three now-supported extensions. `.zst` and
  the archive formats (`.zip`, `.tar`, `.tgz`) stay excluded — no stdlib zstd,
  and an archive is a container of files, not a log.

**Tests required** (`tests/test_discovery_reader.py`, new
`tests/test_compressed.py`):
- Round-trip a known corpus through each of gzip, bzip2 and xz.
- Line budget honoured: a compressed file larger than the budget yields exactly
  the budgeted line count.
- Size cap: a highly compressible file does not exceed the memory ceiling.
- Rotation grouping across all four naming patterns, including a set with a
  gap in the numbering.
- Ordering: entries emerge oldest-first across members.
- The live member tails; compressed members are not re-read on poll — assert
  the read count.
- `group_rotated = false` lists members individually.
- A corrupt compressed file is reported as `unreadable` and named, per the
  existing discovery contract.

**GUI.** No new binding. Advanced drawer, Source discovery section: "Group
rotated logs" switch. Rotated sets are visually distinct in the tree (member
count on the node label).

**README.** Update the "Any file, not just `*.log`" bullet to cover compressed
and rotated sets; new `settings.conf` row for `group_rotated`; a paragraph
under "Bounded memory" honestly stating that compressed members are the
exception and why.

**As shipped.** Four things this item did not spell out:

- **The exception this item asks for is a *byte* cap, not a line budget.** The
  item says compressed members are "read forward under a *line* budget,
  exactly as `DocumentReader` already does". A line budget bounds *memory*, and
  for a document that is the whole story because extraction stops as soon as
  the budget is met. It cannot do the same job here, because these keep the
  **last** lines rather than the first — a log's newest content is at the end,
  which is the one place a document has nothing worth keeping. Reaching the end
  of a deflate stream means decompressing all of it, so work stays proportional
  to the member no matter what the budget says. The decompressed-byte cap is
  what actually bounds the work, and `compressed.py`'s docstring and the README
  both state the split rather than implying the line budget covers both.
- **A corrupt archive changed what an existing skip *means*.** `*.gz` leaving
  `DEFAULT_EXCLUDE_GLOBS` moved a damaged archive from "unsupported file type"
  to "unreadable", because CLV now supports the format and it is this file that
  is broken. That is the more actionable of the two answers and the reason
  those counters are separate at all, but it did edit an existing test — the
  only one in this phase that needed it. Discovery asks `compressed.probe()`
  instead of the NUL-byte sniff, which would reject every compressed file for
  looking like the compressed bytes it is.
- **Lines carry their origin, and that is what the status line reads.** "The
  status line names which member the cursor is in" needs a per-*entry* answer,
  and the buffer is a bounded deque, so nothing positional survives eviction.
  `TailRead` gained an optional `origins` tuple parallel to `lines`, and
  `SourceBuffer` tags entries with `fields["source"]` when a reader supplies
  one. Deliberately not added to `NORMALISED_FIELD_KEYS`: a key is a query term
  only when it is known, and the buffer's own field names already count, so
  `source:app.log.1` filters exactly when entries carry an origin and stays
  plain text everywhere else. This is the same mechanism Item 13 needs for its
  source column, built once.
- **Grouping is per folder, not per root.** `app.log.1` is a rotation of the
  `app.log` beside it, never of one two directories away that happens to share
  a name.

Two smaller notes: a set with no live head (`app.log.1` + `app.log.2.gz`, the
head already deleted) still groups, and its newest member is what gets tailed —
harmless, since nothing will append to it. And a damaged member costs only
itself: the rest of the set still reads, and opening that member directly is
where the failure is reported.

---

## 12. Wire `LogSourceProvider` and ship a journald plugin

**Goal.** Event Viewer's entire premise is reading *the OS event log*. On Linux
that is the systemd journal — and CLV excludes it (`*.journal` is in
`DEFAULT_EXCLUDE_GLOBS`, and it is binary regardless). Today CLV is an
excellent multi-file tailer that stops precisely where Event Viewer starts.

`LogSourceProvider` is currently dead code: the registry loads it, the status
line counts it, and nothing consults `registry.sources`. This item wires the
interface and ships the plugin that justifies it. The parser needs **no**
changes — `priority` is already in
[`_JSON_LEVEL_KEYS`](clv/services/parsing.py#L270), so `journalctl -o json`
output parses today.

**Production ready when:**

*Wiring (core):*
- `registry.sources` is consulted during discovery; provider-supplied sources
  appear in the tree in their own group, visually distinct from filesystem
  paths.
- `open_reader()` dispatches to a provider-backed reader honouring the same
  `prime()` / `poll()` contract.
- A provider that raises during `discover()` or `open()` is recorded in
  `PluginRegistry.errors` and skipped, exactly as a failing `FilterStage` is.
  A broken provider must not break discovery.
- Provider sources are excluded from operations that assume a real path
  (starring by path, glob filtering) or those operations are generalised —
  whichever, the choice is documented rather than discovered as a bug.

*journald plugin:*
- Ships in `clv/plugins/sources/journald.py`, using `journalctl -o json
  --follow`.
- **Disabled by default.** `clv/plugins/AGENTS.md` states plugins must not
  execute subprocesses without user consent; enabling is an explicit
  `enable_journald` opt-in in `settings.conf`, and the reason is documented.
  This constraint is the main argument for shipping it as a plugin rather than
  in core.
- Absent, non-executable or non-systemd `journalctl` is detected and reported
  as a plugin status message, never a crash. CLV must run normally on a
  non-systemd distribution.
- Exposes per-unit sources (`--unit`), so the tree gets the Event Viewer
  structure — one node per unit is exactly "Windows Logs → Application /
  Security / System".
- Boot selection (`--boot 0`, `--boot -1`, …) surfaced as sources.
- Severity filtering pushes down to `journalctl --priority` when a severity
  bucket is active, rather than reading everything and discarding it
  client-side.
- The subprocess is terminated cleanly on source switch and on app exit —
  including on crash. A leaked `journalctl --follow` per source switch is the
  obvious failure here and must be tested, not assumed.
- Output is read incrementally with a bounded buffer. `--lines` bounds the
  initial read, matching the backwards-seek guarantee.
- `fields` (Item 1) is populated from the journal's own fields (`_SYSTEMD_UNIT`,
  `_PID`, `_HOSTNAME`, `SYSLOG_IDENTIFIER`), which makes Item 8's field queries
  immediately useful against the journal.

**Tests required** (new `tests/test_journald.py`, plus `tests/test_plugins.py`):
- Provider discovery wiring with a fake provider — sources appear in the tree.
- A provider raising in `discover()` and in `open()` is recorded and skipped.
- journald plugin parses a captured `journalctl -o json` fixture into the
  expected entries and `fields`. **Fixture-based, no live systemd** — the suite
  must pass in a container.
- Missing/non-executable `journalctl` reports a status message and does not
  raise.
- Disabled by default: with no opt-in, no subprocess is spawned — assert on a
  patched spawn point.
- Subprocess is terminated on source switch and on app shutdown.
- Severity push-down builds the expected `--priority` argument.
- Unit and boot enumeration from fixture output.

**GUI.** No new binding — journal sources appear in the tree and use every
existing control. Advanced drawer gains a "Journal (systemd)" switch under
Source discovery, disabled with an explanatory caption where `journalctl` is
unavailable. Plugin status line reports journald state.

**README.** New "systemd journal" section covering the opt-in, why it is
opt-in, per-unit and per-boot sources, and the non-systemd fallback; new
`settings.conf` row for `enable_journald`; update the plugin section to note
that `LogSourceProvider` is now wired.

**As shipped.** One claim in this item was simply wrong, and it is the one the
rest of the plan rested on:

- **The parser does *not* read `journalctl -o json` today.** This item states
  "The parser needs **no** changes — `priority` is already in
  `_JSON_LEVEL_KEYS`". It is, but nothing else lines up: the journal emits
  `MESSAGE`, `PRIORITY` and `__REALTIME_TIMESTAMP` — uppercase keys the JSON
  matcher never looks for, a priority that is a numeric string rather than a
  level name, and a timestamp in microseconds since the epoch. Fed in as-is,
  every record parses as a JSON line with **no timestamp, no level, and the
  whole record as its message**. The fix is in the plugin, not in
  `parsing.py`: the plugin chose `-o json`, so it owns what that produces, and
  teaching core about one source's field names would put a niche vocabulary in
  the file every format shares. `translate()` emits a JSON line with
  normalised keys and keeps every original journal field beside them, so both
  `unit:sshd.service` and `_SYSTEMD_UNIT:sshd.service` work.
  `test_untranslated_journal_output_would_not_have_parsed` is kept as the
  record of why the translation exists.
- **Severity push-down only helps for three buckets.** `error`, `warn` and
  `info` map to `--priority=3/4/6`; `debug` and `trace` map to 7, which is
  everything. Pushing those down would filter nothing while looking like it
  did, so nothing is pushed and the item's "when a severity bucket is active"
  is narrowed to "when it would actually filter". A pushed-down priority is
  always a *superset* of what the bucket keeps — the client-side filter still
  decides exactly, and this only avoids carrying what it would discard.
- **The interface gained `open_reader()` rather than changing `open()`.** An
  iterator cannot express tailing, cannot be asked to stop, and has nowhere to
  put a subprocess's cleanup. `open()` still works and core wraps it in
  `IteratorReader`, so nothing written against the old interface breaks.
- **Provider sources are a type, not a path.** `ProviderSource` is what the
  tree carries, and starring, glob filtering and rotated-set grouping all test
  `isinstance(data, Path)`, so they skip it without a single new branch. The
  item asked for the choice between excluding and generalising to be
  documented; excluding won, because a journal unit has no directory to walk
  and no file to persist.
- **The drawer's plugin count now includes a shipped plugin.** Item 3
  deliberately kept built-in exporters out of `clv/plugins/` so the count would
  keep meaning "plugins someone installed". This item asks for journald *in*
  `clv/plugins/sources/`, and for a good reason — a subprocess behind a default
  is exactly what shipping it in core would mean — so the count now reads 1
  where a clean install used to read 0. Recorded rather than quietly fixed:
  the two items want different things and this one wins, because consent
  matters more than a tally.

The subprocess lifetime is handled at two levels: `JournalReader.close()`
terminates and then kills, and `SourceSession` calls it on every source switch
and from `on_unmount`. An exited `journalctl` is *not* respawned by the next
poll — that would be a fork bomb with a nice name — and there is a test for it.

---

## 13. Merged multi-source view

**Goal.** The app tracks a single `_selected_source` and a single entry buffer,
so "centralized" currently means centralized *discovery* — you still read one
file at a time. Selecting several sources and getting one timestamp-ordered
stream is what the product name implies, and unlike remote aggregation (a
stated non-goal) it is entirely local. This is also the honest answer to most
requests for "centralized" features, and the README should say so.

**Production ready when:**
- `x` toggles the tree source under the cursor into the merged set; `u` opens
  the merged view. Members are indicated in the tree.
- Entries merge by timestamp using a k-way merge over the per-source buffers.
  Memory stays bounded: `max_buffer_lines` applies **per source**, and the
  merged view is a view over those buffers, not a fourth copy.
- Entries with no timestamp are **not dropped**. They are anchored after the
  last timestamped entry from their own source, and the merged view reports how
  many are so anchored — the "never silently lose a line" rule applied to
  ordering.
- Mixed tz-aware and naive timestamps are handled through the existing
  `_comparable` normalisation, not by refusing to merge.
- A source column is shown per row, its width responsive and its content
  abbreviated at `-compact`. This is where Item 1's `fields` carries the origin.
- Live tailing across all members, with per-source poll cost unchanged. The
  merged view must not multiply poll frequency by member count.
- Filters, navigation, marks, detail pane and export all operate on the merged
  view identically to a single source. This is the acceptance bar: no feature
  becomes single-source-only.
- Removing a source from the set, or a member disappearing from disk, degrades
  gracefully with a notification.
- Merged set is captured in saved views (Item 9) and persists in
  `SessionState`.

**Tests required** (new `tests/test_merged_view.py`):
- Two and three sources merge into correct timestamp order.
- Untimestamped entries are anchored per source and counted, never dropped.
- Mixed aware/naive timestamps merge without error.
- Per-source buffer caps are respected; total memory is bounded by
  `n * max_buffer_lines`.
- Tailing appends to the correct position in the merged stream.
- Poll count does not scale super-linearly with member count.
- Filtering, `n`/`N` navigation, marks and export each behave correctly on a
  merged view.
- Region assertions at 80 columns with the source column present.
- A member deleted mid-session is reported and dropped.

**GUI.** `x`, `u` (hidden). Tree shows a merge indicator per member and a
count. Status line names the merged set. Export dialog defaults its filename
from the set name.

**README.** New "Merging sources" section, prominently placed — this is the
feature the product name promises; shortcuts rows for `x` and `u`; an explicit
paragraph stating that merging is local-only and that remote aggregation
remains out of scope, so the non-goal does not have to be relitigated in every
issue.

---

# Phase 5 — Analysis

Both items are what push CLV past the tool it is imitating.

## 14. Severity timeline

**Goal.** Event Viewer's "Summary of Administrative Events", except live and
one keystroke from the logs. A single-row sparkline of event volume by severity
across the current window turns "when did this start" from a scrolling exercise
into pointing at a spike.

**Production ready when:**
- A one-row timeline above the log pane, bucketed to fit the available width,
  coloured by the highest severity in each bucket, using the existing
  `SEVERITY_COLORS`.
- Selecting a bucket (keyboard and mouse) sets the time window to that bucket's
  range — the histogram is a control, not decoration.
- Rendered from the in-memory buffer only. No extra file reads, no second pass
  over the source.
- Recomputed incrementally on tail, not rebuilt per poll.
- Degrades explicitly when a source has no parsed timestamps: says so, in the
  same voice as `describe_empty_result`, rather than rendering an empty bar.
- Readable at 80 columns, and does not rely on colour alone — bar height
  carries the volume.
- Visibility toggled by `b`, persisted in `SessionState`.
- Reflects the *filtered* set, so it answers "when did the thing I am looking
  at happen".

**Tests required** (new `tests/test_timeline.py`):
- Bucketing arithmetic across window sizes and terminal widths.
- Bucket colour is the highest severity present in the bucket.
- Selecting a bucket sets the expected `TimeWindow`.
- Source with no timestamps renders the explanatory message.
- Timeline reflects the filtered set, not the raw buffer.
- Incremental update on tail matches a full rebuild — the correctness guard on
  the optimisation.
- Region assertion at 80 columns.

**GUI.** `b` (hidden). Advanced drawer, View section: "Timeline" switch
mirroring `b`, using the established two-homes pattern.

**README.** New "Timeline" subsection with an ASCII example; shortcuts row for
`b`.

---

## 15. Repeat clustering / noise collapse

**Goal.** The single highest-leverage readability feature, and one nothing in
Event Viewer does. Normalise volatile tokens (numbers, UUIDs, IPs, hex, paths)
into a shape, cluster identical shapes, and collapse runs into one row with a
`×147` count and an expand key. This is the difference between a 5000-line
buffer being scrollable and being readable.

**Production ready when:**
- `c` toggles clustering. Off by default — the unmodified view stays the
  default reading experience.
- Clustering operates on a normalised shape derived from `message` and `fields`
  (Item 1), not the raw line, so two entries differing only in a request ID
  cluster together.
- Normalisation rules are a documented, ordered list (integers, floats, hex,
  UUIDs, IPv4/IPv6, timestamps, quoted strings, absolute paths). Not
  configurable in this item; a rules DSL is out of scope.
- **A collapsed cluster is expandable in place** (`Enter` on the cluster row)
  and the underlying lines remain individually selectable, markable and
  exportable. Collapsing is a display transform, never a filter — this is the
  "never silently lose a line" rule, and the item fails without it.
- The cluster row shows count, first and last timestamp, and highest severity
  in the cluster.
- Clusters form only over a bounded lookback window, so cost stays linear in
  buffer size and a cluster cannot span the whole session.
- Clustering runs on the filtered set, after plugin stages, and is recomputed
  incrementally on tail.
- Measurable cost documented: clustering a full 5000-line buffer must stay
  within a stated frame budget, benchmarked in the PR.
- Export offers "expanded" (default) or "clustered" output.

**Tests required** (new `tests/test_clustering.py`):
- Each normalisation rule, individually and in combination.
- Two lines differing only in a request ID cluster; two genuinely different
  lines do not.
- Cluster metadata: count, first/last timestamp, highest severity.
- Expanding a cluster yields exactly the original lines, in order, byte-identical
  — the no-loss guarantee.
- Marks and export work on lines inside a collapsed cluster.
- Lookback bound: entries beyond the window do not cluster together.
- Incremental clustering on tail matches a full recompute.
- Benchmark test asserting the frame budget on a 5000-line buffer.
- Clustering applies after plugin `FilterStage`s.

**GUI.** `c` (hidden), `Enter` to expand a cluster row (shares the binding with
the detail pane; a cluster row expands, a normal row opens detail). Advanced
drawer, View section: "Collapse repeats" switch. Status line reports "N lines
in M clusters".

**README.** New "Noise reduction" section with a before/after example;
shortcuts row for `c`; an explicit statement that clustering never hides a line
and that every clustered line stays selectable and exportable.

---

# Deliberately out of scope

Recorded so these do not have to be re-argued each time they are proposed.

- **Remote collection, multi-node aggregation, remote tailing.** A documented
  non-goal. Item 13 (local merge) is what most requests for this actually want,
  and the README should say so directly.
- **A query DSL with boolean operators, parentheses and precedence.** Item 8
  stops deliberately at `key:value` terms with implicit AND.
- **Schema-aware pipelines and log-format definition files.** The parser
  auto-detects; that is the product.
- **Background daemons and privileged operations.** Item 10's watch rules run
  in-process, in the foreground, for the life of the session only.
- **`.zst` support.** No stdlib decompressor; it would be the first new runtime
  dependency and does not clear the bar.
- **PDF and other reflowed-prose formats.** Already reasoned through in
  `discovery.py`; the conclusion has not changed.
- **Desktop notification daemons for Item 10.** Would require a subprocess or a
  D-Bus dependency; the in-app notification and optional bell are enough.
