# SSH_TODO — Remote sources over SSH

Planned work, in dependency order. Each phase assumes the phases above it have
landed. Every phase ends in a commit, and every phase leaves `main` shippable:
no phase may land a half-wired seam that only the next phase makes safe.

The ordering is deliberate. Phases 1 and 2 produce **no user-visible feature**
and are prerequisites for everything after them; doing them last would mean
reworking the transport, the tree, the merge and the session against a
filesystem assumption that had already spread further.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 — Doctrine | Reverse the non-goal in the internal contracts | ✅ **Complete** (`aba1b94`) |
| 1 — Source identity | `SourceRef`, and the end of bare `Path(entry)` | ✅ **Complete** (`9c546c4`) |
| 2 — Filesystem seam | Injectable backend, off the event loop | ⬜ Not started |
| 3 — Configuration | `[ssh:<name>]` sections, `enable_ssh` | ⬜ Not started |
| 4 — Transport & plugin | First readable remote log | ⬜ Not started |
| 5 — Reachability | A host that goes away says so | ⬜ Not started |
| 6 — Parity | Star, merge, rotation, hierarchy, time, session | ⬜ Not started |
| 7 — Remote UI | Host dialog, cross-host merge, `node` in the chrome | ⬜ Not started |
| 8 — Documentation & release | README, `settings.conf`, help overlay, version | ⬜ Not started |
| 9 — The remote journal | `journalctl` over the same transport | ⬜ Not started |

---

## Goal

Point CLV at folders on a remote machine over SSH and have them behave like
folders on the local one: discovered recursively, listed in the tree under a
folder hierarchy, opened, tailed, filtered, queried, starred, merged with local
logs in a single timestamp-ordered pane, grouped into rotated sets, and
restored on the next launch.

