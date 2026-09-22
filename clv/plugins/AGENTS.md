# AGENTS.md — CLV Plugin Development Guidelines

## Purpose
This document defines how to build and integrate **plugins** into the Centralized Log Viewer (CLV).  
Plugins extend CLV’s core functionality without modifying the main codebase.

---

## Philosophy

CLV’s plugin system enables third-party developers to add new log sources, filters, and exporters — safely and predictably.

- **Isolation:** Plugins must never modify core behavior directly.  
- **Failure isolation:** A plugin that raises is reported and skipped, never
  fatal. This is a guarantee about *CLV's* behaviour, not about the plugin's —
  see [Trust model](#trust-model).  
- **Extensibility:** Core should discover and integrate plugins dynamically.  
- **Minimal coupling:** Plugins depend only on public APIs.

---

## Trust model

Read this before you install a plugin, and before you write documentation that
describes what one can do.

### What a plugin can do

**A plugin is trusted code.** It is Python, executed at CLV's privilege, inside
CLV's process. Four consequences, none of which CLV can change:

- It can read anything the operator can read — every log CLV has open, every
  file under `$HOME`, `~/.ssh`, `settings.conf`, the environment.
- It can write, spawn a subprocess, and open a socket. The
  [conventions](#conventions-for-plugin-authors) below ask it not to; nothing
  stops it.
- `import` runs its module-level code **before any interface check runs**. By
  the time CLV can tell whether an object implements `FilterStage`, the module
  has already executed.
- The interfaces bound what CLV *asks* of a plugin. They do not bound what a
  plugin *can do*. `FilterStage.apply` is where CLV calls in; it is not a wall.

Installing a plugin is the same act of trust as installing any other program.
Judge it the same way: by who wrote it and whether you read it.

### What isolation does and does not do

Isolation is **failure containment, not safety**. A subprocess host can be
killed on crash, hang or timeout, which is the first time in CLV's history a
plugin can be *stopped*. It does not make an untrusted plugin safe. The child
runs as the operator, with the operator's filesystem and the operator's
network. Everything in *What a plugin can do* stays true of an isolated plugin
except the ability to take the viewer down with it.

**It is opt-in, and only four kinds may ask.** A plugin declares
`isolated = True`, or an operator writes `isolated = true` in its
`[plugin:<name>]` section; the coarse-grained kinds get a host and the per-entry
kinds are refused at load with the reason named. [Isolation](#isolation) below
has the mechanism.

**By default there is none**, and for a plugin that has not asked, nothing in
this section applies: it runs in CLV's process, and a plugin that hangs or leaks
there cannot be stopped. CLV can catch an exception, and it can disable a stage
that is repeatedly *slow* on the render path (see [Performance](#performance)) —
but a budget only works on code that returns, and neither of those is isolation.

**The import is contained only through the settings door.** A class attribute
has to be read to be honoured, and reading it means importing the module that
declares it, in this process, before anything is contained. An operator who
writes `isolated = true` in the plugin's own section is asking for something
CLV can answer before the import: that module is never imported here at all.

Any wording that would let this section be summarised as "plugins are contained,
therefore plugins are safe" is wrong. `tests/test_plugin_docs.py` enforces the
mechanical half of that: the word this paragraph is refusing to use may not
appear anywhere in this file, in any casing. The honest statements never need
it, so the rule costs nothing and closes the door on the hedged version.

### Reviewing a third-party plugin

Before you enable one, read it in this order. It is ordered by how much damage
the thing you are looking at can do before you notice it:

1. **The imports.** `subprocess`, `socket`, `http`, `urllib`, `ctypes`,
   `shutil`, `os.system` — and anything reaching into `clv.services.*` rather
   than the published interfaces. An import is also the file's first chance to
   run code.
2. **Module-level code**, and `discover()`. Both run at startup, before you have
   selected anything. `register()` runs at import too.
3. **Anything touching a write path, a subprocess or a socket**, wherever it
   appears. Check what it writes, where, and whether log content goes into it —
   log content is sensitive, and a plugin that copies it into a cache or a temp
   file has leaked it whether or not that was the intent.

If the plugin ships a `requires_clv` constraint, a name, and tests, that is
evidence of care. It is not evidence of safety.

---

## Plugin Structure

Each plugin is a Python module or package located in one of the following:

1. **The user plugin directory**, `~/.config/clv/plugins/` — where a plugin
   somebody else wrote gets installed. Either `my_plugin.py` or a directory
   `my_plugin/` with an `__init__.py`; both load the same way.
2. **`CLV_PLUGIN_PATH`** — extra directories, `os.pathsep`-separated, searched
   ahead of the user directory. A development and test mechanism.
3. **Bundled drop-ins**, `clv/plugins/` and its `sources/`, `filters/` and
   `exporters/` subpackages — for a plugin shipped as part of CLV.
4. **Installed entry point** — a Python package advertising the `clv.plugins`
   entry point group in its `pyproject.toml`.

### Example Structure

A plugin an operator installed:

```
~/.config/clv/
  settings.conf         # plugins = redact_filter, nginx_format
  plugins/
    README.txt          # written by CLV on first run
    redact_filter.py
    nginx_format/
      __init__.py
      patterns.py
```

One shipped with CLV:

```
clv/
  plugins/
    sources/
      journald_source.py
    filters/
      redact_filter.py
    exporters/
      json_exporter.py
```

Each plugin must define a class implementing one of the **Abstract Base Classes (ABCs)** below.

---

## Plugin Interfaces

### 1. LogSourceProvider

Provides a new source of logs to tail or read.

```python
from clv.api import LogSourceProvider

class MySource(LogSourceProvider):
    name = "My Custom Source"

    def discover(self):
        # Return a list of available sources
        return ["/var/log/custom.log"]

    def open(self, path):
        # Yield lines from the log source
        with open(path, "r") as f:
            for line in f:
                yield line
```

Sources are wired: whatever `discover()` returns appears in a **Providers**
group in the source tree, and selecting one opens it like any other source.
Return `ProviderSource(path, label)` records rather than bare identifiers when
you have a better name than the identifier's last component.

**A provider source is not a file, and CLV does not treat it as one.**
Include/exclude globs describe a directory walk and rotated-set grouping is name
arithmetic over files that rotate, so both refuse a provider identifier by name.
Starring and merging used to be on that list and are not any more — see
[Reversed](#reversed): a persisted *identifier* is not a persisted path, and a
journal unit is exactly the source an operator wants starred and compared across
a fleet.

#### Tailing a live source

`open()` returns an iterator, which is enough for a finite list of lines and
nothing more: it cannot express tailing, cannot be asked to stop, and has
nowhere to put cleanup. For a live stream, implement the optional
`open_reader()` instead:

```python
    def open_reader(self, path, *, max_lines):
        return MyReader(path, max_lines=max_lines)   # or None to use open()
```

The returned object must expose `path`, `prime()`, `poll()` and
`RELOAD_NOTICE`, and should expose `close()` if it holds anything — CLV calls
it on every source switch and again at shutdown. Optionally expose
`set_severity(bucket) -> bool` to filter at the source, returning whether that
required a restart.

Returning `None` (the default) means "use `open()`", so a provider written
before this existed keeps working unchanged.

**Discovery must not ask the filesystem where a package is.** Drop-ins are
found by importing each subpackage and walking its own `__path__`. A frozen
build (PyInstaller) has the modules inside an archive and no
`clv/plugins/sources/` directory on disk, so a `Path.is_dir()` check skips
every drop-in — and reports nothing, because loading no plugins is a valid
state rather than an error. That combination cost the shipped binary its
journal support entirely; see
`test_drop_ins_are_found_without_the_folder_existing_on_disk`.

**A subprocess must not inherit a frozen build's environment.** PyInstaller
puts its own `_internal` directory on `LD_LIBRARY_PATH` so the bundled
interpreter finds the libraries shipped beside it. Children inherit that, so a
*system* binary loads the bundle's libcrypto/libssl instead of the system's and
dies whenever the build machine's distribution differs from the user's — which
for a released binary is the normal case. Run system tools with
`journald.child_environment()`, which restores `*_ORIG` or strips the bundle's
own entry and leaves anything the operator set alone.

**Subprocesses need consent.** A plugin must not run one because it was
installed. The shipped `journald` provider is the pattern: a `settings.conf`
opt-in, read fresh on every `discover()`, returning no sources at all until it
is true — and reporting, never raising, where the tool it needs is absent.

### 2. FilterStage

Transforms or drops entries before they reach the pane.

`apply` receives a `LogEntry` (frozen dataclass: `raw`, `timestamp`, `level`,
`message`, `format_name`, `continuation`, `fields`) and a `FilterContext`
(`spec`, `source`). Return an entry to keep it, or `None` to drop it. Use
`dataclasses.replace` to modify — entries are immutable.

`fields` is the structure the parser recovered from the line: a **read-only
mapping of string to string**, empty for a line no format matched. Key names
are normalised across formats, so `entry.fields.get("host")` means the same
thing whether the line came from syslog or from an access log; the full
vocabulary is documented in the `clv.services.parsing` module docstring. Values
are never coerced — an HTTP status is `"500"`, not `500`.

Three things to know before using it:

- A continuation line (a stack trace frame, say) inherits its parent's
  timestamp and level but **not** its fields, so `fields` is empty there.
- It is a `mappingproxy`. `copy.deepcopy` and therefore `dataclasses.asdict`
  cannot handle one; call `dict(entry.fields)` if you need a plain dict.
- To add fields, pass a new mapping to `replace`. Do not try to mutate the one
  you were given — it is read-only by design.

```python
class TagUnknownHosts(FilterStage):
    name = "TagUnknownHosts"

    def apply(self, entry, context):
        if "host" in entry.fields:
            return entry
        return replace(entry, fields={**entry.fields, "host": "unknown"})
```

```python
from dataclasses import replace
from clv.api import FilterStage

class RedactFilter(FilterStage):
    name = "RedactSensitiveData"

    def apply(self, entry, context):
        if "password" not in entry.raw:
            return entry
        return replace(entry, raw=entry.raw.replace("password", "******"))
```

Dropping is the same method:

```python
class DropDebug(FilterStage):
    name = "DropDebug"

    def apply(self, entry, context):
        return None if entry.level == "DEBUG" else entry
```

Stages run *before* the user's query, severity and time filters.

### 3. LogFormat

Teaches CLV to parse a line no built-in matcher recognises.

**Built-ins first, plugins second, `raw` last.** `parse()` is offered only the
lines every built-in already declined, so a syslog file costs an installed
format nothing and a plugin cannot take over a name CLV already answers to.
*Replacing* a built-in is out of scope and always will be: when a format is
worth CLV's own attention it goes into `clv.services.parsing` on CLV's account.

**A `format_name` is four registrations, and only one of them is parsing.** The
others are a label, a column profile and — where a format recovers nothing — a
sentence saying why. Miss them and nothing raises: the entry renders with the
right timestamp and level, the bare identifier where the format name should be,
no source cell and no chips. That is a worse row than a built-in gets for the
same line, with no diagnosis anywhere. So a format *declares* them:

```python
from clv.api import FormatProfile, LogEntry, LogFormat, normalize_level

class NginxError(LogFormat):
    name = "nginx-error"
    requires_api = ">=1.0,<2.0"

    #: What every entry this format returns must carry. Not a display name.
    format_name = "nginx-error"
    #: What an operator calls it in the detail pane.
    label = "nginx error log"
    #: What it can produce, so a field query and the query box's completions
    #: know the word before a matching line has been read.
    field_names = frozenset({"pid", "host", "server", "request", "upstream"})
    #: Which of those earn the source cell and the chips.
    columns = FormatProfile(
        source_keys=("server", "host"), pid_key="pid", chips=("host", "upstream")
    )

    def parse(self, line):
        if len(line) < 20 or line[4] != "/":   # the cheap rejection, first
            return None
        match = _LINE.match(line)
        if match is None:
            return None
        return LogEntry(
            raw=line,
            timestamp=...,
            level=normalize_level(match.group("level")),
            message=match.group("msg"),
            format_name=self.format_name,
            fields={"host": match.group("client")},
        )
```

`clv/examples/nginx_error.py` is that plugin in full, commented; CLV writes a
copy into `~/.config/clv/plugins/examples/` on first run.

**Normalise onto CLV's vocabulary.** `NORMALISED_FIELD_KEYS` names the keys the
parser already uses across formats — `host`, `tag`, `pid`, `request`, `status`
and the rest. File your equivalent under the existing key rather than inventing
a synonym: `host:10.0.0.5` should answer for your format the way it answers for
syslog and for an access log. A genuinely new concept gets a key of its own.

**What `parse()` must return.** An entry whose `format_name` is the one the class
declared, and whose `fields` map strings to strings — values are compared as the
parser stored them and nothing downstream coerces, so an HTTP status is `"500"`
and not `500`. A plain `dict` is a fine `fields`; CLV treats it as read-only and
never writes to it, and omitting it entirely gets the shared empty mapping that
costs nothing. Return `None` to pass the line to the next format.

**Anything else takes the format out of service**, named by the rule it broke —
not a `LogEntry`, a `format_name` other than the declared one, a non-string
field value. The read path is stricter than the render path on purpose: a stage
that misbehaves costs a render, and a format that misbehaves writes a wrong
entry into the buffer that every query, bucket and cluster downstream then
believes.

**Refused at load**, before a line is read, because each of these is silent at
runtime: no `format_name`; a `format_name` that is a built-in's, including
`raw`; one another loaded format already claimed; `field_names` that is not a
set of non-empty strings; a `columns` naming a key outside `field_names`.

**Continuation needs no cooperation.** An entry whose `format_name` is not
`"raw"` is structured, so the unparsed line after it inherits its timestamp and
level exactly as it would after a built-in's — and inherits no fields, because a
stack trace frame has no host or PID of its own to report.

**What you get for free, and it is the point of the seam.** An entry a plugin
format produced is searchable by field query, bucketed by the timeline, folded
by the repeat clusterer, shown in the detail pane, markable, watchable and
exportable. None of that needed a line of code: it follows from the entry being
well-formed, and `tests/test_plugin_formats.py` has one test per feature to keep
it that way.

**A format disabled mid-session does not re-parse what is already in the
buffer.** Lines keep the `format_name` they were read with; re-opening the
source is what re-reads them. New lines stop being offered to it immediately.

### 4. QueryOperator

Adds a comparison token to the query grammar.

```python
import re
from clv.api import QueryOperator

class RegexMatch(QueryOperator):
    name = "field-regex"
    token = "~"

    def test(self, stored, value):
        return re.search(value, stored) is not None
```

`host~^web[0-9]+` then works in the query box, in a saved view and in a watch
rule, because all three route through `parse_query` and none of them knows a
plugin exists.

**Vocabulary, not structure.** This adds a word; it does not add a sentence
shape. There is still no `OR`, no parentheses and no precedence — terms stay
implicit-AND and flat. `clv/services/query.py`'s module docstring records how
narrow that reversal is and why it stops where it does.

**`test` receives two strings and returns a bool.** `stored` is the field value
exactly as the parser stored it, `value` is what the operator typed. Nothing
downstream coerces either, which is why `>=` has to decide numeric versus
lexicographic per comparison rather than per field — your operator makes the
same decision for itself.

#### Which tokens are legal

Checked at load, so a bad token is a message in the `P` dialog rather than a
mystery at the first term:

| Rule | Why |
| --- | --- |
| Not one of `>=` `<=` `!=` `>` `<` `=` `:` | Redefining `:` would change what every saved query already means. The list is published as `clv.api.BUILTIN_OPERATORS` so you can check rather than discover it from a load error. |
| No character a *key* may contain — letters, digits, `_`, `.`, `-` | `key~avalue` could not be told from a key called `key~avalue`. The tokeniser has no way to prefer one reading, so the ambiguity is refused instead of resolved by accident. |
| No whitespace, no `"` or `'` | A space ends a token and a quote groups a value. |
| Not already claimed by another loaded plugin | Reported once, naming both. |

**Longest token wins.** The alternation is rebuilt from the installed set,
sorted longest first, so registering `~` cannot break `>=` and registering `~=`
cannot break `~`. `!~` is found before a bare `~` for the same reason.

### 5. ComputedField

Adds a queryable field that is *derived* rather than parsed.

```python
from datetime import datetime, timezone
from clv.api import ComputedField

class EntryAge(ComputedField):
    name = "entry-age"
    field_name = "age"

    def value(self, entry):
        if entry.timestamp is None:
            return None
        now = datetime.now(timezone.utc) if entry.timestamp.tzinfo else datetime.now()
        return str(int((now - entry.timestamp).total_seconds()))
```

`age<60 level:error` is then "what has gone wrong in the last minute", without
touching the time window. The name joins the query vocabulary and the
completion list immediately, before any line has been read — the same promise
the parser's normalised keys make.

**Parsed fields resolve first, per entry.** `match_terms` asks the entry's own
`fields` first and only calls a plugin when the entry has no field of that name.
So a computed `field_name` may collide with a parsed key and is *not* rejected
for it: on a line that carries the key, the line wins. A plugin can add to what
CLV can be asked; it can never change what a line said.

**Return a string, or `None`.** `None` means "this entry has no such field",
which is a different outcome from "did not match": the entry is hidden and
counted into `hidden_missing_field`, and the UI explains it by name. Returning
anything that is not a string takes the plugin out of service with a message
saying so — a number compared against a string would quietly never match.

`field_name` must be a legal query key: it starts with a letter or underscore
and uses only letters, digits, `_`, `.` and `-`. Anything else could never be
typed as a term.

### 6. WatchMatcher

Teaches CLV a kind of watch rule that is not "this pattern matched".

A watch rule carries a `kind`. The default is `"pattern"` — CLV's own, where the
rule's `pattern` field is a query in the field-query syntax. A matcher claims a
different one, and for a rule declaring it the `pattern` field stops being a
query and becomes **your parameter string**: CLV does not parse it, does not
validate it, and does not interpret it in any way.

```python
from clv.api import WatchMatcher

class Burst(WatchMatcher):
    name = "watch-alerts"
    kind = "burst"                      # "pattern" is reserved

    def matches(self, entry, rule) -> bool:
        ...                             # rule.pattern is yours to read

    def validate(self, pattern):        # optional
        return None if _OK.match(pattern) else "Looks like: oom-killer x5/60."
```

**Reachable from the UI:** `W` opens the rules dialog, which grows a `Kind`
button as soon as one matcher is installed and cycles through the kinds that
are. A viewer with no matcher installed has no such button.

#### Which kinds are legal

`kind` is matched **casefolded**, must contain no whitespace, and may not be
`pattern`. That last one is not tidiness: every watch rule ever saved already
means something under `pattern`, and a plugin redefining it would change what
those rules do. Two matchers claiming one kind is refused at load, with the
second one named — compared casefolded, because `Burst` and `burst` are one kind
to every rule that could declare either.

#### What a matcher can and cannot express

`matches` is called **once per newly arrived entry** per enabled rule of your
kind — including repeats of a line CLV has already seen, because a matcher may
be counting and the answer cache that covers a pattern rule would be wrong for
one that is.

There is no tick. A matcher is offered lines and nothing else, so:

- a **threshold** or **burst** kind works: hold your own state, keyed on
  `rule.name`, and bound it yourself — the budget measures time, not memory;
- an **absence** or **silence** kind cannot be written at all, because nothing
  arriving means `matches` is never called. `PLUGIN_TODO.md` Phase 9 listed
  "absence" as an example kind and was wrong to; the correction is recorded
  there rather than quietly dropped.

**Never raise for something the operator typed.** A malformed parameter is
`validate`'s business, and `validate` is called when they press Save, where the
complaint can be shown next to the field. Raising from `matches` disables the
plugin for the session over a rule that could have been fixed in the dialog.

### 7. WatchSink

Where a watch hit is delivered, besides the toast.

```python
from clv.api import WatchSink

class AlertFile(WatchSink):
    name = "watch-alerts"
    wants_entries = False               # the default; see below

    def deliver(self, name, count, context, entries=()):
        ...
```

**You are fed the result of rate limiting, never the raw hits.** A rule matching
five hundred lines inside one window reaches you **once**, with `count=500`.
That is not a convenience. A rule matching every line is the behaviour that
makes people switch a feature like this off, and a sink that could bypass the
coalescing would be able to do it to somebody else's inbox rather than to their
own status bar.

**CLV's own toast is a sink too.** It is in the same list, marked as running
inline because it paints; there is one delivery path rather than a plugin path
bolted beside a core one.

#### You do not run on the event loop

Every sink gets a thread of its own. Blocking on a socket is allowed here in a
way it is allowed nowhere else in a plugin — and a sink that blocks holds up
nothing but itself, because one shared worker would have let one sick sink
starve every healthy one.

There is a deadline. A sink that has not returned from one `deliver` within
`plugin_sink_timeout_ms` (default 5000, `0` for none) is taken out of service,
named in the `P` dialog and fed nothing further.

**Being abandoned is not being stopped.** CLV cannot kill a thread, so the call
goes on running for as long as it likes; all the deadline buys is that CLV stops
waiting for it and stops queueing behind it. A sink that talks to the network
should therefore set **its own** timeouts rather than relying on this one. The
[isolation host](#isolation) is what makes a hang genuinely stoppable — a sink
that declared `isolated = True` runs where CLV can kill it — and a thread never
will be.

#### What a sink is given, and what it is not

A rule name and a count. That is the default and it is deliberately meagre:

```python
def deliver(self, name, count, context, entries=()):
    # name  -- the rule's name, as the operator typed it
    # count -- how many lines matched inside the window
    # context -- the FilterContext: the active filter spec and the open source
    # entries -- empty, unless you declared wants_entries
```

Set `wants_entries = True` and you receive a bounded sample of the matching
lines — at most `SINK_SAMPLE_LIMIT` of them, most recent last, alongside the
**true** count, which is not capped. Nothing is retained at all unless some
installed sink has asked for it.

That declaration is shown to the operator: a plugin supplying such a sink is
flagged in the `P` dialog, with the sentence "reads your log lines and delivers
them wherever it is configured to send them" on its row. This is the right price
for the capability — and a sink that does not need it should not pay it. A name
and a count are the whole of what an alert record usually needs.

#### Egress is the operator's decision, not yours

A sink that leaves the machine is the strongest case in this file for the
consent convention, and the pattern is the one the journal provider already
follows for *reading*: ship inert, read your destination from your own
`[plugin:<name>]` section, and deliver nothing until it is set.

```python
import json
import urllib.request
from clv.api import WatchSink, setting_bool

class Webhook(WatchSink):
    """POSTs a rule name and a count. Inert until an endpoint is configured."""

    name = "webhook"
    # Deliberately not set: this sink has no need of the lines themselves, and
    # a webhook that shipped log content by default would be the clearest
    # possible example of a plugin deciding something that is not its to decide.
    wants_entries = False

    def configure(self, settings):
        self._url = settings.get("endpoint", "").strip()
        self._timeout = float(settings.get("timeout", "5"))

    def deliver(self, name, count, context, entries=()):
        if not self._url:
            return                      # no endpoint, no egress, no complaint
        payload = json.dumps({"rule": name, "count": count}).encode()
        request = urllib.request.Request(
            self._url, data=payload, headers={"Content-Type": "application/json"}
        )
        # Your own timeout, not CLV's: CLV's deadline stops it waiting for you,
        # it does not stop you waiting for the network.
        urllib.request.urlopen(request, timeout=self._timeout).close()
```

```ini
[log_viewer]
plugins = webhook

[plugin:webhook]
endpoint = https://hooks.example.invalid/clv
timeout = 5
```

Nothing here is enforced. CLV does not inspect what a sink imports or intercept
what it sends — see [Trust model](#trust-model). What CLV does is make the
capability **visible** (the flag in `P`), make it **rate-limited** (you cannot be
used for a storm), and make it **stoppable enough** (the deadline). The decision
to install a plugin that posts anywhere is the operator's, made with those three
facts in hand.

### 8. ClusterRule

One more volatile token for the repeat clusterer (`c`) to normalise out.

Clustering folds lines that read the same once their volatile tokens are
replaced by placeholders. CLV's nine rules — quoted strings, timestamps, UUIDs,
IPv6, IPv4, hex, paths, floats, integers — are fixed and ordered. A log whose
noisy token is none of those gets one cluster per line.

```python
from clv.api import ClusterRule

class EmailAddress(ClusterRule):
    name = "email-address"
    pattern = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")   # or the string
    placeholder = "<email>"
```

**There is no method to implement.** You declare the pattern and the
placeholder; CLV performs the substitution. That is what keeps a seam which
runs per line free of any third-party call — and it is why every mistake you
can make here is caught **when the plugin loads**, with a message, rather than
on a line in a running pane.

What is refused at load, and why each one is invisible at runtime:

| Refused | Because |
| --- | --- |
| no `pattern`, or one that does not compile | it would normalise nothing; the `re.error` is reported as written |
| a `pattern` that matches the **empty string** | `\d*` matches at every position, so the placeholder would be written between every character of every line |
| a `placeholder` containing a **digit** | plugin rules run after CLV's own, so a later one matching numbers would rewrite what this one produced — the reason every built-in placeholder is digit-free |
| a `placeholder` containing a **backslash** | it is a `re.sub` replacement template: a group reference would splice in what the pattern captured, or raise |

An **empty** placeholder is legal and deletes the token. Stripping decoration —
an ANSI colour run — is a real rule, and the reason this is not treated as an
oversight.

#### You are handed the line CLV has already normalised

Plugin rules run **after every built-in**, in `priority` order among
themselves. Appending is the only position that cannot break the built-in
order, in which each rule runs on what the previous left behind — and `priority`
cannot move a plugin rule ahead of CLV's own nine.

It is also the trap. The obvious Kubernetes rule never fires:

```python
pattern = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b")   # matches nothing
```

By the time it runs, `api-7d9f8b6c4-x2n9q` is already `api-<hex>-x2n9q`: the
hex rule matched the middle hash first. The same happens to `\x1b\[[0-9;]*m`,
because the integer rule has turned `\x1b[31m` into `\x1b[<int>m`. Press `c`
and look at what a line already collapses to before writing a rule for it, and
write the pattern against *that*. `clv/examples/cluster_rules.py` works through
both cases.

#### Cost: once per distinct line

`normalise` is memoised, so your pattern runs once per **distinct** line
however often the pane re-renders. A buffer of repeats — which is the buffer
clustering is for — costs one substitution per shape. The cluster budget
measures what is left, which is the work that is actually new.

### 9. ShapeContributor

An extra component of the key two entries must share to cluster together.

A shape is the source, the level and the normalised message. Field *values* are
deliberately not in it — a request ID differing between two lines is exactly
what must not split a cluster — but sometimes a value is precisely what should.

```python
from clv.api import ShapeContributor

class ByUnit(ShapeContributor):
    name = "cluster-by-unit"

    def contribute(self, entry) -> str:
        return entry.fields.get("unit", "")
```

`Started` from `nginx.service` and `Started` from `postgres.service` then stay
two clusters, and nothing about what a shape already means has changed.

- **A contributor that returns a constant is a no-op.** It can only ever split
  clusters further, never merge two that were apart — which is what makes
  adding one safe.
- **`""` adds nothing at all**, not an empty component. It is how you answer
  for the entries you know about and stay out of the way for the rest, and it
  is what a contributor taken out of service falls back to, so switching one
  off leaves shapes byte-identical to a build without it.
- **Your answer must be a pure function of the entry.** CLV clusters the
  filtered set in one pass and folds tailed lines into that same stream one at
  a time; a contribution that changed between those two calls would make the
  incremental path and a full recompute disagree, and nothing would report it.
- **Return a string.** Anything else takes the plugin out of service: the value
  is composed into the key, and a repr carrying an address would give every
  entry a shape of its own — clustering switched off, with nothing on screen to
  say so.

#### Cost: per entry, per render, memoised by nothing

This is the expensive half of the clustering seam. The shape cache is keyed on
the line's text, which cannot remember an answer that depends on the whole
entry, so `contribute` runs for every entry on every render — every keystroke
in the query box, over the whole buffer. Read a field. Do not compute one.

#### Switching either off takes effect immediately

Both are installed **enabled-only**, which is where clustering differs from the
query and watch seams. A `QueryOperator`'s token and a `WatchMatcher`'s kind
stay claimed while their plugin is switched off, because a saved query or rule
means something under them. Nothing saved names a cluster rule, so there is
nothing to reserve: switch one off in `P` and the shapes go back to what they
were, on the next render. CLV clears the shape cache as part of installing a
rule set, which is what makes "the next render" true rather than "the next line
nothing has shaped before".

### 10. TimelineAnnotation

Marks on the timeline's axis: not how much happened, but what else did.

The histogram (`b`) answers *when did this start*. An annotation answers *what
was happening then*, which is the difference between a spike and a spike with a
cause.

```python
from clv.api import TimelineAnnotation

class Deploys(TimelineAnnotation):
    name = "deploy-marks"

    def annotations(self, window):
        for moment, version in self._deploys:      # read at setup()
            yield (moment, f"deploy {version}", "notice")
```

The bucket a mark lands in is drawn **underlined and in the mark's own severity
colour**, its label goes in the caption when that bucket is selected, and
`shift+←` / `shift+→` step between marked buckets. The glyph itself is not
replaced: the volume has to survive the mark, and the bar has to stay two rows
tall whatever is installed.

- **You are called on the event loop, and you may not do I/O.** This runs inside
  the render that draws the bar, so a provider that opens a socket blocks the
  pane — and it is charged against `plugin_time_budget_ms`, so one that does it
  anyway strikes out and is switched off. Fetch in `setup()`, or on a thread of
  your own, and answer this call from memory.
- **You are asked once per window, not once per render.** The answer is cached
  against the window on screen, so typing in the query box does not re-ask you.
  The cache is cleared whenever the plugin registry changes, which is how
  switching a provider off takes its marks off the bar.
- **Return what you have; CLV drops what does not fit.** A moment outside the
  window is not drawn, so filtering by `window` yourself is an optimisation and
  not a requirement. At most 200 marks are kept for one grid.
- **A label goes in a caption.** One row, under a bar as wide as the pane. Keep
  it to a few words; several marks in one bucket caption as the first plus
  `(+N more)`.
- **The level is a severity**, normalised exactly as a parsed line's is, and it
  is what colours the mark. Return `None` for something that is not a severity —
  a maintenance window is not a warning.
- **Answer in the right shape or not at all.** Anything that is not a
  `(datetime, str, level|None)` triple takes the plugin out of service, and the
  whole call is dropped rather than the offending mark: a provider that cannot
  say what shape its marks are is one whose good marks cannot be trusted to mean
  what their labels say.

### 11. TimelineMetric

What a timeline bucket measures, when counting entries is the wrong unit.

A hundred lines is not a hundred kilobytes. On a log where the interesting
quantity is bytes, duration or retries, a histogram of line counts is a
histogram of the wrong thing.

```python
from clv.api import TimelineMetric

class BytesRead(TimelineMetric):
    name = "bytes-metric"
    metric_name = "bytes read"
    unit = "B"

    def value(self, entry):
        return float(entry.fields.get("bytes", 0) or 0)
```

The bar is then scaled by the metric and the caption **leads with it and names
this plugin** — because a bar whose heights mean bytes looks exactly like a bar
whose heights mean lines, and the caption is the only thing that says which.

#### Your metric must be foldable, and that is why this interface is so thin

`Timeline.extend` folds a newly tailed line into its bucket by arithmetic, which
is what makes tailing cost what arrived rather than what is buffered. A sum
survives that. A median does not, and neither does a percentile, a distinct
count, or an average that has forgotten its denominator.

So you declare a per-entry number and **CLV does the summing**. There is no
`aggregate()` to implement, which makes a non-foldable metric *unexpressible*
rather than broken — the alternative would produce a bar that was right on the
first render and silently wrong on every line tailed after it.

- **`None` means "not measured by me"** and contributes nothing, so a metric can
  answer for the entries it understands and stay out of the way for the rest.
- **An entry with no timestamp is never passed to you.** It has no bucket, it is
  reported in `undated`, and a metric does not get to change that.
- **`count` never stops meaning entries.** A metric changes what the bar is
  scaled by and what the caption leads with; nothing downstream that reads a
  bucket's count has to know one is installed.
- **Return a finite number or `None`.** A string, an infinity or a NaN takes the
  plugin out of service, because each one makes the *scale* meaningless rather
  than one bucket wrong. A bool is refused too, with a message: `1.0 if ... else
  0.0` is how you count a subset, and it says so where a `True` would not.
- **`metric_name` is required.** A plugin that cannot name its metric is refused
  at load — the caption would have nothing to say.

#### One metric at a time

Two enabled metrics is a conflict, and CLV resolves it by `priority` and reports
that it did: the winner runs, the loser is named in the `P` dialog with the
plugin that beat it, and switching the winner off promotes the runner-up on the
next render. Losing a tie-break is **not a fault** — the loser stays `loaded`,
nothing is marked failed, and no operator action is required unless they wanted
the other one.

#### Cost: per entry, per rebuild, memoised by nothing

The same shape as a `ShapeContributor`, on the same ceiling: `value` runs for
every entry every time the bar is rebuilt, which is every keystroke in the query
box while `b` is open. Read a field. Do not compute one.

#### Switching either off takes effect immediately

Both are installed **enabled-only**, like the clustering seam and for the same
reason: no saved view, watch rule or session names a metric or a mark, so there
is nothing to reserve. Switch one off in `P` and the bar goes back to counting
entries on the next render, with the annotation cache cleared as part of
installing the new set.

### 12. Exporter

Saves or transmits the entries the filters kept.

**Reachable from the UI:** `Ctrl+E` opens the export dialog, which lists every
loaded `Exporter` below CLV's three built-in formats (JSON Lines, CSV and plain
text — those live in `clv.services.export`, not here, so that a built-in cannot
fail to load).

`export` receives the sequence of `LogEntry` objects that passed the plugin
stages and the user's filters, plus the `FilterContext`, and returns an
`ExportResult`. Three points follow from that:

- The sequence is the **whole filtered set**, not the `_show_lines` window the
  pane happens to be showing. Do not assume it is small.
- **By default there is no destination argument.** An exporter picks its own
  path — or sends the entries somewhere that is not a path at all — and reports
  what it did as `ExportResult.destination`; the dialog disables its path input
  and says so. Confine writes to somewhere the operator would expect, and never
  to a temp or cache directory — log content is sensitive.
- Raising is survivable but visible: the exception is recorded in
  `PluginRegistry.errors`, surfaced as a notification and shown in the Advanced
  drawer. Returning `ExportResult(ok=False, detail=...)` is the way to report a
  failure you expected.

```python
import json
from pathlib import Path
from clv.api import Exporter, ExportResult

class JsonExporter(Exporter):
    name = "JSON Exporter"

    def export(self, entries, context):
        destination = Path("export.json")
        destination.write_text(
            json.dumps([entry.raw for entry in entries], indent=2),
            encoding="utf-8",
        )
        return ExportResult(ok=True, detail=f"{len(entries)} lines", destination=destination)
```

#### Asking for the operator's destination

An exporter that writes a **file** usually wants the path the operator just
typed, and until API 1.0 it could not have one: every plugin choice was marked
as supplying its own destination, so the dialog's path input was disabled and an
exporter had to invent a location nobody had agreed to.

Set `wants_path` and the input is enabled, the suggested filename takes your
`suggested_extension`, the overwrite confirmation applies as it does to a
built-in format, and the chosen path arrives as the keyword-only `destination`:

```python
class NdjsonExporter(Exporter):
    name = "NDJSON"
    wants_path = True
    suggested_extension = "ndjson"

    def export(self, entries, context, *, destination=None):
        destination.write_text(
            "\n".join(json.dumps({"raw": e.raw}) for e in entries),
            encoding="utf-8",
        )
        return ExportResult(ok=True, detail=f"{len(entries)} lines", destination=destination)
```

`destination` is passed **only** when `wants_path` is set, so an exporter
written as `export(self, entries, context)` against the original interface is
called exactly as it always was. That is the compatibility rule for this
attribute and it is pinned by a test.

### 13. Command

A named action an operator can invoke, optionally bound to a key. The only seam
in CLV that a plugin does not wait to be *consulted* through: a format is asked
when a line arrives, a metric when a bucket is drawn, a matcher when a rule is
tested. A command runs because somebody asked for it.

**Reachable from the UI:** `C` opens the command list, which names every loaded
command and the key it got. A command that declared a key is also reachable from
that key directly. Both are listed in the `?` overlay under **Plugins**.

```python
from clv.api import Command, CommandContext

class ShowErrors(Command):
    name = "show-errors"
    command_name = "show-errors"     # what CLV dispatches on
    title = "Show errors only"       # what ? and C print
    key = "e"                        # optional

    def run(self, context: CommandContext):
        context.request_query("level>=error")
```

Four declarations, and two of them are refused at load if they are missing:
`command_name`, because nothing could address the command without it, and
`title`, because the overlay and the `C` dialog would have a blank row. A
duplicate `command_name` is refused too, folded — the second would load cleanly
and then never be reached, which from the outside is a plugin that does nothing.

#### You run on the event loop, synchronously

The same terms a plugin `Exporter` has always run on, and the reason is the same:
a command is coarse-grained and invoked on demand, so it is called where it was
asked for rather than shipped off somewhere.

Nothing else in CLV happens while `run` is executing. A command that blocks on a
socket freezes the pane, and **there is no budget that can save you** — the six
render-path ceilings work by measuring a pass and disabling the plugin before
the *next* one, and CLV cannot interrupt a call it is inside. Do the slow part
on a thread of your own and answer from what that thread last stored, exactly as
an annotation provider does.

This is the one kind of failure CLV cannot contain in its own process, and it
is why `Command` is one of the four [isolable](#isolation) kinds: a command that
declares `isolated = True` runs in a child that *can* be killed, and the
ceiling it is held to is `plugin_host_timeout_ms`.

Raising is survivable and visible. The exception disables the command for the
session through the same mechanism a raising `FilterStage` goes through — its
key stops working, the row in `P` says why, and **Re-enable** is the way back.

#### What a command is handed, and how it answers

`CommandContext` is data in both directions, and the second half is the part
worth understanding.

| Member | What it is |
| --- | --- |
| `entry` | The selected line, or `None` |
| `entries` | The **whole filtered set**, not the window the pane is showing |
| `spec` | The `FilterSpec` that produced `entries` |
| `source` | The open source, or `None` |
| `values` | The live panel form; empty outside a panel |

There is no callable into CLV here, and that is deliberate rather than
minimalist. The obvious way to let a command raise a toast is to hand it a
bound method — but a bound method carries `__self__`, a closure carries
`__closure__`, and a context built either way has a live route to the running
application for anyone who looks. So every outward call **queues**:

```python
context.notify("Showing errors and worse")
context.request_query("level>=error")
context.request_source(ref)
context.request_view("errors-last-hour")
```

Each appends to `context.requests`, and CLV drains the queue after `run`
returns and performs each one itself. A command cannot touch the screen, the
registry, the session or the store. It says what it would like to happen, and
CLV decides.

Two consequences worth knowing:

- **A long-running command cannot report progress part-way through.** Its
  messages arrive together, the moment it returns.
- **Every member of the context is encodable**, which is what makes
  [isolation](#isolation) possible for this kind at all — including the outbox,
  which is filled in the child and drained here.

#### Asking is asking

`request_query` is refused if the query does not parse — with the query
parser's own message, so a plugin's bad query reads exactly as an operator's
would. `request_source` is refused for a ref CLV is not currently offering: a
command can *move* to a source, it cannot conjure one. `request_view` is refused
for a name that is not a saved view, and a view whose plugin is missing still
degrades by preserve-disable-explain — it is refused with what it needs named,
never applied with some of its filters silently meaning something else.

A refused request is reported against your plugin and shown to the operator. It
does **not** disable you: being wrong about the state of the world is not being
broken, and the view you asked for may exist again after the next rescan.

The last request of each kind wins, and there is one re-render at the end. Two
`request_query` calls mean the second one.

#### Your key is hidden, and it may be refused

Whatever you set, your binding is installed with `show=False`. The footer fills
from the left and drops from the right, its ordering is hand-tuned against an
80-column floor, and a plugin cannot know what its entry would push off. Setting
`show = True` is **refused with a reason** rather than honoured — it exists so
that an author who wants the footer reads why they may not have it instead of
wondering why nothing happened. `?` is how hidden bindings are found, which is
already how CLV handles its own overflow.

A key is refused when it is one of CLV's — including the ones bound on the log
pane and the timeline bar, and Textual's command palette — or when another
command claimed it first. First is by `priority`, then by name, so which command
wins is a fact about what is installed rather than about what the filesystem
listed first. Either way you are told, the built-in keeps working, and **you
stay invocable by name from `C`**, which is the whole reason that dialog exists.

Prefer letting the operator choose. A key is a scarce shared resource and there
are only so many single characters; reading yours from your own
`[plugin:<name>]` section is the neighbourly version of asking for one.

#### Drawing: a panel is described, not built

Return a `Panel` from `run` to open a modal. A panel is a title and a tuple of
`Control` descriptions; CLV builds the widgets, owns the CSS and owns the
breakpoint behaviour.

**Plugins ship no CSS.** That is a rule with a reason: CLV's responsive
breakpoints and its 80-column floor are CLV's, and every breakpoint test in the
suite stays unconditional on what happens to be installed. A plugin that could
hand over a widget — or style one — would make that untrue for everyone.

A modal is where this costs you least. Full-screen means no interaction with the
main layout at all, which is exactly why it is the one place a plugin gets real
room.

```python
from clv.api import Control, Panel

def run(self, context):
    return Panel(
        title="Save this view",
        controls=(
            Control("static", "summary", label=f"{len(context.entries)} lines match."),
            Control("input", "path", label="Write to", placeholder="/tmp/view.txt"),
            Control("switch", "raw", label="Raw lines", value=False),
            Control("button", "save", label="Save"),
        ),
    )
```

Six kinds, and the list is short on purpose — every one of them is something
CLV already styles and already tests at 80 columns:

| `kind` | What it is | `value` |
| --- | --- | --- |
| `label` | A heading for what follows | — |
| `static` | A line of text | — |
| `switch` | On or off | `bool` |
| `input` | A line of text, with an optional `placeholder` | `str` |
| `select` | One of `options`, each a `(value, label)` pair | the selected *value* |
| `button` | Pressing it calls `on_control` with `True` | — |

`id` must be non-empty and unique within the panel: `values` is keyed on it, and
two controls sharing one is a form that silently loses a field. A panel CLV
cannot draw — a bad kind, a duplicate id, a select with no options, more than
`MAX_PANEL_CONTROLS` controls — takes the plugin out of service with a message
saying which, because a half-drawn form is worse than none.

#### `on_control` is on a keystroke path

```python
def on_control(self, control_id, value, context):
    if control_id != "save":
        return None
    write(context.values["path"], context.entries)
    return Panel(dismiss=True)
```

Return a `Panel` to redraw, `Panel(dismiss=True)` to close, and `None` — the
default — to leave the screen alone. `context.values` holds the whole live form
keyed by `id`, so there is no need to track what you have been told so far;
`label`, `static` and `button` controls are not in it, because none of them
holds a value.

You are called for **every character typed into an input**, so this is charged
against `plugin_time_budget_ms` like anything else CLV calls from a render.
Three consecutive passes over the ceiling take you out of service and close the
panel — a form whose callbacks no longer run is a form that lies about what
pressing its buttons will do. Do the work when a button is pressed, not on the
way past.

You are **not** called for a value that did not change, including as the panel
first mounts.

---

## Plugin Discovery

Four stages, searched in this order. **The first to claim a name wins, and the
loser is reported** — never silently dropped, because two plugins quietly
resolving by load order is the defect this ordering exists to prevent.

1. **`CLV_PLUGIN_PATH`** — extra roots, `os.pathsep`-separated. For development
   and for CLV's own tests: it is how a plugin runs from where it is being
   edited, and how the test suite gets a plugin root without writing into the
   source tree. Not documented to users as a way to install anything.
2. **The user plugin directory**, `~/.config/clv/plugins/`. Created beside
   `settings.conf` on first run, with a `README.txt` in it. **Governed by the
   enable-list** (below).
3. **Bundled drop-ins** — modules directly under `clv/plugins/` and in the
   `sources/`, `filters/` and `exporters/` subpackages.
4. **Entry points** — installed distributions advertising the `clv.plugins`
   entry point group.

Modules whose name starts with `_` are skipped at every stage.

### The enable-list

**A file in the user plugin directory is not run because it is there.** CLV
records its name and does nothing else — it is not imported — until the name
appears in `settings.conf`:

```ini
[log_viewer]
plugins = redact_secrets, nginx_format
```

Installing a plugin and running a plugin are deliberately two decisions. A
directory that runs whatever is dropped into it is a directory that anything
able to write to `$HOME` can run code from, and "enable everything here" is
exactly the behaviour that would make it one.

Names are the module name without `.py`, comma separated, and are matched
**case-insensitively**: a settings file is operator prose, and `Redact` where
the file is `redact.py` is a typo class rather than an intent. Whitespace,
trailing commas and duplicates are tolerated. A name that could not be a module
name — `my-plugin`, `foo.bar` — is dropped and reported on its own, and the
rest of the list still loads. A name that is listed but not present in any root
is reported by name, so a typo says so rather than doing nothing.

**Bundled drop-ins ignore the enable-list.** They shipped with CLV, and the
operator's trust in them is the trust they already placed in CLV. The journald
provider's `enable_journald` opt-in is a separate and unrelated thing: it gates
what `discover()` offers, not whether the module loads.

### Per-plugin configuration

A plugin that needs settings of its own gets a section named after it, and the
name is the same one the enable-list uses:

```ini
[log_viewer]
plugins = redact_secrets

[plugin:redact_secrets]
patterns = password, api_key, token
replacement = ******
```

CLV parses the section and hands it over; it never interprets it. What a key
means is the plugin's business, and a validator in `config.py` would be CLV
guessing at a schema it does not own.

The section reaches the plugin through the optional `configure()` hook:

```python
from clv.api import FilterStage, setting_list

class Redact(FilterStage):
    name = "redact-secrets"

    def configure(self, settings):
        self._settings = settings

    @property
    def patterns(self):
        return setting_list(self._settings, "patterns")
```

Four things about `settings` are worth knowing before writing against it.

**It is a read-only view of a mapping CLV owns, not a copy.** When CLV re-reads
the settings file — the operator pressed `Ctrl+R`, or flipped a switch in the
Advanced drawer — the values behind the view change and the plugin sees the new
ones without being called again. So keep the mapping and read through it, as
above, rather than copying values out in `configure()`. That is what lets the
journal provider honour the drawer's switch without a restart.

**It is empty when the operator wrote no section**, which is the common case.
An empty mapping is not an error and must not be treated as one; every key a
plugin reads needs a default.

**The values are the raw strings `configparser` read.** Use `setting_bool` and
`setting_list` from `clv.api` rather than writing the coercions again — CLV and
a plugin disagreeing about whether `yes` is true is a bug an operator has no
way to see. Both are total: an unreadable value is the default, never a raise.

**A section that configures nothing is reported.** A `[plugin:x]` for a plugin
that is installed but not enabled says so; one for a name that is nowhere says
that instead. The two need different answers, so they get different messages.

Section names are validated exactly as enable-list names are — a name that
could not be a module name is dropped and reported, and the sections around it
still load. `[plugin:]` with no name, and a duplicated section, are each
reported and skipped.

**One key CLV reads for itself: `isolated`.** Everything else in the section is
the plugin's business, and this is the exception — `isolated = true` asks CLV to
run that plugin in a child process it can kill, and it is answered **before the
module is imported**, which is the whole reason it is a settings key and not
only a class attribute. See [Isolation](#isolation). It is still handed to the
plugin with the rest of the section; a plugin that wants its own `isolated` key
for something else will be confusing an operator, not colliding with CLV.

**One legacy key, folded in rather than renamed.** `enable_journald` lives in
`[log_viewer]`, is in every operator's settings file, and is what the Advanced
drawer's switch writes. It is read into `[plugin:journald]` as `enabled`, so
the journal provider reads one place while nobody's settings file changes. A
section that sets `enabled` itself wins.

### Lifecycle

Three optional hooks, all defaulting to doing nothing, so a plugin that
implements none of them behaves exactly as it did before they existed.

| Hook | When | Guarantee |
|------|------|-----------|
| `configure(settings)` | Once, straight after instantiation | Before `setup()`, and before anything asks the plugin for anything |
| `setup()` | Once, after every plugin has loaded and been configured | Before first use |
| `teardown()` | Once, at shutdown | After CLV has closed its readers, before the session is persisted |

All three run **in the child** for an [isolated](#isolation) plugin, and its
`setup()` runs when its host starts — which for a plugin nobody has called yet
is never. Nothing else about the order changes.


`teardown()` runs after the readers so a plugin cannot resurrect a source on
its way out, and before the session is persisted so a plugin that fails on exit
still leaves the operator's session intact.

**Failure in any of the three disables the plugin for the session** and records
the reason once — the same mechanism a raising `apply()` has used since the
loader was made correct, not a second one with its own semantics. A plugin that
fails `configure()` is never `setup()`; one that fails `setup()` is never
`teardown()`, because calling `teardown()` on a half-built object is how a
shutdown path acquires bugs of its own.

**An exception in `teardown()` is contained. A hang is not.** CLV records the
exception and carries on, but a `teardown()` that blocks forever blocks exit,
and nothing here stops it. The [budget](#the-budget) bounds a stage's time on
the render path and deliberately does not reach the lifecycle hooks: a hook that
never returns cannot be timed out from inside the process it is hanging, which
needs a process CLV can kill. For an [isolated](#isolation) plugin that is
exactly what happens: `teardown()` runs in the child, is bounded by
`plugin_host_timeout_ms`, and a child that will not leave is terminated and then
killed. In-process, a plugin author is being trusted not to block on the way
out, and that is a convention rather than a protection, like every other one on
this page.

**`setup()` and `teardown()` are session lifecycle, not the enable switch.**
Turning a plugin off in the `P` dialog and back on does not re-run `setup()`.

**Plugin state is the plugin's own problem.** CLV's session file has a closed
set of fields, and it is closed deliberately: every field in it carries an
argument about whether recording it leaks what somebody was reading. A plugin
that needs to remember something across runs writes its own file under its own
directory and owns the same question about its contents.

### Managing what is installed

`P` in the viewer — or the **Plugins** button in the Advanced drawer — lists
every plugin CLV found, one row per **installable unit**: the module, not the
plugin object inside it, because that is what an operator installs, names and
deletes. A module exporting three stages is one row saying `filter`.

Five states:

| State | Meaning |
| --- | --- |
| `loaded` | Imported and in service. |
| `not enabled` | Present, and not named in `plugins` — or switched off from the dialog. Not a fault, and not reported as one. |
| `failed` | It raised at import or at runtime, or CLV could not read it. The row carries the recorded message in full. |
| `incompatible` | An unsatisfied `requires_clv` or `requires_api`. The row names the constraint *and* the running version. |
| `isolated` | Every plugin this module loaded runs in a child process CLV can stop. A module with one isolated plugin and one in-process reads `loaded`, and says how many are contained in its detail — see [Isolation](#isolation). |

Two asymmetries the dialog states as it is used, because neither is guessable:

- **Enabling something CLV has not imported needs a restart.** Loading is
  import-time and single-shot; there is no hot reload. The name is written to
  `settings.conf` at once and the plugin loads next launch.
- **Disabling a bundled plugin lasts for the session.** The enable-list governs
  the user directory only, so a bundled drop-in has no name in it to remove. It
  returns on restart. A *user* plugin's disable is written to `settings.conf`
  and also takes effect immediately.

A plugin a fault took out of service can be put back with `r`. Doing so
discards the recorded failure as well as clearing the disable — left on the
record, it would keep the row reading `failed`, and the next genuine failure
would collapse into it as a repeat of something already dealt with rather than
be reported as news.

Nothing is written until the dialog closes, so `Esc` cancels for real and one
confirm is one write to a file full of the operator's comments.

### Shadowing

A user plugin may take a name a bundled drop-in uses, which is how a plugin
*replaces* a shipped one — but **only if it is enabled**. An unlisted file
shadows nothing, because it is never imported and so cannot displace anything;
this is what stops a dropped file from changing CLV's behaviour without being
named. The shadowed plugin is reported with the origin that won.

Two bundled subpackages may share a module basename, exactly as they always
could: `sources/x.py` and `filters/x.py` are two different modules and neither
shadows the other.

### How a module says what it exports

Three strategies, tried in order. The first that produces anything wins.

1. **`register()`** — returns one plugin, or any iterable of them (a list, a
   tuple, a set, or a generator).

   ```python
   def register():
       return MySource()
   ```

   Returning `None` or an empty list is a *deliberate* decline — it is how a
   plugin says "not on this machine", the way the shipped `journald` provider
   would if its tool were missing. It is never reported as a problem.

2. **`__all__`** — a list of plugin class or instance names.

   ```python
   __all__ = ["MySource"]
   ```

3. **A namespace scan.** If the module defines neither of the above, CLV looks
   for concrete `Plugin` subclasses **that module itself defined**. A class
   imported from elsewhere is not collected, and neither is a subclass that has
   not implemented its interface method — that one would otherwise be
   instantiated into a confusing `TypeError`.

A module that defines no plugin and says nothing about why is **reported**:
`defines no plugin — add register() or __all__`. Writing the class and
forgetting the boilerplate is the most likely first mistake, and it used to
produce zero plugins, zero errors and no clue.

### What an entry point may point at

Four legal target shapes, all handled:

| Target | Example |
| --- | --- |
| A module | `mypkg.plugin` — its `register()` / `__all__` / namespace is read |
| A plugin class | `mypkg:MyFilter` — CLV instantiates it |
| A zero-argument factory | `mypkg:make_plugin` — called once |
| A plugin instance | `mypkg:INSTANCE` |

A callable that requires arguments is reported as such rather than rejected with
a message about interfaces.

---

## Ordering

Every plugin carries a `priority`, and every ordered registry CLV keeps runs in
ascending order of it:

```python
class Redact(FilterStage):
    name = "redact-secrets"
    priority = 50          # sees the line before anything has rewritten it
```

The default is **100**, with room deliberately left on both sides. A stage that
must see a line before anything has touched it — an audit trail, a metric
counter — takes a low number; one that must see the final text takes a high one.

**Ties are broken by name**, casefolded. That matters more than it looks: before
it, two stages that both took the default composed in whatever order
`pkgutil.iter_modules` happened to list them, so the same two plugins could
redact-then-rewrite on one machine and rewrite-then-redact on another. Name
order is not meaningful, but it is *predictable*, which is what an author needs
to design around.

The order is settled once, when plugins are loaded, and does not change for the
session. Enabling or disabling a plugin from the `P` dialog changes whether it
runs, never where.

**One registry uses `priority` to decide rather than to order.** A timeline
bucket measures one thing, so two enabled `TimelineMetric` plugins are a
conflict rather than a sequence: the lowest number wins, the other is named in
`P` with the plugin that beat it, and switching the winner off promotes it.
Everywhere else a low number means *first*, not *instead* — this is the one
place the two differ, and it is called out because an author reading the
paragraph above would have no reason to expect it.

---

## Saved views, watch rules and a missing plugin

A `QueryOperator` or `ComputedField` is the first kind of plugin whose absence
can change what a **saved** thing *means*, and that is a different problem from
a plugin that is merely not there.

`host~^web` without the operator is not a syntax error. The token is unknown, so
nothing in the string is recognised as a term, the whole query falls through to
the regex half, and it matches lines containing the literal text `host~^web` —
a different query that happens to parse. A saved view that quietly did that
would be worse than one that refused.

A watch rule has a second way to lose its meaning, and it is worse. A rule
declaring `kind = "burst"` whose matcher is gone would fall back to the pattern
path, and `oom-killer x5/60` is a perfectly good query — so the rule would start
matching lines containing that literal text instead of doing nothing. Same
failure, one level up.

So a `SavedView` and a `WatchRule` each carry `requires`: the plugins their
query depends on, recorded **when the view or rule is saved** and never
recomputed afterwards. Recomputing while a plugin was missing would erase the
record that marks it unusable, which is why only the record an operator just
typed is ever stamped.

**There are two absences and they are reported differently.**

| State | What happens | Where the operator sees it |
| --- | --- | --- |
| The plugin is **not installed** | The view or rule is kept byte-intact, marked unusable, and named with the plugin it needs. A view refuses to apply; a rule never matches. | `⚠ needs the 'x' plugin, which is not installed` — on the tree row, in the view picker, in the rules dialog |
| The plugin is installed but **switched off** (a fault, the time budget, or the `P` dialog) | Its token stays reserved and the query reports it. The saved record is untouched and still applicable the moment the plugin is back. | The query bar's validation line: `~ needs the 'x' plugin, which is not in service` |
| A watch rule's **`kind`** names a matcher nothing provides | The rule is kept byte-intact, never matches, and is named with the kind. It is reported by *kind* and not by plugin on purpose: the kind is the contract and a plugin is one implementation of it, so a rule written against one `burst` matcher and opened where a different one is installed still runs. | `⚠ needs the 'burst' rule kind, which no installed plugin provides` — in the rules dialog |

Collapsing the two would mean an operator who switched a plugin off for a minute
found their saved views marked broken; keeping the token reserved in the second
case is what stops the query silently becoming a regex in the meantime.

**Nothing is ever rewritten.** A state file written before `requires` existed
loads with an empty one, and one written before `kind` existed loads as a
`pattern` rule; a file written after either stays readable on a build without
the plugin. A view or rule is preserved, disabled and explained — in
that order, and never reinterpreted into meaning something else.

---

## Performance

CLV contains a plugin's *exceptions*. Until now it did nothing about a plugin's
*time*, and the two failures look completely different from the operator's
chair: a stage that raises is named in the `P` dialog with its traceback, while
a stage that is merely slow makes CLV look broken and says nothing at all.

### How often each kind is called

| Kind | Called | Per what |
| --- | --- | --- |
| `LogSourceProvider.discover` | on startup and on rescan | once |
| `LogSourceProvider.open` | when a source is opened | once |
| `LogFormat.parse` | **every read** | **once per unrecognised line** |
| `QueryOperator.test` | **every render** | **once per buffered entry, per term** |
| `ComputedField.value` | **every render** | **once per buffered entry whose own fields lack the key** |
| `FilterStage.apply` | **every render** | **once per buffered entry** |
| `ClusterRule` (CLV substitutes) | **every render with `c` on** | **once per distinct line, memoised** |
| `ShapeContributor.contribute` | **every render with `c` on** | **once per buffered entry, memoised by nothing** |
| `TimelineAnnotation.annotations` | when the bar's window changes | once per window, **not** per rebuild |
| `TimelineMetric.value` | **every rebuild with `b` on** | **once per buffered entry, memoised by nothing** |
| `WatchMatcher.matches` | **every poll** | **once per newly arrived entry, per rule of its kind** |
| `WatchSink.deliver` | when a rate-limit window closes | once per rule per window, **on its own thread** |
| `Exporter.export` | on `Ctrl+E` | once |
| `Command.run` | on its key, or from `C` | once, **on the event loop** |
| `Command.on_control` | while its panel is open | **once per control change, per keystroke in an input** |

The bold rows are the ones to design against, and they are bold for
different reasons. `apply` is called *often*: a render happens on every keystroke
in the query box. `parse` is called once per line, but on a source nothing
recognises that is every line of the file, arriving in one batch while the
operator waits for the pane to appear — and it runs before anything is on screen
to show for it. A render happens on every keystroke
in the query box, and `max_buffer_lines` is configurable up to **500 000** — so
a stage doing one regex match per entry at that ceiling is running half a
million regexes between one character and the next. Make the cheap rejection
first:

```python
def apply(self, entry, context):
    if "password" not in entry.raw:      # a substring scan, not a regex
        return entry
    return replace(entry, raw=_SECRET.sub("******", entry.raw))
```

The same rule in a `LogFormat`, where the rejection is the common case rather
than the exception — and compile the pattern once at class level, never inside
`parse`:

```python
def parse(self, line):
    if len(line) < 20 or line[4] != "/":   # two character tests
        return None
    ...
```

A `QueryOperator` is the one where the cheap work is usually a *cache*: the
query box re-filters the whole buffer on every keystroke, so compiling the same
pattern per entry means compiling it once per line per keypress. Cache on the
value the operator typed, and cache the failure too — a half-finished `svc~(`
should not re-raise and re-catch half a million times:

```python
@lru_cache(maxsize=256)
def _compiled(pattern):
    try:
        return re.compile(pattern)
    except re.error:
        return None            # matches nothing; never raises at the operator
```

### The budget

Every stage is charged the wall time of its own calls, accumulated per render
pass. A pass over `plugin_time_budget_ms` is a strike; a pass under it clears
the count. **Three strikes in a row and the plugin is disabled** for the
session, through the same `disable()` a raising stage goes through — so it turns
up in the `P` dialog as `failed`, with the elapsed number and the ceiling in the
message, and **Re-enable** puts it back with a clean count.

Three consecutive passes rather than one, because the first render after a
source opens pays every cold cost a plugin has, and a large paste or a loaded
machine can put a healthy stage over the line for a pass or two. A plugin that
is genuinely slow still strikes out within about a second and a half of typing.

**Seven budgets, one policy.** `LogFormat.parse` is charged against a separate
ceiling, because it is measured against a different thing: a pass on the read
path is one batch of lines from a reader's `prime` or `poll`, not one render.
The query plugins get a third instance, sharing the render path's ceiling
because they are the same kind of work on the same trigger — but settling
separately, so a slow `FilterStage` and a slow `QueryOperator` do not have their
strikes interleaved by whichever happened to be measured first. `WatchMatcher`
gets a fourth, on the **read** ceiling, because a matcher pass is one poll's
batch of newly arrived lines and not one keystroke. The clustering plugins get a
fifth, back on the render ceiling — a cluster pass is one shaping of the
filtered set, which is a keystroke and not a batch of read lines. The timeline
plugins get a sixth, also on the render ceiling, because a timeline pass is one
rebuild of the bar over that same filtered set. `Command.on_control` gets a
seventh — the odd one out, because it sweeps nothing: a pass there is one
keystroke in a modal, and it takes the render ceiling because that is what a
keystroke is. Everything else is identical in all seven: three consecutive
passes over the line, disabled, named in `P`, and reachable by **Re-enable**.

The timeline budget measures its two halves very differently, and that is
deliberate. A `TimelineMetric` is charged per entry per rebuild, memoised by
nothing — the expensive half, and the one the ceiling is really for. A
`TimelineAnnotation` is asked once per *window*, so most passes charge it
nothing at all and the passes that do are the ones where it actually went and
did something.

```ini
[log_viewer]
plugin_time_budget_ms = 250     # FilterStage.apply, the query, cluster, timeline and panel plugins
plugin_read_budget_ms = 50      # the read path: LogFormat.parse, WatchMatcher.matches
plugin_sink_timeout_ms = 5000   # not a budget -- see below
```

**`Command.run` is on none of them, and that one *is* a limitation.** A command
runs synchronously on the event loop, so the thing to bound is a call CLV is
currently inside — and a budget cannot interrupt one, it can only decline to
make the next. A command that hangs hangs CLV — unless it is
[isolated](#isolation), which is the one and only answer to this and the first
time in CLV's history a plugin can be stopped.

**A `WatchSink` is on none of them, and that is not an omission.** A sink runs
on a thread of its own, so being slow costs the pane nothing and there is no
render pass to count three of. What can actually go wrong is a call that never
comes back, and a stopwatch around a call that has already returned cannot see
that. `plugin_sink_timeout_ms` is a **deadline**, not a ceiling: one call, one
limit, out of service if it is exceeded.

Setting either to `0` turns that guard off entirely, for an operator who would rather
have a slow plugin than a disabled one. There is no per-plugin override: a
budget a plugin could raise for itself is not a budget.

**Being disabled by the budget is the intended outcome, not a bug to work
around.** If your stage cannot do its work per entry at the operator's buffer
size, the fix is a cheaper stage — not a larger ceiling.

### What is not bounded

Time inside `apply()` is measured; nothing else is. In particular:

- **`setup()`, `configure()` and `teardown()` are outside every budget.** A
  plugin that *hangs* in one of them hangs CLV, and on `teardown()` that means
  hanging exit. Exceptions there are contained; time is not. Bounding it needs a
  process CLV can kill, which is [isolation](#isolation) and not a timer.
- **`Command.run` is outside every budget, for the same reason.** It is called
  on the event loop and CLV is inside it for as long as it takes; a stopwatch
  around a call that has already returned cannot bound one that has not. Its
  `on_control` *is* charged, because that one is a keystroke and CLV gets the
  loop back between them.
- **A sink that hangs is abandoned, not killed.** The deadline stops CLV
  waiting for it and stops CLV queueing behind it. The thread goes on running
  for as long as the call takes, holding whatever it holds. This is the same
  limit as above wearing different clothes: bounding it needs a process CLV can
  kill.
- **Memory is not bounded at all.** A plugin that accumulates every entry it
  sees will exhaust the process, and nothing here will notice. A `WatchMatcher`
  holding per-rule state is the easiest place to do this by accident — the
  budget measures time, and a deque that only ever grows costs none.

Both are consequences of a plugin running in CLV's own process — see
[Trust model](#trust-model). All four have one answer, and it is the next
section: not a better timer, a process CLV can kill.

---

## Isolation

**Isolation contains crashes, hangs and leaks; it does not make an untrusted
plugin safe.** The child runs as the operator, with the operator's filesystem,
the operator's environment and the operator's credentials. Read
[Trust model](#trust-model) first and do not let this section be summarised into
its opposite.

What it buys is precisely one thing: your plugin can be *stopped*. Nothing else
in CLV can do that. A budget measures a pass that finished and declines to start
the next one; a sink's deadline stops CLV waiting for a thread it cannot end.
A child process is killable, and that is the whole of the feature.

### Asking for it

Two doors, and they are not equivalent.

```python
class Shipper(Exporter):
    name = "shipper"
    isolated = True          # the author asks
```

```ini
[plugin:shipper]
isolated = true              # the operator asks
```

The class attribute is the author saying "my work belongs somewhere it can be
killed". The settings key is the operator saying the same thing about a plugin
whose author did not — and it buys strictly more, because CLV can read it
*before* the import.

The difference is worth being exact about. For the class attribute, CLV imports
the module and constructs the plugin **in its own process** — it has to, in
order to read the attribute at all — and only the calls go to the child. So
module-level code and `__init__`, which are the first things a plugin gets to
do and both run before any interface check, are not contained. A module
isolated from `settings.conf` is never imported here at all, and both of those
run in the child with everything else.

The enable-list still applies. Isolation is not a way around consent, and a
plugin that is not named in `plugins =` is not loaded however it is isolated.

### Which kinds may ask

| Isolable | Refused |
| --- | --- |
| `Exporter`, `WatchSink`, `Command`, `TimelineAnnotation` | `LogSourceProvider`, `LogFormat`, `QueryOperator`, `ComputedField`, `FilterStage`, `ClusterRule`, `ShapeContributor`, `TimelineMetric`, `WatchMatcher` |

The four on the left are coarse-grained: called on demand, or once per rebuild.
Eight of the nine on the right are called **per entry or per line**, where a
round trip is not a slower version of the same program but a different one —
see [How often each kind is called](#how-often-each-kind-is-called) for the
numbers that make that a fact rather than an opinion.

`LogSourceProvider` is refused for its own reason, kept separate because it is
about *when* rather than *how often*: `open_reader()` hands back a live reader
that CLV polls from the event loop on every tick, which needs a streaming host
rather than this one.

A refusal is reported at load, names the kind and says why, and the plugin is
**not loaded**. It is never run in-process instead: a stated preference that
silently degrades is worse than no isolation, because the operator believes
otherwise.

### What it costs you

- **One child per origin.** A module's plugins share it, because they share the
  module's imported state.
- **Started on first use**, so a command nobody presses costs nothing. A plugin
  isolated from `settings.conf` is the exception and starts at load, because
  asking the child is the only way to learn what is in a module CLV did not
  import.
- **Everything you are handed is a copy**, encoded and rebuilt: entries through
  the [wire form](#the-wire-form), the spec, the window, the source ref, your
  panel. Mutating what you were handed changes nothing anywhere.
- **`configure()` gets a snapshot, not a live view.** In-process, the mapping
  you keep changes underneath you when the operator edits `settings.conf`. A
  snapshot cannot, so CLV sends you a new one before your next call after a
  reload. Read through the mapping as usual; just do not assume an edit reaches
  you without a call.
- **`print()` goes nowhere.** The child's stdout and stderr are `/dev/null`,
  because they would otherwise be the terminal CLV is drawing on and your
  output would look like the viewer corrupting itself. Use `context.notify()`,
  or write to a file of your own.
- **A slow call is now a killed call.** `plugin_host_timeout_ms` (default 5000)
  bounds every call including the handshake that starts your host. Over it, the
  child is killed, your plugin is disabled, and the `P` dialog says so with
  **Re-enable** as the way back.
- **It bounds the freeze; it does not remove it.** A `Command` and an
  `Exporter` are still called from the event loop, and CLV still waits for the
  answer — so a command of yours that hangs pauses the pane for up to
  `plugin_host_timeout_ms` and then comes back, where in-process it paused the
  pane for good. A `WatchSink` is the one that is genuinely unaffected: it was
  already on a thread of its own. Isolation is what makes a hang **end**, not
  what makes it invisible; if you need CLV to stay live while you work, do the
  work on a thread of your own and answer from what it last stored.

### What a failure looks like

| What happened | What CLV does |
| --- | --- |
| Your code raised | The ordinary third-party failure. Reported by the call site that made the call; your host stays up |
| You did not answer in time | Child killed, plugin disabled, reason recorded. **Re-enable** starts a fresh one |
| The child crashed or exited | Same, with "exited" in the reason |
| The host would not start | Plugin disabled and reported. **Never** run in-process instead |

### Frozen builds

CLV's release binaries are PyInstaller bundles, and both halves of this matter
if you are working on the host itself:

- `clv/__main__.py` completes the spawn handshake **before** it imports
  anything of CLV's. Spawn starts a child by re-running `sys.executable`, which
  in a frozen build is the CLV binary; without it the first isolated call
  re-launches the viewer. It calls `multiprocessing.spawn.freeze_support()`
  rather than the documented `multiprocessing.freeze_support()` on purpose: on
  **3.11**, the version the release binaries are built with, the documented one
  is gated on `sys.platform == 'win32'` and does nothing at all on the Linux
  bundle it exists for. 3.14 widened that check, which is why a green local
  suite says nothing about it, and why the release workflow has a smoke test
  that runs an isolated plugin in the built binary.
- The child scrubs its own environment with
  `clv.plugins.sources.journald.child_environment()` — the same function the
  journal provider passes to `Popen` — because PyInstaller puts `_internal` on
  `LD_LIBRARY_PATH` and a child that then execs a system binary loads the
  bundle's libcrypto instead of the system's.

---

## Conventions for plugin authors

These are **conventions, not protections**. Each says who is trusting whom, and
what CLV actually enforces — which in all three cases is nothing. CLV does not
inspect a plugin's imports, intercept its file access, or filter what it logs.
A reviewer enforces these by reading the code; the operator enforces them by
choosing what to install.

- **Never perform a network call or spawn a subprocess without user consent.**
  *Not enforced.* The shipped `journald` provider is the pattern to copy, and it
  is now copyable: an `enabled` key in its own `[plugin:journald]` section,
  taken through `configure()`, read fresh on every `discover()`, offering
  nothing at all until it is true. No import of CLV's settings parser is
  involved, which is what makes it a pattern rather than a privilege.
- **Confine file reads and writes to configured directories.** *Not enforced.*
  A plugin runs with the operator's full filesystem access; see
  [Trust model](#trust-model).
- **Never log or transmit sensitive information** (passwords, tokens, and log
  content itself). *Not enforced.* This is the convention most worth honouring
  and the one CLV has least ability to check.

---

## Plugin Testing

| Type | What to Test | Tools |
|------|---------------|-------|
| **Unit** | Validate `discover()`, `apply()`, `export()` methods | pytest |
| **Integration** | Verify plugin registration and runtime behavior | textual + pytest |
| **Static** | Lint for unsafe imports and access | ruff, mypy |

---

## API surface and stability

**Import from `clv.api`.** It is the whole of what CLV publishes to plugins, and
the only part of CLV covered by a stability promise.

```python
from clv.api import FilterStage, LogEntry, normalize_level
```

Everything there is a **re-export of the real object**, never a wrapper or a
DTO. Your `apply` receives the same `LogEntry` CLV's own render path holds. That
is a deliberate refusal: converting an entry per plugin per render is the one
cost CLV cannot pay, and an author who had to convert would be writing against a
lesser version of the core than the core writes against itself.

### What is published

| Group | Names |
| --- | --- |
| Version | `PLUGIN_API_VERSION` |
| Interfaces | `Plugin`, `LogSourceProvider`, `LogFormat`, `QueryOperator`, `ComputedField`, `FilterStage`, `ClusterRule`, `ShapeContributor`, `TimelineAnnotation`, `TimelineMetric`, `WatchMatcher`, `WatchSink`, `Exporter`, `Command` |
| Handed to you | `LogEntry`, `FilterContext`, `FilterSpec`, `TimeWindow`, `ProviderSource`, `SourceRef` |
| Declaring a format | `FormatProfile`, `DEFAULT_PROFILE`, `FORMAT_NAMES` |
| Being invoked, and drawing | `CommandContext`, `Panel`, `Control`, `CONTROL_KINDS`, `MAX_PANEL_CONTROLS` |
| Handed back | `ExportResult` |
| Severity | `normalize_level`, `level_rank`, `level_matches`, `highest_level`, `LEVEL_TRACE` … `LEVEL_CRITICAL`, `LEVEL_ORDER`, `SEVERITY_BUCKETS` |
| Fields | `NORMALISED_FIELD_KEYS` |
| Extending the query | `BUILTIN_OPERATORS` |
| Extending the watch rules | `WatchRule`, `KIND_PATTERN`, `SINK_SAMPLE_LIMIT` |
| Settings | `setting_bool`, `setting_list` |
| Process boundary | `WIRE_VERSION`, `entry_to_wire`, `entry_from_wire` |

The severity helpers are published because the alternative is every plugin
reimplementing them, and reimplementing them badly: `WARNING`, `WARN` and
syslog's numeric `4` are one severity, and a plugin that decides otherwise makes
CLV disagree with itself about what the operator filtered for.
`SEVERITY_BUCKETS` is a plain dict and is read-only **by convention** — mutating
it changes what every severity filter in the process means.

`FORMAT_NAMES` is published for the same kind of reason: it is the set of names
a `LogFormat` may *not* claim, and an author should be able to check that rather
than discover it from a load error. `BUILTIN_OPERATORS` is the same idea one
seam along: the comparison tokens a `QueryOperator` may not claim, and
`KIND_PATTERN` is the third: the rule kind a `WatchMatcher` may not claim.
`WatchRule` is published because a matcher is handed one, and
`SINK_SAMPLE_LIMIT` because a sink that asked for content should size its
payload against the real ceiling rather than against the count it is given.
`CONTROL_KINDS` and `MAX_PANEL_CONTROLS` are the same argument a fourth time,
for the panel vocabulary: an author should be able to read what a `Control` may
be and how many one panel may hold, rather than discover either from a refused
panel.

Four of these interfaces were missing from this table when Phase 12 came to add
`Command` to it — `ClusterRule`, `ShapeContributor`, `TimelineAnnotation` and
`TimelineMetric` were published by Phases 10 and 11 and listed everywhere except
here. `tests/test_plugin_docs.py` now checks the table against `clv.api.__all__`
rather than against a list written out a second time, so the next one cannot go
missing the same way.

### Two versions, and they are not the same version

`PLUGIN_API_VERSION` tracks the promise. `clv.__version__` tracks the
application. They move independently, and **`requires_api` is the one to
declare**:

```python
class Redact(FilterStage):
    name = "redact-secrets"
    requires_api = ">=1.0,<2.0"   # what you actually depend on
    requires_clv = ">=2.0,<3.0"   # optional, and rarely what you mean
```

Both use the grammar in [The constraint grammar](#the-constraint-grammar) and
both fail the same two ways: an unsatisfied constraint names your constraint and
the running version, an unreadable one names the constraint and says it could
not be read. Neither is ever a silent skip.

Pinning `requires_clv` instead means re-releasing your plugin every time CLV
ships a release that changed nothing you can see. The API is additive from 1.0:
each new seam adds names and the version stays 1.0, which is what that
separation is for.

### The deprecation policy

- A name published in `clv.api` is **removed only on an API major**.
- A name deprecated in API *N* keeps working for the whole of *N* and emits a
  `DeprecationWarning` naming what to use instead.
- **Anything not in `clv.api` is internal and may move without notice** —
  including `clv.services.parsing.LogEntry` under its own name, and including
  every name `clv.plugins` exports beyond the interfaces re-exported here.
  Importing from `clv.services.*` is a plugin taking a risk it has been warned
  about; it is not forbidden, and it is not supported.

`tests/test_api_surface.py` holds the published list and every published
signature as literal data, so changing any of this means changing that file, in
the diff, where a reviewer sees it.

### The wire form

`LogEntry.fields` is a `mappingproxy`, so `pickle.dumps` raises `TypeError` on
*every* entry CLV produces — the shared empty mapping included. An entry
therefore cannot cross a process boundary by the obvious route, and
`entry_to_wire` / `entry_from_wire` are the route it does take:

```python
payload = entry_to_wire(entry)     # a plain dict, every value a JSON scalar
same = entry_from_wire(payload)    # fields come back read-only
```

The payload carries `WIRE_VERSION` under `"v"`, and `entry_from_wire` refuses a
version it does not know **before reading any other key** — a payload from a
future CLV is rejected whole rather than half-decoded into an entry that looks
plausible and is not.

You will not need this in-process. It is what an [isolated](#isolation)
plugin's entries travel as, and it was published ahead of the host that consumes
it so that the encoding is part of the frozen contract rather than an artefact
of whichever phase first needed it.

---

## Versioning & Compatibility

- Follow **semantic versioning** for each plugin.
- Set `requires_api` on the plugin class to declare what you depend on, e.g.
  `requires_api = ">=1.0,<2.0"`. This is the one to reach for; see
  [Two versions, and they are not the same version](#two-versions-and-they-are-not-the-same-version).
- Set `requires_clv` when you genuinely depend on the application rather than on
  the API, e.g. `requires_clv = ">=2.0,<3.0"`. Omitting either means "any
  version", and a plugin may declare both — each is checked on its own account.
- A plugin failing either constraint is skipped and reported in
  `PluginRegistry.errors`, which the Advanced drawer surfaces.

### The constraint grammar

A PEP 440 subset, hand-rolled — `packaging` is not a dependency and will not
become one. Comma-separated pieces are ANDed and whitespace is ignored.

| Form | Meaning |
| --- | --- |
| `>=` `<=` `>` `<` `==` `!=` | ordered comparison; a bare version means `==` |
| `==2.6.*`, `!=2.6.*` | release-prefix match |
| `~=2.6` | compatible release — `>=2.6, ==2.*` |
| `~=2.6.1` | `>=2.6.1, ==2.6.*` |
| `^2.0.0` | Poetry's caret, a documented alias — `>=2.0.0,<3.0.0` |

Versions may carry a prerelease (`a`/`b`/`rc`, with `alpha`/`beta`/`pre`
normalised), `.postN` and `.devN`. They order as
`1.0.dev1 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 < 1.0.post1` — so `2.6.0rc1` is
correctly **older** than `2.6.0`, which the previous comparator got backwards by
stripping the letters and reading it as `2.6.1`.

**One deliberate divergence from PEP 440: prereleases are always considered.**
Strict PEP 440 excludes a prerelease from a range that does not name one, which
would make `requires_clv = ">=2.6"` unsatisfied on a running `2.7.0rc1` and
silently disable every installed plugin on any release-candidate build.

An **unparseable** constraint — `~~2.6`, `>=abc`, a bare `~=2` — is reported
against your plugin by name, as `bad requires_clv:` or `bad requires_api:`. It
is never a silent "unsatisfied": a typo and a genuine incompatibility must not
look the same from the outside.

## Failure Handling

Loading never raises. An import error, an unreadable or unsatisfied
`requires_clv`, a class that cannot be instantiated, an object that implements
no interface, a module that exports nothing, or a `FilterStage` that throws
mid-render is recorded in `PluginRegistry.errors` and skipped. A third-party
plugin must never prevent CLV from starting or break a render.

**A plugin disabled at runtime stays disabled for the session.** A `FilterStage`
that raises is taken out of service and its failure recorded **once**; the
remaining stages keep running and the pane keeps rendering. It is not retried on
the next render — doing that cost one identical error per render pass, and a
plugin that raised on one entry will raise on the next.

`PluginRegistry.errors` deduplicates identical `(origin, message)` pairs into a
single entry with a count, and caps the number of distinct problems it stores,
reporting how many it dropped. Each entry also carries a `category` — `load`,
`incompatible`, `shadowed`, `missing` or `runtime` — so "this plugin is broken"
and "this plugin wants a CLV you are not running" can be told apart without
reading the message, which is what lets the management UI name a state.

The log panel reports plugin problems as **one line** pointing at `P`, not one
line per problem: deduplication capped the repeats, but four distinct faults
still buried the discovery summary the operator opened CLV to read, and none of
those lines had room to say what to do about any of them.

**Turning a plugin off is not a failure.** `PluginRegistry.disable()` takes
`record=False` for an operator's own decision, so `errors` stays what it is —
the amber channel for things that went wrong — and a plugin doing exactly as it
was told never appears in it.

---

## Developer Workflow

1. Create your plugin module in `clv/plugins/` or as a separate package.  
2. Implement one of the ABCs (`LogSourceProvider`, `FilterStage`, or `Exporter`).  
3. Add minimal tests.  
4. Add documentation to this folder’s `README.md` if distributing internally.  
5. Submit PRs with a short demo (e.g., asciinema or screenshot).

---

## Plugin Review Criteria

- ✅ Conforms to ABCs  
- ✅ Does not alter core logic or CSS  
- ✅ Has tests and docstrings  
- ✅ Respects CLV’s minimal dependency policy  
- ✅ Passes linting and security checks

---

## Non-Goals

Still non-goals:

- Collection infrastructure: unattended collection, an agent or daemon on a
  remote host, store-and-forward pipelines, or spooling. A source plugin may
  read a remote log on demand over a connection the operator already has; it may
  not install anything, leave anything running, or cache content to disk.  
- Kernel-level or privileged operations. No `sudo`, `doas` or `pkexec`, at
  either end of a connection, not behind a setting.  
- Credentials of any kind — no password field, no key generation, no agent
  management, and never a disabled host key check.  
- Background daemons or telemetry. A plugin may not run unattended, and CLV
  reports nothing anywhere. The opt-in [subprocess host](#isolation) is not a
  daemon: it lives and dies with the viewer, it is started only for a plugin
  whose author or operator asked for it, and it exists to make a plugin
  *killable*, not to make it long-lived.

### Reversed

Kept rather than deleted, so the argument is on the record rather than erased —
the rule stated at the head of [TODO.md](../../TODO.md).

- **"Extension is in-process only."** *Reversed 2026-08-14* by
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phase 13. The objection was that a
  process boundary buys less than it appears to, and that objection stands
  unchanged — see [Trust model](#trust-model). What changed is that failure
  containment turned out to be worth having on its own: a plugin that hangs or
  leaks currently cannot be stopped at all. Isolation is opt-in per plugin, is
  refused outright for per-entry kinds, and is described only as what it is —
  see [What isolation does and does not do](#what-isolation-does-and-does-not-do).
- **"CLV has no plugin distribution mechanism."** *Reversed 2026-08-14* by
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phases 3 and 15. The objection was to
  CLV running a hosted index — a server, a namespace and a moderation queue,
  where every listing is a trust signal CLV would be issuing. That objection
  stands and **no index is planned**. What is planned is a user plugin
  directory, an explicit enable-list, and manifests a `clv plugin install` can
  verify from a path, a tarball or a URL that anyone may host.
- **"No rules DSL for clustering."** *Reversed 2026-08-14* by
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phase 10, and recorded in
  `clv/services/clustering.py`'s own docstring where a reader of that module
  meets it. The objection was to **the operator** hand-writing regex rules into
  `settings.conf`, where a typo is a silently mis-clustered pane and there is no
  review, no test and no way to tell a bad rule from a bad log. That stands: the
  rules are still unconfigurable from `settings.conf` and nothing here became a
  text format. What a plugin author may now add is a `ClusterRule` — a compiled
  pattern and a placeholder, validated at load — and a `ShapeContributor`. A
  different party, writing Python against a reviewed interface, making a
  different promise.
- **"No query DSL."** *Reversed 2026-08-14* by
  [PLUGIN_TODO.md](../../PLUGIN_TODO.md) Phase 8, and the reversal is narrow
  enough to state exactly. The objection was to a query *language* — `OR`,
  parentheses, precedence — and it stands: none of the three exists, and all
  three remain out of scope in [TODO.md](../../TODO.md). What a plugin may now
  add is a `QueryOperator` (a comparison token) and a `ComputedField` (a
  queryable field derived rather than parsed). Both add vocabulary; neither adds
  structure. The grammar is still implicit-AND and still flat — a plugin can
  teach it a new word, not a new sentence shape. CLV's own tokens stay reserved,
  and a computed field resolves *after* the parsed ones, so neither can change
  what an existing query means.
- **"A provider source cannot be starred or merged."** *Reversed 2026-08-19* by
  [SSH_TODO.md](../../SSH_TODO.md) Phase 9. The objection was that a provider
  source is not a file — nothing on disk answers to `journal:unit/sshd.service`
  — so putting one in `session.json` would record a path that does not exist.
  Two thirds of that stands and is now enforced by name rather than by accident:
  glob filtering and rotated-set grouping both refuse a `JournalRef` explicitly,
  because a journal has no directory and nothing that rotates. The third was
  never an argument, only an implementation: a persisted **identifier** is not a
  persisted path, and a journal unit is exactly the source an operator wants
  starred and compared across a fleet. `ProviderSource` is still not a ref — it
  is the tree node's payload, and its `path` carries the identity.

- **"Network aggregation or remote log collection."** *Reversed 2026-08-16* by
  [SSH_TODO.md](../../SSH_TODO.md). The objection was to CLV becoming collection
  infrastructure, and it stands — it is the first bullet above. What changed is
  that reading a folder on a host the operator can already `ssh` into needs none
  of that. A source plugin may now open a network connection, under the same
  consent rule every subprocess already lives under: a `settings.conf` opt-in,
  read fresh on every `discover()`, offering nothing at all until it is true.
  A *network* subprocess raises that bar rather than lowering it.

---

## The SSH transport is a module here, not a plugin

`sources/ssh.py` lives in this package and its `register()` returns `[]`. That
is not a stub — it is the point.

A `LogSourceProvider` hands back a `ProviderSource`, which is deliberately *not*
a path: glob filtering and rotated-set grouping cannot see one. That is right
for a journal unit, which has no directory to walk and nothing that rotates. It
is exactly wrong for `/var/log/syslog` on `web01`, which has both — and a remote
log that cannot be starred or merged has not met the goal `SSH_TODO.md` sets.

**Starring and merging used to be on that list and no longer are.** The
exclusion was enforced by a provider source not being a `SourceRef` at all,
which was cheap rather than right: a journal unit is the clearest case of a
source someone wants starred, and comparing one unit across a fleet is the
workflow remote sources exist for. `JournalRef` is a ref, so both work. What
still cannot reach the starred set is a provider source whose `path` is not a
concrete ref type — the union in `refs.SOURCE_REF_TYPES` is closed, and it is a
union of types rather than a duck test precisely so that stays true.

So the module supplies a **`SourceBackend`** instead. A remote root reaches
`SourceManager` as an ordinary root, builds the same nested folder tree, and is
read by the same `SourceReader`; `app.build_backends` wires the resolver in.

The journal reaches the same hosts through the same connections, and the import
direction is why that is a paragraph rather than a diagram: `ssh.py` imports
`journald.py` (for `child_environment` and `JournalReader`), so `journald.py`
reaches back only through **injection** — `use_remote(resolver, hosts)`, called
by the app — and one deferred import for the reader itself. It never constructs
an `SSHConnection`. That is a correctness requirement rather than a layering
preference: `SSHConnection.socket` hashes `(name, host, user, port)`, so a
connection built here would resolve to the *same* multiplex socket the resolver
is using, and closing it would tear down a master CLV is actively reading
through. A test asserts the class names appear nowhere in that module.

Remote journal enumeration runs in the app's **host** stage rather than in
`discover()`, and that is a timing decision a test caught rather than a design
one that was foreseen. Discovery is two stages so a machine that is down costs
the local tree nothing; enumerating remote journals in the first stage meant
paying a connect timeout per host before anything appeared, and paying it twice
on `Ctrl+R`, which resets the backoff.

What keeps it in this package is consent, not layering — the same reason the
journal is here. Reading a remote source spawns `ssh`, a plugin may not spawn a
subprocess without the operator asking, and `enable_ssh` is read fresh on every
call rather than once at import. Nothing connects, and no socket exists, until
it is true.

---

> 🧭 **Goal:**  
> The plugin system empowers developers to extend CLV responsibly — adding sources, filters, or exporters — without sacrificing the project’s speed, security, or simplicity.