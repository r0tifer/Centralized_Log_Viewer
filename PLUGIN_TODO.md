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

Eighteen phases is a lot. The alternative was to declare half of them out of
scope, which is what the first draft of this file did — and the objection to
that draft was correct: a plugin system that cannot touch the query language,
the watch rules, the clustering, the timeline or the screen is not an
ecosystem, it is three hooks and a directory. Seventeen of the eighteen are
that argument; Phase 7b is the exception, and it is here because designing the
`LogFormat` seam is what proved one particular format could not go through it.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| **Stage A — Foundations** | | |
| 0 — Doctrine | Withdraw the sandbox claim; reverse three stated non-goals | ✅ Done |
| 1 — Loader correctness | The six defects, before anyone depends on them | ✅ Done |
| 2 — The contract | `clv/api.py`, `PLUGIN_API_VERSION`, the entry wire form | ✅ Done |
| **Stage B — Reach** | | |
| 3 — Installation | `~/.config/clv/plugins/`, `CLV_PLUGIN_PATH`, the enable-list | ✅ Done |
| 4 — Management UI | A plugin surface, not a status string | ✅ Done |
| 5 — Ordering, config, lifecycle | `priority`, `[plugin:<name>]`, `setup`/`teardown` | ✅ Done |
| 6 — Performance guard | A slow plugin costs itself, not the pane | ✅ Done |
| **Stage C — Core seams** | | |
| 7a — Parsing | `LogFormat`, and a plugin that teaches CLV a format | ✅ Done |
| 7b — logfmt | `key=value` parsing, built in because a plugin cannot | ✅ Done |
| 8 — Query | `QueryOperator`, `ComputedField`, and the degradation rule | ✅ Done |
| 9 — Watch | `WatchMatcher`, `WatchSink`, off the event loop | ✅ Done |
| 10 — Clustering | `ClusterRule`, `ShapeContributor`, and the shape cache | ✅ Done |
| 11 — Timeline | `TimelineAnnotation`, `TimelineMetric`, foldable only | ✅ Done |
| **Stage D — Surface** | | |
| 12 — Commands and controls | Commands, bindings, modal panels | ✅ Done |
| **Stage E — Trust and distribution** | | |
| 13 — Isolation | An opt-in subprocess host, and honesty about what it buys | ✅ Done |
| 14 — The CLI layer | `clv` grows an argv, without changing what bare `clv` does | ✅ Done |
| 15 — Registry | Manifests, `clv plugin install`, signatures, no hosted index | ✅ Done |
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
bucketed by the timeline, folded by the clusterer, and drawn as a structured row
with its own source cell and chips, with no special-casing anywhere; a
plugin-supplied query operator must work in a saved view and in a watch rule; a
plugin-supplied command must appear in the help overlay next to `?`. Where a
seam cannot offer equal terms, the phase says so in the same sentence it offers
the seam.

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
| Isolation model | **Author opts in, operator may insist, CLV gates by kind** | A plugin declares `isolated = True`, **or** the operator writes `isolated = true` in its `[plugin:<name>]` section. Call-and-return kinds — `Exporter`, `WatchSink`, `Command`, `TimelineAnnotation` — get a host. Everything else is refused with a stated reason, `LogSourceProvider` for its own. Opt-in rather than mandatory because a five-line exporter should not pay subprocess startup it did not ask for. *Amended on landing: this row and the phase text disagreed about which kinds — see Phase 13's* **Corrected on landing**. |
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
| `LogFormat` | 7a | Teach CLV to parse a line | ❌ per-line |
| `QueryOperator` | 8 | A new comparison token | ❌ per-entry |
| `ComputedField` | 8 | A queryable field derived, not parsed | ❌ per-entry |
| `WatchMatcher` | 9 | A rule kind beyond pattern | ❌ per-entry |
| `WatchSink` | 9 | Where a watch hit is delivered | ✅ |
| `ClusterRule` | 10 | A volatile token to normalise out | ❌ per-line |
| `ShapeContributor` | 10 | An extra component of a cluster's shape | ❌ per-entry |
| `TimelineAnnotation` | 11 | Marks on the time axis | ✅ |
| `TimelineMetric` | 11 | What a bucket measures, if not count | ❌ per-entry |
| `Command` | 12 | A named action, optionally bound to a key | ✅ |

Isolable, as landed: `Exporter`, `WatchSink`, `Command`, `TimelineAnnotation`.
`LogSourceProvider` is refused — see Phase 13.

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
couplings, both one-directional, and one of them already discharged:

