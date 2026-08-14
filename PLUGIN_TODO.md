# PLUGIN_TODO — A plugin ecosystem, not a plugin hook

Planned work, in dependency order. Each phase assumes the phases above it have
landed. Every phase ends in a commit, and every phase leaves `main` shippable:
no phase may land a half-wired seam that only the next phase makes safe.

The ordering is deliberate, and it is the opposite of the tempting one. The
obvious first move is "add a user plugin directory" — that is the blocker
everyone can see. It is Phase 3 here, because opening a public install path on
top of a loader that silently ignores a plugin with no `register()`, that
serves the wrong provider's lines when two identifiers collide, and that
rejects `~=2.6` as unsatisfied, converts six latent defects into six classes of
bug report from people who cannot read the source to work out what happened.
Correctness and the contract come first; the door opens third; the seams that
make the door worth walking through come after that.

Seventeen phases is a lot. The alternative was to declare half of them out of
scope, which is what the first draft of this file did — and the objection to
that draft was correct: a plugin system that cannot touch the query language,
the watch rules, the clustering, the timeline or the screen is not an
ecosystem, it is three hooks and a directory.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| **Stage A — Foundations** | | |
| 0 — Doctrine | Withdraw the sandbox claim; reverse three stated non-goals | ✅ Done |
| 1 — Loader correctness | The six defects, before anyone depends on them | ⬜ Not started |
| 2 — The contract | `clv/api.py`, `PLUGIN_API_VERSION`, the entry wire form | ⬜ Not started |
| **Stage B — Reach** | | |
| 3 — Installation | `~/.config/clv/plugins/`, `CLV_PLUGIN_PATH`, the enable-list | ⬜ Not started |
| 4 — Management UI | A plugin surface, not a status string | ⬜ Not started |
| 5 — Ordering, config, lifecycle | `priority`, `[plugin:<name>]`, `setup`/`teardown` | ⬜ Not started |
| 6 — Performance guard | A slow plugin costs itself, not the pane | ⬜ Not started |
| **Stage C — Core seams** | | |
| 7 — Parsing | `LogFormat`, and a plugin that teaches CLV a format | ⬜ Not started |
| 8 — Query | `QueryOperator`, `ComputedField`, and the degradation rule | ⬜ Not started |
| 9 — Watch | `WatchMatcher`, `WatchSink`, off the event loop | ⬜ Not started |
| 10 — Clustering | `ClusterRule`, `ShapeContributor`, and the shape cache | ⬜ Not started |
| 11 — Timeline | `TimelineAnnotation`, `TimelineMetric`, foldable only | ⬜ Not started |
| **Stage D — Surface** | | |
| 12 — Commands and controls | Commands, bindings, drawer sections, modal screens | ⬜ Not started |
| **Stage E — Trust and distribution** | | |
| 13 — Isolation | An opt-in subprocess host, and honesty about what it buys | ⬜ Not started |
| 14 — The CLI layer | `clv` grows an argv, without changing what bare `clv` does | ⬜ Not started |
| 15 — Registry | Manifests, `clv plugin install`, signatures, no hosted index | ⬜ Not started |
| **Stage F — Release** | | |
| 16 — Documentation & release | The plugin chapter, worked examples, 3.0.0 | ⬜ Not started |

---

## Goal

Let someone who has never read CLV's source write a plugin, publish it, and
have an operator who installed CLV from a `.deb` use it — without `sudo`,
without a Python toolchain, and without the plugin being able to take the
viewer down.

The bar is **a plugin extends a core feature on equal terms with a built-in**.
Not "beside" it: a plugin-supplied log format must be searchable by field query,
bucketed by the timeline and folded by the clusterer with no special-casing
anywhere; a plugin-supplied query operator must work in a saved view and in a
watch rule; a plugin-supplied command must appear in the help overlay next to
`?`. Where a seam cannot offer equal terms, the phase says so in the same
sentence it offers the seam.

Three things follow that bound every phase below.