The bar is **functional parity, not a second-class source type**. A remote log
that cannot be starred or merged has not met this goal. This is the whole
reason the work is scoped as core seams plus a plugin, rather than as a
provider plugin alone — `LogSourceProvider` deliberately hands back something
that is *not* a path, and everything keyed on a real file skips it by design
([plugins/\_\_init\_\_.py:59-83](clv/plugins/__init__.py#L59-L83)).

The name is the argument. A viewer called *Centralized* Log Viewer that can
only read the machine it runs on has centralised nothing.

---

## Decisions already taken

Recorded so they are not relitigated per phase.

| Decision | Choice | Why |
| --- | --- | --- |
| Transport | **System `ssh` binary + `ControlMaster`** | Zero new dependencies, so Requirement 6 survives intact. Inherits `~/.ssh/config` wholesale: `ProxyJump`, per-host keys, `known_hosts`, agent forwarding. Multiplexing makes per-file commands cheap. Follows the journald precedent exactly. |
| Authentication | **Agent and key files only** | Non-interactive. No password field exists anywhere in the config, the dialog, or memory. A host needing a passphrase is reported unreachable with an instruction to load the agent. Enforced mechanically by `BatchMode=yes` — see Phase 4. |
| Configuration | **One `[ssh:<name>]` section per host** | Per-host `log_dirs`, per-host glob overrides, and an obvious owner for every remote root. Costs a small `config.py` extension, which today reads `[log_viewer]` only. |
| GUI | **Full host management dialog** | Add, edit, test and remove hosts in-app, written back to `settings.conf`. Better first run than a config-file-only feature; costs a new widget with its own validation and 80-column layout tests. |
| Path type | **Not a `pathlib.Path` subclass** | `pyproject.toml` requires `>=3.11`, and practical `Path` subclassing only arrived in 3.12. A parallel type implementing the surface CLV actually uses is the portable answer. |
| Machine field | **`node`** | `host` is taken and means *what the log says about itself* — syslog, access logs and journald's `_HOSTNAME` all normalise into it ([parsing.py:345](clv/services/parsing.py#L345), [journald.py:150](clv/plugins/sources/journald.py#L150)). `node` means *where CLV read it from*. Short, unambiguous, and it does not change what a single saved query already means. |
| Clock skew | **Always report, correct only on request** | Measured at connect and surfaced in the merged view the way `anchored` already reports timestamp-less lines. Per-host `correct_clock_skew`, default off; when on, the pane says so. Honest by default, correct when asked. |
| Remote privilege | **Refuse and report — no `sudo`** | CLV reads as the configured SSH user and nothing more. No privilege-escalation code exists, so "read-only, no privileged operations" survives the move to remote. An unreadable file is reported with a remote-aware message naming the host and the fix. |

### Why the shell transport is the *better* engineering choice here, not just the cheaper one

An SFTP client (paramiko) returns `SFTPAttributes`, which carries **no inode**.
`SourceReader` detects rotation by comparing `(st_dev, st_ino)`
([reader.py:263-268](clv/services/reader.py#L263-L268)); without an inode that
degrades to a `(size, mtime)` heuristic that misfires on a log rotated within
the same second.

`stat -c '%d %i %s %Y'` over the shell transport returns device, inode, size
and mtime in one round trip. **Rotation detection keeps full fidelity**, which
the dependency-heavy option would have given up. Record this in the module
docstring so it is not "simplified" later. See Requirement 4 for the caveat:
that command is GNU, and the fallback is exactly the degradation described
above.

---

## Requirements (non-negotiable)

These extend `AGENTS.md`, they do not replace it. Where a requirement below
restates an existing one, it is because the network makes it newly easy to
break.

1. **No new runtime dependency.** Python 3.11+, Textual and Rich. Everything
   below is achievable with the standard library plus the system `ssh` client.
2. **Bounded work applies over the network.** Requirement 3 of `AGENTS.md` is
   not relaxed by distance. Opening a remote source reads a bounded tail, never
   the file; tailing transfers only appended bytes. A remote read must never be
   implemented as `cat`.
3. **No remote IO on the event loop — ever.** This is the requirement most
   easily broken by writing the obvious code, so it is stated before the
   feature exists rather than discovered as a freeze. See the next section.
4. **Round trips are a bounded resource too.** Discovery of a remote root is
   **one** command, not one per file. A per-file round trip is the failure mode
   that makes this feature unusable at 400 files, and it is the specific thing
   that makes an `sshfs` mount slow.
5. **The remote is not assumed to be GNU/Linux.** `find -printf`, `stat -c` and
   `dd iflag=skip_bytes` are GNU extensions that BusyBox and BSD do not have.
   Capability is probed, never assumed, and the degraded path is documented
   rather than left to fail obscurely. Alpine containers are a first-class
   target, not an edge case.
6. **Time is per-host, not global.** Clock skew and timezone are properties of
   a host and must be captured and carried. A merged view across machines with
   disagreeing clocks must never present a confident ordering it cannot
   justify. See the next section.
7. **Never silently lose a line — or a host.** An unreachable host, a dropped
   connection, and a host with no matching files are three different facts and
   must read as three different messages. A remote pane that goes quiet because
   the link dropped is the single worst outcome of this feature.
8. **Opt-in.** No connection is attempted, and no `ssh` process spawned, until
   `enable_ssh` is true. `clv/plugins/AGENTS.md` already forbids a subprocess
   without consent; a *network* subprocess raises the bar, it does not lower it.
9. **No credentials, ever.** No password field in the config schema, the
   dialog, `SessionState`, or memory. If a connection needs interactive input,
   it fails as unreachable.
10. **Host key verification is never disabled.** No `StrictHostKeyChecking=no`,
    no `UserKnownHostsFile=/dev/null`, not behind a flag, not "for testing". An
    unknown host key is an unreachable host with a message saying so.
11. **No privilege escalation.** CLV never invokes `sudo`, `doas`, `pkexec` or
    an equivalent, locally or remotely. Read-only, as the configured user.
12. **Log content never touches disk.** No caching a remote file locally to
    make seeking cheap. The existing rule, restated because the network is
    exactly the pressure that would justify breaking it.
13. **Local behaviour is byte-for-byte unchanged when `enable_ssh` is false.**
    Phases 1 and 2 refactor the code every local read goes through; the
    existing suite is the proof, and it passes **without its assertions being
    edited**.
14. **The suite never touches a network.** No SSH server, no localhost
    loopback, in the default run. See the testing strategy below for the one
    marked exception.
15. **Services stay UI-free**, 80 columns stays supported, every action keeps a
    keyboard path.
16. **Python 3.11 is a supported target.** Local default is 3.14; a green suite
    there is not evidence. Every phase gate runs both.

---

## The concurrency rule (Requirement 3, in full)

The plan would produce a frozen UI if this were left implicit, because the
natural implementation of every remote operation is a blocking one.

**The two places that block today, and are safe only because they are local:**

- `_poll_tail` ([app.py:1393](clv/app.py#L1393)) is a synchronous callback on a
  `set_interval` timer running at `refresh_hz` — default 2 Hz
  ([app.py:1374](clv/app.py#L1374)). It calls `session.poll()`, which calls
  every reader's `poll()`. Locally that is a `stat` plus a bounded read:
  microseconds. Backed by a remote `stat -c` round trip it is a network RTT on
  the event loop, twice a second, **per merged source**. Five remote logs on a
  60 ms link is 600 ms of frozen UI per second.
- `_select_source` ([app.py:1083](clv/app.py#L1083)) is not `async`. It calls
  `open_single` → `prime()` → the whole bounded read, blocking, before the pane
  can repaint. Every remote open would stall the UI for a round trip.

Discovery is already threaded ([app.py:874](clv/app.py#L874)), which is why
this does not show up in the walk. The *walk* is safe and the *read* is not.

**The rule, and the two mechanisms — both already precedented in this codebase:**

- **`poll()` must never perform a round trip.** A remote source is followed by
  a persistent `tail -F` whose stdout is drained non-blocking on the poll that
  already runs. This is exactly `JournalReader._drain`
  ([journald.py:331](clv/plugins/sources/journald.py#L331)), including the
  `os.set_blocking(fd, False)` and the partial-line remainder. A poll that
  finds nothing costs nothing.
- **Everything one-shot runs in a worker thread.** `prime()`, connection
  probes, capability probes and skew measurement go through
  `run_worker(thread=True)`, as discovery already does. `_select_source` gains
  a pending state and becomes awaitable for remote refs.

**Consequences that must be designed for, not patched in later:**

- The Phase 2 backend protocol needs a declared blocking contract: which
  methods may block, and which are guaranteed cheap. A backend that blocks in a
  method the caller believes is cheap is the bug this requirement exists to
  prevent.
- The pane needs an honest *opening…* state. `_select_rotated_set`
  ([app.py:1103](clv/app.py#L1103)) already sets the precedent — it is "the one
  path in CLV that is not instant" and it says what it did rather than going
  quiet. Remote opens inherit that treatment.
- Source switching while a remote open is in flight must cancel cleanly, not
  race two primes into one session.

---

## The time rule (Requirement 6, in full)

Merging was local-only, so there was one clock and one timezone. Across
machines both assumptions fail, and the failure is silent — the merged pane
presents a confident ordering that is wrong, and the operator reads causation
out of it.

**Skew.** `web01` four seconds ahead of `db02` interleaves the merged stream
wrongly. The error appears before the request that caused it. Nothing on screen
suggests anything is amiss.

**Timezone, which is worse.** Syslog format carries **no offset at all**. A
naive stamp from a UTC host and a naive stamp from an EST host are compared
directly: `_merged` drops offsets whenever the set mixes aware and naive
stamps ([session.py:448](clv/services/session.py#L448)), which is correct for
one machine and five hours of confident misordering across two.

**What is built:**

- At connect, one command captures `date +'%s %z'` alongside the capability
  probe — no extra round trip. Local time is sampled either side of it, and the
  midpoint used, so the measurement is not itself skewed by latency.
- `RemoteHost` carries the measured offset and the UTC offset. Both are
  refreshed on reconnect, because a host that just rebooted may have just
  stepped its clock.
- **Naive timestamps from a remote host are interpreted in that host's
  timezone**, which makes them aware and lets the existing merge order them
  correctly with no change to `sortable_moment`. This is the fix that matters
  most and it costs the merge nothing.
- **Skew is always reported**, in the merged view's status detail beside the
  existing `anchored` count. A set whose hosts agree within a threshold says
  nothing; a set that disagrees says by how much.
- **Correction is opt-in per host** (`correct_clock_skew`, default false). When
  active the pane states that timestamps are adjusted, so a displayed stamp
  that differs from the raw log text is never unexplained. The raw line is
  untouched, as always.

---

## Testing strategy

Spans every phase, so it is stated once.

- **The default suite never touches a network.** Fake runners, exactly as
  `tests/test_journald.py` injects `runner=` and `spawn=`. Every command this
  feature issues is asserted as a string, so a change to an argv is visible in
  review rather than only at runtime.
- **The backend contract suite is the parity mechanism.** Written once in
  Phase 2, parameterised, run against `LocalBackend` and later against
  `RemoteBackend` unchanged. "Parity" becomes a passing test rather than a
  claim, and a remote regression fails the same assertion its local twin
  passes.
- **One marked, opt-in integration suite.** `-m remote_integration`, skipped by
  default, never in the phase gates, run against throwaway containers. It is
  the only thing that will actually catch Requirement 5 — a BusyBox `find` with
  no `-printf`, a BSD `stat` — because a fake runner will happily return
  whatever fixture it was given. At minimum: one glibc/GNU image, one Alpine.
- **Round-trip counting is a test, not a review comment.** Discovery over a
  fixture tree asserts a bounded number of commands. It fails loudly the day
  someone reintroduces a per-file call.
- **Security assertions are tests.** No argv ever contains
  `StrictHostKeyChecking=no`, `UserKnownHostsFile`, `sudo`, or a password. A
  hostile-path fixture table covers quoting.

---

## Phase 0 — Doctrine

Remote sources are currently a documented non-goal in **six** places — this
phase originally named four, and the survey found two more saying the same
thing. Every phase after this one contradicts a shipped document until this
lands, so it goes first.

> **Line references below are as of 2.6.1 (`662df42`), before this phase
> landed.** Kept as written so the diff can be checked against them; they are
> a record, not a map of the current file.

**Scope.** The *internal* contracts only. The README is a promise to users and
is not touched until Phase 8, when the feature actually works — advertising it
earlier is how a doc becomes a lie.

**Expected outcomes**
- [AGENTS.md:346](AGENTS.md#L346) — "Network collection, multi-node
  aggregation, remote tailing" removed from Non-Goals. Replaced with the
  narrowed non-goal that survives: *unattended collection, agents/daemons on
  the remote host, store-and-forward pipelines, and privileged operations
  anywhere.* CLV reads on demand over a connection the operator already has; it
  does not become infrastructure.
- [AGENTS.md:337](AGENTS.md#L337) — "Local only: no network, no telemetry, no
  exfiltration" rewritten. The new statement: *no telemetry and no exfiltration
  remain absolute; network access is limited to hosts the operator names, over
  SSH, using their own credentials, initiated only by an explicit action, and
  never with elevated privilege.*
- `AGENTS.md` Product Requirements — Requirement 1 ("Point it anywhere") gains
  remote roots; Requirement 3 gains the round-trip clause and the
  no-IO-on-the-event-loop clause.
- `AGENTS.md` "Keep it honest" gains two lines that later phases depend on: *an
  unreachable source is reported, never rendered as an empty one*, and *an
  ordering across machines is only as trustworthy as their clocks, and says so.*
  (It is a single bullet inside `## Quick Reference`, not a section — the two
  lines become sub-bullets under it.)
- [clv/plugins/AGENTS.md:461](clv/plugins/AGENTS.md#L461) — "Network
  aggregation or remote log collection" narrowed the same way, plus a third
  entry in that file's existing `### Reversed` list. (This phase originally
  cited line 312; 312 is about `register()` returning `None`.)
- **[clv/AGENTS.md:97](clv/AGENTS.md#L97)** — "Network log aggregation or remote
  tailing", a fourth copy of the non-goal this phase originally missed. Narrowed
  identically. Without it the gate below does not hold: a reader of the `clv/`
  contract would still be told remote is refused.
- **[TODO.md:48-49](TODO.md#L48-L49)** — "**Local only.** No network, no
  telemetry", a second declaration this phase originally missed. It sits under
  *Constraints that apply to every item*, which states it derives from
  `AGENTS.md`, so it must move with the Security & Privacy bullet above.
- [TODO.md:1244](TODO.md#L1244) — the *Deliberately out of scope* entry is
  **not deleted**. It is rewritten to record that the decision was reversed, on
  what date, and why, with a pointer to this file. That section exists so
  arguments are not repeated; erasing the entry would destroy the record of the
  argument rather than settle it.
- This file is referenced from `TODO.md`'s Status table as the successor plan.

**Documentation changes.** This phase *is* the documentation change.

**Testing.** None beyond the suite staying green — no code changes.

**Gate.** No source file modified. `python -m pytest` reports **791** passed on
3.11 and 3.14 — the count `AGENTS.md`'s Testing section already carries; the
621 this file was written against predates it. A reader of `AGENTS.md` alone can
tell that remote sources are now in scope and what kind of remote access is
still refused.

**Commit.** `docs: bring remote sources into scope`

---

## Phase 1 — Source identity

The systemic risk, and the reason a plugin alone cannot deliver the goal.

State persists sources as **strings** and reconstructs them with a bare
`Path(entry)` in **twelve** places — this phase originally named eight, and the
survey found four more of the same shape. All line numbers are as of 2.6.1
(`662df42`); three of the eight originally listed had drifted.

*Originally named:* [app.py:1032](clv/app.py#L1032) (starred),
[1155](clv/app.py#L1155), [1205](clv/app.py#L1205), [1237](clv/app.py#L1237),
[1321](clv/app.py#L1321) (merged), [2660](clv/app.py#L2660) (`SavedView.source`,
was cited as 2650), [2847](clv/app.py#L2847) (starred at launch, was cited as
2837).

*Found by the survey:* [storage.py:109](clv/storage.py#L109)
(`SavedView.summary`), [session.py:534](clv/services/session.py#L534)
(`origin_of`), [app.py:1487](clv/app.py#L1487) (`ORIGIN_FIELD` values taken off
the entry stream), and [app.py:3166](clv/app.py#L3166) —
`Path(self._merged_name())`, a *label* wrapped in a `Path` purely to reach
`export.default_stem`, and the last `Path(<non-path>)` in `app.py`. It is in
scope because leaving it forces the guard test below to carry a whitelist, and
a whitelist is how a guard rots.

Plus `config.parse_log_dirs` and `sources.normalize_path` — but see the note
below: those two are **not** the same operation as the twelve.

Every one of the twelve turns a remote identifier into a wrong local path on
restore, silently.

**Restore and user input are two boundaries, not one.** The twelve sites read a
string CLV itself wrote, which is already canonical: they must not expand `~`,
prepend the working directory, or resolve. `parse_log_dirs` and
`SourceManager.add` read a string a person typed and must do all three.
Collapsing them is the only way this phase can move behaviour, so `refs.py`
ships `parse_ref`/`format_ref` for the first and `normalize_ref` for the second.
That split is also what makes `parse_ref("journal:unit/sshd.service")` safe:
`parse_ref` contains no operation that could damage a scheme ref.

**The scheme is constrained by this, and the constraint is already verified.**
`Path("ssh://web01/var/log")` collapses to `ssh:/web01/var/log`, reports
`is_absolute() == False`, and `parse_log_dirs` then prefixes it with the
working directory and resolves it — yielding `/current/dir/ssh:/web01/var/log`.
Either every construction site routes through a factory, or the scheme uses
journald's single-colon shape. **Do both**: the factory is the correctness fix,
and the single-colon shape means a missed site degrades visibly instead of
inventing a plausible local path.

**Expected outcomes**
- New `clv/services/refs.py`: a `SourceRef` protocol documenting the exact
  surface CLV requires of a source — `name`, `parent`, `parts`, `suffix`,
  `__truediv__`, `with_name`, `relative_to`, `__str__`, `__fspath__`, plus the
  IO surface Phase 2 takes over (`is_file`, `is_dir`, `stat`, `open`). Derived
  from the survey of all 88 filesystem call sites, not guessed.
- `parse_ref(str) -> SourceRef` and `format_ref(SourceRef) -> str`, an exact
  round trip. `Path` satisfies the protocol as-is for local sources.
- **A remote ref is host-qualified in its string form**, and this is a
  correctness requirement rather than cosmetics. `ORIGIN_FIELD` stores
  `str(path)` on every entry of a merged set
  ([session.py:48](clv/services/session.py#L48)), so an unqualified
  `/var/log/syslog` would make `source:/var/log/syslog` match every host at
  once. The ref string is what disambiguates them.
- Every string→source reconstruction site routes through `parse_ref`. Every
  source→string persistence site routes through `format_ref`.
- `_resolve` ([app.py:3663](clv/app.py#L3663), cited above as 3653) becomes
  ref-aware and moves into `refs.py` as `identity`: a local path stays
  `path.resolve()`; a remote ref resolves to itself. It has **two** definitions
  today, not one — `sources._marker`
  ([sources.py:27-34](clv/services/sources.py#L27-L34)) is the same idea
  returning a `str`. Unifying them as `identity` and `ref_key` is the more
  valuable half of this bullet.
- No remote implementation exists yet. This phase ships one implementation of
  the protocol: `pathlib.Path`.

**Documentation changes.** `AGENTS.md` Architecture table gains `refs.py` under
Services. `clv/AGENTS.md` gains the identity rule: *a source is a `SourceRef`;
`Path` is one implementation and no longer the assumed one.*

**Testing** (new `tests/test_refs.py`)
- `parse_ref` / `format_ref` round trip for absolute, relative, `~`-prefixed
  and whitespace-padded local paths.
- A local ref is a `Path` and is accepted everywhere a `Path` was.
- The regression that motivates this phase: a ref string containing a colon
  survives a save/load cycle through `StateStore` unchanged. Written now,
  against a stub remote ref, so Phase 4 does not have to discover it.
- Two stub remote refs with the same path on different hosts are distinct
  identities and produce distinct `ORIGIN_FIELD` values.
- Grep-style guard test asserting no bare `Path(` reconstruction remains in the
  persistence paths, so the seam cannot quietly rot back.

**Gate.** All **791** existing tests pass **with no assertion edited** — prove
it with `git diff -- tests/` showing additions only. Any test that needed
changing means behaviour moved, which this phase forbids. Both Python versions.

**Commit.** `refactor: introduce SourceRef and route source identity through it`

---

## Phase 2 — Filesystem seam

Discovery and reading talk to `os` directly. This phase puts a backend between
them, ships exactly one backend whose behaviour is today's, and establishes the
blocking contract Requirement 3 depends on.

**Expected outcomes**
- New `SourceBackend` protocol (in `refs.py` or `clv/services/backend.py`):
  `walk`, `access`, `stat`, `identity`, `open`, `capabilities`.
- **Every method declares whether it may block.** The protocol states which
  calls are guaranteed cheap (safe from `poll()`) and which must be driven from
  a worker. `LocalBackend` is cheap throughout; a remote backend is not, and
  the caller must be able to tell without knowing which backend it holds. A
  backend that blocks in a method the caller believes is cheap is precisely the
  bug Requirement 3 exists to prevent, so the contract is part of the type, not
  a comment.
- `LocalBackend` implements it over `os` and `pathlib`, unchanged semantics.
- Seams introduced:
  - `discovery._walk_directory` ([discovery.py:314](clv/services/discovery.py#L314))
    — `os.walk` becomes `backend.walk`.
  - `discovery.skip_reason` ([discovery.py:265](clv/services/discovery.py#L265))
    — `os.access` becomes `backend.access`.
  - `reader.read_last_lines`, `detect_file_encoding`, `looks_binary`,
    `SourceReader._read_from` — `path.open("rb")` becomes `backend.open`.
  - `SourceReader._stat_identity` ([reader.py:263](clv/services/reader.py#L263))
    — becomes `backend.identity`, which returns an opaque comparable. A backend
    that cannot produce one declares so, and the reader documents the
    degradation rather than pretending. Phase 4 needs this twice: for a
    non-GNU remote, and as the SFTP-style fallback.
  - `sources.check_access` ([sources.py:53](clv/services/sources.py#L53)),
    including a backend-supplied hint so `ACCESS_HINT`
    ([sources.py:9](clv/services/sources.py#L9)) stops being the only answer —
    "re-launch with sudo" is meaningless for a file on another machine.
- `compressed.py` and `documents.py` accept an **open handle** instead of a
  path. `gzip`/`bz2`/`lzma.open` and `zipfile.ZipFile` all take file objects,
  so this is mechanical — but it is what makes a remote `.gz` readable, so it
  belongs here rather than being deferred.
- `SourceSession` needs nothing: `ReaderFactory` is already injectable
  ([session.py:37](clv/services/session.py#L37)).

**Documentation changes.** `clv/AGENTS.md` and `AGENTS.md` Architecture table
gain the backend. Requirement 3's exceptions list gains a third entry naming
the backend seam and stating that a backend may not silently substitute an
unbounded read for a bounded one, nor a blocking call for a cheap one.

**Testing** (extend `tests/test_discovery_reader.py`, new `tests/test_backend.py`)
- **The backend contract suite**, parameterised, so Phase 4's remote backend
  runs the *same* tests. The highest-value test in the plan — it is what makes
  "parity" measurable.
- A backend that declares no stable identity causes `SourceReader` to fall back
  to `(size, mtime)` and to report rotation conservatively, with a test for
  each direction.
- The blocking contract is asserted: a deliberately slow fake backend used from
  a `poll()` path fails the test. This is the guard against Requirement 3
  eroding.
- Compressed and document reads work from a handle.

**Gate.** All existing tests pass with no assertion edited. Benchmark recorded
in the commit message: discovery over a ≥2000-file local tree, before and
after, no regression beyond noise. Both Python versions.

**Commit.** `refactor: put a source backend behind discovery and reading`

---

## Phase 3 — Configuration

**Expected outcomes**
- `config.py` reads `[ssh:<name>]` sections alongside `[log_viewer]`.
- New `RemoteHost` dataclass: `name`, `host`, `user`, `port`, `identity_file`,
  `log_dirs`, `enabled`, optional per-host `include_globs` / `exclude_globs` /
  `max_files` / `max_buffer_lines`, and `correct_clock_skew`.
- `enable_ssh` in `[log_viewer]` is the master switch, defaulting **false**.
- Per-host settings fall back to the global ones when absent.
- **`max_files` and `max_buffer_lines` become per-host budgets.** Globally,
  one noisy host consumes the whole `max_files` allowance and truncates the
  others, and `DiscoveryReport.truncated` cannot say whose files were cut.
  Per-host budgets with per-host truncation reporting. `max_buffer_lines` is
  already per source, but five merged remote sources pull five times the
  history over the link on open, so a per-host override is the pressure valve
  for a slow connection.
- **No `sudo` key exists**, and a config carrying one is reported as
  unsupported with a pointer to the group-membership/ACL answer. Requirement 11
  is enforced at the schema, where it cannot be forgotten.
- **A malformed section degrades to being skipped and reported, never to a
  startup failure** — the rule the whole of `config.py` already follows.
- Validation with actionable messages: a section with no `host`, an
  unreadable `identity_file`, a port outside 1–65535, an empty `log_dirs`.
  Reported through the same channel as plugin errors.
- **No network access in this phase.** Parse and expose only.

Shape:

```ini
[log_viewer]
enable_ssh = true

[ssh:web01]
host = web01.internal
user = ops
port = 22
identity_file = ~/.ssh/id_ed25519
log_dirs = /var/log, /srv/app/logs
include_globs = *.log, syslog*
max_files = 2000
correct_clock_skew = false
enabled = true

[ssh:db02]
host = 10.0.0.12
user = ops
log_dirs = /var/log/postgresql
```

**Documentation changes.** `DEFAULT_SETTINGS_TEMPLATE` in
[config.py:50](clv/services/config.py#L50) gains `enable_ssh` with a commented
example `[ssh:...]` block, matching how `enable_journald` documents itself. The
shipped `settings.conf` gains the same, commented out. Both note that there is
no password option and no `sudo` option, and why.

**Testing** (extend `tests/test_config.py`)
- Multiple hosts parse into `RemoteHost` records with per-host overrides.
- Fallback to global discovery settings when a host omits them.
- Every malformed shape: missing `host`, bad port, absent identity file,
  duplicate section name, empty `log_dirs`. Each is skipped, reported, and
  never raises.
- `enable_ssh` defaults false; hosts parse but stay inert when it is false.
- **No password key is accepted.** A `password =` line is ignored and reported
  as unsupported, with a test asserting the value never reaches `RemoteHost`.
- **No sudo key is accepted**, same treatment, same assertion.

**Gate.** A config file containing `[ssh:...]` sections loads correctly on a
build with the SSH plugin absent entirely. Both Python versions.

**Commit.** `feat(config): remote host sections and the enable_ssh switch`

---

## Phase 4 — Transport and plugin

The first phase with a user-visible feature: a remote log on screen, tailing.

**Expected outcomes**

*Connection*
- New `clv/plugins/sources/ssh.py`, modelled on
  [journald.py](clv/plugins/sources/journald.py) — the same opt-in check on
  every `discover()`, the same injected runner, the same non-blocking drain,
  the same explicit `close()`.
- **Connection manager** using `ControlMaster`:
  `ssh -o ControlMaster=auto -o ControlPersist=<n> -o ControlPath=<socket>`.
  The socket lives under the user's runtime dir, mode `0600`, and is **torn
  down explicitly on unmount** with `ssh -O exit`. A persisted multiplex socket
  is a live authenticated connection any local process running as that user can
  ride; leaving one behind after CLV exits is a real exposure, so
  `ControlPersist` is short and the teardown is tested, not assumed.
- **The three flags that make the auth decision enforceable rather than
  aspirational**, on every invocation:
  - `-o BatchMode=yes` — converts every interactive prompt into a clean
    non-zero exit with usable stderr. Without it, the first connection to an
    unknown host writes `Are you sure you want to continue connecting?` to a
    stdin nobody is reading and **CLV hangs invisibly inside the TUI**. This is
    the single flag that turns "agent and keys only" from a policy into a
    mechanism, and it is what lets Requirement 10 be kept without freezing:
    unknown host key becomes a clear message telling the operator to verify and
    connect once by hand.
  - `-T` — no pseudo-terminal, so nothing tries to render a prompt.
  - `-o LogLevel=ERROR` — suppresses the client's own chatter.
- **Shell noise is a data-integrity problem, not cosmetics.** A login shell may
  print an MOTD, a legal banner or a `.bashrc` echo, and that text lands in
  `find` output as phantom filenames. Commands are issued non-interactively,
  output is framed with an explicit sentinel so anything before it is
  discarded, and stderr is captured separately rather than merged.
- `child_environment()` from journald is reused verbatim — the PyInstaller
  `LD_LIBRARY_PATH` problem it solves
  ([journald.py:77](clv/plugins/sources/journald.py#L77)) applies identically
  to `/usr/bin/ssh`.

*Capability probing (Requirement 5)*
- **One probe command at connect**, combined with the clock capture so it costs
  a single round trip: shell identity, `find` / `stat` / `dd` variant, and
  `date +'%s %z'`.
- A **command profile** per host — GNU, BusyBox, BSD — selecting the argv set.
  Where a capability is absent the fallback is explicit and its cost stated:
  no `find -printf` means a two-command discovery; no `stat -c '%i'` means
  rotation detection falls back to the `(size, mtime)` path Phase 2 already
  built. Nothing fails obscurely because the remote was Alpine.

*Reading*
- **`RemoteBackend`** implementing the Phase 2 protocol, honouring the blocking
  contract:
  - `walk` — one command per root, `find <root> -type f -printf '%s %i %p\n'`
    on GNU, with a `-name` pushdown built from `include_globs` where it is
    expressible. Satisfies Requirement 4.
  - `stat`/`identity` — `stat -c '%d %i %s %Y'`, giving genuine inode-based
    rotation detection.
  - `open`/bounded read — `tail -c <n>` for the initial backwards read,
    `dd iflag=skip_bytes skip=<offset>` for an incremental tail.
  - `access` — encoded in the `find` predicates, not a second round trip. A
    file the SSH user cannot read is reported with a **remote-aware message**
    naming host and path and suggesting group membership (`adm`,
    `systemd-journal`) or an ACL. Never `sudo`.
- **The binary sniff is the trap.** `discovery.skip_reason` reads 8 KB per
  candidate ([reader.py:114](clv/services/reader.py#L114)); done naively that
  is one round trip per file. Batch it into a single remote command over the
  candidate list, or defer the verdict to open time and mark the file
  provisional. Whichever is chosen, state it in the module docstring and prove
  the round-trip count with a test.
- **Follow** via `tail -F` (not `-f`, so remote rotation is survived) as a
  persistent subprocess, stdout non-blocking, drained on the existing poll
  exactly as `JournalReader._drain` does. `poll()` performs no round trip.
- `prime()`, probes and skew measurement run in a worker thread; the pane shows
  a pending state and `_select_source` becomes awaitable for remote refs.
- Remote roots reach `SourceManager` as ordinary roots. Nothing is a
  `ProviderSource` — that type is exactly what this plan is avoiding.
- Nothing spawns until `enable_ssh` is true **and** the host is enabled.

**Documentation changes.** `clv/plugins/README.md` gains the SSH source. Module
docstring covers the transport choice, the inode advantage, the round-trip
budget, the binary-sniff decision, the `BatchMode` rationale, and the command
profiles.

**Testing** (new `tests/test_ssh_source.py`)
- The whole backend contract suite from Phase 2, run against `RemoteBackend`
  with a fake runner.
- Command construction per profile: exact argv for discovery, bounded read,
  incremental tail and follow, under GNU, BusyBox and BSD. Asserted as strings.
- **Round-trip budget**: discovering a 200-file fixture tree issues a bounded
  number of commands.
- **`poll()` issues no command at all** — the Requirement 3 regression test.
- Disabled by default: with `enable_ssh` false, **no process is spawned** —
  asserted on a patched spawn point, as the journald suite does.
- `ControlMaster` socket has restrictive permissions and **does not exist after
  unmount**.
- No leaked `tail -F` across a source switch or at shutdown.
- Banner/MOTD contamination: a fixture whose output is prefixed with junk still
  yields the correct file list.
- **Security assertions:** no argv contains `StrictHostKeyChecking=no`,
  `UserKnownHostsFile`, `sudo`, or a password; every argv carries
  `BatchMode=yes`; a host that would prompt is reported unreachable.
- Remote paths containing spaces, quotes and glob characters are quoted
  correctly. **Command injection through a configured path is the live risk
  here** — a table-driven test with hostile path fixtures.

**Gate.** Suite passes with no network and no SSH server. `enable_ssh = false`
leaves the local suite bit-identical. The opt-in integration suite passes
against a GNU image and an Alpine image. A remote log opens, tails and filters
in a manual smoke test against a real host with **no perceptible UI stall**,
recorded in the commit message. Both Python versions.

**Commit.** `feat(ssh): read remote log folders over a multiplexed SSH connection`

---

## Phase 5 — Reachability

Phase 4 works when the network does. This phase is about when it does not, and
it comes before parity deliberately: merging multiplies the number of things
that can fail, and the failure path should be honest before it is multiplied.

The specific hazard is a deliberate existing behaviour. `SourceBuffer.poll`
swallows `OSError` because *"a source that vanished mid-session is not an error
worth taking the pane down for"* ([session.py:171-174](clv/services/session.py#L171-L174)).
Exactly right for a rotated local file. Exactly wrong for a dropped SSH
connection, where it renders as a log that has simply gone quiet.

**Expected outcomes**
- A source has an explicit reachability state: `connected`, `connecting`,
  `unreachable(reason)`, `disabled`.
- A remote read failure is distinguished from an empty read at the backend, so
  `poll` can stay silent for one and speak for the other.
- **The pane says so.** A remote source whose connection dropped shows a notice
  in the log pane, not an absence. `describe_empty_result` gains the case.
- Bounded reconnection with backoff, driven from a worker. Never a reconnect
  per poll tick — the journald "fork bomb with a nice name" lesson applies to
  connections too, and Requirement 3 forbids doing it on the event loop anyway.
- Failure reasons are distinct and actionable, each with its own message: DNS
  failure, connection refused, auth rejected, **host key mismatch or unknown
  host** (with the verify-and-connect-once instruction `BatchMode` makes
  possible), permission denied on the path, and a missing remote utility.
- Discovery reports an unreachable host through the existing `unreadable_roots`
  channel plus a named message, so a host that is down does not silently shrink
  the tree.
- A host being down never blocks startup and never delays the local tree.
- **`Ctrl+R` becomes per-host aware.** `action_reload_sources` re-walks every
  root; over five SSH connections that is expensive and mostly wasted. Reload
  reuses live connections, skips unreachable hosts rather than blocking on
  them, and reports what it refreshed.

**Documentation changes.** `AGENTS.md` "Keep it honest" gains the unreachable
clause (drafted in Phase 0, now true).

**Testing** (extend `tests/test_ssh_source.py`, `tests/test_session.py`)
- Each failure mode maps to its own message. Table-driven over captured stderr.
- A mid-session drop produces a visible notice, not silence. The regression
  test this phase exists for.
- Reconnect backoff: N failures produce N bounded attempts, not one per tick.
- A recovered host resumes tailing without losing its buffer.
- An unreachable host at startup leaves local sources fully functional and the
  app responsive.
- `Ctrl+R` with one host down completes promptly and refreshes the rest.

**Gate.** Pull the cable on a real host during a manual smoke test: the pane
says what happened within one backoff interval, and recovers when it returns.
Suite green on both Python versions.

**Commit.** `feat(ssh): make connection state visible and recoverable`

---

## Phase 6 — Parity

The phase the goal is measured against. Most of it should be *small*, because
Phases 1 and 2 did the work — if an item below turns out to be large, a seam
was missed and the fix belongs in the seam, not here. The exception is time,
which is genuinely new behaviour and is the largest single item in this phase.

**Expected outcomes**

*Structure and identity*
- **Folder hierarchy.** Remote roots build the same nested folder tree as local
  ones in `_build_tree` ([app.py:955](clv/app.py#L955)), with the host as the
  root node. Not a flat group — the flat group is what a `ProviderSource` gets
  and precisely what this plan rejects.
- **Starring.** `_star_target` ([app.py:2778](clv/app.py#L2778)) stops gating
  on `isinstance(data, Path) and data.is_file()` and asks the ref instead. A
  starred remote log survives a restart via Phase 1's round trip.
- **Rotated sets.** `group_rotated` is pure name arithmetic
  ([rotation.py:92](clv/services/rotation.py#L92)) and should need nothing;
  confirm remote members group and that a compressed remote member is read
  through the Phase 2 handle path.
- **Session restore.** The selected source, starred set and merged set all
  round-trip when any member is remote.
- **Ad-hoc add.** `a` accepts a remote ref for a configured host, so a folder
  can be added without editing config first. `SourceManager.add` and
  `check_access` go through the backend; a ref naming an unconfigured host is
  rejected with a message pointing at the host dialog.

*Merging across machines*
- `x` adds a remote log to the merged set; `u` opens local and remote together
  in one timestamp-ordered pane. `open_many`
  ([session.py:312](clv/services/session.py#L312)) dispatches per-ref through
  the backend. **This is the feature that most justifies the name of the
  product** and it gets its own smoke test.
- **The `node` field.** Every entry carries the machine it was read from, under
  `node`, added to `query.NORMALISED_FIELD_KEYS`
  ([query.py:100](clv/services/query.py#L100)). `host` is untouched and keeps
  meaning what the log says about itself, so no saved query changes meaning —
  the rule `query.py` is built around. `node:web01 status>=500` works on day
  one, which is the single most obvious query in the whole feature and would
  otherwise have silently matched the wrong field or landed in
  `hidden_missing_field`.
- **`source:` disambiguates across hosts**, because Phase 1 made the ref string
  host-qualified. `source:` on a path that exists on three machines no longer
  matches all three.

*Time (see "The time rule" above)*
- Naive timestamps from a remote host are interpreted in that host's captured
  timezone, making them aware, so `_merged`
  ([session.py:428](clv/services/session.py#L428)) orders them correctly with
  no change to `sortable_moment`.
- Measured skew is reported in the merged view's status detail beside the
  existing `anchored` count.
- `correct_clock_skew` applies the measured offset when enabled, and the pane
  states that it is doing so.

*Downstream verification*
- **Export, clipboard, marks, watch rules, timeline, clustering** verified
  against a remote source. These sit downstream of the buffer and should be
  free; verified rather than assumed, because "should be free" is how a gap
  ships.
- **Export naming.** `default_stem` ([export.py:164](clv/services/export.py#L164))
  derives a filename from the source; a multi-host merged export must not
  produce a name that hides which machines are in it, and the exported rows
  carry `node`.

**Documentation changes.** `README.md`'s "Merging is local only" section
([README.md:265-268](README.md#L265-L268), plus the Feature Highlights bullet at
[README.md:64](README.md#L64)) is rewritten — the user-facing promise Phase
0 deliberately left alone, changed now because the capability exists.

**Testing** (extend `tests/test_sources.py`, `test_merged_view.py`,
`test_saved_views.py`, `test_session.py`, `test_bookmarks.py`,
`test_exporters.py`, `test_field_query.py`)
- A remote source is starred, persisted, and restored.
- A merged view spanning one local and one remote source orders correctly,
  including the mixed aware/naive case.
- **The timezone regression**: two hosts in different zones emitting naive
  syslog stamps interleave correctly. Without the fix this test misorders by
  the zone difference, which is the bug it exists to prevent.
- Skew is measured, reported, and — when enabled — applied, with the pane
  stating it.
- `node:` filters correctly, and `host:` still means what it always meant. A
  saved query using `host:` produces identical results before and after.
- `source:` distinguishes the same path on two hosts.
- A merged set with one unreachable member opens the rest and names the one it
  could not — the `open_many` failure contract, over the network.
- Remote rotated set groups and reads, including a compressed member.
- Tree hierarchy assertions: a remote root produces nested folder nodes.
- Full session round trip with remote entries in every persisted collection.
- Export of a multi-host merge names the hosts and carries `node`.

**Gate.** A parity checklist in the commit message, one line per feature, each
marked verified by a named test. Any feature that cannot reach parity is
**documented as a known limitation in `README.md` in the same commit**. Both
Python versions.

**Commit.** `feat(ssh): full feature parity for remote sources`

---

## Phase 7 — Remote UI

**Expected outcomes**

*Host management dialog*
- New `clv/widgets/remote_hosts_dialog.py`. Lists configured hosts with live
  reachability from Phase 5; add, edit, remove; a **Test connection** button
  that runs one bounded probe in a worker and reports the Phase 5 reason
  verbatim, along with the detected command profile and measured skew.
- Writes back to `settings.conf` **in place**, preserving the operator's
  comments — the rule `persist_setting` and `persist_log_sources` already
  follow ([sources.py:184](clv/services/sources.py#L184)). Extended to whole
  sections rather than single options.
- **No password field and no sudo toggle.** Requirements 9 and 11, restated at
  the exact point where adding one would feel helpful.
- New binding **`R`** — "Remote hosts", `show=False`, discovered through the
  help overlay. Consistent with the uppercase-opens-a-dialog convention (`V`,
  `W`, `X`, `M`) and free today; `Ctrl+R` remains reload.

*Cross-host merge — the workflow the feature exists for*
- **"Merge this path across all reachable hosts."** Comparing
  `/var/log/nginx/access.log` across five web servers is the canonical reason
  to want this product, and building that set by pressing `x` on five leaves in
  five separate collapsed host trees is tedious enough to stop people using it.
  One action on a source builds the set from every reachable host that has that
  path, reports which hosts contributed and which lacked it, and hands off to
  the existing `u`.
- Bound as a modifier on the existing merge gesture rather than a new top-level
  key, keeping the keybinding budget intact.

*Chrome*
- Advanced drawer gains a "Remote sources (SSH)" switch beside the journald one
  ([advanced_drawer.py:313](clv/widgets/advanced_drawer.py#L313)), writing
  `enable_ssh` through `persist_setting`, with a per-host status line.
- Tree labels and the status line disambiguate hosts. `_compact_path`
  ([app.py:3647](clv/app.py#L3647)) shows `parent/name`, which is ambiguous the
  moment two machines have the same log; remote refs show the host.
- Every control keyboard-reachable, dialog usable at 80 columns.

**Documentation changes.** `README.md` gains the dialog and the cross-host
merge with keybinding rows. Help overlay sections in `build_help_sections`
([app.py:304](clv/app.py#L304)) gain both. `AGENTS.md` keybinding table gains
the rows.

**Testing** (new `tests/test_remote_hosts_dialog.py`, extend
`tests/test_merged_view.py`)
- Add, edit and remove round-trip through `settings.conf` **with surrounding
  comments preserved** — assert on the full file text, not just the parse.
- Test-connection surfaces each Phase 5 failure reason, plus profile and skew.
- Validation rejects a bad port, an absent identity file, a duplicate name,
  with the message shown in the dialog rather than a toast.
- Cross-host merge builds the expected set, names hosts that lacked the path,
  and skips unreachable ones without blocking.
- Layout regression at 80 columns, asserting widget `region` bounds — the
  project's method, not screenshots.
- Full keyboard traversal reaches every control.
- The drawer switch writes `enable_ssh` and takes effect on the next rescan
  without a restart, as the journald switch does.

**Gate.** Dialog usable at exactly 80 columns with no clipping. A hand-written
`settings.conf` full of comments survives an edit cycle intact. Both Python
versions.

**Commit.** `feat(ui): remote host management and cross-host merge`

---

## Phase 8 — Documentation and release

**Expected outcomes**
- `README.md`: a "Remote sources over SSH" section covering setup, the
  agent/key-only auth model and why, `BatchMode` behaviour on an unknown host,
  the `[ssh:<name>]` schema, the dialog, the `R` binding, cross-host merge,
  `node` versus `host` in queries, clock-skew reporting and the correction
  opt-in, the no-`sudo` position and what to do instead, `ControlMaster`
  behaviour, non-GNU remotes and what degrades, performance over a slow link,
  and any known limitation from Phase 6.
- **`sshfs` is recommended as a legitimate alternative**, with its trade-off
  stated: a mount gives full fidelity with no code at all, at the cost of a
  per-file round trip during discovery. Naming the alternative costs nothing
  and buys trust.
- `README.md` keybinding table and settings table updated.
- `settings.conf` ships the commented `[ssh:...]` example.
- `AGENTS.md` reviewed end to end for statements Phases 0–7 outdated.
- `TODO.md` Status table records this plan complete, with commit hashes.
- Version bump in `pyproject.toml` — **2.7.0**. A new capability and a reversed
  non-goal; not a patch.
- `install.sh` and the PyInstaller build verified: the bundle must find the
  system `ssh` through `child_environment()`, which is the exact failure
  journald already hit. Test the built binary, not just the checkout.

**Testing**
- `tests/test_install_script.py` and `tests/test_version.py` updated.
- A documentation test asserting the shipped `settings.conf` parses into the
  documented defaults, so the template and the parser cannot drift.

**Gate.** Full suite green on 3.11 and 3.14. The opt-in integration suite green
on GNU and Alpine images. A frozen build on one distribution reads a remote log
on another. Every requirement in this file traceable to a passing test or a
stated limitation.

**Commit.** `release: 2.7.0 — remote sources over SSH`

---

## Phase 9 — The remote journal

Scheduled rather than aspirational, because it is close to free once Phase 4
exists and it is the strongest form of the product's own premise. Remote hosts
are overwhelmingly systemd, and *the remote OS event log* is much closer to the
Event Viewer comparison than remote flat files are.

Almost nothing new is needed. `JournalReader.command()` builds an argv
([journald.py:248](clv/plugins/sources/journald.py#L248)) and `translate()`
converts records ([journald.py:157](clv/plugins/sources/journald.py#L157)) —
both are transport-independent. The work is prefixing the argv with the
connection manager's `ssh` invocation and enumerating units per host.

**Expected outcomes**
- A configured host with `journalctl` present offers its journal, its units and
  its boots as sources, in the same tree position as local journal sources.
- Requires **both** `enable_journald` and `enable_ssh` — two subprocess
  consents, and the remote one does not imply the local one.
- Unit enumeration runs once per host, in a worker, with the existing
  `QUERY_TIMEOUT` applied to a now-slower call, and reports failure the way
  `unit_error` already does.
- Severity push-down to `--priority` still applies, and matters more: it avoids
  carrying discarded lines across the network rather than across a pipe.
- The follow is one persistent `ssh … journalctl --follow` per open source,
  drained non-blocking, closed on switch and shutdown like every other reader.
- `node` distinguishes journal entries by machine, so a merged view of the same
  unit across a fleet is queryable.
- Absence of `journalctl` on a host is reported as a host capability, not an
  error — reusing the Phase 4 capability profile.

**Documentation changes.** `README.md` systemd journal section gains remote
hosts. `clv/plugins/README.md` updated. Both state the dual opt-in.

**Testing** (extend `tests/test_journald.py`, `tests/test_ssh_source.py`)
- Remote argv construction for the journal: all/unit/boot, with priority
  push-down, over the connection manager.
- Captured `journalctl -o json` fixtures translate identically whether read
  locally or remotely — the same fixture, both paths, one assertion.
- Neither opt-in alone spawns anything.
- Per-host unit enumeration, including a host where `journalctl` is absent.
- No leaked remote follow on switch or shutdown.

**Gate.** Suite green with no network. Manual smoke test: a remote unit tails
and filters, and a merged view of one unit across two hosts orders correctly.
Both Python versions.

**Commit.** `feat(ssh): read the systemd journal on remote hosts`

---

## Risks, recorded up front

| Risk | Where it bites | Mitigation |
| --- | --- | --- |
| **Command injection** through configured paths or host names | Phase 4 | Strict quoting, hostile-input test table, argv assertions. The highest-severity risk in this plan. |
| **Blocking IO on the event loop** freezes the UI | Phases 2, 4 | Requirement 3, the declared blocking contract, `poll()`-issues-no-command test, persistent follow drained non-blocking. |
| **Silent misordering** across machines with skewed clocks or differing zones | Phase 6 | Requirement 6, per-host timezone applied to naive stamps, skew always reported. The timezone regression test is the guard. |
| **`host:` returns the wrong thing** in a multi-host merge | Phase 6 | A distinct `node` field; `host` untouched so no saved query changes meaning. |
| **Non-GNU remotes** fail obscurely | Phase 4 | Requirement 5, capability probe, per-profile argv, the opt-in Alpine integration run. |
| **An unknown host key hangs the TUI** on stdin nobody reads | Phase 4 | `BatchMode=yes`, plus a Phase 5 message telling the operator how to verify once by hand. |
| **A leaked `ControlMaster` socket** is a rideable authenticated connection | Phase 4 | Mode `0600`, short `ControlPersist`, explicit `ssh -O exit` on unmount, tested. |
| **Per-file round trips** make discovery unusable | Phase 4 | Requirement 4, batched `find`, a test that counts commands. |
| **The binary sniff** silently reintroduces per-file reads | Phase 4 | Explicit decision in the module docstring plus the round-trip count test. |
| **Seam rot** — a later change reaches past the backend to `os` | Phases 2+ | Shared backend contract suite; the guard test from Phase 1. |
| **A dropped link renders as a quiet log** | Phase 5 | The phase exists for this; it has a named regression test. |
| **Root-only remote logs** produce a wall of `unreadable` | Phases 3, 4 | Remote-aware access message naming host, path and the group/ACL fix; no `sudo` by design, said out loud in the README. |
| **Scope creep into collection infrastructure** | Throughout | Phase 0's narrowed non-goal: on-demand reads over the operator's own SSH, never an agent, a daemon, store-and-forward, or elevated privilege. |
| **`sshfs` is simply better for some users** | Phase 8 | Say so in the README. A mounted remote folder has always worked and needs no code; this feature exists for people who do not want a mount. |

---

## What is still out of scope

Narrowed, not abandoned. These stay refused after this plan lands:

- **Agents or daemons on the remote host.** CLV reads with `ssh`; it never
  installs anything, never leaves anything running, and requires no privilege
  beyond reading the files.
- **Privilege escalation, anywhere.** No `sudo`, no `doas`, no `pkexec`, local
  or remote, not behind a setting. Requirement 11.
- **Store-and-forward, spooling, or a collection pipeline.** No remote log
  content is cached, spooled or written to disk locally. Requirement 12.
- **Credential management.** No password storage, no key generation, no agent
  management. CLV uses the SSH setup the operator already has.
- **Transports other than SSH.** No syslog receiver, no HTTP log APIs, no
  cloud-provider log services. Each is a different product.
- **Writing to the remote host.** Read-only, always.