- **Phase 7a's coupling to SSH_TODO Phase 1 is spent.** That phase has landed
  and `node` is already in `NORMALISED_FIELD_KEYS`
  ([query.py:114-120](clv/services/query.py#L114-L120)), so a format's
  `field_names` now meets it as an existing key rather than a moving one. Kept
  rather than deleted because the constraint was real: a format declaring `node`
  would have collided with the one key that means *where CLV read the line*
  rather than what the line says about itself.
- **Phase 12's drawer sections should follow SSH_TODO Phase 7**, which adds the
  host-management dialog and is the larger claim on the drawer's layout budget.

  **Spent, and then moot.** That phase has landed, and Phase 12 then found the
  drawer had no room for a plugin section at all — see its own *Corrected on
  landing*. The coupling was real while both were competing for rows; it ended
  when one of them stopped asking for any.

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
check. Suite green on 3.11 and 3.14: 791 passed on both.

**As shipped.** Two decisions worth recording, neither a change of scope.

*`disable()` marks; it never removes.* [app.py:3113](clv/app.py#L3113) builds the
export dialog's choices keyed `plugin:<index>` and
[app.py:3236](clv/app.py#L3236) resolves them back by *position* in
`registry.exporters`. Removing a disabled plugin from that list would silently
re-target an in-flight export, so `sources`, `filters` and `exporters` stay
append-only-at-load and every use site skips what is disabled. Phase 4's
management UI and Phase 6's budget both inherit that constraint.

*Only `apply_filters` calls `disable()` yet.* That is the defect this phase
exists to fix — a per-render loop that re-reported the same failure every pass.
Providers and exporters are operator-initiated one-shots, and disabling an
exporter for the session because one export hit a permission error would be a
worse bug than the one being fixed, with no way back until Phase 4's re-enable
control. `is_disabled` is honoured everywhere from day one, so later phases add
callers rather than plumbing.

*The prerelease rule diverges from PEP 440 on purpose.* Strict PEP 440 excludes
prereleases from a range that does not name one, which would make
`requires_clv = ">=2.6"` unsatisfied on a running `2.7.0rc1` and disable every
installed plugin on any release-candidate build. CLV compares in plain order.
Documented in `clv/plugins/AGENTS.md` and pinned by a test.

**Also swept here.** The CI matrix gains `3.14` — Requirement 8 names 3.11 and
3.14 and nothing enforced the latter. The stale test counts in
[AGENTS.md:330](AGENTS.md#L330) (620) and [README.md:768](README.md#L768) (290)
are corrected to 791; Phase 16 still owns the test that keeps them honest.

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
for every format CLV parses. Suite green on 3.11 and 3.14: 1625 passed on both.

**As shipped.** Four decisions worth recording, none a change of scope.

*`PLUGIN_API_VERSION` lives in `clv/plugins/__init__.py`, not in `clv/api.py`.*
`api.py` imports the interfaces from `plugins`, so a constant defined in `api.py`
and checked in `PluginRegistry.add` would be a cycle. The loader owns the check,
so the loader owns the constant; `clv.api` re-exports it as the name authors
see. `add()` takes it as a defaulted `api_version=` keyword — a parameter only so
a test can pin it, and defaulted so every existing call site is untouched.

*Both constraints are checked by one loop, and it does not short-circuit.*
`requires_clv` satisfied is not a reason to skip `requires_api`; each fails on
its own account with the same two shapes Phase 1 settled — `bad requires_api:`
naming an unreadable constraint, and `requires plugin API X, running Y` for an
unsatisfied one. Pinned by a test that runs it both ways round.

*Three names beyond the list above are published.* `SourceRef`, because
`ProviderSource.path` is one and `discover()` may return one, so every provider
author needs the type; and the severity helpers `level_matches`,
`highest_level`, `LEVEL_ORDER` and `SEVERITY_BUCKETS`, on the same argument that
already published `normalize_level` and `level_rank` — the alternative is every
plugin reimplementing the level vocabulary and making CLV disagree with itself
about what an operator filtered for. Additive, so the API is still 1.0.

*`==` cannot test the wire round trip on its own.* `LogEntry.fields` is
`compare=False`, so `entry_from_wire(entry_to_wire(e)) == e` passes even when
the fields are dropped entirely. Every round-trip assertion in
`tests/test_api_surface.py` pairs it with an explicit `dict(...) == dict(...)`
and a read-only check, and a decoded entry with no fields comes back on the
*shared* empty mapping rather than a fresh one.

**Also swept here.** [README.md](README.md)'s test count (791, five phases
stale) and its copy of the provider-source claim that
`tests/test_plugin_docs.py` already forbids in `clv/plugins/AGENTS.md` —
"starring … tests for a real `Path`" stopped being true when `JournalRef` became
a ref. `AGENTS.md` carried the same sentence under different wording and is
corrected too.

**A note for Phase 7a.** `tests/test_api_surface.py` asserts out of process that
importing `clv.api` pulls in no Textual widget and no Rich renderable, so a
plugin's own unit tests need no screen. Phase 7a plans to re-export
`FormatProfile` from [columns.py](clv/widgets/columns.py), which imports both.
That type has to move, or be mirrored, before it can join `clv.api` — the test
is the constraint, and it is the right one.

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
hand on a real frozen build. Suite green on 3.11 and 3.14: 1660 passed on both.

**Outstanding: the frozen build has not been checked by hand.** The gate above
asks for a tarball install on a machine with no Python toolchain, and that has
not been done — there is no build spec in the tree and no PyInstaller here, so
the check belongs to whoever cuts the next artifact. What was done instead is a
test pinning the property whose loss would cause it
(`test_the_user_root_does_not_depend_on_where_the_package_lives`): the user root
comes from `$XDG_CONFIG_HOME` and never from `__file__`, asserted with
`sys._MEIPASS` set. That is a proxy for a frozen build and not a substitute for
one — recorded here because `Path.is_dir()` silently removing the journal from
the shipped binary is precisely the failure a source checkout cannot see.

**As shipped.** Six decisions worth recording, none a change of scope.

*User plugins are imported under a synthetic package, not off `sys.path`.* A
module in `~/.config/clv/plugins/` is not reachable by dotted import, and the
obvious fix — prepend the directory to `sys.path` — is the wrong one: a user
file called `json.py` would then shadow the standard library for the whole
process, and a plugin directory is exactly where an unremarkable name like that
gets used. Instead a namespace package (`clv_user_plugins`) is installed in
`sys.modules` with the search roots as its `__path__`. That is not a workaround
but the cheaper design: a bare `foo.py` and a package directory `foo/` load
through one code path, relative imports inside a package plugin work, every
module gets a stable `__name__` for `_extract_plugins`' namespace scan to
compare against, and the walk is the same shape `_load_local` already used — so
the per-module body is now **shared** by both (`_load_module`) rather than
copied.

*Only an **enabled** user module may shadow a bundled one.* The search order in
this file, read literally, says a user root wins over `clv/plugins/`. Taken at
face value that means dropping a file called `journald.py` into the plugin
directory takes the shipped journal provider out of service — an install-time
side effect from a file the operator never named, which is the precise thing
Requirement 2 exists to prevent. An unlisted module is never imported and
therefore claims nothing. Deliberately overriding a bundled plugin is still
possible, still first-name-wins, and still reported; it now requires the same
act of consent as running any other user plugin. Pinned by a test in both
directions.

*Two bundled subpackages may still share a module basename.* `sources/x.py` and
`filters/x.py` have always been two different modules. The shadow rule consults
a map of names claimed by *user* roots and never writes bundled names into it,
so nothing about the bundled walk changed. Pinned, because the general version
of the rule would have quietly broken it.

*A malformed enable-list entry is dropped on its own account.* This file said
the value "degrades to empty and is reported", which read strictly means one
stray character voids the whole list. `parse_log_dirs` is the house pattern for
a validated list key and it drops the entry it cannot read and keeps the rest;
`plugins` follows it. Voiding the list would silently disable a working set of
plugins while the file the operator is staring at looks correct — the failure
they are least equipped to diagnose. "Degrades to empty" now applies only to a
value that cannot be read at all. An underscore-prefixed name gets its own
message rather than the generic one, because it *is* a legal module name and
the generic message would be false.

*Discovered-but-not-enabled is registry state, not an error.* `PluginErrors` has
no severity, and [app.py:3589](clv/app.py#L3589) prints every entry into the log
panel in amber as a *problem*. A plugin waiting to be named is not a problem —
it is the designed resting state of an installed plugin — so it became a
`DiscoveredPlugin` list on the registry, surfaced by `available()` and by one
extra clause in the drawer's status string. Phase 4 replaces that whole string
and inherits the state rather than inventing it.

*Names are matched case-insensitively, and `config_version` moved to 2.* A
settings file is operator prose; `Redact` where the file is `redact.py` is a
typo class, not an intent. And the template's option set changed, which is the
stated rule for bumping `CURRENT_CONFIG_VERSION` — so existing operators get the
upgrade notice, and `clv --upgrade-config` merges the new `plugins` block into
their file instead of leaving them a key with no prose.

**Also swept here.** `plugin_search_roots()` de-duplicates by resolved path, so
naming the user directory in `CLV_PLUGIN_PATH` does not make every plugin in it
report itself as shadowing itself. `tests/test_plugins.py` gained an autouse
fixture dropping the synthetic package from `sys.modules` between tests: without
it a plugin loaded from one test's temp directory was handed to the next test
that asked for the same name.

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
which and what to do about each, at 80 columns. Checked by hand against a real
`CLV_PLUGIN_PATH` root, which also produced a fifth row this phase did not
plan for: a name in `settings.conf` matching nothing on disk. Suite green on
3.11 and 3.14: 1690 passed on both.

**As shipped.** Five decisions worth recording, one of them a departure from the
text above.

*The rows are in a modal, not in the drawer.* This phase asked for "a plugin
section in the Advanced drawer", and that section cannot exist. The drawer is
capped at `max-height: 16` and
[clv/widgets/AGENTS.md](clv/widgets/AGENTS.md) records the consequence in the
imperative: a new **row** pushes what follows below the fold, where it lays out
and paints nothing — join an existing row, or use `#drawer-actions`, which is
horizontal and costs no rows. One row per installed plugin is precisely the
shape that box has no room for, and four installed plugins is four rows before
any of them has said anything. The SSH fleet hit the same wall and settled it
the same way ([SSH_TODO.md](SSH_TODO.md) Phase 7): a summary line in the drawer,
the detail in a modal. Plugins follow the precedent rather than inventing a
second answer — a `Plugins` button beside `Rescan sources`, and `P`.

*The row is the origin, not the plugin object.* The enable-list names modules
and one module may export three stages, so a row is the thing an operator
installs, names and deletes. Kinds aggregate over the plugins the module
supplied, which is what makes "a row lists kinds rather than one kind" true
rather than decorative.

*`add()` now files a plugin under every interface it implements.* It used
`if/elif/else`, so a plugin that was both a provider and a stage was stored as a
provider alone and its `apply()` was never called — a plugin silently doing half
of what it declares, with no diagnosis anywhere, because from the outside it had
loaded. A row cannot honestly list two kinds while that is true. Strictly a
fix, and `sources`, `filters` and `exporters` stay append-only-at-load so the
positional `plugin:<index>` export key is untouched.

*Disabling a bundled plugin lasts for the session.* The enable-list governs the
user directory only, so a shipped drop-in has no name in it to remove. The
alternative — a `disabled_plugins` key — was declined: it adds config surface
Phase 3 deliberately did not create, and gives a plugin's fate two places to be
decided. The dialog says which kind of disable it is offering, beside the
control, before it is pressed.

*`DiscoveredPlugin.enabled` is kept in step with the file.* Found in review, not
in the plan: a plugin that raises on *import* has no live object to mark
disabled, so switching it off removed the name from `settings.conf` and the row
sprang straight back to `enabled` on the next redraw. `_adopt_enable_list`
re-reads the decision onto the discovered entries after every write. Pinned by a
test, because it is invisible in every other state.

**Also swept here.** A disabled exporter is no longer offered by `Ctrl+E`.
[app.py](clv/app.py)'s `_exporter_choices` kept disabled entries so that
`plugin:<n>` stayed positional — but the index rides *in the key*, so omitting
one renumbers nothing, and without this the disable control never reached the
one kind an operator is most likely to aim it at. `_exporter_at` re-checks too:
a request can outlive the list it was built from, because `P` is reachable while
the export dialog is open.

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
reads its own settings without importing anything from `clv.services`. Checked
by hand against a real `CLV_PLUGIN_PATH` root with two stages that fight over
the same text: at `priority = 50` the redactor sees `password` and replaces it;
at `950` the shouter has already uppercased the line and the redactor's pattern
no longer matches. The composition flips, deterministically, and nothing else
moves. Suite green on 3.11 and 3.14: 1767 passed on both.

**As shipped.** Five decisions worth recording, one of them a bug this phase
found rather than a choice it made.

*`configure()` hands over a live mapping, not a snapshot.* The phase asked for
"a read-only mapping" and for journald to keep "re-reading its opt-in on every
`discover()`", and a frozen mapping cannot do both. What is handed over is a
`MappingProxyType` over a dict the registry owns: CLV mutates the dict in place
when it re-reads the settings file, and every view already handed out sees the
new values. So a plugin keeps its mapping and reads through it, `configure()`
stays a one-shot that nobody has to make idempotent, and the drawer's switch
still takes effect without a restart. `LogEntry.fields` is the same mechanism,
which is why `refresh_settings` clears and repopulates rather than replacing:
replacing a dict strands every view onto it.

*`enable_journald` stayed in `[log_viewer]`, aliased into `[plugin:journald]`.*
Moving it was the tidier end state and was declined. It is in every operator's
settings file, it is what the Advanced drawer writes, and it is in the README —
so renaming it buys tidiness at the cost of a config migration and a drawer
change. `config._LEGACY_PLUGIN_KEYS` is one entry, and it makes the provider's
`configure()` exactly what a third party would write, which is the whole of what
the phase wanted proved. A section that sets `enabled` itself still wins.

*The module-level `journald.enabled()` survives, and is not a second mechanism.*
It is what the drawer's `_sync_journald_status` calls and what a directly
constructed provider falls back to — which is every test in
`tests/test_journald.py`, unchanged, as the phase required. What changed is
which of the two is the *plugin's* route to its own settings: it is
`configure()`, and `test_a_configured_provider_never_reads_the_settings_file`
pins that by making `load_config` raise.

*`setup()` runs from the app, not from the loader.* "Before first use" has no
single meaning here, and putting it inside `load_plugins()` would make a
function that half this suite and every plugin author's unit tests call start
acquiring resources on their behalf. `PluginRegistry.start()` is called from
`on_mount` after the remote wiring, so a provider's `setup()` sees a fully
assembled plugin rather than one still waiting for its resolver. `shutdown()` is
the mirror, from `on_unmount`, between the readers closing and the session being
saved — pinned as an exact three-element sequence rather than a pair of
inequalities, because "after the readers" and "before the save" are two claims
and a test that checked one would pass while the other rotted.

*A hanging `teardown()` still hangs exit, and the docs say so.* Exceptions are
contained; time is not. Bounding plugin *time* is Phase 6, and building a second
time-bounding mechanism here is exactly what having a Phase 6 is meant to
prevent. `clv/plugins/AGENTS.md` states the limit in the same paragraph that
offers the hook, per Requirement 3 — which applies to CLV's own documentation
about itself, not only to the isolation chapter.

**Also swept here.** `clv --upgrade-config` deleted `[plugin:<name>]` sections.
`config_upgrade._merge` copied `[log_viewer]` options and `[ssh:<name>]` sections
and dropped everything else, so the first upgrade after this phase shipped would
have silently removed an operator's plugin configuration — silently, because
nothing else in the file changes and the plugin simply starts running on its
defaults. The section loop is now driven by a prefix table rather than by `ssh:`
alone, and the rule it encodes is the general one: a section this module does not
understand is *copied*, not judged unused. `UpgradeResult` gained `plugins`
alongside `hosts` and `describe()` names them. Found in planning, so the
regression test fails against the previous code rather than merely passing
against the new.

*`setting_bool` and `setting_list` joined `clv.api`.* Beyond the phase text, on
the argument that already published `normalize_level`: `configure()` hands over
the raw strings `configparser` read, and without these every plugin writes
`value.lower() == "true"` and CLV ends up disagreeing with the operator's own
file about what `yes` means. Additive, so the API is still 1.0.

*`_set_enable_ssh` needed no refresh call.* It persists and then delegates to
`action_reload_sources`, which re-reads the config and refreshes there. Noted
because the plan listed it as a third call site and it would have been a second
refresh of the same values.

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
  `LogFormat` and `ClusterRule` in Phase 7a and 10. The read path is where an
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

Checked by hand against a 300-line buffer with two stages installed, one
sleeping 1 ms per line and one doing the redaction the README documents. Renders
1, 2 and 3 cost 332, 329 and 333 ms; render 4 costs 5.2 ms, and the `P` dialog
reads:

```
cheap.py      ('filter',)  loaded
sluggish.py   ('filter',)  failed   over the render budget (328 ms against
                                    250 ms) on 3 consecutive passes
```

The redactor is still running and still redacting. At
`plugin_time_budget_ms = 0` the same six renders all cost ~330 ms and nothing is
disabled, which is the escape hatch doing exactly what it says.

Suite green on 3.11 and 3.14: 1803 passed on both.

Benchmarks, 5 000 entries, one stage:

| | |
| --- | --- |
| staged view, uncached | 30.44 ms |
| staged view, cached repeat | 0.01 ms |
| `apply_filters`, untimed | 18.27 ms |
| `apply_filters`, timed | 18.35 ms |
| zero plugins, uncached | 0.52 ms |

**As shipped.** Six decisions worth recording, two of them reversals of what
this phase's own text said.

*The filter loop was inverted to stage-outer, and measurement is what decided
it.* The plan for this phase argued for keeping the entry-outer loop: inverting
it changes the order third-party code sees its entries in and costs an
intermediate list per stage. That was the wrong trade, and the benchmark said
so. Entry-outer needs two clock reads **per call** — entries times stages — and
against a stage that does almost nothing the measurement cost 2.5 ms where the
work cost 0.6 ms. At the `max_buffer_lines` ceiling that is a fifth of a second
of pure measurement per pass, which the budget would then charge to the plugin:
a guard that disables plugins for the cost of guarding them. Stage-outer needs
two reads per stage per pass, and the measured overhead is 0.08 ms on an 18 ms
pass — the cost of the guard stops depending on the size of the buffer. What it
buys back is one list of pointers per stage, two live at a time. The composition
is unchanged in every respect that is observable: a dropped entry is invisible
to later stages, a raising stage still lets the entry it choked on through
untouched and is still skipped by everything after it, and order within a stage
is still the buffer's.

*`set_entries` did not bump `revision`, and a cache keyed on revision would have
been wrong from the first render.* `SourceBuffer.revision` was bumped by
`prime`, `poll` and rotation — but not by the one path that replaces a buffer's
deque wholesale, which is what `app._entries = ...` does and what several
hundred tests in this suite use to seed a pane. It bumps now, on its own
account: it is the largest change a buffer's contents can undergo, and the merge
cache one level down was keyed on the same number. The residual hole is stated
rather than closed — a deque mutated in place behind the session's back still
moves nothing — so the cache key pairs the revision with the line count, which
catches it.

*A relative time window had to be anchored before any cache could hit.*
`parse_relative_window` reads `datetime.now()`, so `_filter_spec()` returned a
window a few microseconds narrower on every call. With `15m` selected the cache
key could never repeat and the whole phase would have been a no-op for the case
with the most entries to filter. Worse, and true before this phase: the pane,
the status line and the histogram were each filtering against a *different*
window inside one render. The instant a relative window counts back from is now
held still and re-anchored when the lines change, when the open source changes,
or when the operator picks a different window — so it is exactly as fresh as the
data it describes, and nothing on screen can have moved without one of those
three having happened.

*`refresh_settings` bumps the generation, and leaving it out would have been a
bug.* The phase text lists enable, disable and failure. But Phase 5 hands a
plugin a **live** mapping it reads *through*, so editing a redaction pattern in
`settings.conf` changes what a stage returns without any call reaching the
registry — and a staged view keyed only on enable/disable would have gone on
serving the old text. Pinned by a test that changes a section and asserts the
output changes.

*One budget class, one wired instance.* The phase asked for "two budgets, not
one" — a render-path budget and a read-path budget for `LogFormat` and
`ClusterRule`. Those kinds do not exist until Phases 7a and 10, and shipping
`plugin_read_budget_ms` now would be a documented config key that does nothing
for three phases, which is the "half-wired seam" this file's own preamble
refuses. What landed is the general `PluginBudget` — a ceiling, a label and a
strike count — with exactly one instance constructed. Phase 7a builds its own
from the same class and adds its key beside its first caller. The policy is
stated once and neither path can drift from it.

*Coarse kinds are not timed, and the reason is Phase 1's.* "Per call for coarse
ones" is not implemented for `LogSourceProvider` and `Exporter`. Phase 1
recorded the argument already: those are operator-initiated one-shots, and
disabling an exporter for the session because one export was slow would be a
worse bug than any this fixes, with no way back but the `P` dialog. A timing
that can lead to no action is a number nobody reads. What a slow `discover()`
actually needs is a *report* — "this provider took 4 s" — which is a different
mechanism with a different surface, and is not smuggled in here.

**Also swept here.** `apply_filters` used to test `is_disabled` once per entry
per stage; the check is hoisted to one list built per pass. `PluginBudget.settle`
re-checks `active` itself rather than trusting its caller, so a budget set to
`0` is off however it is driven. `CURRENT_CONFIG_VERSION` moved to 3 — the
template's option set changed, which is the stated rule — and `clv
--upgrade-config` was checked by hand against a v2 file carrying a
`[plugin:redact_secrets]` section: the new key is merged in, the section, the
host block and the operator's own values all survive.

**Commit.** `perf(plugins): cache the staged view and budget every plugin kind`

---

# Stage C — Core seams

Five seams, one per core feature a plugin could not previously reach. Each ends
with the same kind of gate: a plugin-supplied thing working through a core
feature that knows nothing about plugins (Requirement 9).

Six phases, though, and only five of them are seams. Designing the parsing seam
is what found the format it cannot carry: logfmt needs a built-in matcher to
stand down in its favour, and *"replacing a built-in format"* is refused to
plugins in *Still deliberately out of scope*. Phase 7b is therefore core work
with no plugin in it. Where a seam cannot offer equal terms the phase says so in
the same sentence it offers the seam; here the phase after it does the work.

## Phase 7a — Parsing

`_parse_structured` ([parsing.py:545-632](clv/services/parsing.py#L545-L632))
dispatches through hardcoded formats on cheap first-character checks and falls
through to `raw`. There is no registry and no hook, so "CLV does not know my
format" has no plugin-shaped answer — and a `FilterStage` is not one: it can
rewrite `fields` after the fact but cannot make a line parse, cannot claim a
`format_name`, and cannot supply the timestamp the timeline buckets on.

A `format_name` is four registrations, though, and only the first is in
`parsing.py`: the dispatch that sets the name, `FORMAT_LABELS` and
`NO_FIELD_REASONS` ([detail_pane.py:40-66](clv/widgets/detail_pane.py#L40-L66)),
`FORMAT_PROFILES` ([columns.py:128](clv/widgets/columns.py#L128)) and
`_JSON_CONSUMED` ([columns.py:179](clv/widgets/columns.py#L179)). The last two
postdate this phase's first draft and are the ones that fail quietly: a format
with no profile gets `DEFAULT_PROFILE`, which is a row with no source cell and
no chips. No test imports any of the three tables, and there is no canonical
list of format names to check them against.

**Expected outcomes**

- **`LogFormat`**, in `clv.api`:

  ```python
  class LogFormat(Plugin):
      #: Field names this format can produce, so field queries know them
      #: before a matching line has been seen.
      field_names: frozenset[str] = frozenset()

      #: What an operator calls this format in the detail pane. Without
      #: one the pane falls back to the bare `format_name` identifier.
      label: str = ""

      #: Which of `field_names` earns the source cell, a pinned chip or a
      #: varying chip in a structured row. `FormatProfile()` is a legal
      #: answer and means "message only" — but it has to be the author's
      #: answer rather than a default they never saw.
      columns: FormatProfile = FormatProfile()

      @abstractmethod
      def parse(self, line: str) -> Optional[LogEntry]: ...
  ```

- **Built-ins first, plugins second, `raw` last.** Two load-bearing reasons: a
  line that already parses costs a plugin nothing, and a third-party format
  cannot shadow syslog. A plugin wanting to *replace* a built-in is out of
  scope and the documentation says so — Phase 7b is what CLV does instead, on
  its own account, when the format is worth the phase.
- **Injection, not import.** `LogParser`
  ([parsing.py:699-741](clv/services/parsing.py#L699-L741)) gains a `formats=()`
  keyword, `Buffer` ([session.py:119](clv/services/session.py#L119)) passes what
  the session was given, and the app supplies the registry's formats.
  `parsing.py` keeps knowing nothing about plugins.
- **A format declares its row, or it does not get one.** `FormatProfile`
  ([columns.py:110-124](clv/widgets/columns.py#L110-L124)) is re-exported from
  `clv.api` and gains one field, `consumed: frozenset[str] = frozenset()` — the
  keys the format already spent on the time, level or message cell and must not
  get back as chips. That set is `_JSON_CONSUMED` today, reached through
  `if entry.format_name == "json"`
  ([columns.py:381](clv/widgets/columns.py#L381)), and generalising it is not
  tidying: a format whose keys are the writer's choice rather than a regex's
  group names is exactly the case that needs it, and **every** plugin format is
  one. The special case goes, `json`'s entry carries the set that used to be
  hardcoded, and `_chips_for` reads it off the profile.
- **Profiles are injected, like everything else.** `columns.py` does not import
  `clv.plugins`; the app calls `columns.install_profiles(...)` once at startup
  with the enabled formats' `columns` and `label`, and the two lookup sites
  ([columns.py:603](clv/widgets/columns.py#L603),
  [columns.py:647](clv/widgets/columns.py#L647)) resolve through it before
  falling back to `FORMAT_PROFILES` and then `DEFAULT_PROFILE`. The same
  asymmetry as Phase 8's `query.install_operators(...)`, for the same reason:
  the renderer runs per row and cannot afford a registry walk.
- **A format's entries are first-class.** This is Requirement 9 for this phase.
  An entry a plugin format produced must be searchable by field query — which
  means `field_names` feeds `NORMALISED_FIELD_KEYS`
  ([query.py:110-122](clv/services/query.py#L110-L122)) and `_known_fields`
  ([app.py:3321-3328](clv/app.py#L3321-L3328)) so completion offers the field
  before a matching line is on screen — bucketed by the timeline, clustered by
  the repeat folder, shown in the detail pane, and exportable. Those five need
  no new code if the entry is well-formed, and the test is that they need none.
- **The structured row is the exception, and it is why the bullet above no
  longer says "none of these".** `plan_columns` and `render_row` key off
  `format_name` in tables `parsing.py` does not own, so an entry from a format
  that declared no `label` and no `columns` renders without error and reads as a
  downgrade: the right timestamp and level, the bare identifier where the format
  name should be, no source cell, no chips. Equal terms there is a
  *declaration*, not an inference — which is what the two new attributes buy,
  and the reason they are not optional in the docs even though they are optional
  in the type.
- **Continuation still works.** A plugin format participates in carry-forward
  exactly as a built-in does: an unparsed line after a plugin-format line
  inherits its timestamp and level, and inherits no fields
  ([parsing.py:718-741](clv/services/parsing.py#L718-L741)).
- **A format that raises is disabled**; a format returning a malformed
  `LogEntry` — wrong types, `format_name` of `"raw"`, a `format_name` already in
  `FORMAT_NAMES`, a non-string field value, a non-`LogEntry` — is rejected with
  a message naming the rule it broke. A `columns` naming a key outside
  `field_names` is rejected the same way and at load, because a profile pointing
  at a field the format never produces is a row that quietly loses its source
  cell. The read path cannot afford to trust this one: a stage that misbehaves
  costs a render, a format that misbehaves corrupts the buffer.
- **A format is inside the read-path budget** from Phase 6, measured per line.
- **A worked example ships**: `clv/plugins/formats/` as a live drop-in
  directory, with nginx `error_log` as the reference — genuinely common and
  genuinely not covered by the built-ins. It ships **disabled** and the
  operator enables it like any other, so the drawer's count keeps meaning
  "plugins someone installed" ([TODO.md:243-246](TODO.md#L243-L246)).

**Documentation changes.** `clv/plugins/AGENTS.md` gains a `LogFormat` section
of equal weight to the others: the interface, the built-ins-first rule,
`field_names`, `label` and `columns` and why each exists, the `LogEntry`
contract a format must honour, carry-forward, the per-line budget, and the note
that `parse()` is the hottest third-party code in CLV. `FORMAT_PROFILES`' own
comment ([columns.py:126-127](clv/widgets/columns.py#L126-L127)) stops being a
note about a dict and becomes the statement of the contract — what a format has
to register, in which four places, and what `DEFAULT_PROFILE` means when it is
reached by accident rather than on purpose. `parsing.py`'s module docstring
gains `FORMAT_NAMES` beside its format/keys table
([parsing.py:22-31](clv/services/parsing.py#L22-L31)) as the list all four are
checked against. `README.md`'s multi-format bullet gains "and any format a
plugin teaches it."

**Testing** (new `tests/test_plugin_formats.py` and
`tests/test_format_registration.py`)

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
- **The completeness test, and it is about built-ins as much as plugins.**
  `FORMAT_NAMES` in `parsing.py` becomes the canonical list and carries one
  fixture line per name. For every name: a real line parses to it, it has a
  `FORMAT_LABELS` entry, and if that line recovered any fields it has a
  `FORMAT_PROFILES` entry too. In the other direction, no key of
  `FORMAT_LABELS`, `NO_FIELD_REASONS` or `FORMAT_PROFILES` is missing from
  `FORMAT_NAMES`. Adding a format to the parser and nowhere else has to fail the
  suite, because today it fails nothing.
- A plugin format that declares neither `label` nor `columns` still renders, so
  the degradation is defined rather than accidental; one that declares both gets
  a source cell and chips, asserted through `render_row` rather than by reading
  the profile table back.
- A `columns` naming a key outside `field_names` is rejected at load, and so is
  a format claiming a `format_name` already in `FORMAT_NAMES`.
- `consumed` off the profile reproduces `_JSON_CONSUMED` exactly: the journald
  allowlist test ([test_log_columns.py:125](tests/test_log_columns.py#L125))
  passes unmodified with the `format_name == "json"` branch deleted.
- Zero format plugins: `LogParser` behaves byte-identically to today, asserted
  against the existing parsing suite unchanged.

**Gate.** A plugin file in `~/.config/clv/plugins/` teaches CLV a format it did
not know, every core feature works on the result with no core change beyond this
phase's, and its rows carry a source cell and chips rather than
`DEFAULT_PROFILE`. Adding a format to the parser and to nothing else now fails
the suite — which is the first thing Phase 7b leans on. `tests/test_parsing.py`
passes unmodified. Suite green on 3.11 and 3.14: 1890 passed on both.

Checked by hand against a seeded `nginx_error.py` copied up into a real plugin
root. Unnamed it reads `not enabled`; named in `settings.conf` it parses the
error log, and the same lines that were eight identical raw rows before become:

```
09:25:01 ERROR shop.example.com[1234] connect() failed (111: Connection refused)
                                      while connecting to upstream  host=10.0.0.5
```

`host:10.0.0.5` answers for it and for an access log in the same query, which is
the normalisation rule doing what it is for. The completeness test was checked
by breaking it both ways: a name added to `FORMAT_NAMES` alone fails five tests,
and a `FORMAT_LABELS` entry with no parser behind it fails one.

**As shipped.** Six decisions worth recording, two of them departures from this
phase's own text.

*`format_name` is a declared attribute, and the sketch above could not work
without it.* The interface as written carries `field_names`, `label` and
`columns` but no name to key them on — and all three are consumed *before* a
line has been parsed: profiles and labels are installed at startup, and
`field_names` has to reach the query vocabulary so a field completes against an
empty buffer. So `format_name` joins them, is required in practice though
defaulted in the type, and is what every entry `parse()` returns must carry.
That last part is also the cheapest runtime check in the validator, and it
catches the copy-paste mistake — a format returning another's entries — that
nothing else would notice.

*`formats` is a `FormatStack`, not a bare sequence.* The phase says `LogParser`
gains a `formats=()` keyword, and it does; what it holds is one object rather
than a list of plugins. The reason is the injection rule: `parsing.py` may not
import `clv.plugins`, but a format that raises still has to go through
`PluginRegistry.disable`, a malformed return still has to be reported by the
rule it broke, and the read budget still has to be charged. Handing the parser
bare plugins would mean handing it a fault callback and a budget as well, and
spreading one plugin's guard across two modules that are not allowed to know
about each other. `FormatStack` is a `Sequence`, so `()` and a live stack are
the same kind of thing and the zero-plugin path is a falsiness test.

*The worked example is seeded into `plugins/examples/`, not into the plugin
directory.* The phase asked for `clv/plugins/formats/` as a live drop-in
directory shipping *disabled* — and bundled drop-ins load without being named,
so "disabled" would have needed a fourth classification that only this one
plugin uses. Seeding it as a user plugin was the obvious fix and was wrong in a
way the suite caught: it made every fresh install report `1 not enabled`
forever, against a plugin CLV put there, which is exactly the count this file
wanted to keep meaning "plugins someone installed". One directory down,
`pkgutil.iter_modules` does not descend and a folder with no `__init__.py` is
not a package, so the example is shipped, readable, and invisible. Copying it up
one level is the same act as installing any other plugin — and is a better start
for an author than an empty file.

*`FormatProfile` moved, and so did `FORMAT_LABELS` and `NO_FIELD_REASONS`.* The
phase's note for this phase predicted the first: `tests/test_api_surface.py`
forbids `clv.api` from pulling in Rich, and `columns.py` imports it. The other
two followed from a constraint that only appeared once `columns.format_label`
existed — it needs `FORMAT_LABELS`, and `columns.py`'s own docstring is explicit
that nothing in it imports Textual, which `detail_pane.py` does. All three are
declarations about a format rather than about a widget, so
`clv/services/formats.py` is where they live; both widget modules re-export what
they used to define and every existing import site is untouched.

*The read path is measured per call, and the render path's own finding is why it
can be.* Phase 6 inverted the filter loop because two clock reads per call cost
four times what a no-op stage costs. A batch of lines cannot be inverted the
same way without restructuring carry-forward, which is the one piece of state
`LogParser` exists to hold — so the reads are halved instead, by reusing each
call's end stamp as the next one's start. It is affordable here for a reason the
filter path did not have: a line only reaches a format after every built-in
matcher has already declined it, so the guarded work is expensive by
construction. Benchmarked in `tests/test_plugin_perf.py` rather than asserted.

*One `install_profiles` call, not two.* A format's `label` and its `columns` are
registered together because registering them apart is how a format ends up
half-declared — the failure mode this whole phase exists to make visible. The
detail pane reads the label back through `columns.format_label`.

**Also swept here.** A bare `[991]` in the source cell. `_source_value` appended
the PID to whatever source it had, including none — so a line with a PID and no
program name rendered a PID as a cell of its own, which that method's own
docstring rules out. Reachable with no plugin at all (a JSON line carrying `pid`
and none of the `json` profile's source keys), and found here because a profile
naming a source key a particular line did not carry is the ordinary case for a
plugin format rather than the unlucky one.

`CURRENT_CONFIG_VERSION` moved to 4: the template's option
set changed, which is the stated rule. Re-enabling a plugin now clears its
strike count on *both* budgets — a format is charged against the read one, and
nothing else would have forgotten it, so Re-enable would have put a
budget-disabled format back one slow batch away from being disabled again.
`FORMAT_PROFILES`' comment stopped being a note about a dict and became the
statement of the four-registration contract, which is what
`tests/test_format_registration.py` now enforces.

**Commit.** `feat(plugins): a LogFormat seam, column profiles and an nginx reference`

---

## Phase 7b — logfmt as a built-in

`key=value key=value` is the densest format CLV cannot read, and it is what Go,
Rust and most of the Prometheus-adjacent ecosystem write by default.
`level=info msg="thing happened" dur=1.2ms` is `raw` today; so is
`ts=2026-08-07T09:25:01Z level=error msg="boom" svc=api`; but
`2026-08-07T09:25:01Z level=info msg="boom" svc=api` is **`iso`**, its whole
logfmt remainder sitting in `message` with no fields and no level, because
`_RE_ISO_PLAIN` ([parsing.py:449-452](clv/services/parsing.py#L449-L452)) got
there first — all three verified against the live parser. Teaching a built-in
branch to stand down is what a plugin is refused in *Still deliberately out of
scope*, so logfmt is a phase rather than the reference plugin: Phase 7a's seam
is sound; this is the one format it cannot carry.

**Expected outcomes**

- **A sixth built-in, `logfmt`, matched last.** After every anchored format has
  declined — BSD syslog in particular — and before the fall-through to `raw`.
  Ordering is the anti-false-positive mechanism, not a performance detail:
  `key=value` is ordinary inside a syslog *message*. `Failed password for root
  from 10.0.0.5 rhost=10.0.0.5 user=root` and `audit: type=1400
  apparmor=DENIED pid=991` both parse as `syslog` today, verified, and this
  phase's first requirement is that they still do.
- **The guards are structural and deliberately hard to satisfy**, in the voice
  of `payloads.py`'s detectors and for the reason `_csv` gives about commas in
  prose:
  * **The line opens with a pair** — `key=` at the start, a key being
    `[A-Za-z_][A-Za-z0-9_.\-/]*`. Prose that mentions `rhost=` in the middle
    never reaches the second guard.
  * **Every token is a pair.** Pairs are consumed left to right; anything left
    over that is not whitespace refuses the line, so `audit: type=1400 ...`
    fails on its first token even with no syslog prefix in front of it.
  * **Two pairs at least.** One `key=value` alone is a `.env` line, a
    `.properties` entry or a shell assignment, and CLV opens *any readable text
    file* ([README.md:39](README.md#L39)).
  * **One anchor key** from `_JSON_TS_KEYS`, `_JSON_LEVEL_KEYS` or
    `_JSON_MSG_KEYS` ([parsing.py:474-476](clv/services/parsing.py#L474-L476)).
    A line with none of the three has nothing to put in the message cell, so
    claiming it buys a structured row that says less than the raw one did. This
    is the guard most likely to be revisited: `dur=1.2ms code=500 path=/x` is
    real logfmt and is refused by it.
- **Quoting and escaping, stated rather than inherited.** A value runs bare to
  the next space or is double-quoted. Inside quotes `\"` and `\\` are honoured
  and every other backslash stays literal, because `path="C:\Users\bob"` is the
  common case and a deliberate `\n` is not. An unterminated quote refuses the
  pair and therefore the line. `key=` with an empty value is **kept**, as
  `_parse_json` keeps `{"err":""}` — a writer who typed `err=` said something,
  and `_chips_for` already declines to draw an empty chip.
- **Both ISO branches defer; nothing else does.** When `_RE_ISO_LEVEL` or
  `_RE_ISO_PLAIN` matches, its `msg` remainder is offered to the collector under
  the same guards, and on acceptance the entry is `logfmt` carrying that
  branch's timestamp — and, from `iso-level`, the level it already recovered.
  Syslog does **not** defer: a syslog line whose payload happens to be logfmt is
  still a syslog line, and its `host`, `tag` and `pid` outrank the relabelling.
- **`normalize_level()` on the parsed value, never `_scan_level`.**
  `_RE_BARE_LEVEL` ([parsing.py:479](clv/services/parsing.py#L479)) is not
  `re.IGNORECASE`, so the scanner reads `level=INFO` and misses `level=info` —
  which is what the `iso` path does to these lines today. The branch resolves
  the level from the value through `_JSON_LEVEL_KEYS` in order, as `_parse_json`
  does, and the collector is `_flatten_json`'s shape
  ([parsing.py:406-428](clv/services/parsing.py#L406-L428)) rather than
  `_match_fields`' ([parsing.py:362-377](clv/services/parsing.py#L362-L377)),
  which reads named regex groups against a fixed key list and has nothing to
  offer a format whose keys are the writer's: build a dict, stop at
  `_MAX_FIELDS = 64` in document order, freeze with `_freeze_fields`. Values are
  strings and never coerced; a repeated key keeps the last, which is what
  `json.loads` does with a repeated object key. **Every key is kept, including
  the consumed ones** ([parsing.py:534-536](clv/services/parsing.py#L534-L536))
  — `msg:` has to stay queryable on the line that carried it.
- **Registered in all four places, or it is not done.** The dispatch sets the
  name; `FORMAT_LABELS` gains `"logfmt": "logfmt (key=value)"`;
  `FORMAT_PROFILES` gains an entry — `source_keys` of `("service", "logger",
  "component", "app", "subsystem")`, `pid_key="pid"`, and a chip **allowlist,
  never a sweep**, for the reason the `json` entry already argues
  ([columns.py:150-156](clv/widgets/columns.py#L150-L156)) — whose `consumed` is
  the set Phase 7a lifted off `_JSON_CONSUMED`, shared with `json` because
  `msg=` and `"msg":` mean the same thing. `NO_FIELD_REASONS` needs no entry: a
  line that cleared the guards carries two fields by construction.
- **Logfmt lines stop being continuations, and that is the trade.**
  `LogEntry.structured` is `format_name != "raw"`
  ([parsing.py:237-239](clv/services/parsing.py#L237-L239)) and it gates
  carry-forward in `parse_lines`
  ([parsing.py:682](clv/services/parsing.py#L682)) and `LogParser.feed`
  ([parsing.py:726](clv/services/parsing.py#L726)). In a mixed file a logfmt
  line that today inherits the timestamp above it and renders dimmed becomes a
  first-class row, with its own level and fields and — if it carries no `ts=` —
  an **empty time cell where one used to appear**. Taken deliberately:
  `parse_lines`' docstring already argues a continuation must not claim facts
  its line never stated, and a line that said `level=error` stated plenty. It is
  also why the anchor guard is not negotiable — a line claimed on `a=1 b=2`
  alone would give up an inherited timestamp and return nothing for it.
- **Field names behave exactly like JSON's, including where that stings.**
  Logfmt keys are the writer's, so they do **not** join `NORMALISED_FIELD_KEYS`
  ([query.py:110-122](clv/services/query.py#L110-L122)), which `README.md`
  publishes as a fixed list. They reach completion through `collect_field_names`
  ([query.py:198-209](clv/services/query.py#L198-L209)) the moment a line
  carrying one is read — no change to `query.py` at all, which is the parity
  claim rather than a convenience. `host=`, `status=`, `user=` and `size=` are
  common logfmt keys that collide with normalised names, and the collision
  resolves the way JSON's already does: the line's own value wins.

**Documentation changes.** `parsing.py`'s module docstring gains a `logfmt` row
in its format/keys table ([parsing.py:22-31](clv/services/parsing.py#L22-L31)) —
*every `key=value` pair on the line* — and a paragraph on the guards and the ISO
deferral, because *"why was my line not claimed"* is the question this format
will generate and the answer has to be findable from the source. `README.md`'s
multi-format bullet ([README.md:16-18](README.md#L16-L18)) names logfmt, and the
field-vocabulary sentence becomes "every key a JSON or logfmt line carries". The
new `FORMAT_PROFILES` entry carries its allowlist argument inline, as `json`'s
does.

**Testing** (new `tests/test_logfmt.py`, extend `tests/test_parsing.py`)

- The detection table ([test_parsing.py:35-51](tests/test_parsing.py#L35-L51))
  and the fields table
  ([test_parsing.py:148-173](tests/test_parsing.py#L148-L173)) each gain all
  three dialects: bare, `ts=`-first, and ISO-timestamp-first.
- **The false-positive corpus is what `tests/test_logfmt.py` is for.** Both
  syslog lines above still parse as `syslog` with `host`, `tag` and `pid`
  intact; a one-pair line, a `KEY=value` `.env` line, a line with a trailing
  bare word, an unterminated quote and a pair-free line all stay `raw`.
- `level=info` and `level=INFO` both yield INFO — the test that fails the day
  the branch reaches for `_scan_level`.
- Quoting: spaces inside quotes survive, `\"` and `\\` unescape, a Windows path
  keeps its backslashes, and `key=` yields an empty string that is kept.
- Sixty-five pairs yield sixty-four fields, in document order.
- **The four-table registration**, through Phase 7a's
  `tests/test_format_registration.py` with no edit beyond one fixture line: this
  phase is the first proof that the completeness test does its job.
- A logfmt row renders with a source cell and chips and repeats neither `msg`,
  `level` nor `ts` as a chip, with the journald allowlist test
  ([test_log_columns.py:125](tests/test_log_columns.py#L125)) as the template;
  the detail pane shows `logfmt (key=value)` and every pair, after the JSON
  dotted-keys test ([test_detail_pane.py:299](tests/test_detail_pane.py#L299)).
- `collect_field_names` reports a logfmt line's keys
  ([test_field_query.py:257](tests/test_field_query.py#L257)); a colliding
  `host=` keeps the line's value, mirroring
  `test_a_json_key_colliding_with_a_normalised_name_keeps_the_json_value`
  ([test_parsing.py:267](tests/test_parsing.py#L267)); and
  `NORMALISED_FIELD_KEYS` is asserted unchanged.
- **The carry-forward change is an assertion, not a discovery**: a logfmt line
  after an ISO line is not a continuation, inherits no timestamp, and is counted
  in `Timeline.undated` when it carries no `ts=`.

**Gate.** A Go service's log reads as columns — time, level, source, message,
chips — with no plugin installed and no setting touched; a syslog line
mentioning `user=root` is still syslog; and every existing case in
`tests/test_parsing.py` passes unchanged except the rows this phase added. Suite
green on 3.11 and 3.14: 1943 passed on both.

Checked by hand against a mixed file, rendered through the real column planner.
The four logfmt rows carry time, level, a source cell and chips; `msg`, `level`
and `ts` appear in none of them; and the one line with no `ts=` shows the empty
time cell this phase traded for:

```
09:25:00       sshd[991]  Accepted publickey for root from 10.0.0.5 rhost=10.0.0.5 user=root  host=web01
09:25:01 INFO  api[991]   server listening
09:25:02 ERROR api        connect() failed  host=web01  status=500
         DEBUG api        cache miss
09:25:04       kernel     audit: type=1400 apparmor=DENIED pid=991  host=web01
```

The false-positive corpus was then checked against real logs rather than
written ones: **122,269 lines** across every readable file in `/var/log`, and
**20,000 syslog lines** out of the journal. Nothing was relabelled — the journal
stayed 20,000 `syslog` — and of the 86 syslog lines on this machine whose
message contains a `=`, none is *itself* valid logfmt, so the four guards are
declining on structure rather than on luck. The completeness test was broken
both ways: a name in `FORMAT_NAMES` with no parser behind it fails five tests,
a `FORMAT_LABELS` entry with no format behind it fails one.

**As shipped.** Four decisions worth recording, two of them departures from this
phase's own text.

*`svc` joined the source keys.* This phase names five — `service`, `logger`,
`component`, `app`, `subsystem` — and its own worked example is
`... msg="boom" svc=api`, which would have rendered with an empty source cell
against them. `svc` is the commonest spelling in the Go and Rust ecosystems the
format comes from, so the list would have been wrong on the first line anyone
pointed at it. `service` still sorts first, so a writer who spells out both is
unaffected.

*`tests/test_format_registration.py` needed a second edit, not "one fixture
line".* `test_a_profile_only_names_fields_its_format_recovers` checks a profile
against what its fixture line was *seen* to produce, and an eighteen-key
allowlist cannot ride on one realistic line. `json` already had exactly this
problem and already solves it with a `known_json` seed; `logfmt` takes the same
treatment for the same reason. The other five tests in the file passed on the
fixture line alone, which is the part the phase got right — adding the format to
the parser and nowhere else failed five of them before either edit landed.

*The anchor guard is checked against the **stored** keys, not every pair on the
line.* Storage stops at `_MAX_FIELDS = 64` the way `_walk_json` does, while the
scan runs to the end of the line so guard 2 is not weakened by the cap. The
consequence is that a line whose `msg=` is its 65th distinct pair is refused
rather than claimed with no message. Bounded was preferred to complete: the
alternative builds an unbounded intermediate dict for a pathological line, which
is the thing `_MAX_FIELDS` exists to prevent.

*The quoted and bare values need **separate groups**, and one alternation is a
bug.* The obvious pattern is `"(?:\\.|[^"\\])*"|\S*`, and then the caller asks
whether the value starts with a quote. It does not work: an *unterminated* quote
also starts with one, so `msg="unterminated level=info` was claimed with its
first and last characters stripped — `msg` came back as `unterminate`. Caught by
an assertion that passed against the prototype and failed against the branch.
"Was this properly closed" has to be something the pattern reports, so the two
forms get their own groups and a bare value containing a `"` refuses the line.
`tests/test_logfmt.py` pins the nastier shape too: a bare token that both starts
and ends with a quote, `msg="ends with an escape\"`.

**Also swept here.** `_JSON_CONSUMED` is a hand-copy of the parser's three key
tuples, and two profiles now depend on it: it decides which chips `json` and
`logfmt` withhold, while the same three tuples decide which keys the parser
spends and which anchor a logfmt line. Nothing noticed if they drifted, so
`tests/test_logfmt.py` asserts they agree. The suite counts in
[AGENTS.md:496](AGENTS.md#L496) and [README.md:1363](README.md#L1363) are
refreshed to 1943.

**Commit.** `feat(parsing): logfmt as a sixth built-in format`

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
without corrupting them. Suite green on 3.11 and 3.14: 2042 passed on both.

**As shipped.** Eight decisions worth recording, one of them a correction to
this phase's own text.

*There are two absences, not one, and this file said one.* The text above says a
view whose `requires` names "a missing **or disabled** plugin" is marked
unusable. Shipped as two states, because collapsing them punishes the wrong
person: an operator who switches a plugin off in the `P` dialog for a minute
would find every saved view that uses it marked broken, and switching it back on
is the fix for a problem they did not have. So `unsatisfied()` asks only whether
a plugin is **installed**. A plugin that is installed and merely out of service
keeps its token registered and its terms raise `QueryError` naming it — which is
not a softer version of the same report but the *stronger* one, because it is
the case where the grammar still exists to complain with. Uninstalling is the
case where nothing is left to notice, which is exactly why the record has to
live on the saved thing.

*A token stays reserved while its plugin is switched off.* The tempting
implementation installs only the enabled plugins, and it is wrong in the one way
this phase exists to prevent: dropping `~` from the alternation makes `svc~web`
stop parsing as a term and fall through to `compile_query`, so switching a
plugin off would silently convert every query using it into a regex over the raw
line. `QueryStack` therefore builds a spec for every *loaded* plugin and sets the
callable to `None` for the ones out of service.

*The alternation with nothing installed is byte-identical to the one this module
shipped with.* Not by accident and not by preserving a literal: the tokens are
sorted by length *alone* and Python's sort is stable, so the built-ins keep their
declared order. Two tokens of equal length cannot be a prefix of one another, so
their relative order is free — which is what makes the stable sort correct and
not merely convenient. `tests/test_plugin_query.py` pins the pattern string.

*`_HAS_OPERATOR` is one character per token, and the last one.* The cheap
pre-test that keeps a plain regex out of the tokeniser had to be derived too. Any
single character of a token is a sound representative — the token cannot be
present unless all of its characters are — and taking the last reproduces
`[:=<>]` for the built-in set exactly, rather than adding `!` and sending
`don't` through the tokeniser for nothing.

*`FieldTerm` gained `compare()` rather than a bound predicate.* Resolving the
operator at parse time and hanging the callable on the term would save a dict
lookup per comparison and would break the type: `FieldTerm` is frozen and
slotted and compares by value, so two identical terms carrying two closures
would be unequal, and every existing test that compares a literal `FieldTerm`
would have had to change. The lookup is on a dict that is empty unless a query
plugin is installed.

*A third `PluginBudget`, sharing the render ceiling and settling separately.*
Query plugins are the same kind of work on the same trigger as `FilterStage` —
a keystroke, over the whole buffer — so they take the same `plugin_time_budget_ms`.
They get their own instance because a pass is `start()`/`settle()` and
`apply_filters` opens and closes its own: a stage and an operator sharing one
pass would have their strikes interleaved by whichever was measured first.

*A computed field's name collides casefolded, and a token's does not.*
`query._lookup` already matched a field key case-insensitively, so `Age` and
`age` are one field to any query that could ask for either; compared exactly,
the second plugin to claim one would load and then silently replace the first in
the installed registry — a plugin doing nothing, with no diagnosis, from the
outside having loaded fine. Tokens need no such rule, because a letter is a key
character and a token may not contain one.

*The `Query` section became three.* This file asked for one section in
`clv/plugins/AGENTS.md`. The interfaces went into the numbered list where every
other interface lives, because that is where an author looks for a signature;
the `requires` rule went to a section of its own, because it belongs to neither
interface alone and Phases 9 through 11 inherit it rather than restating it.

**Also swept here.** [AGENTS.md](AGENTS.md)'s plugin table still said "Three
interfaces" and listed the original three — it went stale when Phase 7a added
`LogFormat` and nobody noticed, which is the failure a table maintained by hand
beside a `_KINDS` tuple will keep having. Now six, with a pointer at `clv/api.py`
as the thing a plugin actually imports. `tests/conftest.py` gained an autouse
fixture resetting the installed grammar between tests — module state shared
across a suite fails *quietly*, and a leaked `~` changes what `_TERM_RE`
matches, so the compatibility test pinning "a plain regex is passed through
untouched" would pass or fail on the order the files happened to run in. `tests/test_state_schema.py` gained a
field-by-field round trip for `WatchRule`, which is a *third* hand-written copy
of the "one bad record must not cost the list" convention and inherited nothing
from the two that are annotation-driven — so a field added to it tomorrow now
fails there rather than on someone's disk. `tests/test_config.py`'s seeding
check is driven off `SEEDED_EXAMPLES` rather than naming one file, so the second
worked example could not be added without also being written up in the plugin
directory's `README.txt`.

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
green on 3.11 and 3.14: 2085 passed on both.

**As shipped.** Eight decisions worth recording, two of them corrections to this
phase's own text.

*A missing matcher is named by its **kind**, not by its plugin.* This file said
a rule whose `kind` names a missing matcher is "listed with the plugin it
needs", and that is the wrong record to keep. The kind is the contract and a
plugin is one implementation of it: a rule written on a machine carrying one
`burst` matcher and opened on a machine carrying a different one is a rule that
still runs, and a recorded plugin name would have marked it broken. So `kind`
*is* the dependency record — `⚠ needs the 'burst' rule kind, which no installed
plugin provides` — which is also the more actionable sentence, and which keeps
`requires` meaning exactly what Phase 8 defined it to mean rather than acquiring
a second sense.

*"Absence" is not expressible against this seam, and the example list was wrong
to say it was.* `matches(entry, rule)` is offered lines; a rule that must fire
because *nothing* arrived is never called. A threshold or burst kind works —
the matcher holds its own window state — and a silence rule needs a clock this
seam does not hand out. Adding a `poll(now, rule)` tick was considered and
declined for this phase: it is a second guarded call path, on a different
trigger, with its own budget and its own disable route, for a kind nobody has
asked for yet. `clv/plugins/AGENTS.md` says so in the interface's own section,
where an author meets it before writing the plugin rather than after.

*`WatchIndex` suspends its answer **reuse** when a plugin kind is in the rule
set.* Found by the first test written against the seam, and it would have made
the flagship example silently not work. The index caches by line *content*,
which is sound for a pattern — a pure function of the text — and wrong for a
matcher that is counting: five identical `oom-killer` lines are one cache key,
so a `x5/60` rule saw one of them and never fired, with nothing to diagnose
because from the outside the rule simply did nothing. Answers are still
*stored*, so `hits()` stays the lookup that keeps re-rendering free; what is
skipped is reading one back, and only for a rule set that actually contains a
plugin kind. Every rule set that existed before this phase takes neither the
flag nor the extra call, and a test pins that.

*A matcher may return anything truthy, and is not faulted for it.* The plan for
this phase said a non-`bool` return should disable the plugin, by analogy with
`ComputedField.value`. The analogy does not hold. A computed field's wrong type
is a silent never-matches, because the value is compared against a string; a
truthy object here is exactly what an author writing
`return self._pattern.search(entry.raw)` meant, and refusing it would be
pedantry with a plugin taken out of service at the end of it. `bool(result)`,
and the house convention is Python's.

*A fourth `PluginBudget`, on the read ceiling — and sinks are on none of them.*
A matcher pass is one poll's batch of newly arrived lines, which is the read
path's unit and not the render's, so it takes `plugin_read_budget_ms` and its
own instance. A `WatchSink` is deliberately not budgeted at all: it runs on a
thread of its own, so "slow" costs the pane nothing and there is no pass to
count three of. What can go wrong is a call that never comes back, and a
stopwatch around a call that has already returned cannot see that.
`plugin_sink_timeout_ms` is a **deadline** — one call, one limit — and it
retires the sink rather than striking it.

*The toast is a sink, and the lane is a field rather than a branch.*
`SinkSpec.inline` marks CLV's own destination, which must run on the event loop
because it paints; everything else is dispatched to a thread. `_poll_watch`
therefore has one fan-out and no special case, which is what "the built-in path
becomes the default sink" has to mean if it is to mean anything. The built-in
destination is also given exactly what a plugin is given — a name and a count —
and rebuilds its sentence from them, rather than being the one caller that gets
more.

*`due_hits` is the drain and `due` is its projection.* A sink needs the name and
the count as data; the toast needs the sentence. Both come out of one coalescing,
which is what makes "a sink cannot bypass the rate limiter" a property of the
code rather than a promise. `due()` keeps returning the list of strings it always
returned, because `tests/test_watch_rules.py` compares against those literals and
this phase owed that file the right to be **literally** unmodified — which is
Requirement 10's evidence rather than an assertion about it.

*A sink that is out of service is simply absent; a matcher that is out of
service is not.* The asymmetry is deliberate and is the one place this seam
diverges from Phase 8's shape. A kind has to stay claimed while its plugin is
switched off, because a saved rule *means* something under it and dropping it
would send the rule back to the pattern path — where `oom-killer x5/60` parses
as a query and starts matching the wrong lines. A destination reserves nothing
and means nothing, so an out-of-service sink is left out of the list and nothing
is delivered to it.

**Also swept here.** `WatchRulesDialog._toggle_current` rebuilt a rule by listing
its fields by hand, so pressing `space` on a rule dropped its `requires` — the
Phase 8 record that marks it unusable — and the pattern went straight back to
being matched as a regex, which is the exact failure Requirement 12 exists to
prevent, reachable with one keystroke. It is a `replace()` now, and `kind` would
have been the second field to go the same way. The worked example
`clv/examples/watch_alerts.py` joins the two Phase 7a and 8 seeded, and
`tests/test_config.py`'s seeding check is driven off `SEEDED_EXAMPLES`, so it
could not be added without also being written up in the plugin directory's
`README.txt`. `tests/conftest.py` gained the autouse fixture resetting the
installed matchers and sinks between tests, on the same argument as the query
one: a leaked `burst` makes `matcher_kinds()` report two kinds, and the test
asserting that a build with no watch plugins composes no `Kind` control would
then pass or fail on the order the files ran in.

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
on 3.11 and 3.14: 2137 passed on both.

**As shipped.** Six decisions worth recording, two of them corrections to this
phase's own text.

*The budget is the **render** ceiling, not the read one.* This file said "rules
are per-line and inside the read-path budget", and the second half is wrong
about where clustering happens. Nothing shapes a line on the read path: a
cluster pass is `_write_rows` over the *filtered* set, driven by a keystroke in
the query box, which is the render path's unit exactly. So `plugin_time_budget_ms`,
a fifth `PluginBudget` instance labelled `cluster`, settling on its own so a
slow `FilterStage` and a slow `ShapeContributor` inside one render do not have
their strikes interleaved — the argument `_new_query_budget` already made.

*A `ClusterRule` cannot raise, and the load checks are what replaced that test.*
The testing list above asks for "a rule that raises is disabled". Nothing can:
the interface is **data**, CLV performs the substitution, and there is no
third-party call on the path at all. That is not a gap in the seam but the best
property it has — the one kind called per line contains no plugin code. What
replaced the test is a rejection matrix, because everything that can go wrong is
knowable at load: a pattern that does not compile, one that **matches the empty
string** (it would write its placeholder at every position of every line), a
placeholder carrying a digit, and a placeholder carrying a backslash. That last
one was not in the plan and is the one an author is least equipped to diagnose:
the placeholder is a `re.sub` *replacement template*, so `<\1>` splices in
whatever the pattern captured rather than writing the two characters that were
typed. A `ShapeContributor` keeps the ordinary runtime guard, because it is an
ordinary method.

*Plugin rules are handed the line CLV has already normalised, and the phase's
own example proved it.* The Kubernetes pod suffix this phase was drafted around
cannot be matched by the obvious rule: by the time a plugin rule runs,
`api-7d9f8b6c4-x2n9q` is `api-<hex>-x2n9q`, because the built-in hex rule got
the middle hash first. The worked example's ANSI rule fell into the same hole
one level down — the integer rule had already made `\x1b[31m` into
`\x1b[<int>m`. Both are consequences of "plugin rules run last", which is not
negotiable: appending is the only position that cannot break the built-in
order. So the trap is documented where an author meets it — in the interface,
in `clv/plugins/AGENTS.md`, and in `clv/examples/cluster_rules.py`, whose two
rules are now written against what actually reaches them — and pinned by
`test_a_rule_is_handed_the_line_the_builtins_already_normalised`.

*Only **enabled** plugins are installed, which inverts Phase 8's rule on
purpose.* A `QueryOperator`'s token and a `WatchMatcher`'s kind stay claimed
while their plugin is switched off, because a saved query or rule *means*
something under them and dropping one would silently reinterpret it. Nothing
saved names a cluster rule — `state.clustering` is a bool and a cluster key is
content-derived and session-only — so there is nothing to reserve and nothing
that could change meaning. An out-of-service rule is simply absent and the
shapes go back to what they were, which is also the only reading of the `P`
dialog's switch that is not a lie. Requirement 12 is vacuous for this seam, and
that is a property of it rather than an omission.

*An empty contribution adds nothing, not an empty component.* Found by the test
written for the disabled case: a contributor that is switched off or has raised
returns `""`, and appending `"\0" + ""` produced a shape with a trailing
separator — one no build without the plugin could ever produce, so "clustering
continues on the shape it had before" was false by one byte. Empty
contributions are skipped. The cost is a theoretical ambiguity between two
contributors answering in complementary halves; the benefit is that taking a
contributor out of service is byte-identical to never having installed it,
which is the property the guard exists to provide.

*The shape cache is cleared by `install_cluster_plugins`, not by its caller.*
`app.py` had been clearing it in `_sync_plugin_generation` since Phase 6, in
advance of this phase. A service that owns a cache owns its invalidation: the
clear moved inside the install, where it cannot be forgotten by a test, a
future caller, or the next seam. What the app still owes it is ordering, and
that is all it owes it.

**Also swept here.** `PluginRegistry.order()` never sorted `matchers` or
`sinks`, though both docstrings claimed `plugin_sort_key` order: a matcher is
looked up by kind so it cost nothing, but sinks were delivered to in
`pkgutil.iter_modules` order — the filesystem accident `priority` exists to
remove — for a release. Both join the sort, and the test that pins it is driven
off `_KINDS` rather than off a list someone remembered to name, because a seam
phase adds a list every time. `clv/plugins/AGENTS.md`'s `Reversed` list gains
the clustering non-goal, which had been recorded only in `clustering.py`'s
docstring while the query DSL reversal beside it was recorded in both — leaving
a reader of the contract's own Non-Goals with a rule the code no longer follows.

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

  **Corrected on landing: `shift+←`/`shift+→`, which is a new key.** The
  sentence above asks for two things that turn out to be incompatible. Plain
  arrows already reach every annotation, because a mark lives in a bucket and
  the arrows walk buckets — so "step to annotations as well" can only mean
  *skip* to them, and a key that skips when a plugin is installed and steps when
  one is not is exactly the kind of binding Requirement 11 exists to refuse. The
  arrows keep meaning one bucket; `shift` means the next marked one; both are on
  `TimelineBar.BINDINGS`, which is the half of the instruction that was about
  where the binding lives rather than how many there are.
- **`TimelineMetric`** — what a bucket measures, if not count:

  ```python
  class TimelineMetric(Plugin):
      metric_name: str = ""
      def value(self, entry: LogEntry) -> Optional[float]: ...
  ```

  `Bucket` ([timeline.py:44](clv/services/timeline.py#L44)) gains `value: float`
  beside `count`; `count` never stops meaning entries, so nothing downstream
  that reads it has to change.

  **Corrected on landing: a metric declares a name and a `unit`, not only a
  value.** The phase text says a metric "declares nothing but a per-entry
  value", and that sentence is load-bearing about *aggregation* — there is no
  `aggregate()` and there never will be. It is wrong about *display*: a total is
  a float, and a caption that prints `1449984.0` where it meant `1.4 MB` fails
  the same test the caption rule below sets. `metric_name` is required (a metric
  that cannot name itself is refused at load, because the caption would have
  nothing to say) and `unit` is optional. Neither is reachable from the fold, so
  neither weakens the constraint the interface exists to enforce.
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

  Landed as: once per **window**, which is stronger than once per rebuild and is
  what the sentence was reaching for — a rebuild happens on every keystroke in
  the query box. The cache is a single `(window, triples)` pair in
  `timeline.py`, cleared by `install_timeline_plugins`, which is how the
  generation reaches it without the service learning what a generation is. The
  provider is called **on the event loop** and is told so in the contract: it
  answers from memory, does its I/O in `setup()` or on a thread of its own, and
  is charged against the render ceiling if it does otherwise.
- **One metric at a time.** Two metric plugins both enabled is a conflict the
  operator resolves; CLV picks by `priority` and reports that it did. The loser
  is *loaded and not installed* rather than disabled — a new `conflict` error
  category that supplies a row's detail and leaves its state alone, so a module
  shipping a losing metric beside a working annotation is not reported as
  broken, and switching the winner off promotes the runner-up on the next
  render.
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

  **Corrected on landing: there is no drawer section.** That section cannot
  exist, and the reason is the one Phase 4 already found and
  [clv/widgets/AGENTS.md](clv/widgets/AGENTS.md) now records in the imperative:
  `AdvancedFiltersDrawer` is capped at `max-height: 16`, and a new **row**
  pushes what follows below the fold, where it lays out and paints nothing. A
  plugin-supplied section *is* a row, by definition, and the phase asked for one
  per plugin that wants controls. The drawer has now refused a plugin section
  twice, for the same measurement, and both times the answer was the same one
  the SSH fleet reached ([SSH_TODO.md](SSH_TODO.md) Phase 7): a summary line and
  a button in the drawer, the detail in a modal. So the vocabulary below is the
  whole vocabulary, and the modal is the whole of where it draws. Requirement 11
  is better served by the correction than by the text — the thing a drawer
  section would have bought is room, and the modal is the one place a plugin can
  be given room that no breakpoint test has to know about.
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

Checked by hand against a real `CLV_PLUGIN_PATH` root carrying six commands —
one keyed, one asking for `t`, one asking for the footer, one opening a panel,
and the two from `clv/examples/commands.py`. The footer at 80 columns came back
byte-identical to a build with no plugins, before and after; `t` still cycled
the time window; the three that were granted keys were in the `?` overlay under
**Plugins** and all six were in `C`; the panel opened inside 80x24, round-tripped
a switch, an input and a select, refused to write with an empty path, and wrote
twelve raw lines when given one; and switching every command off took the keys
and the overlay rows away and left the footer where it started. There is **no
drawer section** — see the correction above. Suite green on 3.11 and 3.14:
2293 passed, 1 skipped on both.

**As shipped.** Five decisions worth recording, one of them a departure from the
text above and one of them a bug this phase found rather than a choice it made.

*`CommandContext` queues everything, including `notify()`.* The phase asked for a
context carrying "a `notify()`", and for a command that "cannot reach the app
object, which is what stops the vocabulary being a formality". Those two
sentences are in tension and the obvious implementation loses: a `notify`
injected as `self._notify` carries the whole application on `__self__`, and a
lambda carries it on `__closure__`. Either would pass a test asserting the
context has no `app` attribute while leaving a live route to the screen, the
registry and the store for anyone who looked. So all four outward calls append
to `context.requests` and the app drains the queue after `run()` returns and
performs each one itself. The cost is that a long-running command cannot report
progress part-way through; the benefit, unplanned, is that every member of the
context is now encodable, which is exactly what Phase 13 needs from this kind.
`test_the_context_has_no_route_to_the_app` walks bound methods and closure cells
rather than attributes, because the shallower test is the one the rejected
design passes.

*The mediated requests are three, and asking is asking.* `request_query`,
`request_source` and `request_view` — chosen over a read-only context because a
command whose only effects are a toast and a modal cannot express "show me the
errors", which is the first thing anybody would write. Each is validated by the
path that already owns it: a query by the real parser, reported with the
parser's own words; a source against what discovery actually offered, because a
command may move to a source and not conjure one; a view through `_apply_view`,
so Requirement 12's preserve-disable-explain is inherited rather than
reimplemented. A refused request is reported and does **not** disable the
plugin: being wrong about the state of the world is not being broken, and the
view may exist again after the next rescan.

*A command runs on the event loop, and no budget can save it.* The same terms a
plugin `Exporter` has run on since the beginning, and the guard in `_run_command`
is `_export_via_plugin`'s one seam later. It is worth saying out loud rather
than leaving implied: the six render-path budgets work by measuring a pass and
declining to start the next, and CLV cannot interrupt a call it is inside. A
command that hangs hangs CLV. That is the first limitation in this file that the
isolation host is the *only* answer to, which is why Phase 13 lists `Command` as
isolable and why the docs say so in the same paragraph that says a command is
invoked synchronously.

*`on_control` is charged, and it is the seventh budget.* The odd one out: the
other six measure a plugin sweeping a set, and a panel callback sweeps nothing
— it is called once, for one control, because somebody typed a character. It
still needs a ceiling for the same reason the render path does, because a plugin
that spends a second deciding what to redraw has stopped the modal echoing what
is being typed into it. `_forget_budgets` already iterated rather than naming
its members, which is the docstring in that method predicting this phase and
being right: the seventh cost one line.

*A `Select` posts `Changed` as it mounts, and that was firing callbacks nobody
triggered.* Found by a test, not in review. Every panel called the plugin's
`on_control` once per seeded control the moment it opened — before the operator
had seen the screen, and with a plugin that answers a callback with a redraw it
would have looped. `prevent()` is the house pattern for this and does not reach
it: the message arrives from a widget's own mount, after any block in the
drawing method has ended. Comparing the reported value against the stored form
does reach it, and is truer anyway — `on_control` means a control changed, and
being told a switch is still off is not that.

**Also swept here.** `clv/plugins/AGENTS.md`'s *What is published* table was
missing `ClusterRule`, `ShapeContributor`, `TimelineAnnotation` and
`TimelineMetric` — published by Phases 10 and 11, listed everywhere else, and
absent here for two phases with nothing to notice. The table is now compared
against `clv.api.__all__` by `tests/test_plugin_docs.py` rather than restating
it, so the next one cannot go missing the same way. `tests/test_plugin_docs.py`
also stopped pinning the budget count in two places: one test owns the running
total and changes when a budget arrives, rather than every budget's test
changing for every other budget's arrival.

**Commit.** `feat(plugins): commands, bindings and modal panels`

---

# Stage E — Trust and distribution

## Phase 13 — Isolation

The first time in CLV's history that a plugin can be *stopped*. It is a real
improvement and it is not a sandbox, and this phase is written so that the
second half of that sentence survives contact with the first.

**Expected outcomes**

- **`isolated = True`** on a plugin class asks for a subprocess host.

  **Added on landing: `isolated = true` in `[plugin:<name>]` asks for one too**,
  and it buys strictly more. A class attribute has to be *read* to be honoured,
  and reading it means importing the module in CLV's process — where module-level
  code has already run before any interface check, which is the first sentence of
  the trust model. The settings key is answerable before the import, from a
  section the loader already has in hand before it touches a root
  (`refresh_settings` is seeded first, and `pkgutil.iter_modules` yields names
  without importing them). A plugin isolated that way is never imported here at
  all, so an import that raises, hangs or spawns something is contained like any
  other call. The two doors meet at one `manifest_for()` and one `load` message;
  what differs is only who is asking and when CLV can hear it.
- **Kind gating, enforced at load with a stated reason.** Allowed:
  `Exporter`, `LogSourceProvider`, `WatchSink`, `Command`, `TimelineAnnotation`
  — all coarse-grained, all called on demand or once per rebuild. Refused:
  `FilterStage`, `LogFormat`, `QueryOperator`, `ComputedField`, `WatchMatcher`,
  `ClusterRule`, `ShapeContributor`, `TimelineMetric` — all per entry or per
  line. The refusal names the kind and says why, so an author reads a reason
  rather than discovering an omission.

  **Corrected on landing: four kinds, not five, and the two lists above this
  one did not agree.** The decisions table said `Exporter`,
  `LogSourceProvider`, `WatchSink`, `Command`; this said those plus
  `TimelineAnnotation`, and neither refusal list mentioned `WatchMatcher`. Both
  are now one list: **`Exporter`, `WatchSink`, `Command`,
  `TimelineAnnotation`** — every kind CLV calls and waits for an answer from.

  `LogSourceProvider` is refused, and it is the correction with an argument
  behind it rather than a bookkeeping error. A provider does not answer a
  question; it hands back a **live reader** whose `poll()` runs on the tail
  timer at `refresh_hz`, on the event loop. Isolating that is not this host with
  a different payload — it is a second protocol in which the child *pushes*
  framed lines and the parent drains them non-blocking, the way
  `JournalReader._drain` already does, plus rotation and `RELOAD_NOTICE`
  semantics across the boundary. That is a phase, not a bullet, and shipping the
  bullet would have meant either a round trip per tick on the event loop — the
  freeze this whole phase exists to prevent — or a provider quietly running
  in-process after asking not to. It is refused by name, with that reason.
- **The host.** One subprocess per isolated plugin, started lazily on first use
  and stopped at `teardown()`. `multiprocessing` with the **spawn** start
  method — not fork, which in a running Textual app with open readers and a
  terminal in raw mode is a footgun. Spawn requires picklable arguments, which
  is precisely why Phase 2 published a wire form: `LogEntry` cannot be pickled
  at all (verified), so nothing crosses without `entry_to_wire`.

  **One per origin, not per plugin.** A module's plugins share the module's
  imported state, so a host each would import it once per plugin and hand two
  halves of one plugin two sets of globals. It also means one connection with
  two possible callers — a `WatchSink` on `SinkDispatcher`'s thread and a
  `Command` on the event loop, from one module — which is a lock rather than a
  design change, and interleaved requests on one pipe would have been a
  corrupted stream rather than a slow one.
- **Frozen builds work, and this is the requirement most likely to be missed.**
  `multiprocessing` under PyInstaller needs `freeze_support()` as the first
  thing in `clv/__main__.py`, or a spawned child re-runs the app and the binary
  forks itself repeatedly. Tested against a real frozen build, not only against
  a source checkout — `release.yml` grows a second smoke step that enables an
  isolated plugin in the built binary and fails if it was imported anything
  other than exactly once, in a process that is not the viewer's. See *As
  shipped* for which `freeze_support` it has to be.
- **The child does not inherit the bundle's library path.** PyInstaller puts
  `_internal` on `LD_LIBRARY_PATH` and a child that then execs a system binary
  loads the bundle's libcrypto instead of the system's —
  `journald.child_environment()`
  ([journald.py:77](clv/plugins/sources/journald.py#L77)) already solves this
  and the host uses it rather than a second implementation.
- **A host that crashes, hangs or exits is reported and its plugin disabled**
  through Phase 1's `disable()` and Phase 4's row. A call exceeding the budget
  kills the host. This is the capability that does not exist today at any price.

  **It is a deadline, not an eighth budget.** The seven `PluginBudget`s measure
  a pass that finished and refuse to start the next; there is no pass to measure
  here and nothing to give three strikes to. `plugin_host_timeout_ms` (5 s,
  clamped like its neighbours, 0 waits forever) bounds one call, including the
  handshake that starts the host, and a breach kills rather than counts. So
  `_forget_budgets` and `test_plugin_perf.py`'s `== 7` are untouched, which is
  the right answer rather than a convenient one.
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

  **Corrected on landing: four places, and the second one is not the drawer.**
  Phase 4 moved the per-plugin rows out of the drawer and into the `P` dialog —
  the drawer keeps one summary line and a button — so the isolated-state help
  text lives in `clv/widgets/plugins_dialog.py`. The fourth is the shipped
  `settings.conf`, beside `plugin_host_timeout_ms`: an operator raising the
  ceiling after CLV killed something is the reader most likely to want
  containment to mean more than it does, and that is the moment to say it does
  not.
- **Isolation is visible.** The `isolated` state Phase 4 reserved is filled in,
  so an operator can see which plugins are contained and which are not. A row
  says `isolated` only when **every** plugin that origin loaded is; a module
  running one plugin here and one in a child reads `loaded` and says how many
  are contained, because a row claiming containment for a module that is half
  in-process is the one lie this surface cannot afford.
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

  **Corrected on landing: the UI is not responsive throughout, and could not
  be.** A `Command` and an `Exporter` are called *from* the event loop and CLV
  waits for the answer, so a hanging isolated command parks the pane for the
  length of the deadline — 5 s by default — and then returns. What the host
  changes is that it returns at all; in-process it never did. The one kind
  genuinely unaffected is `WatchSink`, which was already on a thread. The test
  asserts what is true: the app comes back, is usable afterwards, and the child
  is gone. Claiming responsiveness would have been the phase's own wording
  surviving contact with nothing.
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

Checked by hand against a real `CLV_PLUGIN_PATH` root carrying two modules — one
declaring `isolated = True` on an exporter and a keyed command, one an ordinary
command the operator isolated from `settings.conf`. Both rows in `P` read
**isolated** and carried the sentence about still running as you; `j` opened the
plugin's panel inside 80x24 and a switch round-tripped through the child and
dismissed it; the exporter wrote its file through the app's own export path and
reported a pid that was not the viewer's; and the forced command ran by name.
The import marks are the phase in one file: `forced` was imported **only** by
the child, and `shipper` by both the viewer and its child — which is exactly the
documented difference between the two doors, observed rather than asserted. No
child outlived the run.

Suite green on 3.11 and 3.14: 2338 passed, 1 skipped on both. Each full run also
turned up one failure in `tests/test_plugin_perf.py` — a *different* test each
time, both of them wall-clock ratio checks over the read path this phase does
not touch, both reporting a 10x blowup between two timings of the same work on a
box under a load average of 12, and both passing alone on both interpreters.
That is the family of flake `tests/test_clustering.py` warns about in its own
preamble; it is recorded here rather than quietly re-run, because "it passed the
second time" is the sentence that hides a real regression.

**As shipped.** Six decisions worth recording beyond the corrections above, and
four of them are things this phase *found* rather than chose. Three of those four
turned up only because the tests drive real children — a fake host would have
asserted that CLV sends the right message and proved nothing about any of them —
and the fourth turned up by reading 3.11's standard library rather than by
running anything at all.

*The registry holds a stand-in, and no call site knows.* Every seam in CLV
resolves a plugin by `isinstance` and reads its declarative attributes directly
— `_KINDS` files by interface, the stacks guard by interface, the export dialog
reads `wants_path`, the binding installer reads `key`. So an isolated plugin is
represented by a proxy composed from the interfaces it actually implements
(`_proxy_class`, cached per kind set), carrying the manifest's attributes on
itself and forwarding only the calls. The result is that `_export_via_plugin`,
`_run_command`, `_panel_control`, `WatchStack._guard_deliver` and
`TimelineStack._guard_fetch` are **unchanged** — `app.py`'s whole diff for this
phase is two lines, both of them the operator's ceiling reaching the registry.

*A raise and a kill are different facts, and the proxy keeps them apart.* A
plugin that raises in the child comes back as an ordinary exception and reaches
the guard that call site already has, which disables it or reports it exactly as
it does in-process; the host stays up, because nothing about it failed. A
timeout or a dead child is different in kind — there is nothing left to call —
so the proxy disables itself through the same `disable()` a raising stage goes
through and re-raises, and `enable()` revives the host so **Re-enable** starts a
fresh one rather than handing back the corpse of the last attempt.

*`freeze_support()` is a no-op on the floor it was added for.* The phase text
says to call it first in `clv/__main__.py`, and doing exactly that would have
shipped a frozen build where isolation did not work: on **3.11**, which is what
`release.yml` builds the binaries with, `multiprocessing.freeze_support` is
gated on `sys.platform == 'win32'` and does nothing on Linux. 3.14 widened the
gate to any frozen build, so the local suite, the CI matrix and every test in
this file agree it is fine — the divergence is only visible in a bundle, which
is the one thing none of them has. CLV calls
`multiprocessing.spawn.freeze_support()`, the implementation underneath both,
which detects a spawned child from argv on every platform and every version; the
call is guarded by `sys.frozen`, so a source run does not pay 33 ms to import
`multiprocessing` for a handshake that cannot apply to it. Requirement 8 caught
this, and it caught it by reading 3.11's source rather than by running anything.

*Spawn re-runs the parent's `__main__`, and that is wrong in all three places
CLV runs.* The default bootstrap re-imports whatever started the process so a
target defined there can be found; CLV's target is `clv.plugins.host._child_main`
and needs none of it. Under `python -m clv` it re-imported the whole
application into a process that exists to run one exporter; under the test suite
it re-imported pytest per host; and in anything embedding CLV without an
`if __name__` guard it raised the classic bootstrapping `RuntimeError` — from
CLV's subprocess, about the operator's file. `Process.start()` is now bracketed
by a `__main__` with no spec and no file, which is how `multiprocessing` already
handles an interactive interpreter. A host starts in ~0.2 s.

*Textual replaces `sys.stderr`, and `multiprocessing` wants a real descriptor.*
`resource_tracker` passes `sys.stderr.fileno()` to the tracker process it starts
on the first spawn, and inside a running app that call does not answer with a
descriptor: the very first isolated call in a live viewer died with
`bad value(s) in fds_to_keep`, a message about neither plugins nor isolation,
while the identical call from a script worked. The start is bracketed with
`os.devnull` — **not** `sys.__stderr__`, which is the terminal the viewer is
drawing on and which the tracker writes to uninvited on exit.

*The child inherits the terminal, so it is silenced at the descriptor.* A
plugin's `print()`, a library's warning, or an unraisable exception on the way
out would otherwise land on the pane, and what an operator sees is not stray
output but CLV appearing to corrupt its own screen. `dup2` onto fds 1 and 2
rather than rebinding `sys.stdout`, because a C library writing to the
descriptor directly is precisely the case worth covering. Documented as a cost:
an isolated plugin talks through `notify()`.

**Also swept here.** `clv/plugins/host.py` sits beside the drop-in folders, and
the flat walk in `_load_local` imports every module there — so CLV's own
machinery was loaded as a plugin, found to export nothing, and reported to the
operator as "defines no plugin" against an origin they could do nothing about.
`_LOADER_MODULES` names what is CLV's rather than a drop-in; Phase 15's
`manifest.py` would have hit it next.

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
broken plugin without a terminal. Suite green on 3.11 and 3.14: 2372 passed,
1 skipped, 11 deselected on both.

Checked by hand against a real `CLV_PLUGIN_PATH` root carrying four modules — a
working format, one left unnamed, one raising on import, one requiring CLV 99 —
plus a name in `settings.conf` matching nothing on disk. All five rows read
correctly beside the bundled journal provider, and `clv doctor` exited 0 with two
of them broken. `clv plugin list` over the same root reported the same set, and
the usage errors were each run: a bare path, an unknown subcommand, `clv plugin`
with no subcommand, and an unknown one under it. The new `release.yml`
step was run end to end against a source checkout with the broken plugin in the
**real** user plugin directory rather than on `CLV_PLUGIN_PATH`, which is the path
an operator takes and the one the workflow uses; every grep in it holds.

The isolated row, the sentinel that proves `clv plugin list` imports nothing, and
the assertion that no host outlives `doctor` are covered by
`tests/test_cli.py` rather than by hand — said plainly because the rest of this
paragraph is a by-hand claim and the two should not be read as one.

One thing the by-hand check turned up that no test would have: **an existing
install keeps working without being reinstalled.** The console script in a
`pip install -e .` environment has `clv.app:run` baked into it, and `clv doctor`
through that script works — the shim is not only for the test suite's import, it
is what an operator with CLV already installed gets the new subcommands through.

**Corrected on landing: CLV already had an argv layer, and this phase's premise
was stale.** The text above says there is "**no argv handling anywhere**" and "no `argparse` import in the codebase", citing `app.py:3678-3679`.
That stopped being true when [SSH_TODO.md](SSH_TODO.md)'s config-upgrade work
landed: `clv/app.py` carried an `argparse.ArgumentParser(prog="clv")` with
`--version`, `--print-default-config` and `--upgrade-config`, and the exit codes
this phase "defines" — 0, 1 on a failed upgrade, 2 from argparse — were already
exactly those. `install.sh` forwarded argv and `release.yml` already ran
`clv --upgrade-config` against the built binary.

So the risky part was never adding argv. It was *extending* a parser three flags
and two test files already depended on, and the compatibility surface was four
entry paths rather than one: two console scripts, `python -m clv`, the frozen
bundle, and `clv.app.main` called directly from `tests/test_config_upgrade.py`.
Recorded rather than quietly fixed, because a phase that had been read as
greenfield would have been implemented as a second parser beside the first.

**As shipped.** Six decisions worth recording, two of them things this phase
found rather than chose.

*The layer moved to `clv/cli.py`, and `clv.app` keeps two shims.* `clv doctor`
has to run over a pipe, in a CI step and in a bug report, and anything reachable
from `app.py` brings Textual with it — so `clv/cli.py` imports the app inside the
branch that launches it and nowhere else, `clv/__main__.py` and both console
scripts point at `clv.cli:main`, and `clv.app.main` / `clv.app.run` stay as
one-line shims. A test asserts out of process that a full `clv doctor` run leaves
`textual`, `rich`, `clv.app` and `clv.widgets` unimported — in process it would
pass on any session where something else had already imported a widget, which is
every session.

*`clv doctor` reports `PluginRegistry.status()` and derives nothing.* That method
already said in its own docstring that it lives in the registry "so it can be
asserted without a screen", and this is the caller that took it up on the offer:
the rows, the five states, the details and the sort order are all its, so the
report and the `P` dialog cannot reach two conclusions about one plugin. What the
CLI adds is the frame around it — version, build kind and interpreter, the
settings file, the `ConfigIssue`s beside the plugin errors for the reason `app.py`
gives them one colour, and the search roots each with what was found in it. The
one count it adds to the drawer's summary is `isolated`, which that line omits: a
subprocess must not be invisible to the command meant to find it.

*It does not call `start()`, and says so.* A diagnostic must not run `setup()` and
begin acquiring sockets and connections on the operator's behalf — the loader
declines to for the same reason. The cost is stated in the output's own
documentation rather than left to be discovered: a plugin that fails in `setup()`
is reported here as loaded, and that is the one thing this report cannot tell you.

*`stop_hosts()` exists because `shutdown()`'s premise is false here.* A plugin
the operator isolated in `settings.conf` starts its host during the **load** —
`PluginHost`'s "that door pays for its handshake at load" — so a command that
only reads the registry would leave a live child and a `clv-plugin-*` temp
directory behind on every run. `shutdown()` is the wrong tool: it runs
`teardown()` on every plugin that was *set up*, and under `doctor` none were.
The new method stops the processes and runs no hook, and it is a separate method
rather than a flag so the viewer's exit path cannot take the branch that skips
every `teardown()`. Pinned by a test that counts what is left in `/tmp`.

*`clv plugin list` imports nothing, and that is a property of the code rather
than of the command.* The enumeration was inside `_load_user_roots`, tangled with
the imports it gated. It is now `discover_user_plugins`, returning a
`UserPluginScan`, and `_load_user_roots` consumes it — one walk, so the listing an
operator reads and the listing CLV imports from cannot disagree, asserted by a
test that compares them. Phase 15's rule that no `clv plugin` command may import
plugin code therefore arrives already satisfied, with the sentinel test already
written, rather than being retrofitted onto five new subcommands at once.

*A plugin cannot add a subcommand, refused twice over.* `SUBCOMMANDS` is a closed
literal and a test asserts the parser's choices are exactly it, so a later change
that fed it a registry list fails there however reasonable it looked. And
`PluginRegistry.add` reports a plugin declaring `subcommand`, `subcommands` or
`cli_command`, naming the attribute and pointing at `Command` — because without
it such a plugin loads, works, and simply never gets a subcommand, which is the
failure an author cannot diagnose from outside CLV's source. The attribute is
refused; the plugin is not, and keeps every interface it implements.

**Found rather than chosen.**

*The bare-path check rests on every top-level option being a flag, so that is
pinned rather than assumed.* `clv /var/log/syslog` has to be declined by name
rather than as an invalid choice, which means finding the first token that is not
an option — and that is only safe while no option consumes a value. Verified that
it is not merely theoretical: with a hypothetical `--config` added,
`clv --config /etc/clv.conf` is reported as an attempt to open a log. A test
asserts every top-level action takes zero arguments, so adding such an option
fails there instead.

*The frozen-build ordering test had to change, and it string-matched the import.*
`tests/test_plugin_isolation.py` asserts against the source of `clv/__main__.py`
that `freeze_support()` precedes CLV's import, because no test can build a
bundle. It did so with `next(… if "from clv.app import run" in line)`, so
repointing that import raised `StopIteration` rather than failing — a guard that
reports a crash instead of a verdict. It now matches whatever of CLV's the file
imports, and asserts there is one, so the next rename cannot pass it by default.
The ordering matters more than it did: `clv.cli` builds a parser, and
`clv --multiprocessing-fork …` is not a command line it knows, so reaching it
before the handshake would turn every isolated call in a bundle into a usage
error.

**Also swept here.** `default_config_text`, `describe_upgrade` and
`upgrade_user_settings` became unused in `clv/app.py` when the flags moved and
were removed. `release.yml` gained a CLI smoke step: the existing one proves the
binary can take a terminal, and this one proves it can answer without one —
a deliberately broken plugin installed in the throwaway `XDG_CONFIG_HOME`, and
`clv doctor` asserted to name it, to name the reason, and to exit 0 anyway. It is
the only place the phase's gate can be checked on the build almost every operator
has.

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

Checked by hand end to end: a plugin packaged with `sha256sum` and
`ssh-keygen -Y sign`, `tar czf`'d under a `name-version/` wrapper, installed
from a `file://` URL, reported **untrusted**, then trusted and re-verified
without reinstalling, tampered with and caught by both `verify` and `doctor`,
and removed with its enable-list entry dropped and its `[plugin:<name>]` section
kept. The malicious archives and the URL guards are covered by
`tests/test_plugin_registry.py` rather than by hand — said plainly, because the
rest of this paragraph is a by-hand claim and the two should not be read as one.

**As shipped.** Six decisions worth recording.

*Signatures are `ssh-keygen -Y`, and the operator's own key material.* The
stdlib has no asymmetric crypto and Requirement 7 forbids adding a dependency,
which left hand-rolling Ed25519 or reusing something already on the machine.
OpenSSH's signature mode is the second, and it is the same posture the SSH
transport already takes — *CLV uses the setup the operator already has*. A key
they keep in `~/.ssh/allowed_signers` for git works unchanged, and there is no
crypto in this tree to maintain or get wrong. Three consequences were chosen
deliberately: `-n clv-plugin` is mandatory, so a signature made over some other
file for some other purpose cannot replay as a plugin signature; the signer is
resolved by `ssh-keygen -Y find-principals` against the operator's store rather
than declared in the manifest, so a plugin cannot assert who signed it and have
CLV repeat the claim; and `unverifiable` — no `ssh-keygen` installed — is its
own state rather than being folded into `unsigned`, because "we could not check"
and "there was nothing to check" are different facts and an operator acts
differently on each.

*A manifest is compulsory for an archive or a URL, optional for a local path.*
The asymmetry is the whole distinction between the two doors. A local `foo.py`
is the documented `cp` under another name, with the operator looking straight at
it; a tarball arrived from somewhere else and has to say what it is before CLV
unpacks it into an import path. An unmanifested install is recorded as such and
`verify` later says there is nothing to check rather than presenting a hash CLV
computed from the file itself as evidence about it — a digest is worth something
only if it came from somewhere the file did not.

*The record is JSON, in a dot-prefixed directory, and the manifest is kept
beside it.* `.installed/<name>.json` rather than `.toml`, because `tomllib`
parses and does not write: honouring the suffix would mean hand-rolling a
serializer for a file CLV then re-parses, and an apostrophe in an author's name
is enough to make that a correctness bug in the file the integrity check depends
on. The authored manifest is TOML and is read exactly as specified. It is also
kept **byte-for-byte** next to the record, which was not in the plan and turned
out to be the point: a signature covers exact bytes, so paraphrasing them into
JSON would make re-verification impossible, and re-verification is what lets an
install that read `untrusted` in March read `verified` in April because a key was
trusted in between. Trusting keys rather than files is the entire idea, and
without the kept manifest it would not have worked.

*Extraction is manual, and streamed.* `extractfile()` with a chunked copy rather
than `extractall()`, because `tarfile`'s filtering default differs between the
3.11 floor and the 3.14 this was written on, and `data_filter` does not exist
before 3.11.4 at all — doing it by hand is what makes the rules identical on
both, which Requirement 8 needs. And `r|*` rather than `r:*`, which is the less
obvious half: `getmembers()` on a seekable archive decompresses the whole thing
to build its index, so a bomb would already have cost exactly what the cap exists
to prevent before the first member was judged. Streaming means a member is judged
from its header and an over-budget one stops the read. The decompressed-byte cap
is then checked **per chunk** rather than against the header's `size`, because
the header is the archive's own claim about itself.

*`remove` drops the enable-list entry and keeps the config section.* Two
asymmetries, each with a line of output. The name comes out of `plugins`,
because leaving it would report the plugin as named-but-absent on every launch
from then on — accurate, and indistinguishable from a bug to whoever just ran
the command. The `[plugin:<name>]` section stays, because it is what the operator
wrote and a reinstall should not mean setting it up again; `--purge` removes it
for the operator who wants it gone.

*Tamper detection is in `doctor` as well as `verify`.* The phase text framed
`verify` as "the check nobody runs until they need it", which is true and is an
argument for putting it somewhere people already go. `clv doctor` re-hashes and
reports beside the plugin's own row; the `P` dialog does **not**, and gets only
the provenance line, because opening a dialog should not cost work proportional
to what is installed. `manifest.annotate()` is one function with two callers —
the dialog and the report — for the same reason `PluginRegistry.status()` lives
where it does.

**Found rather than chosen.** Four, and the first two are the phase text's own
claims turning out to need more than they said.

*`--sha256` exists because everything else the install checks comes from inside
the archive.* The phase text asks for the payload to be "checksum-verified
before anything is unpacked", and writing it exposed that there was nothing to
verify it *against*: a manifest's checksums prove the files match the manifest,
which anyone who replaced both can arrange, and they live inside the thing being
checked. So `install` takes a digest the operator got out of band — from the
download page, or from whoever sent them the file — and hashes the archive as a
file before a single member is read. It is optional, and it is the only number
in the whole flow that an attacker holding the archive cannot rewrite. The
publishing instructions say to publish one.

*A broken signature is `bad` whether or not its signer is trusted.* Written
first as "find out who signed it, then check it", which put the trust-store
lookup ahead of the signature check — and since CLV ships no keys, the ordinary
state of every installation is trusting nobody, so a tampered manifest fell
straight through to `untrusted`, whose sentence says the signature is valid and
only the signer is unknown. `ssh-keygen -Y check-novalidate` answers "are these
the bytes that were signed" without consulting the store at all, and now runs
first. The distinction is pinned by a test that trusts nobody deliberately.

*`clv/plugins/` is a plugin drop-in directory, and the two new modules were
promptly loaded as plugins.* `_LOADER_MODULES` existed for exactly this and held
one name, `host`. `manifest.py` and `install.py` went in beside it and the flat
walk imported both, found their dataclasses, failed to instantiate them, and
reported two broken bundled plugins to the operator — caught by running
`clv doctor` by hand, which no test would have covered. The tuple now has three
names and a test asserts it covers **every** non-drop-in module in the package,
so the next one cannot repeat it.

*A settings file can carry the same key twice, and the two readers of it
disagree about which one wins.* `configparser`, which `config.py` loads through,
takes the **last**; `SettingsDocument`, which every edit goes through, takes the
first. So `clv plugin remove` edited the first `plugins =` — the empty one the
template ships — reported success, and changed nothing, with the plugin still
enabled. This is not new and is not confined to this phase: `persist_setting` has
the same blind spot wherever an operator appended a key rather than editing the
one they had, which is what the README's own instructions invite. Fixed narrowly
rather than globally: `SettingsDocument.values()` returns every assignment so a
caller can agree with the reader that *loads* the file, and `set`/`remove_option`
take `every=` to rewrite all of them. Both default to the old behaviour, because
an unconditional switch would quietly rewrite a second assignment someone may
have put there deliberately.

**Also swept here.** `clv/app.py`'s `import sys` became unused when Phase 14
moved the config flags to `clv/cli.py` and was removed. The `README.md` plugin
chapter gained the packaged-install, signature, verification and removal
sections beside the manual `cp`, which stays documented and stays supported as
the path that works with no network at all.

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
| **Replacing a built-in format, operator or cluster rule** | Plugins extend; they do not override. A plugin that could shadow syslog parsing or redefine `:` would make every bug report unanswerable without knowing what was installed. The escape hatch is CLV's to pull and not the author's: a format that needs a built-in branch to stand down in its favour gets absorbed as a built-in instead, which is what Phase 7b is and why logfmt is not the reference plugin. | Nothing anticipated. |
| **Plugin-owned persisted session state** | `SessionState.PERSISTED_FIELDS` is closed on purpose — every field carries an argument about whether recording it leaks what someone was reading. A plugin's own file under its own directory has none of that ambiguity. | A case where a plugin's state genuinely belongs in CLV's session rather than beside it. |
| **Hot reload** | A plugin swapped underneath a running viewer is a debugging surface nobody asked for. Enable and disable change what is *active*, which covers the real need. | Nothing anticipated. |
| **Plugin-supplied CLI subcommands** | Requirement 13. An installed file must not change what a shell command does. | Nothing anticipated. |

---

## Summary of new and changed files

| File | Phases | Change |
| --- | --- | --- |
| `clv/plugins/AGENTS.md` | 0–16 | Trust model, contract, and every seam |
| `clv/plugins/__init__.py` | 1,3,5,6,7a,13 | Loader, search roots, ordering, timing, registries, host |
| `clv/api.py` | 2, all seams | **New** — the published surface, extended per phase |
| `clv/plugins/host.py` | 13 | **New** — the subprocess host and the wire protocol |
| `clv/cli.py` | 14,15 | **New** — argv, `doctor`, the six `plugin` subcommands. Absorbed the parser that was in `app.py`, which keeps `main`/`run` as shims |
| `clv/plugins/manifest.py` | 15 | **New** — manifest parsing, checksums, signatures, the install record |
| `clv/plugins/install.py` | 15 | **New** — the URL fetcher and the hostile-archive extractor. Split from the row above on landing: one of them handles input from the internet and the other does not, and the malicious-archive tests should target a module that does nothing else |
| `clv/services/settings_file.py` | 15 | `values()`, and `every=` on `set`/`remove_option` — a duplicated key edited in the wrong place |
| `clv/services/config.py` | 3,5,6,13 | `plugins`, `[plugin:<name>]`, `plugin_time_budget_ms`, `plugin_host_timeout_ms` |
| `clv/services/parsing.py` | 7a,7b | `LogParser(formats=...)`, injected dispatch, `FORMAT_NAMES`, the logfmt matcher and the ISO deferral |
| `clv/services/query.py` | 8 | Operator registry, computed fields, generated `_TERM_RE` |
| `clv/services/filtering.py` | 8 | `FilterSpec.parse` routes through the operator registry |
| `clv/services/watch.py` | 9 | `WatchRule.kind`, matcher dispatch, sink delivery |
| `clv/services/clustering.py` | 10 | Plugin rules, shape contributors, cache generation |
| `clv/services/timeline.py` | 11 | `Bucket.value`, annotations, foldable metrics |
| `clv/services/session.py` | 7a | `Buffer` passes formats to its parser |
| `clv/storage.py` | 8 | `SavedView.requires` |
| `clv/app.py` | 1,2,4,5,6,7a,8,9,11,12,13 | Wiring, cache, dispatch, drawer, host lifecycle |
| `clv/widgets/advanced_drawer.py` | 4,12 | Plugin section; plugin-contributed sections |
| `clv/widgets/plugins_dialog.py` | 4,13 | The per-plugin rows, and the isolated state's own sentence |
| `clv/widgets/columns.py` | 7a,7b | `FormatProfile.consumed`, `install_profiles`, the logfmt profile |
| `clv/widgets/detail_pane.py` | 7a,7b | Plugin format labels; `FORMAT_LABELS` gains logfmt |
| `clv/widgets/help_overlay.py` | 12 | Plugin command section |
| `clv/widgets/timeline.py` | 11 | Annotation rendering and stepping |
| `clv/__main__.py` | 13,14 | `freeze_support()`, before anything of CLV's is imported; argv entry (`clv.cli`, not `clv.app`) |
| `clv/plugins/formats/` | 7a | **New** — drop-in directory and the nginx reference |
| `examples/plugins/` | 16 | **New** — one copyable example per interface |
| `clv/plugins/README.md` | 16 | **New** — author-facing quick start. Covers the **shipped** sources too: journald, and the SSH transport `SSH_TODO.md` Phase 4 added (a backend rather than a provider — see `clv/plugins/AGENTS.md`, which holds that contract until this file exists). |
| `settings.conf`, `README.md` | 3,5,6,7b,8,9,10,11,12,14,15,16 | Keys, the format list, chapter, sweep |
| `tests/test_api_surface.py` | 2 | **New** — the freeze |
| `tests/test_plugin_drawer.py` | 4 | **New** |
| `tests/test_plugin_perf.py` | 6 | **New** |
| `tests/test_plugin_formats.py` | 7a | **New** |
| `tests/test_format_registration.py` | 7a | **New** — the four-table completeness test |
| `tests/test_logfmt.py` | 7b | **New** — the three dialects, the guards, the false-positive corpus |
| `tests/test_plugin_query.py` | 8 | **New** |
| `tests/test_plugin_watch.py` | 9 | **New** |
| `tests/test_plugin_clustering.py` | 10 | **New** |
| `tests/test_plugin_timeline.py` | 11 | **New** |
| `tests/test_plugin_commands.py` | 12 | **New** |
| `tests/test_plugin_isolation.py` | 13 | **New** |
| `tests/test_cli.py` | 14 | **New** |
| `tests/test_plugin_registry.py` | 15 | **New** |
| `tests/test_plugins.py` | 1,2,3,5,6 | Extended throughout |
