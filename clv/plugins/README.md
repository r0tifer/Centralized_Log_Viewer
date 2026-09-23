# Writing a CLV plugin

The quick start. It assumes you have CLV installed and have never read its
source, and it takes you to a working, installed, signed plugin.

Three documents, and they do different jobs:

| | |
| --- | --- |
| **This file** | How to write one, publish one, and where the worked examples are |
| [`AGENTS.md`](AGENTS.md) | The contract: every interface in full, the budgets, the trust model, the failure rules |
| [`../../README.md`](../../README.md) | The operator's side — installing, enabling, managing |

---

## Five minutes

CLV wrote nine worked examples into your plugin directory the first time it
ran. Installing one is copying it up a level and naming it:

```bash
cd ~/.config/clv/plugins
cp examples/redact_secrets.py .
```

Then in `~/.config/clv/settings.conf`, under `[log_viewer]`:

```ini
plugins = redact_secrets
```

Restart CLV. Press `P`: the plugin is listed as **loaded**, and lines
containing `password=hunter2` now read `password=******`.

That is the whole mechanism. No root, no Python toolchain, nothing a package
upgrade overwrites, and it works identically on a `.deb`, an `.rpm`, a tarball
and a source checkout.

**Copying a file in is not consent to run it.** A file in that directory is
listed and left alone until its name appears in `plugins`. The same is true of
`clv plugin install`, which installs and prints the line to add rather than
adding it.

---

## Writing your own

Start from an example rather than an empty file — each one is complete,
commented, and argues for what it declares.

```bash
cp ~/.config/clv/plugins/examples/redact_secrets.py ~/my_plugin.py
```

Every plugin has the same four parts:

```python
from clv.api import FilterContext, FilterStage, LogEntry, setting_list


class Redact(FilterStage):
    name = "redact_secrets"
    requires_api = ">=1.0,<2.0"

    def apply(self, entry: LogEntry, context: FilterContext) -> Optional[LogEntry]:
        ...


def register() -> list[FilterStage]:
    return [Redact()]
```

1. **Import from `clv.api`.** It is the whole of what CLV publishes and the
   only part under a stability promise. It re-exports the *real* objects — your
   `apply` is handed the same `LogEntry` CLV's own render path holds, not a
   copy. Everything else, `clv.services.*` included, is internal and may move.
2. **Subclass one interface** (or several — a module may supply any mix).
3. **Declare `requires_api`.** Constrain the API version, not CLV's release
   number; they move independently and that separation is the point of having
   two.
4. **Hand the instances back**, from `register()`, from `__all__`, or by simply
   defining them at module level — CLV scans for `Plugin` subclasses as a last
   resort and tells you when it finds none.

### While you are working on it

`CLV_PLUGIN_PATH` names extra directories, searched ahead of your plugin
directory, so you can run a plugin from where you are editing it:

```bash
CLV_PLUGIN_PATH=~/src/my-plugin clv
```

It is a development mechanism, not an install path, and the `plugins`
enable-list still applies. There is **no hot reload**: plugins are imported
once at startup, so a code change needs a restart.

When a plugin does not appear, `clv doctor` says why — it prints every
plugin CLV found, its state and, for a failure, the reason. It imports
nothing it does not have to, and it runs over a pipe.

---

## The thirteen interfaces

Each has a worked example in `~/.config/clv/plugins/examples/` and a full
contract in [`AGENTS.md`](AGENTS.md).