**The audience is the binary user.** CLV's primary distribution is a PyInstaller
tree under `/opt/centralized-log-viewer` installed from a `.deb`, `.rpm` or
tarball ([README.md:99-121](README.md#L99-L121)). For that user the current
system has *no* install path at all: the `clv.plugins` entry point group needs
a pip environment a frozen build does not have, and the drop-in directory is
inside a root-owned bundle that a package upgrade overwrites. Every phase here
is measured against that user, not against `pip install -e .`.

**A plugin is trusted code, and isolation changes that less than it sounds.**
[AGENTS.md:14](clv/plugins/AGENTS.md#L14) says plugins "are sandboxed through
defined interfaces." They are not. `import` executes arbitrary code at CLV's
full privilege before any interface check runs. Phase 13 adds a real subprocess
host, and it is a real improvement — a plugin that crashes, hangs or leaks can
be killed, which is impossible today. It is still not a sandbox: the host runs
as the operator and can read every file the operator can. Phase 0 deletes the
false claim; Phase 13 is written so it cannot be replaced by a subtler one.

**Two core seams cannot be isolated, and that is a property of the render path,
not a shortcut.** `FilterStage.apply` and `LogFormat.parse` are called per entry
and per line. `LogEntry` cannot even be pickled — `fields` is a `mappingproxy`,
including the shared empty one, so `pickle.dumps` raises `TypeError` on every
entry CLV produces (verified). Phase 2 gives it a wire form so isolation is
possible at all; Phase 13 still refuses isolation to per-entry kinds, out loud,
because an IPC round trip per line is not a performance trade-off, it is a
different program.

---

## Requirements

Numbered so a phase can cite one, and so a later argument does not have to be
had twice.

1. **An operator can install a plugin without root and without Python.**
   Copying one file into a directory under `$HOME` is the whole procedure, and
   it works identically on a frozen build and a source checkout.
2. **Installing a plugin is not consent to run it.** A file present in the
   plugin directory is discovered and listed; it does not execute until the
   operator names it. This holds at the CLI too: `clv plugin install` installs
   and does not enable.
3. **The documentation never claims a protection that does not exist.** Where
   CLV cannot enforce a rule, it says the rule is a convention and says who is
   trusting whom. This applies to Phase 13's isolation with more force than to
   anything else in this file.
4. **A plugin written against the published API keeps working across a CLV
   minor release.** What is published is explicit, versioned separately from
   CLV's own version, and small enough to be worth freezing.
5. **A plugin cannot take CLV down, slow it down, or make it lie.** The existing
   failure isolation is preserved and extended to cover time, not just
   exceptions.
6. **Plugin failures are legible to a non-developer.** "Which plugin, what it
   did, what to do about it" — in the UI, not only in a log.
7. **No new runtime dependency.** The minimal-dependency policy in
   [pyproject.toml](pyproject.toml) is not relaxed for this work. Everything
   below is standard library, including the isolation host and the CLI.
8. **Every phase leaves the suite green on Python 3.11 and 3.14.** 3.11 is the
   floor the release binaries are built against, and a dataclass default that
   works on 3.14 has already failed to import on 3.11 once
   ([parsing.py:227-231](clv/services/parsing.py#L227-L231)).
9. **A plugin extends each seam on equal terms, provably.** Every seam phase
   ends with a test that a plugin-supplied thing works through a core feature
   that knows nothing about plugins — a plugin format's entries in the timeline,
   a plugin operator in a saved view, a plugin rule in the clusterer.
10. **Nothing here regresses a build with no plugins.** Loading zero plugins is
    a valid state, stays silent, and costs nothing measurable. Every seam is a
    no-op when its registry is empty, asserted per phase.
11. **A plugin cannot break the layout.** The responsive breakpoints and the
    80-column floor are CLV's, and every breakpoint test stays unconditional on
    what is installed. This is why Phase 12 hands out a constrained widget
    vocabulary rather than a widget.
12. **State that references a plugin degrades by preserve-disable-explain.** A
    saved view, watch rule or session whose plugin is missing is kept
    byte-intact, marked unusable, and reported by name with the plugin it needs.
    Nothing is silently reinterpreted into meaning something else.
13. **Bare `clv` never changes.** The CLI layer in Phase 14 adds subcommands;
    `clv` with no arguments launches the TUI exactly as it does today, and a
    plugin can never add a subcommand that shadows that.

---

## Decisions already taken

Recorded so they are not relitigated per phase.

| Decision | Choice | Why |
| --- | --- | --- |
| Scope | **Every seam, including the ones the first draft declined** | Query terms, watch rules, clustering, timeline, screen, isolation and distribution are all in. The argument that they are speculative was wrong in one specific way: they are not speculative to *CLV*, which uses every one of them itself, and a plugin that cannot reach them is a second-class author writing against a first-class core. |
| Install path | **`~/.config/clv/plugins/`, plus `CLV_PLUGIN_PATH`** | Beside `settings.conf`, which CLV already creates on first run and already tells the operator about ([README.md:133](README.md#L133)). One place, already in their muscle memory. The env var is for development and for the tests, not for users. |
| Security posture | **Honest documentation, an explicit enable-list, and opt-in isolation** | The sandbox claim goes. A plugin is trusted code, stated plainly; a file in the plugin directory is inert until named; and a plugin that declares `isolated = True` gets a subprocess it can be killed in. Three separate things, and the documentation never lets them blur into "sandboxed". |
| Isolation model | **Author opts in, CLV gates by kind** | A plugin declares `isolated = True`. Coarse-grained kinds — `Exporter`, `LogSourceProvider`, `WatchSink`, `Command` — get a host. Per-entry kinds — `FilterStage`, `LogFormat`, `QueryOperator`, `ComputedField`, `ClusterRule`, `ShapeContributor`, `TimelineMetric` — are refused with a stated reason. Opt-in rather than mandatory because a five-line exporter should not pay subprocess startup it did not ask for. |
| What isolation buys | **Failure containment, not safety** | A host can be killed on crash, hang or timeout; that is the first time in CLV's history a plugin can be *stopped*. It does not make an untrusted plugin safe: the child runs as the operator with the operator's filesystem. Said in exactly those words in the docs, per Requirement 3. |
| API surface | **`clv/api.py` with `PLUGIN_API_VERSION`** | A thin published module re-exporting the frozen contracts. CLV's version tracks the app; the API version tracks the promise. A plugin declares `requires_api = ">=1.0,<2.0"` and stops caring what `clv.__version__` says. |
| API mechanism | **Re-export in-process, explicit codec across a process** | `clv.api` re-exports the real `LogEntry` and `FilterSpec` rather than converting to DTOs — a conversion per entry per render is the one cost the performance finding says CLV cannot pay. Crossing a process boundary is the exception, and gets a versioned wire form because `mappingproxy` makes pickle impossible anyway. |
| Seam mechanism | **Injection, never import** | No service imports `clv.plugins`. `parsing`, `query`, `clustering`, `timeline` and `watch` each take their extensions as a parameter or from a registry the app installs at startup. The dependency already runs the other way and [TODO.md:246](TODO.md#L246) records the argument. |
| Degradation | **Preserve, disable, explain** | A saved view or watch rule referencing an absent plugin is kept intact, marked unusable and named. Falling back to free text was rejected: [query.py:18-38](clv/services/query.py#L18-L38) exists precisely to stop a query silently meaning something else, and a disabled plugin must not do what an unknown field key was designed not to do. |
| UI seam depth | **Commands, bindings, drawer sections, modal screens — no `compose()` injection** | Plugins get a constrained widget vocabulary that CLV styles and CLV lays out. Styling is CSS-only by doctrine, `BINDINGS` has hand-tuned 80-column footer ordering ([app.py:531-583](clv/app.py#L531-L583)), and a plugin widget in the main tree makes every breakpoint test conditional on what is installed. Requirement 11. |
| Registry | **Manifests and a CLI, no hosted index** | `clv plugin install/remove/list/verify` against a signed manifest, installing from a path, tarball or URL. Anyone can host. A hosted index is a server, a namespace and a moderation queue — an operational commitment, not a feature, and one that turns every listing into a trust signal CLV is issuing. |
| Provider sources | **Stay second-class here** | Making a `ProviderSource` starrable, mergeable and session-persistable means replacing bare `Path(entry)` reconstruction in eight call sites — which is [SSH_TODO.md](SSH_TODO.md) Phase 1 precisely, already planned, already scoped. See *Relationship to SSH_TODO*. |
| Loading model | **Import-time, single-shot, at startup** | No hot reload. `load_plugins()` runs once at mount ([app.py:724](clv/app.py#L724)) and that stays true. Enable and disable from the drawer changes which loaded plugins are *active*, never which modules are imported. |
| Version comparison | **A real PEP 440 subset, hand-rolled** | `packaging` is not a dependency and will not become one (Requirement 7). The current comparator is ~40 lines and gets prereleases wrong; a correct subset is ~80 and is fully testable. |
| Release target | **CLV 3.0.0, plugin API 1.0** | The doctrine reversals and the new argv behaviour are major-version news. The plugin API is additive throughout and stays 1.0 — which is the separation in Phase 2 doing its job, visibly, on its first outing. |

---

## The interfaces, at the end of this file

Twelve, from three. Listed here so the phases can be read against the whole
rather than one at a time.

| Interface | Phase | Kind | Isolable |
| --- | --- | --- | --- |
| `LogSourceProvider` | exists | Where lines come from | ✅ |
| `FilterStage` | exists | Transform or drop an entry | ❌ per-entry |
| `Exporter` | exists | Send the filtered set somewhere | ✅ |
| `LogFormat` | 7 | Teach CLV to parse a line | ❌ per-line |
| `QueryOperator` | 8 | A new comparison token | ❌ per-entry |
| `ComputedField` | 8 | A queryable field derived, not parsed | ❌ per-entry |
| `WatchMatcher` | 9 | A rule kind beyond pattern | ❌ per-entry |
| `WatchSink` | 9 | Where a watch hit is delivered | ✅ |
| `ClusterRule` | 10 | A volatile token to normalise out | ❌ per-line |
| `ShapeContributor` | 10 | An extra component of a cluster's shape | ❌ per-entry |
| `TimelineAnnotation` | 11 | Marks on the time axis | ✅ |
| `TimelineMetric` | 11 | What a bucket measures, if not count | ❌ per-entry |
| `Command` | 12 | A named action, optionally bound to a key | ✅ |

---

## Relationship to SSH_TODO

Both files touch `clv/plugins/`. They do not conflict, and the boundary is
worth stating because it is easy to blur.

[SSH_TODO.md](SSH_TODO.md) makes **one particular kind of source** first-class:
its Phase 1 introduces `SourceRef` and removes the bare `Path(entry)`
reconstruction that makes any non-filesystem source unstarrable, unmergeable
and unrestorable. That is core surgery in `app.py`, `config.py`, `sources.py`
and `storage.py`, and it happens to fix `ProviderSource` as a side effect.

This file makes **the extension mechanism itself** usable by strangers, and
extends it to every core feature.

The overlap is exactly one line item: provider-source parity. It belongs to
SSH_TODO, is not attempted here, and the honest statement of the current
limitation stays in the docs until SSH_TODO Phase 1 lands, at which point
Phase 16's documentation pass is where it gets rewritten.

**Ordering between the two files is mostly free.** Stage A here is strictly
additive and can land during SSH_TODO's Phase 0–2 without interference. Two
couplings, both one-directional:

- **Phase 7 should follow SSH_TODO Phase 1** if both are in flight: a format
  plugin's `field_names` interacts with the `node` field that phase introduces.
- **Phase 12's drawer sections should follow SSH_TODO Phase 7**, which adds the
  host-management dialog and is the larger claim on the drawer's layout budget.

---

# Stage A — Foundations

Nothing here is user-visible. All three phases are prerequisites for everything
below, and doing them later means reworking every seam against a contract that
had already been published wrong.

## Phase 0 — Doctrine

Documentation only. No source file is modified. It is first because it is the
only phase that fixes something actively wrong today, and because this file now
contradicts three separate written non-goals that have to be reversed on the
record rather than quietly outgrown — the precedent being
[SSH_TODO.md](SSH_TODO.md)'s own Phase 0, and the rule in
[TODO.md](TODO.md) that a decision is rewritten with its reversal rather than
deleted.

**Expected outcomes**

*The false claim goes.*

- [AGENTS.md:14](clv/plugins/AGENTS.md#L14) — *"Plugins are sandboxed through
  defined interfaces"* — is **deleted**, not softened. In its place, a
  `Trust model` section that states: a plugin is Python executed at CLV's
  privilege in CLV's process; it can read anything the operator can read,
  including every log CLV has open; the interfaces bound what CLV *asks* of a
  plugin, not what a plugin *can do*; installing a plugin is the same act of
  trust as installing any other program.
- The section is written so that Phase 13's isolation can be added to it
  **without** the word sandbox: a third heading, `What isolation does and does
  not do`, is stubbed now with the honest sentence, so the later phase has
  nowhere to quietly upgrade the claim.
- [AGENTS.md:251-254](clv/plugins/AGENTS.md#L251-L254) — the "Security and
  Safety" rules are relabelled **conventions for plugin authors**, with the
  enforcement status of each stated inline.
- A `Reviewing a third-party plugin` section: what to read before enabling one,
  in the order to read it — imports first, then `discover()`, then anything
  touching `subprocess`, `socket` or a write path.

*Three non-goals are reversed, on the record.*

- **"No rules DSL" for clustering.** [clustering.py:36](clv/services/clustering.py#L36)
  states it as a non-goal. Phase 10 adds plugin normalisation rules. The
  docstring is rewritten to record that the decision was reversed, when, and
  why: the objection was to *the operator* hand-writing regex rules in
  `settings.conf`, and that objection stands. A plugin author writing Python
  against a reviewed interface is a different party, and the rules stay
  unconfigurable from `settings.conf`.
- **"No query DSL."** [query.py:15-16](clv/services/query.py#L15-L16) declines
  `OR`, parentheses and precedence, citing `TODO.md`. Phase 8 adds operators
  and computed fields and **does not** add any of those three. The reversal is
  narrow and the docstring says exactly how narrow: the grammar stays
  implicit-AND, flat, and one step short of the line.
- **`AGENTS.md`'s `Non-Goals` list** is reconciled with the isolation host and
  the registry. Whatever remains a non-goal keeps its entry; anything this file
  contradicts is rewritten with the reversal and a pointer here.

**Documentation changes.** This phase *is* the documentation change.
[README.md:65-67](README.md#L65-L67)'s plugin bullet gains the words "trusted
code" so the claim and the summary do not disagree.

**Testing.** None beyond the suite staying green — no code changes. Add
`tests/test_plugin_docs.py` with one test asserting the string `sandbox` does
not appear in `clv/plugins/AGENTS.md`, so the claim cannot come back by
accident during a later edit — including during Phase 13, which is when it
would be most tempting.

**Gate.** `python -m pytest` reports 722 passed on 3.11 and 3.14 (717 before
this phase, plus the five in `tests/test_plugin_docs.py`). A reader of
`clv/plugins/AGENTS.md` alone can correctly answer "what can a plugin I install
do to me?" and can find, for each reversed non-goal, what changed and why.

**Commit.** `docs(plugins): withdraw the sandbox claim, reverse three non-goals`

---

## Phase 1 — Loader correctness

The six defects, fixed before anyone outside the project depends on the current
behaviour. No new capability; the loader simply stops being wrong in ways a
plugin author cannot diagnose.

**Expected outcomes**

*A plugin that exports nothing is reported, not ignored.*
[`_extract_plugins`](clv/plugins/__init__.py#L409-L429) returns `[]` when a
module has neither `register()` nor `__all__`, and `_load_local` then records
nothing at all — zero plugins loaded, zero errors, no clue. It gains a third
strategy and a diagnosis: scan the module's own namespace for `Plugin`
subclasses defined in that module, use them if found, and if the module defines
no subclass and no export, record `"defines no plugin — add register() or
__all__"`. This is the single most likely first-run experience for a new author
and today it is silent.

*Two providers cannot shadow each other.*
[`_owners`](clv/plugins/__init__.py#L353) is keyed on `ProviderSource.path`
alone, so the second provider to offer an identifier silently wins — verified:
selecting provider A's row yields provider B's lines. The key becomes
`(provider_name, path)`, and a genuine collision is reported once at discovery
with both provider names rather than resolved by luck.

*A failing plugin is disabled until the operator re-enables it.*
[`apply_filters`](clv/plugins/__init__.py#L382-L406) disables a raising stage
for the current pass only, so it is retried on the next render and appends a
fresh identical error each time — 200 render passes produced 200 errors,
verified. The `broken` set moves onto the registry and persists for the
session; the error is recorded **once**. This is written as a general
`disable(plugin, reason)` on the registry rather than a filter-specific fix,
because Phases 6 through 13 each need to disable a plugin for their own reason
and none of them should invent a second mechanism.

*Errors are bounded and deduplicated.* `PluginRegistry.errors` grows without
limit and [app.py:2046-2049](clv/app.py#L2046-L2049) prints every one into the
log panel. It becomes a bounded, deduplicating collection: identical
`(origin, message)` pairs collapse with a count, and the total is capped with
an explicit "and N more".

*Version constraints follow a real spec.*
[`_version_tuple`](clv/plugins/__init__.py#L226-L231) strips non-digits per
segment, so `"2.6.0rc1"` becomes `(2, 6, 1)` — a release candidate compares as
*newer* than its release. `~=` and `^` are rejected outright as unsatisfied,
silently disabling a plugin whose author wrote the most idiomatic constraint in
the Python ecosystem. Replaced by a PEP 440 subset: epoch-free release
segments, prerelease ordering, `~=` compatible-release, and Poetry's `^`
accepted as a documented alias. An **unparseable** constraint is an error
naming the constraint, never a silent False.

*An entry point may point at a factory.*
[`_load_entry_points`](clv/plugins/__init__.py#L488-L501) falls through to
`candidates = [loaded]` for a plain function, which then fails `add()` with
"does not implement a CLV plugin interface" — a misleading message for a
correct-looking `entry_points = {"clv.plugins": ["x = mypkg:make_plugin"]}`.
The three legal target shapes — module, class, zero-argument callable — are
each handled and each documented.

*Tests stop writing into the source tree.*
[test_plugins.py:142-207](tests/test_plugins.py#L142-L207) writes `.py` files
into the live `clv/plugins/filters/` and `exporters/` directories; the stale
`tmp_broken_plugin` and `tmp_test_plugin` bytecode in
`clv/plugins/filters/__pycache__/` is the residue. Rewritten against a
`monkeypatch`-ed search root, and against `CLV_PLUGIN_PATH` once Phase 3 lands.

**Documentation changes.** `clv/plugins/AGENTS.md` gains the three legal entry
point shapes, the namespace-scan fallback with its diagnosis, the supported
constraint grammar with a worked prerelease example, and the rule that a plugin
disabled at runtime stays disabled for the session.

**Testing** (extend `tests/test_plugins.py`)

- A drop-in defining a `FilterStage` subclass with no `register()` and no
  `__all__` is **loaded** by namespace scan; one defining neither is reported
  with the "defines no plugin" message.
- Two providers offering the same identifier: both appear, each opens its own
  lines, and the collision is reported once.
- A raising stage: one error for N passes, the stage stays disabled, the
  remaining stages keep running, the entries still render.
- `disable(plugin, reason)` is idempotent and records one error however many
  times it is called.
- Error collection: 500 identical failures collapse to one entry with a count;
  the cap holds and says how many were dropped.
- Constraint matrix, table-driven — `>=2.0,<3.0`, `~=2.6`, `^2.0`, `==2.6.*`,
  `!=2.6.0`, prerelease vs release both directions, `>=2.10` against `2.6`,
  whitespace, and three malformed strings each producing an error.
- Entry point as module, as class, as factory function — all three load.
- No test writes to `clv/plugins/`; a test asserts the directory is unchanged
  after the suite runs.

**Gate.** Every defect in the investigation has a failing-before/passing-after
test. The shipped journald plugin loads and opens a source unchanged — this
phase touches the loader, and the one real plugin in the tree is the regression
check. Suite green on 3.11 and 3.14.

**Commit.** `fix(plugins): loader correctness before the door opens`

---

## Phase 2 — The contract

What third parties write against on day one is what CLV is stuck with. This
phase decides it deliberately rather than by accident of which imports happened
to work — and it is where the wire form for Phase 13 is settled, because
deciding that later would mean changing a published type.

**Expected outcomes**

- **New `clv/api.py`** — the entire published surface, re-exporting the real
  types rather than wrapping them (see Decisions). At this phase it carries the
  three existing interfaces and their data; each seam phase adds its own names
  to the same module and to the same frozen list.

  ```python
  PLUGIN_API_VERSION = "1.0"

  # interfaces
  Plugin, LogSourceProvider, FilterStage, Exporter
  # data handed to a plugin
  LogEntry, FilterContext, FilterSpec, TimeWindow, ProviderSource
  # data a plugin hands back
  ExportResult
  # helpers a plugin would otherwise reimplement badly
  normalize_level, level_rank, LEVEL_DEBUG … LEVEL_CRITICAL
  NORMALISED_FIELD_KEYS
  # crossing a process boundary (Phase 13 uses these; they are published now
  # so the wire form is part of the frozen contract rather than an artefact)
  WIRE_VERSION, entry_to_wire, entry_from_wire
  ```

- `PLUGIN_API_VERSION` is **separate from `clv.__version__`**. CLV 2.7 through
  3.0 all carry API 1.0; the API version moves only when a published name
  changes meaning. `requires_api` joins `requires_clv` on `Plugin`, is checked
  by the comparator Phase 1 rebuilt, and is the constraint the documentation
  tells authors to use.
- **A wire form for `LogEntry`.** `pickle.dumps` on any entry CLV produces
  raises `TypeError: cannot pickle 'mappingproxy' object` — verified, including
  for the shared empty mapping, so this is not an edge case but every entry.
  `entry_to_wire` produces a plain-`dict` form with an explicit `WIRE_VERSION`;
  `entry_from_wire` reconstructs, restoring the read-only mapping. Round-trip
  fidelity is a frozen test, not an aspiration, because Phase 13 and any future
  out-of-process work both rest on it.
- **A written deprecation policy**, in `clv/plugins/AGENTS.md`: a published
  name is removed only on an API major; a name deprecated in API *N* keeps
  working through all of *N* and emits a `DeprecationWarning`; anything not in
  `clv.api` is internal and may move without notice, including
  `clv.services.parsing.LogEntry` under its own name.
- **The freeze is mechanical.** `tests/test_api_surface.py` holds the expected
  `clv.api.__all__` and the signature of every published callable as literal
  data. Changing the API means changing that file, which means a reviewer sees
  it in the diff. Every seam phase below updates it, deliberately, as part of
  its own diff.
- **`Exporter` gains the destination it was missing.** Today plugin exporters
  are listed with `needs_path=False` and the dialog disables its path input
  ([app.py:3105-3121](clv/app.py#L3105-L3121)), so an exporter cannot honour the
  location the operator just chose. `Exporter` gains `wants_path: bool = False`
  and `suggested_extension: str = ""`, and `export()` gains an optional
  `destination` keyword. An exporter that does not declare `wants_path` is
  called exactly as today.
- `clv.plugins` keeps re-exporting everything it exports now, so every existing
  import path continues to work; `clv.api` is the *recommended* one and the only
  one covered by the policy.

**Documentation changes.** `clv/plugins/AGENTS.md` gains an `API surface and
stability` section: the published list, `requires_api` with examples, the
deprecation policy, the wire form and what it is for, and the rule that an
import from `clv.services.*` is a plugin taking a risk it has been warned about.
Every example in that file and in [README.md:715-761](README.md#L715-L761) is
rewritten to import from `clv.api`.

**Testing** (new `tests/test_api_surface.py`, extend `tests/test_plugins.py`)

- `clv.api.__all__` matches the frozen list exactly — both directions.
- Every published callable's signature matches the frozen record.
- Importing `clv.api` does not import `clv.app` or any widget: the API is
  usable from a plugin's own unit tests without a Textual screen.
- `requires_api` accepted, checked, reported on failure with both versions named.
- Wire round trip: entries from every built-in format, an entry with an empty
  `fields`, a continuation entry, an entry with non-ASCII and with embedded
  NULs. `entry_from_wire(entry_to_wire(e)) == e` and `fields` comes back
  read-only.
- A wire payload with an unknown `WIRE_VERSION` is rejected with a clear error
  rather than half-decoded.
- Exporter without `wants_path` is called with the current signature; one with
  `wants_path=True` receives the operator's chosen destination.

**Gate.** A plugin file whose only CLV import is `from clv.api import ...`
loads and runs every interface. An entry survives a wire round trip unchanged
for every format CLV parses. Suite green on 3.11 and 3.14.

**Commit.** `feat(api): a published, versioned plugin API and an entry wire form`

---

# Stage B — Reach

The plugin system becomes installable, visible, configurable and bounded. After
this stage a third party can ship something and an operator can run it; the
seams in Stage C are what make that worth doing.

## Phase 3 — Installation

The unblocker, and the phase the whole file exists for. It lands third because
Phases 1 and 2 are what make it safe to be depended on.

**Expected outcomes**

- **`~/.config/clv/plugins/` is a search root**, created on first run alongside
  `settings.conf`, with a `README.txt` in it explaining what to put there and
  linking the trust model from Phase 0. Both a bare `foo.py` and a package
  directory `foo/` with an `__init__.py` are valid.
- **`CLV_PLUGIN_PATH`** — an `os.pathsep`-separated list of additional roots,
  prepended to the search. For development and for the tests. Documented as
  such, not as a user feature.
- **Search order is defined and reported**: `CLV_PLUGIN_PATH`, then
  `~/.config/clv/plugins/`, then the bundled `clv/plugins/` drop-ins, then
  `clv.plugins` entry points. First name wins; a shadowed plugin is
  **reported**, not silently dropped.
- **The enable-list.** A new `plugins` key in `[log_viewer]`, comma-separated,
  empty by default. A module found in a user root is **discovered and listed
  but not imported** until its name appears there. This is Requirement 2, and
  it is a list rather than a boolean because "enable everything in this
  directory" is exactly the behaviour that makes a dropped file dangerous.

  ```ini
  [log_viewer]
  # Plugins to load from ~/.config/clv/plugins/. A file placed there is listed
  # but not run until it is named here. A plugin is trusted code: it runs with
  # your privileges and can read every log CLV can open.
  plugins = redact_secrets, nginx_format
  ```

- **Bundled plugins keep their current behaviour.** `clv/plugins/sources/`,
  `filters/` and `exporters/` load without being named: they shipped with CLV
  and the operator's trust in them is the trust they placed in CLV. The
  enable-list governs the user directory only. The journald opt-in is
  unaffected.
- **Discovery of an unlisted plugin costs nothing.** The file is stat'd and its
  name recorded; it is never imported, so an unlisted plugin cannot execute
  code, and a directory of 200 unlisted files does not slow startup.
- A named plugin that is **not present** is reported by name — a typo in
  `settings.conf` says so rather than doing nothing.
- **Frozen builds are equal citizens.** The user root is a real directory in
  every build. The `Path.is_dir()` lesson from
  [`_load_local`](clv/plugins/__init__.py#L432-L442) applies only to the
  bundled subpackages and their handling is unchanged.

**Documentation changes.** `DEFAULT_SETTINGS_TEMPLATE`
([config.py:49](clv/services/config.py#L49)) gains the commented `plugins` key
with the trust sentence, matching how `enable_journald` documents itself at
[config.py:117](clv/services/config.py#L117); the shipped
[settings.conf](settings.conf) gains the same. `README.md` gains an **Installing
a plugin** subsection — three lines of shell, the config key, and the trust
model in two sentences. The plugin bullet at
[README.md:65-67](README.md#L65-L67) is rewritten.

**Testing** (extend `tests/test_plugins.py`, `tests/test_config.py`)

- A module in a `CLV_PLUGIN_PATH` root named in `plugins` loads; the same
  module not named does **not** load and is listed as discovered-not-enabled.
- **An unlisted module is never imported** — asserted by a module whose import
  writes a sentinel file, and the assertion is that the file does not exist.
  This is Requirement 2's teeth and must not be weakened.
- Package-directory plugins load the same as single files.
- Search order: the same plugin name in two roots resolves to the earlier root
  and reports the shadowing.
- A named-but-absent plugin is reported by name.
- Bundled drop-ins still load without appearing in the enable-list; journald
  still respects `enable_journald`.
- Missing, unreadable, and non-Python-containing plugin directories: each is a
  silent non-event or a report, never a raise.
- `plugins` parses with whitespace, trailing commas, duplicates and mixed case;
  a malformed value degrades to empty and is reported.
- Startup cost with 200 unlisted files stays within noise of an empty directory.

**Gate.** On a machine with CLV installed from the tarball and no Python
toolchain: copy a `.py` into `~/.config/clv/plugins/`, add one word to
`settings.conf`, restart, and the plugin is in the drawer's count. Checked by
hand on a real frozen build. Suite green on 3.11 and 3.14.

**Commit.** `feat(plugins): user plugin directory and the enable-list`

---

## Phase 4 — Management UI

The operator can now install a plugin. This phase is how they see what happened.
Today the entire surface is one `Static` line
([advanced_drawer.py:340](clv/widgets/advanced_drawer.py#L340)) rendering a
sentence that concatenates counts with the first three errors truncated
([app.py:3600-3614](clv/app.py#L3600-L3614)) — adequate when the only plugin was
one CLV shipped, useless when the operator installed four, and unusable by the
time Stage C makes twelve interfaces available.

**Expected outcomes**

- **A plugin section in the Advanced drawer**, replacing the status string with
  one row per plugin: name, kind(s), state, and origin. Five states —
  **loaded**, **disabled** (present, not enabled), **failed** (with the
  reason), **incompatible** (with the constraint and the running version), and
  **isolated** (Phase 13 fills this in; the state exists now so the row layout
  is not redesigned later).
- A plugin may implement several interfaces, so a row lists kinds rather than
  one kind — the shape Stage C makes normal.
- **Enable and disable from the UI**, writing back to `settings.conf` through
  the existing `persist_setting` path that `Ctrl+S` and the journald switch
  already use ([app.py:3454-3477](clv/app.py#L3454-L3477)). Enabling a plugin
  that has never been imported needs a restart and **says so**; disabling a
  loaded one takes effect immediately.
- **Re-enabling a plugin disabled by a failure** — the control that Phase 1's
  session-persistent `disable()` was written to need, and that Phases 6, 9 and
  13 all depend on existing.
- **Errors are readable.** A failed plugin's row expands to the recorded
  message; the truncated three-error concatenation goes away. The log-panel
  dump at [app.py:2046-2049](clv/app.py#L2046-L2049) becomes a single line
  pointing at the drawer, so a wall of plugin errors cannot bury the discovery
  summary an operator opened CLV to read.
- **80 columns holds.** Every row degrades to name-plus-state at the narrowest
  breakpoint, per the rules the drawer already lives under
  ([advanced_drawer.py:21-22](clv/widgets/advanced_drawer.py#L21-L22)).
- **The zero state is silent.** No plugins installed renders nothing new
  (Requirement 10).

**Documentation changes.** `README.md`'s plugin material gains a paragraph on
managing them, and the keyboard shortcuts table is checked for any new binding.
The help overlay gains a line only if a binding is added.

**Testing** (new `tests/test_plugin_drawer.py`)

- Each state renders with the right label and detail; a multi-kind plugin lists
  its kinds.
- Enable writes `settings.conf` and says a restart is needed; disable takes
  effect on the next render without one.
- A plugin disabled by a raised exception can be re-enabled, runs again, and
  fails again to the same single error rather than a second growth path.
- The section is absent entirely when no plugins are installed.
- 80-column layout test, matching the existing drawer tests.
- A failing `persist_setting` (read-only config) is reported and the toggle
  reverts, matching the journald switch's behaviour.

**Gate.** With four plugins installed — one working, one disabled, one raising
on import, one requiring a future CLV — the drawer tells an operator which is
which and what to do about each, at 80 columns. Suite green on 3.11 and 3.14.

**Commit.** `feat(drawer): manage plugins instead of describing them`

---

## Phase 5 — Ordering, configuration and lifecycle

Three gaps that only matter once more than one plugin is installed — which is
what Phases 3 and 4 make possible, and what Stage C makes inevitable.

**Expected outcomes**

- **Deterministic order, everywhere.** `FilterStage`s currently run in
  `pkgutil.iter_modules` order, so two redaction plugins compose by filesystem
  accident. `Plugin` gains `priority: int = 100`; every ordered registry in
  this file — stages, formats, cluster rules, query operators, sinks — runs
  ascending with ties broken by name. One rule, defined once here, so no Stage
  C phase invents its own.
- **Per-plugin configuration.** A `[plugin:<name>]` section in `settings.conf`,
  parsed by `config.py` and handed to the plugin as a read-only mapping via an
  optional `configure(settings)` hook. This is what makes Phase 0's "copy the
  journald consent pattern" instruction followable: today the journald provider
  reaches into `clv.services.config.load_config()` directly
  ([journald.py:39](clv/plugins/sources/journald.py#L39)) and a third party has
  no supported equivalent. Phase 9's network-capable sinks depend on this
  existing.

  ```ini
  [plugin:redact_secrets]
  patterns = password, api_key, token
  replacement = ******
  ```

- **A malformed plugin section is skipped and reported**, never a startup
  failure — the rule `config.py` already follows everywhere.
- **Lifecycle hooks**, all optional, all guarded exactly as `apply` is:
  `configure(settings)` after instantiation, `setup()` before first use,
  `teardown()` at shutdown. A plugin that raises in any of them is disabled via
  Phase 1's `disable()` and reported; `teardown()` failures cannot delay or
  block exit.
- **Shutdown ordering is defined**: `teardown()` runs after readers are closed
  and before the session is persisted, so a plugin cannot resurrect a source
  mid-teardown. Phase 13's host shutdown slots into the same point.
- **Plugin-owned persistent state**, deliberately *not* added.
  `SessionState.PERSISTED_FIELDS` ([storage.py:113](clv/storage.py#L113)) is a
  closed set for a reason — every field in it carries an argument about whether
  recording it leaks what someone was reading. A plugin needing state uses its
  own file under its own config directory, and the docs say so.
- **The journald provider migrates onto `configure()`** as the worked example,
  and the migration is the proof the hook is sufficient. Its behaviour is
  unchanged, including re-reading its opt-in on every `discover()`.

**Documentation changes.** `clv/plugins/AGENTS.md` gains `priority`, the
`[plugin:<name>]` section, the three lifecycle hooks with their guarantees and
failure handling, the shutdown ordering, and the note that plugin state is the
plugin's own problem and why. `settings.conf` and the template gain a commented
`[plugin:...]` example.

**Testing** (extend `tests/test_plugins.py`, `tests/test_config.py`)

- Stages run in priority order; equal priorities are name-ordered and stable
  across runs, asserted with a shuffled load order.
- `[plugin:x]` reaches plugin `x`'s `configure()` and nothing else's; a section
  for an absent plugin is reported, not fatal.
- Malformed section: skipped, reported, no raise.
- Each hook raising, independently: plugin disabled, error recorded once, CLV
  unaffected — including `teardown()` raising during shutdown.
- A plugin with none of the hooks works exactly as before.
- `teardown()` is called exactly once, after readers close, on clean exit and
  on exit with a source open.
- journald behaves identically before and after its migration, against
  `tests/test_journald.py` unchanged.

**Gate.** Two ordering-sensitive stages compose predictably; a configured plugin
reads its own settings without importing anything from `clv.services`. Suite
green on 3.11 and 3.14.

**Commit.** `feat(plugins): stage ordering, per-plugin config and lifecycle`

---

## Phase 6 — Performance guard

Exception isolation is solved. Time is not, and a plugin that is merely slow is
currently indistinguishable from CLV being broken. This is a prerequisite for
all of Stage C, where third-party code moves into the per-line read path, the
per-entry query path and the memoised shape path.

**Expected outcomes**

- **The double call goes.** `_visible_entries`
  ([app.py:1496-1500](clv/app.py#L1496-L1500)) runs every stage over the whole
  buffer and is called from both `_render_log` and `_update_status` — twice per
  render, no caching. Memoised on `(buffer revision, FilterSpec, plugin
  generation)`; `Buffer.revision` ([session.py:127](clv/services/session.py#L127))
  already exists for exactly this kind of cache.
- **A generation counter on the registry**, bumped by any enable, disable or
  failure. Every downstream cache in Stage C keys on it — the clustering shape
  cache in particular, which is an `lru_cache` on a module-level function
  ([clustering.py:156](clv/services/clustering.py#L156)) and would otherwise
  serve pre-plugin shapes after a rule was enabled.
- **Plugins are timed, per kind.** Cumulative wall time per plugin per pass for
  per-entry kinds, per call for coarse ones. Zero plugins means zero
  measurement and zero cost (Requirement 10).
- **A plugin over budget is disabled, not tolerated.** A configurable
  `plugin_time_budget_ms` in `[log_viewer]`. Exceeding it repeatedly disables
  the plugin through Phase 1's `disable()`, reported by name, visible in the
  drawer, re-enablable from Phase 4's control.
- **Two budgets, not one.** A render-path budget for anything called per entry
  during filtering, and a **read-path budget** measured per line for
  `LogFormat` and `ClusterRule` in Phase 7 and 10. The read path is where an
  expensive plugin does the most damage and where the operator has the least
  evidence that a plugin is responsible.
- **The buffer ceiling is the stated risk.** `max_buffer_lines` is configurable
  to 500 000 ([config.py:25](clv/services/config.py#L25)); the documentation
  says plainly that a per-entry plugin doing regex work at that ceiling will be
  disabled by the budget, and that the fix is a cheaper plugin.
- **A benchmark that runs in CI**, not a claim: the no-op cost of each per-entry
  kind over 5 000 entries, and the cache hit versus the current double call.

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `Performance`
section: which kinds are called how often, both budgets, what happens when one
is exceeded, and the standing advice to make the cheap rejection first.
`settings.conf` and the template document `plugin_time_budget_ms`.

**Testing** (extend `tests/test_plugins.py`, new `tests/test_plugin_perf.py`)

- A deliberately slow stage is disabled after the budget is exceeded, reported
  once, and the pane keeps rendering with the remaining stages.
- A plugin under budget is never disabled, however many passes run.
- The cache: identical `(revision, spec, generation)` does not re-run plugins; a
  changed spec, a changed buffer, and an enable/disable each invalidate it.
- The generation counter invalidates the clustering shape cache specifically —
  the test that Phase 10 depends on.
- Zero plugins: no measurement path is entered, asserted by patching the timer.
- Correctness under cache: entries rendered are identical to the uncached result
  across a filter change, a tail append, a rotation and a merge.

**Gate.** With a stage sleeping past the budget, the UI stays responsive and the
drawer names the culprit. The benchmark shows a measurable improvement from
removing the double call and no regression with zero plugins. Suite green on
3.11 and 3.14.

**Commit.** `perf(plugins): cache the staged view and budget every plugin kind`

---

# Stage C — Core seams

Five phases, one per core feature a plugin could not previously reach. Each ends
with the same kind of gate: a plugin-supplied thing working through a core
feature that knows nothing about plugins (Requirement 9).

## Phase 7 — Parsing

`_parse_structured` ([parsing.py:545-630](clv/services/parsing.py#L545-L630))
dispatches through hardcoded formats on cheap first-character checks and falls
through to `raw`. There is no registry and no hook, so "CLV does not know my
format" has no plugin-shaped answer — and a `FilterStage` is not one: it can
rewrite `fields` after the fact but cannot make a line parse, cannot claim a
`format_name`, and cannot supply the timestamp the timeline buckets on.

**Expected outcomes**

- **`LogFormat`**, in `clv.api`:

  ```python
  class LogFormat(Plugin):
      #: Field names this format can produce, so field queries know them
      #: before a matching line has been seen.
      field_names: frozenset[str] = frozenset()

      @abstractmethod
      def parse(self, line: str) -> Optional[LogEntry]: ...
  ```

- **Built-ins first, plugins second, `raw` last.** Two load-bearing reasons: a
  line that already parses costs a plugin nothing, and a third-party format
  cannot shadow syslog. A plugin wanting to *replace* a built-in is out of
  scope and the documentation says so.
- **Injection, not import.** `LogParser`
  ([parsing.py:699-741](clv/services/parsing.py#L699-L741)) gains a `formats=()`
  keyword, `Buffer` ([session.py:119](clv/services/session.py#L119)) passes what
  the session was given, and the app supplies the registry's formats.
  `parsing.py` keeps knowing nothing about plugins.
- **A format's entries are first-class.** This is Requirement 9 for this phase.
  An entry a plugin format produced must be searchable by field query — which
  means `field_names` feeds `NORMALISED_FIELD_KEYS`
  ([query.py:99](clv/services/query.py#L99)) and `_known_fields`
  ([app.py:2193](clv/app.py#L2193)) so completion offers the field before a
  matching line is on screen — bucketed by the timeline, clustered by the repeat
  folder, shown in the detail pane with its `format_name`, and exportable. None
  of these need new code if the entry is well-formed, and the test is that none
  of them need new code.
- **Continuation still works.** A plugin format participates in carry-forward
  exactly as a built-in does: an unparsed line after a plugin-format line
  inherits its timestamp and level, and inherits no fields
  ([parsing.py:718-741](clv/services/parsing.py#L718-L741)).
- **A format that raises is disabled**; a format returning a malformed
  `LogEntry` — wrong types, `format_name` of `"raw"`, a non-string field value,
  a non-`LogEntry` — is rejected with a message naming the rule it broke. The
  read path cannot afford to trust this one: a stage that misbehaves costs a
  render, a format that misbehaves corrupts the buffer.
- **A format is inside the read-path budget** from Phase 6, measured per line.
- **A worked example ships**: `clv/plugins/formats/` as a live drop-in
  directory, with nginx `error_log` as the reference — genuinely common and
  genuinely not covered by the five built-ins. It ships **disabled** and the
  operator enables it like any other, so the drawer's count keeps meaning
  "plugins someone installed" ([TODO.md:243-246](TODO.md#L243-L246)).

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `LogFormat` section
of equal weight to the others: the interface, the built-ins-first rule,
`field_names` and why it exists, the `LogEntry` contract a format must honour,
carry-forward, the per-line budget, and the note that `parse()` is the hottest
third-party code in CLV. `README.md`'s multi-format bullet gains "and any format
a plugin teaches it."

**Testing** (new `tests/test_plugin_formats.py`, extend `tests/test_parsing.py`)

- A plugin format parses a line the built-ins return `raw` for; a line a
  built-in already handles is **never** offered to the plugin.
- `field_names` reaches the query vocabulary: the field completes and a field
  query matches before any matching line has been read.
- **Feature parity, one test per feature**, all against entries produced by a
  plugin format: timeline bucketing, clustering, detail pane, export, bookmarks,
  watch rules. This block *is* Requirement 9 for this phase.
- Carry-forward: a continuation after a plugin-format line inherits timestamp
  and level and inherits no fields.
- A format that raises is disabled; parsing continues on the built-ins.
- Malformed returns — each rejected with a specific message and no corrupt entry
  in the buffer.
- A format over the per-line budget is disabled.
- Ordering across several plugin formats follows Phase 5's `priority`.
- The nginx reference against a captured fixture, in the manner of
  `tests/test_journald.py`.
- Zero format plugins: `LogParser` behaves byte-identically to today, asserted
  against the existing parsing suite unchanged.

**Gate.** A plugin file in `~/.config/clv/plugins/` teaches CLV a format it did
not know, and every core feature works on the result with no core change beyond
this phase's. `tests/test_parsing.py` passes unmodified. Suite green on 3.11 and
3.14.

**Commit.** `feat(plugins): a LogFormat seam and an nginx reference format`

---

## Phase 8 — Query

The query grammar is a closed set: `_TERM_RE`
([query.py:111-114](clv/services/query.py#L111-L114)) hardcodes the operator
alternation and `FieldTerm.test`
([query.py:131-146](clv/services/query.py#L131-L146)) is an if-chain over it.
A plugin can produce a field but cannot say anything new *about* one.

This phase is where the degradation rule (Requirement 12) is built, because it
is the first seam whose absence can change what a **saved** thing means.

**Expected outcomes**

- **`QueryOperator`** — a new comparison token and its predicate:

  ```python
  class QueryOperator(Plugin):
      token: str = ""                 # e.g. "~", "!~"
      def test(self, stored: str, value: str) -> bool: ...
  ```

- **`ComputedField`** — a queryable field derived rather than parsed, which is
  what gives the grammar genuinely new power without adding a DSL:

  ```python
  class ComputedField(Plugin):
      field_name: str = ""            # e.g. "age", "length"
      def value(self, entry: LogEntry) -> Optional[str]: ...
  ```

  Computed fields resolve **after** parsed fields, so a plugin can never shadow
  what a line actually said.
- **The grammar does not grow.** No `OR`, no parentheses, no precedence — the
  three things [query.py:15-16](clv/services/query.py#L15-L16) declines, and
  Phase 0's reversal is written to keep declining them. Terms stay
  implicit-AND and flat.
- **`_TERM_RE` is built, not written.** The alternation is generated from the
  operator set, longest token first so `>=` still beats `>`, and rebuilt when
  the registry changes. Built-in tokens are reserved: a plugin claiming `:` or
  `=` is rejected and reported.
- **A module-level registry, not a `FilterSpec` field.** `FilterSpec.parse()`
  ([filtering.py:97](clv/services/filtering.py#L97)) is the only call site, and
  `FilterSpec` is frozen, slotted, hashed into Phase 6's cache key and
  persisted into `SavedView`. Operators are installed once at startup via
  `query.install_operators(...)` and the docstring records why that asymmetry
  exists.
- **Watch rules get this for free**, and that is checked rather than assumed:
  `validate_pattern` and `_CompiledRule`
  ([watch.py:96-140](clv/services/watch.py#L96-L140)) both route through
  `parse_query`, so a plugin operator works in a watch rule the day it works in
  the query box.
- **Degradation, built here and reused by every later phase.**
  `SavedView` ([storage.py:19](clv/storage.py#L19)) and `WatchRule`
  ([watch.py:51](clv/services/watch.py#L51)) each gain
  `requires: tuple[str, ...]` — the plugins their query depends on, recorded
  when the view or rule is saved. On load, a view or rule whose `requires` names
  a missing or disabled plugin is **kept byte-intact, marked unusable, and
  listed with the plugin it needs**. It is never rewritten and never silently
  reinterpreted as free text, which is the failure
  [query.py:18-38](clv/services/query.py#L18-L38) exists to prevent.
- **Old state files load unchanged.** `requires` defaults to empty and a file
  written before this phase is valid; a file written after it is still readable
  by a build without the plugin.
- **Operators are per-entry** and inside the render budget.

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `Query` section: both
interfaces, reserved tokens, the resolution order for computed fields, the
`requires` mechanism and what an operator sees when a plugin is missing.
`README.md`'s *Field queries* section ([README.md:637](README.md#L637)) gains a
paragraph that the operator set is extensible and that a saved view records what
it needs. `query.py`'s module docstring records the narrow reversal.

**Testing** (new `tests/test_plugin_query.py`, extend `tests/test_field_query.py`)

- A plugin operator parses, matches, and renders back through `FieldTerm.render`.
- A plugin claiming a built-in token is rejected and reported; the built-in
  keeps working.
- Longest-token-first: a plugin registering `~` does not break `>=`, and one
  registering `~=` does not break `~`.
- A computed field is queryable, completes, and does **not** shadow a parsed
  field of the same name.
- **A saved view using a plugin operator**: saved with `requires`, reloaded with
  the plugin present and working, reloaded with the plugin absent and marked
  unusable with the plugin named — and its query string byte-identical either
  way. Requirement 12's test.
- The same three cases for a watch rule.
- A state file written before this phase loads; one written after loads on a
  build without the plugin.
- Zero query plugins: `parse_query` behaves byte-identically, asserted against
  `tests/test_field_query.py` unchanged — including the compatibility tests that
  pin "a plain regex is passed through untouched".

**Gate.** A plugin adds `~` for regex-match-a-field, it works in the query box,
in a saved view and in a watch rule, and removing the plugin disables those
without corrupting them. Suite green on 3.11 and 3.14.

**Commit.** `feat(plugins): query operators, computed fields and the requires rule`

---

## Phase 9 — Watch

Watch rules are a fixed shape — a pattern, an action, a rate limit — and their
only destination is a toast. Two seams, and they are very different: one is
per-entry and in-process, the other is the first plugin kind that genuinely
wants the network.

**Expected outcomes**

- **`WatchMatcher`** — a rule kind beyond pattern matching:

  ```python
  class WatchMatcher(Plugin):
      kind: str = ""                  # e.g. "threshold", "absence"
      def matches(self, entry: LogEntry, rule: WatchRule) -> bool: ...
  ```

  `WatchRule` ([watch.py:51](clv/services/watch.py#L51)) gains
  `kind: str = "pattern"`, persisted, defaulting so every existing rule file
  loads unchanged. `_CompiledRule`
  ([watch.py:113-140](clv/services/watch.py#L113-L140)) dispatches on it and
  keeps its "a rule nobody can fix mid-session must never throw on every line"
  behaviour: a matcher that raises marks the rule broken rather than failing the
  poll.
- **`WatchSink`** — where a hit is delivered:

  ```python
  class WatchSink(Plugin):
      def deliver(self, name: str, count: int, context: FilterContext) -> None: ...
  ```

- **Sinks cannot bypass the rate limiter.** `WatchNotifier`
  ([watch.py:254-300](clv/services/watch.py#L254-L300)) exists because a rule
  matching every line must not produce a storm — "the behaviour that makes
  people turn a feature like this off". Sinks receive what `due()` already
  coalesced, at the same window. A sink is fed the *result* of rate limiting,
  never the raw hits.
- **The toast is itself a sink.** The built-in path at
  [app.py:2466-2470](clv/app.py#L2466-L2470) becomes the default sink, so there
  is one delivery path rather than a plugin path bolted beside a core one.
- **Sinks run off the event loop.** A webhook that blocks would block the poll,
  and the no-IO-on-the-event-loop clause is not negotiable. Sinks are dispatched
  to a worker; a sink that hangs is abandoned after the budget and disabled, and
  a sink that raises is disabled and reported.
- **A network sink is the consent case, and the docs use it as the worked
  example.** Phase 0 made "no network without consent" a convention; Phase 5
  gave every plugin a `[plugin:<name>]` section. A webhook sink is expected to
  ship disabled, read its endpoint from its own section, and deliver nothing
  until an endpoint is set — the journald pattern applied to egress. This is
  also the plugin kind most worth isolating, and Phase 13 allows it.
- **Log content leaving the machine is stated in the docs, loudly.** A sink
  receives a rule name and a count by default, not lines. A sink wanting entry
  content declares it, and the drawer shows that it does.
- **Degradation.** A rule whose `kind` names a missing matcher is preserved,
  marked unusable and listed with the plugin it needs — Phase 8's `requires`
  mechanism, reused rather than reinvented.

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `Watch` section: both
interfaces, the rate-limit guarantee, the off-the-event-loop rule, the consent
expectation for egress, and what a sink is and is not given. `README.md`'s
*Watch rules* section ([README.md:501](README.md#L501)) notes that rule kinds
and destinations are extensible and that a rule records what it needs.

**Testing** (extend `tests/test_watch_rules.py`, new `tests/test_plugin_watch.py`)

- A plugin matcher fires a rule; a matcher that raises marks the rule broken and
  does not throw per line.
- A rule file written before this phase loads with `kind="pattern"`.
- **A sink receives exactly what `due()` produced** — a rule matching 500 lines
  in one window delivers once with a count of 500, not 500 times. The
  anti-storm test, at the sink boundary.
- A sink that raises is disabled and reported; a sink that hangs is abandoned
  and disabled, and the poll completes.
- Sinks do not run on the event loop, asserted by a sink that blocks and a poll
  that completes anyway.
- A sink not declaring content access never receives entry text.
- A rule referencing a missing matcher is preserved, marked unusable, named.
- Zero watch plugins: `tests/test_watch_rules.py` passes unmodified.

**Gate.** A rule with a plugin-supplied kind fires, and a plugin sink delivers
it once per window rather than once per line, without blocking the pane. Suite
green on 3.11 and 3.14.

**Commit.** `feat(plugins): watch matchers and delivery sinks`

---

## Phase 10 — Clustering

Clustering normalises volatile tokens out of a line and groups what then looks
identical. `_RULES` ([clustering.py:74](clv/services/clustering.py#L74)) is a
fixed ordered tuple, and the docstring says clustering is "not configurable, and
there is no rules DSL: that is a stated non-goal" — reversed on the record in
Phase 0, narrowly: a plugin author writing Python is not the operator writing
regex into `settings.conf`, and the latter stays refused.

**Expected outcomes**

- **`ClusterRule`** — one more volatile token to normalise out:

  ```python
  class ClusterRule(Plugin):
      pattern: re.Pattern[str]
      placeholder: str = ""
  ```

- **Plugin rules run after the built-ins**, in `priority` order. The built-in
  order is documented as load-bearing — each rule runs on what the previous left
  behind ([clustering.py:29-34](clv/services/clustering.py#L29-L34)) — and
  appending is the only position that cannot break it.
- **The placeholder invariant is validated, not assumed.** Built-in placeholders
  contain no digits, so a later numeric rule cannot chew them up. A plugin
  placeholder containing a digit is **rejected at load** with that reason, which
  is the kind of constraint that is obvious in the source and invisible to
  someone writing their first rule.
- **`ShapeContributor`** — an extra component of the key two entries must share:

  ```python
  class ShapeContributor(Plugin):
      def contribute(self, entry: LogEntry) -> str: ...
  ```

  `shape_of` ([clustering.py:165-171](clv/services/clustering.py#L165-L171))
  currently composes origin, level and the normalised body. Contributions are
  appended in `priority` order, so a plugin can keep two clusters apart —
  by `unit`, by `node` — without touching what a shape already means. A
  contributor returning the same string for everything is a no-op, which is what
  makes it safe to add.
- **The shape cache is invalidated correctly.** `normalise` is an
  `lru_cache`-memoised module function
  ([clustering.py:156](clv/services/clustering.py#L156)) and it is what makes
  clustering affordable — ~115 ms per 5 000 lines uncached, ~6 ms cached.
  Enabling or disabling a rule mid-session must clear it, keyed on Phase 6's
  generation counter. A stale shape cache would silently cluster by the old
  rules, which is exactly the class of bug that never gets reported because it
  looks like the feature working.
- **The no-loss guarantee is untouched.** Collapsing is a display transform;
  `expand()` gives back every original line byte-identically
  ([clustering.py:11-15](clv/services/clustering.py#L11-L15)). A plugin can
  change how lines *group*; nothing a plugin does can make a line disappear, and
  the existing `test_expanding_a_cluster_gives_back_every_original_line` is
  re-run with plugin rules active.
- **Rules are per-line and inside the read-path budget.** Each plugin rule is
  another regex pass over every distinct line.

**Documentation changes.** `clustering.py`'s module docstring records the
reversal, its scope, and the digit-free placeholder invariant.
`clv/plugins/AGENTS.md` gains a `Clustering` section with both interfaces, the
append-only ordering, the invariant, the cache behaviour and the cost model.
`README.md`'s *Noise reduction* section ([README.md:437](README.md#L437)) notes
that the rules are extensible by plugin and not by config file, and why.

**Testing** (extend `tests/test_clustering.py`, new `tests/test_plugin_clustering.py`)

- A plugin rule normalises a token the built-ins leave alone, and two lines
  differing only in it cluster.
- A placeholder containing a digit is rejected at load with that reason.
- Plugin rules run after built-ins: a rule that would eat a built-in placeholder
  cannot, because the placeholder has no digits — asserted directly.
- A `ShapeContributor` splits a cluster that would otherwise merge; one
  returning a constant changes nothing.
- **Cache invalidation**: cluster, enable a rule, re-cluster, and the shapes
  reflect the new rule — the test that would fail if the `lru_cache` were left
  alone.
- `expand()` returns every original line with plugin rules active.
- Incremental clustering with plugin rules matches a full recompute — the
  existing `test_incremental_clustering_matches_a_full_recompute` extended.
- A rule that raises is disabled; clustering continues on the built-ins.
- Zero clustering plugins: `tests/test_clustering.py` passes unmodified and the
  cached timing is unchanged.

**Gate.** A plugin rule folds a repeat the built-ins could not, expansion still
returns every line, and toggling the rule mid-session takes effect. Suite green
on 3.11 and 3.14.

**Commit.** `feat(plugins): cluster rules and shape contributors`

---

## Phase 11 — Timeline

The histogram counts entries per bucket and colours by worst severity. Two
seams, and the constraint that shapes both is that
[`Timeline.extend`](clv/services/timeline.py#L102-L150) folds newly tailed
entries into a fixed grid by arithmetic — which is what makes tailing cost what
arrived rather than what is buffered.

**Expected outcomes**

- **`TimelineAnnotation`** — marks on the time axis:

  ```python
  class TimelineAnnotation(Plugin):
      def annotations(self, window: TimeWindow) -> Iterable[tuple[datetime, str, Optional[str]]]:
          """(moment, label, level) for the visible window."""
  ```

  Deploys, incidents, maintenance windows — the context that makes a spike mean
  something. Rendered on the bar; `←`/`→` step to annotations as well as
  buckets, extending `TimelineBar.BINDINGS` rather than adding a new key.
- **`TimelineMetric`** — what a bucket measures, if not count:

  ```python
  class TimelineMetric(Plugin):
      metric_name: str = ""
      def value(self, entry: LogEntry) -> Optional[float]: ...
  ```

  `Bucket` ([timeline.py:44](clv/services/timeline.py#L44)) gains `value: float`
  beside `count`; `count` never stops meaning entries, so nothing downstream
  that reads it has to change.
- **A metric must be foldable, and this is enforced, not requested.** `extend`
  adds an arrival into an existing bucket by arithmetic; a metric that is a sum
  survives that, and a median or a percentile does not. `TimelineMetric`
  declares nothing but a per-entry value, and CLV does the summing — which makes
  non-foldable metrics unexpressible rather than broken. Stated in the docs as
  the reason the interface is shaped that way.
- **The caption says what it is showing.** A bar showing a metric rather than a
  count is a bar whose numbers mean something different, and the caption names
  the metric and the plugin.
- **Undated entries stay reported.** An entry with no timestamp is counted in
  `Timeline.undated` and explained rather than dropped
  ([timeline.py:22-30](clv/services/timeline.py#L22-L30)). A metric does not get
  to change that: an entry with no timestamp contributes to neither.
- **Annotations outside the window are not drawn and not fetched twice.** The
  provider is asked for the visible window only, once per rebuild, and the
  result is cached against Phase 6's generation counter and the window.
- **One metric at a time.** Two metric plugins both enabled is a conflict the
  operator resolves; CLV picks by `priority` and reports that it did.
- **Annotations are coarse-grained and isolable**; metrics are per-entry and are
  not.

**Documentation changes.** `timeline.py`'s module docstring gains the foldable
constraint and why it bounds the interface. `clv/plugins/AGENTS.md` gains a
`Timeline` section with both interfaces, the fold rule, the caption requirement
and the one-metric rule. `README.md`'s *severity timeline* section
([README.md:387](README.md#L387)) notes annotations and metrics.

**Testing** (extend `tests/test_timeline.py`, new `tests/test_plugin_timeline.py`)

- An annotation renders at the right bucket, is steppable with `←`/`→`, and one
  outside the window is not drawn.
- A metric changes the bar's values, `count` still reports entries, and the
  caption names the metric.
- **A metric survives `extend`**: build, tail ten entries, and the folded result
  equals a full rebuild. The test the fold constraint exists for.
- An entry with no timestamp contributes to neither count nor metric and is
  still reported in `undated`.
- Two metric plugins: the higher-priority one wins and the conflict is reported.
- A provider that raises is disabled; the bar renders without annotations.
- The annotation cache is not queried twice for one window and is invalidated by
  a generation bump.
- Zero timeline plugins: `tests/test_timeline.py` passes unmodified.

**Gate.** A plugin marks deploy times on the bar and a plugin metric shows bytes
rather than lines, both survive tailing, and the caption never lies about which
it is showing. Suite green on 3.11 and 3.14.

**Commit.** `feat(plugins): timeline annotations and foldable metrics`

---

# Stage D — Surface

## Phase 12 — Commands and controls

Plugins can now act on data. They still cannot be *invoked*, and cannot show
anything except a toast. This phase gives them a way in and a place to draw,
without giving them the layout.

The constraint that shapes the whole phase is Requirement 11. `compose()`
([app.py:702-720](clv/app.py#L702-L720)) is a fixed tree; `CSS` is a class-level
string ([app.py:419](clv/app.py#L419)); styling is CSS-only by doctrine; and
`BINDINGS` ([app.py:531-583](clv/app.py#L531-L583)) carries hand-tuned ordering
comments about which entry must not fall off the footer at 80 columns. A plugin
widget in that tree makes every breakpoint test conditional on what is
installed. So plugins get a **constrained vocabulary that CLV styles and CLV
lays out**, and never a widget.

**Expected outcomes**

- **`Command`** — a named action, optionally bound to a key:

  ```python
  class Command(Plugin):
      command_name: str = ""          # stable id, used in settings and bindings
      title: str = ""                 # what the help overlay shows
      key: str = ""                   # optional; hidden from the footer by default
      def run(self, context: CommandContext) -> None: ...
  ```

- **`CommandContext`** — read-only, and deliberately small: the selected entry,
  the filtered set, the current `FilterSpec`, the selected source, and a
  `notify()`. A command cannot reach the app object, which is what stops the
  vocabulary being a formality.
- **Dispatch by name.** Textual resolves `action_*` by attribute name, so a
  bridge action `action_plugin_command(name)` looks the command up and runs it
  guarded. No plugin code is ever installed onto the app class.
- **Bindings default to hidden.** `show=False` unless the plugin asks otherwise,
  and a plugin asking for `show=True` is **refused with a reason** rather than
  honoured — the footer ordering is hand-tuned against an 80-column floor and a
  plugin cannot know what it would push off. `?` is how hidden bindings are
  found, which is already how CLV handles its own overflow.
- **Key conflicts are rejected, named, and lost by the plugin.** A binding
  colliding with a built-in or with an earlier plugin's is refused and reported;
  built-ins always win, and the command remains invocable by name.
- **The help overlay lists plugin commands** in their own section. It is already
  built from `Binding` objects gathered across three sources
  ([app.py:2858-2859](clv/app.py#L2858-L2859)) so that a key added anywhere
  cannot go missing from it — plugin bindings join that gathering rather than
  being appended separately.
- **A drawer section**, from a constrained vocabulary: label, switch, input,
  select, button, and static text. CLV owns the CSS and the breakpoint
  behaviour. A plugin's section collapses to its label at the narrowest
  breakpoint like every other block in the drawer.
- **A modal screen**, from the same vocabulary, pushed by a command. Full-screen
  means no interaction with the main layout, which is why it is the one place a
  plugin gets real room.
- **Plugins ship no CSS.** Stated as a rule with its reason. This is the
  concession that keeps every breakpoint test unconditional.
- **Commands are isolable** (Phase 13): a command is coarse-grained, runs on
  demand, and is exactly the kind of plugin worth running where it can be killed.

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `Commands and
controls` section: the interface, the context, the binding rules and why
`show=True` is refused, the widget vocabulary, the no-CSS rule, and the modal
screen. `README.md`'s keyboard shortcuts table gains a note that plugins may add
hidden bindings and that `?` lists them. The help overlay's own section header
is added.

**Testing** (new `tests/test_plugin_commands.py`, extend `tests/test_help_overlay.py`)

- A command runs by name and by key; a command that raises is disabled and
  reported and the app survives.
- A binding colliding with a built-in is refused and reported; the built-in
  keeps working and the command is still invocable by name.
- A plugin requesting `show=True` is refused with a reason.
- **Plugin bindings appear in the help overlay**, in their own section — the
  test that mirrors the existing guarantee that no key can go missing from it.
- **The footer at 80 columns is unchanged** with five plugin commands installed.
  Requirement 11's test.
- A drawer section renders, collapses correctly at the narrowest breakpoint, and
  its controls round-trip their values.
- A modal screen opens, is dismissable, and cannot stack twice.
- `CommandContext` exposes no route to the app object, asserted directly.
- Zero command plugins: the footer, the help overlay and the drawer are
  byte-identical to today.

**Gate.** Five plugin commands are installed. The footer at 80 columns is
unchanged, all five are in the help overlay, one draws a drawer section and one
opens a modal, and every existing breakpoint test passes without modification.
Suite green on 3.11 and 3.14.

**Commit.** `feat(plugins): commands, bindings, drawer sections and modals`

---

# Stage E — Trust and distribution

## Phase 13 — Isolation

The first time in CLV's history that a plugin can be *stopped*. It is a real
improvement and it is not a sandbox, and this phase is written so that the
second half of that sentence survives contact with the first.

**Expected outcomes**

- **`isolated = True`** on a plugin class asks for a subprocess host.
- **Kind gating, enforced at load with a stated reason.** Allowed:
  `Exporter`, `LogSourceProvider`, `WatchSink`, `Command`, `TimelineAnnotation`
  — all coarse-grained, all called on demand or once per rebuild. Refused:
  `FilterStage`, `LogFormat`, `QueryOperator`, `ComputedField`, `WatchMatcher`,
  `ClusterRule`, `ShapeContributor`, `TimelineMetric` — all per entry or per
  line. The refusal names the kind and says why, so an author reads a reason
  rather than discovering an omission.
- **The host.** One subprocess per isolated plugin, started lazily on first use
  and stopped at `teardown()`. `multiprocessing` with the **spawn** start
  method — not fork, which in a running Textual app with open readers and a
  terminal in raw mode is a footgun. Spawn requires picklable arguments, which
  is precisely why Phase 2 published a wire form: `LogEntry` cannot be pickled
  at all (verified), so nothing crosses without `entry_to_wire`.
- **Frozen builds work, and this is the requirement most likely to be missed.**
  `multiprocessing` under PyInstaller needs `freeze_support()` as the first
  thing in `clv/__main__.py`, or a spawned child re-runs the app and the binary
  forks itself repeatedly. Tested against a real frozen build, not only against
  a source checkout.
- **The child does not inherit the bundle's library path.** PyInstaller puts
  `_internal` on `LD_LIBRARY_PATH` and a child that then execs a system binary
  loads the bundle's libcrypto instead of the system's —
  `journald.child_environment()`
  ([journald.py:77](clv/plugins/sources/journald.py#L77)) already solves this
  and the host uses it rather than a second implementation.
- **A host that crashes, hangs or exits is reported and its plugin disabled**
  through Phase 1's `disable()` and Phase 4's row. A call exceeding the budget
  kills the host. This is the capability that does not exist today at any price.
- **Privilege reduction where the platform offers it, described exactly.** The
  child starts in a scrubbed environment and a restricted working directory.
  That is the extent of it. No claim of namespaces, seccomp or capability
  dropping is made, and the docs say the child runs as the operator with the
  operator's filesystem.
- **The honest sentence, in three places** — `clv/plugins/AGENTS.md`, the
  drawer's isolated-state help text, and `README.md`: *isolation contains
  crashes, hangs and leaks; it does not make an untrusted plugin safe.* Phase
  0's stub is where the first of those goes, and Phase 0's test that the word
  "sandbox" does not appear still passes after this phase.
- **Isolation is visible.** The drawer's `isolated` state from Phase 4 is
  filled in, so an operator can see which plugins are contained and which are
  not.
- **A plugin that fails to start isolated is not silently run in-process.** It
  is disabled and reported — the opposite choice would turn a security
  preference into a suggestion.

**Documentation changes.** `clv/plugins/AGENTS.md` gains an `Isolation` section:
the opt-in, the kind gate with its reasoning, the wire form, what is contained
and what is not, the frozen-build requirements, and the rule that a failed start
disables rather than degrades. Phase 0's `What isolation does and does not do`
stub is filled in with exactly that sentence and no more.

**Testing** (new `tests/test_plugin_isolation.py`)

- An isolated exporter runs in a child process, returns its result, and its
  destination reaches the UI.
- A per-entry kind declaring `isolated = True` is refused at load with a message
  naming the kind and the reason.
- A host that crashes: plugin disabled, error reported, CLV unaffected.
- A host that hangs: killed at the budget, plugin disabled, UI responsive
  throughout.
- A host that fails to start: plugin disabled, **not** run in-process — asserted
  by a plugin whose in-process execution would write a sentinel.
- Wire round trip across a real process boundary for every built-in format.
- `teardown()` stops every host exactly once, on clean exit and on exit with a
  host mid-call.
- The child's environment carries no bundle library path.
- **Frozen-build smoke test**: a spawned child does not re-launch the app.
- Zero isolated plugins: no subprocess is created, asserted by patching the
  start method.

**Gate.** An isolated exporter works; an isolated plugin that hangs is killed
without the pane stuttering; a frozen build spawns a child without forking
itself. `clv/plugins/AGENTS.md` still contains no occurrence of "sandbox". Suite
green on 3.11 and 3.14.

**Commit.** `feat(plugins): an opt-in subprocess host for coarse-grained plugins`

---

## Phase 14 — The CLI layer

A prerequisite for Phase 15 and useful on its own. CLV has **no argv handling
anywhere**: `run()` is `LogViewerApp().run()`, two lines
([app.py:3678-3679](clv/app.py#L3678-L3679)), and there is no `argparse` import
in the codebase. The launcher already does `exec "${libdir}/clv" "$@"`
([install.sh:218](install.sh#L218)), so argv reaches the binary today and is
discarded — packaging needs no change.

**Expected outcomes**

- **Bare `clv` launches the TUI, unchanged.** Requirement 13, and the first test
  in the phase. Only a recognised subcommand diverts.
- **`clv --version`, `clv --help`** print and exit.
- **`clv doctor`** — load plugins, print what loaded, what did not and why, and
  exit without starting a screen. The support tool this file has been implying
  since Phase 1: "send me the output of `clv doctor`" is a better first question
  than "open the drawer and read me the yellow text".
- **`clv plugin ...`** is registered here as a subcommand group with `list` only;
  Phase 15 fills it in. Splitting it this way keeps the argv compatibility
  question — which is the risky part — in a phase where it is the only question.
- **Plugins cannot add subcommands.** A plugin subcommand would let an installed
  file change what a shell command does, and Requirement 13 says bare `clv` is
  inviolable. Commands (Phase 12) are the supported way to be invoked, and they
  require the TUI to be running, which is the point.
- **A bare path argument is reserved, not implemented.** `clv /var/log/foo`
  reports that opening a source from the command line is not supported and how
  to add it. It belongs to a source-selection item, not to this file, and
  quietly making it work here would put a feature in a plugin file where nobody
  would look for it.
- **Exit codes are defined**: 0 success, 1 runtime failure, 2 usage error.
- **Standard library only** — `argparse`, per Requirement 7.

**Documentation changes.** `README.md` gains a short **Command line** section
under *Usage*. `clv/plugins/AGENTS.md` documents `clv doctor` as the first thing
to run when a plugin does not appear, and states that plugins do not add
subcommands and why. The `--help` output is itself documentation and is reviewed
as such.

**Testing** (new `tests/test_cli.py`)

- **Bare `clv` starts the TUI** — the compatibility test, asserted without
  launching a screen.
- `--version` matches `clv.__version__`; `--help` exits 0.
- `doctor` reports loaded, disabled, failed and incompatible plugins and exits 0
  with plugins present and with none.
- `doctor` exits 0 on a plugin failure — a broken plugin is a report, not a
  failed command.
- An unknown subcommand exits 2 with a usage message.
- A bare path argument reports the reserved behaviour and exits 2.
- A plugin attempting to register a subcommand is refused and reported.
- The install-script launcher still forwards arguments —
  `tests/test_install_script.py` extended.

**Gate.** `clv` launches the TUI exactly as before; `clv doctor` diagnoses a
broken plugin without a terminal. Suite green on 3.11 and 3.14.

**Commit.** `feat(cli): an argv layer that leaves bare clv alone`

---

## Phase 15 — Registry

Distribution, without operating an index. A manifest format, a verification
story, and the commands that use them; hosting is left to whoever wrote the
plugin.

**Expected outcomes**

- **A manifest**, `clv-plugin.toml`, beside a single-file plugin or inside a
  package:

  ```toml
  name = "nginx_format"
  version = "1.2.0"
  requires_api = ">=1.0,<2.0"
  requires_clv = ">=3.0"
  kinds = ["LogFormat"]
  author = "..."
  homepage = "..."
  files = [{ path = "nginx_format.py", sha256 = "..." }]
  ```

  Parsed with `tomllib` — standard library since 3.11, which is CLV's floor.
- **`clv plugin list`** — installed plugins, versions, state. Reads the
  filesystem and `settings.conf`; does not import anything, so listing a
  malicious plugin cannot run it.
- **`clv plugin info <name>`** — manifest, kinds, state, and where it came from.
- **`clv plugin install <path|tarball|url>`** — verify, then copy into
  `~/.config/clv/plugins/`, then record the manifest. **Install does not
  enable**, and prints the exact line to add to `settings.conf`. Requirement 2,
  at the CLI.
- **`clv plugin remove <name>`** — delete the files, leave the
  `[plugin:<name>]` config section alone and say so; an operator reinstalling a
  plugin should not lose its settings.
- **`clv plugin verify <name>`** — re-check the checksums of what is installed
  against the manifest. Tamper detection after install, which is the check
  nobody runs until they need it.
- **Signatures, optional and operator-rooted.** A detached signature verified
  against a key the operator explicitly trusted with `clv plugin trust <key>`.
  No key means the install proceeds and is reported as **unsigned**, in the
  output and in the drawer. CLV ships no trust root and never will: a bundled
  key would make CLV the arbiter of which plugins are legitimate, which is the
  hosted-index commitment arriving through a side door.
- **URL install is the operator's explicit act**, and that is the consent. HTTPS
  only, no cross-host redirects, a size cap, and the payload is written to disk
  and checksum-verified before anything is unpacked — never imported to inspect
  it. Standard library `urllib`, per Requirement 7.
- **Archive extraction is hostile-input handling.** Path traversal, absolute
  paths, symlinks, hardlinks and device nodes are rejected; extraction is to a
  temporary directory and moved into place only after verification. A plugin
  tarball is untrusted input from the internet and is treated as such regardless
  of what the manifest claims about its author.
- **No index, no search, no auto-update.** Stated, with the reason, so the
  absence reads as a decision.

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `Publishing` section:
the manifest, how to generate checksums, how to sign, and what an operator sees
when a plugin is unsigned. `README.md` gains the install commands beside the
manual copy from Phase 3 — the manual path stays documented and stays supported,
because it is the one that works with no network at all.

**Testing** (new `tests/test_plugin_registry.py`)

- Manifest parsing: complete, minimal, and six malformed shapes — each reported
  with a usable message, never a traceback.
- Install from a directory, from a tarball, and from a local `file://` URL;
  each lands in the plugin directory, is listed, and is **not** enabled.
- `verify` passes on a clean install and fails naming the file after a byte is
  changed.
- **Malicious archives**: path traversal (`../../etc/x`), an absolute path, a
  symlink pointing outside, a device node, and a zip bomb against the size cap —
  each rejected with nothing written outside the temporary directory. This block
  is the phase's real security surface.
- A checksum mismatch aborts before anything is copied.
- An unsigned plugin installs and is reported as unsigned; a plugin signed by an
  untrusted key is reported as untrusted; one signed by a trusted key verifies.
- `remove` deletes files and preserves the config section.
- Install of a plugin whose `requires_api` is unsatisfiable warns at install
  rather than only at load.
- No command imports plugin code — asserted by a plugin whose import writes a
  sentinel, across every subcommand.

**Gate.** A plugin is packaged, published as a tarball, installed by URL on a
machine with only the binary, verified, enabled, and used. A crafted archive
cannot write outside the plugin directory. Suite green on 3.11 and 3.14.

**Commit.** `feat(cli): plugin manifests, install, verify and trust`

---

# Stage F — Release

## Phase 16 — Documentation and release

The system is complete. This phase makes it findable, and decides whether anyone
outside the project ever writes a plugin.

**Expected outcomes**

- **A `Plugins` chapter in `README.md`**, promoted from the current subsection
  at [README.md:715](README.md#L715). Twelve interfaces, installation, the trust
  model, isolation and what it does not do, the enable-list, per-plugin config,
  the CLI, and one worked example per interface.
- **`clv/plugins/README.md`** — referenced by
  [SSH_TODO.md:588](SSH_TODO.md#L588) and by `clv/plugins/AGENTS.md`'s developer
  workflow, and it does not exist. Created: the author-facing quick start,
  distinct from `AGENTS.md`'s contributor-facing contract.
- **An example plugin set**, under `examples/plugins/`, one per interface, each
  complete and copyable into `~/.config/clv/plugins/` unchanged, each with a
  manifest, each importing only from `clv.api`.
- **A plugin author's checklist** in `clv/plugins/AGENTS.md`, replacing the
  current review criteria: declare `requires_api`, import only `clv.api`, make
  the cheap rejection first, know which budget you are in, ask consent for a
  subprocess or a socket, declare `isolated` if your kind allows it, ship a
  manifest, and state your trust requirements in your own README.
- **A migration note for the three existing interfaces** — nothing breaks, and
  the note says what an existing plugin gains by adopting `clv.api`,
  `requires_api` and a manifest.
- **The stale claims are swept.** [README.md:768](README.md#L768) says "290
  tests" against 717 collected today; the `AGENTS.md` non-goals are reconciled
  with whatever SSH_TODO has changed by then; the provider-source limitation
  note is rewritten or removed depending on whether SSH_TODO Phase 1 has landed.
- **Version bump to `3.0.0`.** The doctrine reversals and the new argv layer are
  major-version news. `PLUGIN_API_VERSION` stays `1.0` — every addition above
  was additive and nothing published was removed, which is the Phase 2
  separation doing its job on its first outing, and the docs say so as the worked
  example of what the two version numbers mean.
- **The install script and packaging** create `~/.config/clv/plugins/` on first
  run and the packaged `settings.conf` carries the commented `plugins` key.

**Documentation changes.** This phase *is* the documentation change.

**Testing** (extend `tests/test_version.py`, `tests/test_install_script.py`)

- Version consistency across `pyproject.toml`, `clv/__init__.py` and packaging
  metadata; `PLUGIN_API_VERSION` is `1.0` and is asserted separately, so a
  future edit that bumps them together has to justify itself.
- **Every example under `examples/plugins/` loads and runs** — imported through
  the real loader and exercised against its interface. Documentation that cannot
  be executed is documentation that rots, and these are the first files a new
  author will copy.
- Every example has a valid manifest that `clv plugin install` accepts.
- Every code block in the plugin chapter and in `clv/plugins/README.md` is one
  of the example files or a fragment of one, asserted by extraction.
- The packaged `settings.conf` carries the `plugins` key.
- The test count referenced in `README.md` matches what the suite collects.

**Gate.** Someone who has never read CLV's source, given only `README.md` and
`clv/plugins/README.md`, can write a `LogFormat` plugin, package it with a
manifest, install it on a binary-installed CLV without root, enable it, and see
their format parsed — without asking a question. That is the goal restated as a
gate, and it is the one that decides whether this file achieved anything. Suite
green on 3.11 and 3.14.

**Commit.** `docs(plugins): the plugin chapter, worked examples, and 3.0.0`

---

## Still deliberately out of scope

Much shorter than it was. Each of these was considered and declined for a
reason that survives the decision to include everything else.

| Not doing | Why | What would change it |
| --- | --- | --- |
| **A hosted plugin index** | A server, a namespace to defend and a moderation queue — an operational commitment, not a feature. Every listing would be a trust signal CLV was issuing. Phase 15 makes distribution work without one. | Enough third-party plugins existing that discovery is a real problem, and someone willing to own the operations. |
| **Widget injection into `compose()` and plugin CSS** | Requirement 11. Styling is CSS-only by doctrine and the breakpoint tests must stay unconditional on what is installed. Phase 12's vocabulary, drawer sections and modal screens cover the cases without handing out the layout. | A design for a plugin widget contract that survives the breakpoint rules — a real piece of design work, not a phase. |
| **Isolating per-entry plugin kinds** | `FilterStage`, `LogFormat` and the rest are called per entry or per line. An IPC round trip there is not a slower version of the same program, it is a different one. Phase 13 refuses it out loud rather than shipping something unusable. | Nothing anticipated at CLV's scale. |
| **`OR`, parentheses and precedence in the query grammar** | Still a stated non-goal, and Phase 0's reversal is written narrowly to keep it one. Operators and computed fields add vocabulary; they do not add structure. | A concrete case that implicit-AND genuinely cannot express — and it would be its own file. |
| **Replacing a built-in format, operator or cluster rule** | Plugins extend; they do not override. A plugin that could shadow syslog parsing or redefine `:` would make every bug report unanswerable without knowing what was installed. | Nothing anticipated. |
| **Plugin-owned persisted session state** | `SessionState.PERSISTED_FIELDS` is closed on purpose — every field carries an argument about whether recording it leaks what someone was reading. A plugin's own file under its own directory has none of that ambiguity. | A case where a plugin's state genuinely belongs in CLV's session rather than beside it. |
| **Hot reload** | A plugin swapped underneath a running viewer is a debugging surface nobody asked for. Enable and disable change what is *active*, which covers the real need. | Nothing anticipated. |
| **Plugin-supplied CLI subcommands** | Requirement 13. An installed file must not change what a shell command does. | Nothing anticipated. |

---

## Summary of new and changed files

| File | Phases | Change |
| --- | --- | --- |
| `clv/plugins/AGENTS.md` | 0–16 | Trust model, contract, and every seam |
| `clv/plugins/__init__.py` | 1,3,5,6,7,13 | Loader, search roots, ordering, timing, registries, host |
| `clv/api.py` | 2, all seams | **New** — the published surface, extended per phase |
| `clv/plugins/host.py` | 13 | **New** — the subprocess host and the wire protocol |
| `clv/cli.py` | 14,15 | **New** — argv, `doctor`, `plugin` subcommands |
| `clv/plugins/manifest.py` | 15 | **New** — manifest parsing, checksums, signatures |
| `clv/services/config.py` | 3,5,6 | `plugins`, `[plugin:<name>]`, `plugin_time_budget_ms` |
| `clv/services/parsing.py` | 7 | `LogParser(formats=...)`, injected dispatch |
| `clv/services/query.py` | 8 | Operator registry, computed fields, generated `_TERM_RE` |
| `clv/services/filtering.py` | 8 | `FilterSpec.parse` routes through the operator registry |
| `clv/services/watch.py` | 9 | `WatchRule.kind`, matcher dispatch, sink delivery |
| `clv/services/clustering.py` | 10 | Plugin rules, shape contributors, cache generation |
| `clv/services/timeline.py` | 11 | `Bucket.value`, annotations, foldable metrics |
| `clv/services/session.py` | 7 | `Buffer` passes formats to its parser |
| `clv/storage.py` | 8 | `SavedView.requires` |
| `clv/app.py` | 1,2,4,5,6,7,8,9,11,12,13 | Wiring, cache, dispatch, drawer, host lifecycle |
| `clv/widgets/advanced_drawer.py` | 4,12 | Plugin section; plugin-contributed sections |
| `clv/widgets/help_overlay.py` | 12 | Plugin command section |
| `clv/widgets/timeline.py` | 11 | Annotation rendering and stepping |
| `clv/__main__.py` | 13,14 | `freeze_support()`, argv entry |
| `clv/plugins/formats/` | 7 | **New** — drop-in directory and the nginx reference |
| `examples/plugins/` | 16 | **New** — one copyable example per interface |
| `clv/plugins/README.md` | 16 | **New** — author-facing quick start |
| `settings.conf`, `README.md` | 3,5,6,8,9,10,11,12,14,15,16 | Keys, chapter, sweep |
| `tests/test_api_surface.py` | 2 | **New** — the freeze |
| `tests/test_plugin_drawer.py` | 4 | **New** |
| `tests/test_plugin_perf.py` | 6 | **New** |
| `tests/test_plugin_formats.py` | 7 | **New** |
| `tests/test_plugin_query.py` | 8 | **New** |
| `tests/test_plugin_watch.py` | 9 | **New** |
| `tests/test_plugin_clustering.py` | 10 | **New** |
| `tests/test_plugin_timeline.py` | 11 | **New** |
| `tests/test_plugin_commands.py` | 12 | **New** |
| `tests/test_plugin_isolation.py` | 13 | **New** |
| `tests/test_cli.py` | 14 | **New** |
| `tests/test_plugin_registry.py` | 15 | **New** |
| `tests/test_plugins.py` | 1,2,3,5,6 | Extended throughout |