| Interface | What it extends | Example |
| --- | --- | --- |
| [`LogSourceProvider`](AGENTS.md#1-logsourceprovider) | Where lines come from | `container_logs.py` |
| [`FilterStage`](AGENTS.md#2-filterstage) | Transform or drop an entry | `redact_secrets.py` |
| [`LogFormat`](AGENTS.md#3-logformat) | A line shape no built-in matcher knows | `nginx_error.py` |
| [`QueryOperator`](AGENTS.md#4-queryoperator) | A comparison token the query box lacks | `field_regex.py` |
| [`ComputedField`](AGENTS.md#5-computedfield) | A queryable field derived rather than parsed | `field_regex.py` |
| [`WatchMatcher`](AGENTS.md#6-watchmatcher) | A watch rule kind beyond "this matched" | `watch_alerts.py` |
| [`WatchSink`](AGENTS.md#7-watchsink) | Where a watch hit is delivered | `watch_alerts.py` |
| [`ClusterRule`](AGENTS.md#8-clusterrule) | One more volatile token for `c` to fold away | `cluster_rules.py` |
| [`ShapeContributor`](AGENTS.md#9-shapecontributor) | An extra component of a cluster's shape | `cluster_rules.py` |
| [`TimelineAnnotation`](AGENTS.md#10-timelineannotation) | Marks on the time axis | `timeline_marks.py` |
| [`TimelineMetric`](AGENTS.md#11-timelinemetric) | What a bucket measures, if not entries | `timeline_marks.py` |
| [`Exporter`](AGENTS.md#12-exporter) | Where the filtered set goes | `html_report.py` |
| [`Command`](AGENTS.md#13-command) | A named action, from `C` or its own key | `commands.py` |

**A plugin extends a seam on equal terms with a built-in.** A plugin-supplied
format is searchable by field query, bucketed by the timeline, folded by `c` and
drawn with its own source cell and chips. A plugin-supplied operator works in a
saved view and in a watch rule. Where a seam cannot offer equal terms,
[`AGENTS.md`](AGENTS.md) says so in the same sentence that offers the seam.

### Settings of your own

A `[plugin:<name>]` section in `settings.conf` reaches your plugin through the
optional `configure(settings)` hook, as a read-only mapping:

```ini
[plugin:redact_secrets]
patterns = password, api_key, token
replacement = ******
```

The mapping is a **live view**, not a copy — hold it rather than reading values
out of it in `configure`, and a change to the file reaches you without a
restart. `setting_bool` and `setting_list` are published so your list parses the
way CLV's own `log_dirs` does; an operator should not have to learn a second
comma convention for one file.

### Consent, for anything that acts on the world

**A plugin must not spawn a subprocess, open a socket or write outside its own
files because it was installed.** The shipped `journald` provider is the
pattern, and `container_logs.py` is that pattern on a second subject: an
`enabled` key in the plugin's own section, read fresh on *every* call rather
than cached at startup, offering nothing and running nothing until it is true.

CLV does not enforce this. It cannot — a plugin runs at your privilege in your
process, and the interfaces bound what CLV *asks* of a plugin, not what a plugin
*can do*. See [Trust model](AGENTS.md#trust-model), which says so in those
words.

### What will get your plugin switched off

CLV disables a plugin rather than letting it hurt the viewer, and says so in the
`P` dialog with `r` to put it back:

- **Raising.** A stage, format or matcher that throws is out for the session,
  reported once rather than once per render.
- **Being slow.** Each seam is timed against a budget — a filter stage over
  `plugin_time_budget_ms` on three consecutive passes is treated as a failure,
  because a stage that runs on every keystroke and is slow is indistinguishable
  from CLV being broken.
- **Hanging.** A budget only catches a pass that *finished*. For that there is
  isolation: four kinds — `Exporter`, `WatchSink`, `Command` and
  `TimelineAnnotation` — can run in a subprocess CLV can kill. The rest are
  called once per line or once per entry, where a round trip between processes
  is not a slower program but a different one, and they are refused by name with
  the reason. **Isolation contains crashes, hangs and leaks. It does not make an
  untrusted plugin safe** — the child runs as you, with your files.

---

## Publishing

There is no index to submit to and there never will be: a hosted index is a
server, a namespace and a moderation queue, and every listing on it would be a
trust signal CLV was issuing. Anyone can host; CLV downloads from the host the
operator names.

A published plugin is a tar archive with a `clv-plugin.toml` at its top level:

```toml
name = "nginx_format"
version = "1.2.0"
requires_api = ">=1.0,<2.0"
requires_clv = ">=3.0"
kinds = ["LogFormat"]
author = "Alice <alice@example.com>"
homepage = "https://example.org/nginx-format"
files = [{ path = "nginx_format.py", sha256 = "e3b0c442…" }]
```

`name`, `version` and `files` are required. `name` must be a legal Python module
name — it is what the operator writes in `plugins` and what CLV imports.

```bash
sha256sum nginx_format.py                    # paste into files = [...]
ssh-keygen -Y sign -n clv-plugin -f ~/.ssh/id_ed25519 clv-plugin.toml
tar czf nginx_format-1.2.0.tar.gz \
    --transform 's,^,nginx_format-1.2.0/,' \
    nginx_format.py clv-plugin.toml clv-plugin.toml.sig
sha256sum nginx_format-1.2.0.tar.gz          # publish this beside the download
```

Three things worth knowing, each of which [`AGENTS.md`](AGENTS.md#publishing)
argues in full:

- **Publish the tarball's own digest.** It is the only number an operator can
  check that did not come out of the archive — your manifest's checksums prove
  the files match your manifest, which anyone who replaced both can arrange.
- **`-n clv-plugin` is not optional.** Without that namespace, a signature you
  made over some other file for some other purpose would count as your word that
  this is your plugin.
- **CLV ships no trusted keys.** An operator trusts yours with `clv plugin
  trust`, or installs unsigned and is told so. A bundled trust root would make
  CLV the arbiter of which plugins are legitimate, which is the hosted index
  arriving through a side door.

---

## The sources CLV ships

Two, and they are shaped differently on purpose.

**`sources/journald.py` is a `LogSourceProvider`**, and it is a plugin for
reasons of *consent* rather than layering: reading the journal runs
`journalctl`, and core shipping that would put a subprocess behind a default.
`enable_journald` is read fresh on every scan, so the drawer switch takes effect
without a reload. It is the reference for `configure()`, for the opt-in pattern,
and for `open_reader()`.

**`sources/ssh.py` is not a provider at all** — its `register()` returns `[]`,
deliberately. A remote `/var/log/syslog` has a directory to walk and files that
rotate, and a `ProviderSource` can express neither; so the module supplies a
`SourceBackend` instead, and a remote root reaches CLV as an ordinary root. The
full argument is in
[AGENTS.md](AGENTS.md#the-ssh-transport-is-a-module-here-not-a-plugin), which is
where it stays: it carries a reversal record, and moving it would erase the
argument rather than relocate it.

**The remote journal needs both opt-ins.** A unit on another machine is
`journalctl` reached over `ssh`, so it appears only when `enable_journald` *and*
`enable_ssh` are true. Neither implies the other, and neither is on by default:
one consents to running a subprocess, the other to opening a connection.

### If you spawn a system binary, fix the environment first

A PyInstaller bundle puts its own `_internal` directory on `LD_LIBRARY_PATH` so
the bundled interpreter finds the libraries shipped beside it. A child process
inherits that, so `/usr/bin/anything` loads the *bundle's* libcrypto and libssl
instead of the machine's — fatal whenever the build distribution differs from
the running one, which for a released binary is the normal case.

CLV's own answer is `journald.child_environment()`. It is **not** part of
`clv.api`, so copying it is currently the right move rather than importing it:
`container_logs.py` carries it in eight lines with that note attached.

---

## Migrating an existing plugin

`LogSourceProvider`, `FilterStage` and `Exporter` predate all of this. **Nothing
about them changed and nothing you wrote has broken.** Four things are worth
adopting:

| Adopt | Instead of | What it buys |
| --- | --- | --- |
| `from clv.api import …` | `from clv.plugins import …` | The only import path under a stability promise. The old one still works and is not covered by it. |
| `requires_api = ">=1.0,<2.0"` | `requires_clv` | Stops caring which CLV release you are on. `requires_clv` still works and is rarely what you mean. |
| `wants_path` on an `Exporter` | Choosing your own destination | The operator's chosen path arrives as `destination`. Without it the dialog's path input is disabled, and an exporter writing a file had no way to honour the location that had just been typed. |
| `open_reader()` on a provider | `open()` alone | Tailing. An iterator cannot be asked to stop, has nowhere to put cleanup, and blocks the poll it is drained from. Returning `None` from `open_reader` means "use `open()`", so adding it breaks nothing. |

Shipping a manifest is worth it too, even unsigned: it is what makes `clv plugin
verify` able to tell an operator their copy is still the one you published.
