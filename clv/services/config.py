"""Settings file discovery, parsing and defaults.

Extracted from ``app.py`` so configuration can be tested and reused without a
running UI. Every value is validated and clamped: a malformed settings file
degrades to defaults rather than preventing startup.

**Two sections, one rule.** ``[log_viewer]`` holds the session's settings;
``[ssh:<name>]`` holds one remote host each. Neither may prevent startup — a
section CLV cannot make sense of is skipped and *reported*, never raised.

Reporting is what this module gained for remote hosts, and it is not cosmetic.
A per-host schema without it loses a machine to a typo in silence, which is the
one outcome ``SSH_TODO.md`` Requirement 7 forbids outright. Problems land in
:attr:`LogConfig.issues` and ``app.py`` prints them beside the plugin errors.
:class:`ConfigIssue` deliberately mirrors ``plugins.PluginError``'s shape rather
than being it: ``clv/services/`` may not import ``clv/plugins/``.

**Nothing here touches the network.** This module parses and exposes host
records; connecting to one is the SSH source's business, and does not happen
until ``enable_ssh`` is true.
"""

from __future__ import annotations

import configparser
import importlib
import inspect
import os
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Literal, Mapping, Optional

from .discovery import DEFAULT_EXCLUDE_GLOBS, DEFAULT_MAX_FILES, DiscoverySettings
from .refs import SourceRef, format_ref, normalize_ref, scheme_of
from .settings_file import SettingsDocument

CONFIG_SECTION = "log_viewer"

#: The schema stamp the upgrade path reads. It lives in ``[log_viewer]`` like
#: any other option -- an older build that does not know it simply ignores it,
#: because nothing refuses unknown keys in the global section.
CONFIG_VERSION_OPTION = "config_version"

#: Bump this, and the marker in the shipped ``settings.conf``, whenever the
#: template's option set changes. Deliberately not ``__version__``: most
#: releases do not touch the settings schema, and stamping the app version
#: would re-migrate every operator's file for nothing.
CURRENT_CONFIG_VERSION = 5

#: One remote host per section: ``[ssh:web01]``. The suffix is the host's name
#: within CLV — what the tree shows, what ``node:`` matches, and the fallback
#: for ``host`` when the operator does not give one.
SSH_SECTION_PREFIX = "ssh:"

#: One plugin's own settings per section: ``[plugin:redact_secrets]``. The
#: suffix is the module name -- the same word the operator writes in the
#: ``plugins`` enable-list, so there is one name for a plugin and not two.
#:
#: CLV parses these and never interprets them. What a key means is the plugin's
#: business; what ``config.py`` guarantees is that the section is well-formed,
#: that a malformed one costs only itself, and that the plugin is handed the
#: values rather than left to import the settings parser.
PLUGIN_SECTION_PREFIX = "plugin:"

#: Keys that predate ``[plugin:<name>]`` and still live in ``[log_viewer]``,
#: read into the section they would live in today.
#:
#: ``enable_journald`` is the whole list, and moving it was declined rather than
#: overlooked: it is in every operator's settings file, it is what the Advanced
#: drawer's switch writes, and it is documented in the README. Renaming it would
#: buy tidiness at the cost of a config migration and a drawer change, and the
#: point of the exercise is that the *plugin* stops importing the settings
#: parser -- which this achieves without touching a single operator's file.
#:
#: A section that sets the key itself wins. The alias only fills a gap.
_LEGACY_PLUGIN_KEYS: dict[str, dict[str, str]] = {
    "journald": {"enabled": "enable_journald"},
}

#: The port range a TCP port can actually occupy. Out of it is a typo, and a
#: typo is reported rather than clamped: clamping 70000 to 65535 would connect
#: somewhere the operator did not name.
_PORT_RANGE = (1, 65535)

_DEFAULT_SSH_PORT = 22

# Bounds keep a typo in settings.conf from producing a pathological UI.
_LIMITS: dict[str, tuple[int, int, int]] = {
    # option: (default, minimum, maximum)
    "max_buffer_lines": (5000, 100, 500_000),
    "default_show_lines": (500, 10, 500_000),
    "refresh_hz": (2, 1, 60),
    "min_show_lines": (10, 1, 10_000),
    "show_step": (50, 1, 10_000),
    "csv_max_rows": (20, 1, 5_000),
    "csv_max_cols": (10, 1, 200),
    "max_files": (DEFAULT_MAX_FILES, 1, 200_000),
    "tree_width": (38, 20, 120),
    # Bytes of log text one OSC 52 copy may carry. The default is under tmux's
    # ~74 kB passthrough limit with room for base64 expansion, so a copy that
    # fits the cap is a copy the terminal will actually accept.
    "clipboard_max_bytes": (65_536, 1_024, 1_000_000),
    # Seconds a watch rule waits before it may notify again. The floor is not
    # zero on purpose: a rule matching every tailed line would otherwise raise
    # a toast per line, which is the behaviour that gets a feature like this
    # switched off for good.
    "watch_rate_limit": (60, 5, 3_600),
    # Entries of lookback for repeat clustering (`c`). A line joins a cluster
    # only when that cluster's last member is within this many entries, which
    # is what stops one cluster spanning a whole session and swallowing an
    # event from an hour ago.
    "cluster_lookback": (200, 2, 100_000),
    # Milliseconds of wall time one plugin may spend on one pass of the render
    # path before it is taken out of service. The floor is **zero**, and zero
    # means no guard at all -- unlike every other option here, where the low
    # end is a typo to be clamped away, an operator who would rather have a
    # slow plugin than a disabled one has a legitimate answer and this is it.
    "plugin_time_budget_ms": (250, 0, 60_000),
    # The same, for the *read* path: milliseconds one `LogFormat` may spend on
    # one batch of lines from a reader. Lower than the render budget and
    # measured against a different thing -- a batch is one `prime` or `poll`,
    # not one keystroke, so a format that needs 50 ms of a single file read is
    # already the slowest thing between opening a log and seeing it. Floor is
    # zero and means no guard, exactly as above.
    "plugin_read_budget_ms": (50, 0, 60_000),
    # Milliseconds a watch sink may spend inside one delivery before CLV stops
    # waiting for it. Not a budget in the sense the two above are -- a sink runs
    # on a thread of its own and being slow costs the pane nothing -- but a
    # deadline: a sink that never returns would otherwise be fed forever and
    # silently deliver nothing. Generous, because the thing on the other end is
    # usually a network. Zero means no deadline.
    "plugin_sink_timeout_ms": (5_000, 0, 60_000),
    # Milliseconds an *isolated* plugin may take to answer one call -- including
    # the handshake that starts its host -- before CLV kills the child and takes
    # the plugin out of service. A deadline rather than a budget, like the sink
    # timeout above, and the first one in CLV that can actually be enforced: a
    # thread cannot be stopped from outside and a process can. Zero waits
    # forever, which is what a plugin running in-process does anyway.
    "plugin_host_timeout_ms": (5_000, 0, 60_000),
}

DEFAULT_SETTINGS_TEMPLATE = f"""[{CONFIG_SECTION}]

# Schema version of this file. Written and read by CLV's upgrade path
# (`clv --upgrade-config`, and the installer); there is no reason to edit it
# by hand. A file with no marker predates it and is upgraded on sight.
config_version = {CURRENT_CONFIG_VERSION}

# Folders and/or individual files to monitor, comma separated.
# Folders are searched recursively. CLV is not limited to *.log files: any
# readable text file is a valid source. Use include_globs/exclude_globs below
# to narrow that down.
log_dirs = /var/log

# Only list files matching these globs. Empty means "every text file".
# Example: include_globs = *.log, *.txt, syslog*
include_globs =

# Never list files matching these globs. Compressed archives and binary
# journals are excluded by default because they cannot be displayed as text.
exclude_globs = {", ".join(DEFAULT_EXCLUDE_GLOBS)}

# Follow symlinked directories while scanning (cycles are detected).
follow_symlinks = false

# Skip files whose first block contains NUL bytes (i.e. binaries).
skip_binary = true

# Stop discovery after this many files.
max_files = {DEFAULT_MAX_FILES}

# Present app.log, app.log.1 and app.log.2.gz as one source spanning all three.
# Every member is still listed underneath it and still openable on its own.
group_rotated = true

# Lines held in memory per source. Older lines are dropped.
max_buffer_lines = 5000

# Lines shown when a source is first opened; adjust at runtime with + / -.
default_show_lines = 500
min_show_lines = 10
show_step = 50

# How often (Hz) to check for new content.
refresh_hz = 2

# Starting width of the source tree, in columns.
tree_width = 38

# How much of a CSV payload the Structured switch previews. JSON, XML,
# HTML and CSS need no limits of their own.
csv_max_rows = 20
csv_max_cols = 10

# Most log text one 'y' (OSC 52 clipboard copy) may carry. Oversized copies are
# truncated at a line boundary and the notification says how much was dropped.
clipboard_max_bytes = 65536

# Watch rules (W). Seconds one rule waits before it may notify again; matches
# inside the window are counted and reported together.
watch_rate_limit = 60

# Ring the terminal bell when a watch rule notifies. Off by default.
watch_bell = false

# Repeat clustering (c). How many entries back a cluster may reach to absorb a
# line that looks like it. Higher collapses more; lower keeps repeats that are
# far apart as separate events.
cluster_lookback = 200

# Read the systemd journal (per unit and per boot) as a source. Off by default:
# reading it means running journalctl, and CLV does not spawn a subprocess
# without being asked. The Advanced drawer's "Journal (systemd)" switch turns
# this on and writes it back here.
enable_journald = false

# Plugins to load from ~/.config/clv/plugins/, comma separated, named without
# the .py. Empty by default. A file placed in that directory is listed but not
# run until it is named here, so installing a plugin and running one are two
# separate decisions.
#
# A plugin is trusted code: it runs in CLV's process with your privileges and
# can read every log CLV can open. CLV does not sandbox one.
#
# Plugins that shipped with CLV are not listed here - they load on their own,
# because trusting them is the trust you already placed in CLV.
plugins =

# How long one plugin may spend on a single pass of the render path, in
# milliseconds. A filter stage runs over every buffered line on every render,
# and a render happens on every keystroke in the query box - so a stage that is
# merely slow is indistinguishable from CLV being broken. A plugin that goes
# over this on three consecutive passes is disabled and named in the plugins
# dialog (P), where it can be switched back on.
#
# Set to 0 to turn the guard off entirely and tolerate a slow plugin.
plugin_time_budget_ms = 250

# The same, for a plugin that teaches CLV a log format. A LogFormat's parse()
# runs once per line *read* rather than once per render, and only for lines no
# built-in format recognised - so this is measured over one batch of lines from
# a file read, not over a render pass. A format that goes over this on three
# consecutive batches is disabled and named in the plugins dialog (P).
#
# Set to 0 to turn this guard off too.
plugin_read_budget_ms = 50

# milliseconds. How long a watch sink - a plugin that delivers a watch hit
# somewhere, typically over the network - may spend in one delivery before CLV
# gives up on it, takes it out of service and says so in the plugins dialog (P).
# A sink runs on its own thread and never blocks the viewer, so this is not
# about speed: it is the only thing standing between a webhook whose endpoint
# stopped answering and a sink that is fed for the rest of the session and
# quietly delivers nothing.
#
# Set to 0 for no deadline.
plugin_sink_timeout_ms = 5000

# milliseconds. How long a plugin that runs in its own process - one that
# declared `isolated = True`, or one you isolated with `isolated = true` in its
# own [plugin:<name>] section below - may take to answer one call before CLV
# kills it and says so in the plugins dialog (P).
#
# This is the only ceiling in CLV that can actually be enforced. The render
# budgets work by measuring a pass and declining to start the next one, so a
# plugin that hangs inside a call hangs the viewer; a child process can simply
# be killed. Isolation contains crashes, hangs and leaks - it does not make an
# untrusted plugin safe, because the child runs as you, with your files.
#
# Set to 0 to wait forever, which is what an in-process plugin does anyway.
plugin_host_timeout_ms = 5000

# Read log folders on other machines over SSH. Off by default, and for a
# stronger version of the reason above: a remote source spawns ssh, and a
# network subprocess needs asking for more than a local one does. With this
# false nothing connects and nothing is spawned, however many hosts are
# configured below.
enable_ssh = false

# One section per remote machine. The name after "ssh:" is CLV's name for it -
# what the tree shows and what `node:` matches in a query.
#
# Press R to manage these in the app, or use "Scan SSH config" in the Advanced
# drawer (f) to import the Host aliases already in your ~/.ssh/config.
#
# Authentication is ssh-agent and key files only, so CLV inherits your existing
# ~/.ssh/config wholesale: aliases, ProxyJump, per-host keys, known_hosts.
# There is no password option, and there never will be - CLV does not store or
# transmit a credential. There is no sudo option either: CLV reads as the
# configured user and never escalates privilege. If a log is unreadable, add
# the user to its group (adm, systemd-journal) or set an ACL on the path.
#
# [ssh:web01]
# host = web01.internal      # optional; defaults to the name above
# user = ops
# port = 22
# identity_file = ~/.ssh/id_ed25519
# log_dirs = /var/log, /srv/app/logs
# include_globs = *.log, syslog*
# max_files = 2000
# correct_clock_skew = false
# enabled = true

# One section per plugin that has settings of its own. The name after "plugin:"
# is the plugin's file name without the .py - the same word you write in the
# plugins line above, so a plugin has one name and not two.
#
# CLV parses these sections and never interprets them: what a key means is the
# plugin's business, and its own documentation is what says which keys it reads.
# A section for a plugin that is not installed, or not enabled, is reported in
# the plugin dialog (P) rather than ignored.
#
# [plugin:redact_secrets]
# patterns = password, api_key, token
# replacement = ******
"""


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """Something in the settings file CLV could not honour, and what to do.

    The shape is ``plugins.PluginError``'s on purpose — same ``(origin,
    message)`` pair, same ``__str__`` — so ``app.py`` can print both into the
    log panel without the operator learning two formats. It is a *separate
    type* because ``clv/services/`` may not import ``clv/plugins/``; sharing the
    class would invert the layering for the sake of two attributes.

    ``severity`` is the difference between *this host is gone* and *this host
    will probably still work*. An unreadable ``identity_file`` is a warning
    because ssh-agent may already hold the key; a port of 70000 is an error
    because there is nothing to connect to.
    """

    #: ``"[ssh:web01]"`` or ``"log_dirs"`` — what the operator should look at.
    origin: str
    message: str
    severity: Literal["warning", "error"] = "error"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.origin}: {self.message}"


@dataclass(frozen=True, slots=True)
class RemoteHost:
    """One ``[ssh:<name>]`` section, parsed and validated.

    Inert on its own. Holding one of these has spawned nothing and connected
    nowhere: ``enable_ssh`` plus :attr:`enabled` is what a transport checks
    before it may.

    **There is no password field and no sudo field**, and their absence is
    load-bearing rather than an oversight. ``SSH_TODO.md`` Requirements 9 and 11
    are enforced here, at the schema, where they cannot be forgotten later —
    :data:`_REFUSED_HOST_KEYS` makes writing one a reported error instead of a
    silently ignored line.

    :attr:`log_dirs` stays a tuple of **strings**, not refs. These are paths on
    another machine, and ``normalize_ref`` would resolve them against *this*
    one's working directory — the exact corruption the ref boundary exists to
    prevent. They become refs once there is a host and a backend to qualify
    them with.
    """

    #: The section suffix: ``[ssh:web01]`` is ``"web01"``. CLV's name for the
    #: machine, and the fallback for :attr:`host`.
    name: str
    #: The address or ``~/.ssh/config`` alias to connect to.
    host: str
    user: Optional[str] = None
    port: int = _DEFAULT_SSH_PORT
    identity_file: Optional[Path] = None
    log_dirs: tuple[str, ...] = ()
    enabled: bool = True
    #: Apply the measured clock offset to this host's timestamps. Off by
    #: default: skew is always *reported*, and only corrected on request.
    correct_clock_skew: bool = False

    # --- per-host overrides --------------------------------------------------
    # ``None`` means "inherit the global value". An empty tuple does not: for a
    # glob list it means "explicitly no filtering", which is a real choice an
    # operator can make and must not collapse into absence.
    include_globs: Optional[tuple[str, ...]] = None
    exclude_globs: Optional[tuple[str, ...]] = None
    max_files: Optional[int] = None
    max_buffer_lines: Optional[int] = None

    def discovery_settings(self, base: DiscoverySettings) -> DiscoverySettings:
        """*base*, with this host's overrides applied.

        Resolution lives here rather than at each call site so "per-host
        settings fall back to the global ones" is one tested function instead of
        a rule every future caller has to remember.
        """

        changes: dict[str, object] = {}
        if self.include_globs is not None:
            changes["include_globs"] = self.include_globs
        if self.exclude_globs is not None:
            changes["exclude_globs"] = self.exclude_globs
        if self.max_files is not None:
            changes["max_files"] = self.max_files
        return replace(base, **changes) if changes else base

    def buffer_lines(self, base: int) -> int:
        """Lines to hold per source from this host.

        Its own knob because five merged remote sources pull five times the
        history across the link on open, and a slow connection needs a pressure
        valve the global default cannot provide.
        """

        return base if self.max_buffer_lines is None else self.max_buffer_lines


#: Options CLV refuses to have in its schema at all, and what to do instead.
#:
#: Ignoring these silently would be the worse failure: an operator who wrote
#: ``password = hunter2`` and saw it work would reasonably believe CLV stores
#: it. The value is never read, never stored on a :class:`RemoteHost`, and the
#: line is reported as unsupported.
_CREDENTIAL_ANSWER = (
    "CLV never stores or transmits a credential. Load your key into ssh-agent, "
    "or point identity_file at a key with no passphrase."
)
_PRIVILEGE_ANSWER = (
    "CLV never escalates privilege, locally or remotely. Add the SSH user to "
    "the log group (adm, systemd-journal) or set an ACL on the path."
)
_REFUSED_HOST_KEYS: dict[str, str] = {
    "password": _CREDENTIAL_ANSWER,
    "passphrase": _CREDENTIAL_ANSWER,
    "password_file": _CREDENTIAL_ANSWER,
    "sudo": _PRIVILEGE_ANSWER,
    "use_sudo": _PRIVILEGE_ANSWER,
    "doas": _PRIVILEGE_ANSWER,
    "pkexec": _PRIVILEGE_ANSWER,
    "become": _PRIVILEGE_ANSWER,
}


@dataclass
class LogConfig:
    """Runtime configuration for one CLV session."""

    log_dirs: list[Path] = field(default_factory=list)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    max_buffer_lines: int = _LIMITS["max_buffer_lines"][0]
    default_show_lines: int = _LIMITS["default_show_lines"][0]
    refresh_hz: int = _LIMITS["refresh_hz"][0]
    min_show_lines: int = _LIMITS["min_show_lines"][0]
    show_step: int = _LIMITS["show_step"][0]
    csv_max_rows: int = _LIMITS["csv_max_rows"][0]
    csv_max_cols: int = _LIMITS["csv_max_cols"][0]
    tree_width: int = _LIMITS["tree_width"][0]
    clipboard_max_bytes: int = _LIMITS["clipboard_max_bytes"][0]
    watch_rate_limit: int = _LIMITS["watch_rate_limit"][0]
    cluster_lookback: int = _LIMITS["cluster_lookback"][0]
    plugin_time_budget_ms: int = _LIMITS["plugin_time_budget_ms"][0]
    plugin_read_budget_ms: int = _LIMITS["plugin_read_budget_ms"][0]
    plugin_sink_timeout_ms: int = _LIMITS["plugin_sink_timeout_ms"][0]
    plugin_host_timeout_ms: int = _LIMITS["plugin_host_timeout_ms"][0]
    #: Ring the terminal bell when a watch rule notifies. Off by default: a
    #: bell is a thing an operator opts into, never a thing a log does to them.
    watch_bell: bool = False
    #: Read the systemd journal. Off by default because reading it means
    #: running `journalctl`, and a plugin may not spawn a subprocess without
    #: consent — which is the argument for the journal being a plugin at all.
    enable_journald: bool = False
    #: Read log folders on remote hosts over SSH. Off by default, and the same
    #: argument as `enable_journald` only more so: reading a remote source means
    #: spawning `ssh`, and a *network* subprocess raises the consent bar rather
    #: than lowering it. With this false nothing connects and nothing spawns,
    #: however many hosts are configured.
    enable_ssh: bool = False
    #: Which plugins in the *user* plugin directory the operator has enabled,
    #: casefolded and de-duplicated, in file order.
    #:
    #: A file in `~/.config/clv/plugins/` is discovered and listed but never
    #: imported until its name appears here. That is the whole of the rule that
    #: installing a plugin is not consent to run it: an unnamed module does not
    #: execute, so dropping a file into the directory cannot run anything.
    #:
    #: A list rather than a boolean on purpose. "Enable everything in this
    #: directory" is exactly the behaviour that makes a dropped file dangerous.
    #:
    #: Bundled drop-ins under `clv/plugins/` are not governed by this: they
    #: shipped with CLV, and the operator's trust in them is the trust they
    #: already placed in CLV.
    plugins: tuple[str, ...] = ()
    #: Every `[plugin:<name>]` section that parsed, keyed on the casefolded
    #: plugin name, values exactly as `configparser` read them.
    #:
    #: Populated regardless of whether the plugin is enabled or even present,
    #: for the same reason `hosts` is populated regardless of `enable_ssh`:
    #: parsing is inert, and a mistake in a section should be reported the
    #: launch it is *made*. Who is missing is the loader's question, not this
    #: one's -- `plugins.load_plugins` reports a section that configures nothing,
    #: because only it knows what loaded.
    #:
    #: The values are raw strings. `config.py` does not interpret a plugin's
    #: keys and must not start: what `patterns` means is the plugin's business,
    #: and a validator here would be CLV guessing at a schema it does not own.
    plugin_settings: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: Every `[ssh:<name>]` section that parsed, in file order.
    #:
    #: Populated regardless of :attr:`enable_ssh`. Parsing is inert, and a
    #: mistake in a host section should be reported the launch it is *made*,
    #: not held back until the launch the switch is flipped.
    hosts: tuple[RemoteHost, ...] = ()
    #: Everything in the settings file CLV could not honour. Never a reason to
    #: fail startup; always a reason to say something.
    issues: tuple[ConfigIssue, ...] = ()

    def with_discovery(self, **changes) -> "LogConfig":
        """Return a copy with individual discovery settings replaced."""
        return replace(self, discovery=replace(self.discovery, **changes))

    def host(self, name: str) -> Optional[RemoteHost]:
        """The configured host called *name*, or ``None``."""

        return next((entry for entry in self.hosts if entry.name == name), None)


def get_xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".config"


def user_config_path() -> Path:
    return get_xdg_config_home() / "clv" / "settings.conf"


def user_plugin_dir() -> Path:
    """Where an operator installs a plugin: beside ``settings.conf``.

    One place, already in their muscle memory, and writable without root on
    every build — which the bundled ``clv/plugins/`` is not. On a `.deb` or
    tarball install that directory is inside a root-owned tree a package
    upgrade overwrites, so for the operator CLV is actually distributed to it
    is not an install path at all.
    """

    return get_xdg_config_home() / "clv" / "plugins"


def bundled_config_path() -> Path:
    """Locate the shipped settings.conf template.

    Under PyInstaller the source tree does not exist on disk; the data file
    added with ``--add-data settings.conf:.`` lands in the extraction root that
    PyInstaller exposes as ``sys._MEIPASS``. Relying on ``__file__`` and
    counting parent directories happens to land in the same place for a onedir
    build, but it is an accident of the layout and breaks under onefile.
    """

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "settings.conf"
    # Development checkout: settings.conf sits beside the clv package.
    return Path(__file__).resolve().parents[2] / "settings.conf"


def bundled_examples_dir() -> Path:
    """Locate the worked example *sources*, wherever this build keeps them.

    **The frozen build is the whole reason this exists**, and the failure it
    fixes was silent in both directions. :data:`SEEDED_EXAMPLES` names its
    modules as strings, so nothing in CLV statically imports ``clv.examples``
    and PyInstaller's analysis never saw them — the modules were not in the
    bundle at all, ``importlib.import_module`` raised, and
    :func:`_seed_plugin_examples` swallowed it because seeding an example is the
    least important thing that happens at startup. Every binary install
    therefore got a ``README.txt`` listing nine files and a directory containing
    none of them, and ``README.md`` told the operator they were "already on your
    machine".

    Even bundled, ``inspect.getsource`` would not have worked: PyInstaller ships
    byte-compiled modules and no source. So they are shipped as **data**
    (``--add-data clv/examples:clv/examples``) and read as files here, with the
    import kept as the source-checkout path.
    """

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "clv" / "examples"
    return Path(__file__).resolve().parents[1] / "examples"


#: Backwards-compatible alias; the template is only a "repo" path when running
#: from a source checkout.
repo_config_path = bundled_config_path


def default_config_text() -> str:
    """The shipped, fully-commented settings file, as text.

    The same precedence :func:`ensure_user_settings_file` uses when it creates a
    file, so what ``--print-default-config`` prints is exactly what a first run
    would have written. It exists because that documentation is *already on every
    user's disk* — PyInstaller puts it beside the binary — with nothing pointing
    at it, and because an existing settings file is never updated: the prose
    explaining a new option only ever reaches a first-run user.
    """

    template = bundled_config_path()
    if template.exists():
        try:
            return template.read_text(encoding="utf-8")
        except OSError:
            pass
    return DEFAULT_SETTINGS_TEMPLATE


def config_version_of(settings_path: Optional[Path]) -> int:
    """The schema version *settings_path* declares, or ``0`` if it declares none.

    Zero is the answer for every file written before the marker existed, for a
    file that is missing, and for a marker that is not an integer. All three
    mean the same thing to the caller -- "older than anything we ship" -- and
    none of them is worth an exception on a path that runs during an install.
    """

    if settings_path is None:
        return 0
    try:
        if not settings_path.exists():
            return 0
        raw = SettingsDocument.load(settings_path).get(
            CONFIG_SECTION, CONFIG_VERSION_OPTION
        )
    except OSError:
        return 0
    return _as_version(raw)


def template_config_version() -> int:
    """The schema version the *shipped* template declares.

    Read out of the template rather than taken from
    :data:`CURRENT_CONFIG_VERSION` so a bundle whose ``settings.conf`` was never
    re-stamped cannot claim to be newer than the file it would write. The
    constant is only the fallback for a template that carries no marker at all.
    """

    document = SettingsDocument(default_config_text().splitlines())
    raw = document.get(CONFIG_SECTION, CONFIG_VERSION_OPTION)
    version = _as_version(raw)
    return version or CURRENT_CONFIG_VERSION


def _as_version(raw: Optional[str]) -> int:
    if raw is None:
        return 0
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def undocumented_settings(settings_path: Optional[Path]) -> tuple[str, ...]:
    """Options the shipped file sets that *settings_path* does not carry.

    Read-only, and deliberately so. Nothing on the launch path rewrites the
    operator's settings file: it is theirs, and every option is
    optional-with-a-default anyway — an old file keeps working, it just stops
    learning. So this reports a difference and nothing here acts on it.
    :mod:`clv.services.config_upgrade` is what acts on it, and only when the
    operator explicitly asks, via ``clv --upgrade-config`` or the installer.

    Only ``[log_viewer]`` is compared. A ``[ssh:<name>]`` section is a machine the
    operator named, not a setting they are missing, and listing the example host
    as an absence would be nonsense.
    """

    if settings_path is None or not settings_path.exists():
        return ()
    try:
        current = SettingsDocument.load(settings_path).options(CONFIG_SECTION)
    except OSError:
        return ()
    reference = SettingsDocument(default_config_text().splitlines())
    known = set(current)
    known.add(CONFIG_VERSION_OPTION)
    return tuple(
        option for option in reference.options(CONFIG_SECTION) if option not in known
    )


def ensure_user_settings_file() -> Optional[Path]:
    """Create the per-user settings file if absent, returning a usable path."""

    target = user_config_path()
    if target.exists():
        return target

    template = repo_config_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return template if template.exists() else None

    # Only copy the shipped template if it actually names sources; an empty
    # log_dirs would hand the user a viewer with nothing in it.
    if template.exists() and _names_sources(template):
        try:
            shutil.copyfile(template, target)
            return target
        except OSError:
            pass

    try:
        target.write_text(DEFAULT_SETTINGS_TEMPLATE, encoding="utf-8")
        return target
    except OSError:
        return template if template.exists() else None


#: Dropped into ``~/.config/clv/plugins/`` the first time CLV runs, so the
#: directory explains itself to an operator who found it before they found the
#: documentation.
#:
#: It says the two things that are not guessable from an empty folder: that a
#: file here does nothing until it is named in ``settings.conf``, and that
#: naming it is an act of trust, because a plugin is ordinary Python running at
#: CLV's privilege rather than something CLV can contain.
PLUGIN_DIR_README = """\
CLV plugins
===========

Put a plugin here: either a single `my_plugin.py`, or a directory
`my_plugin/` containing an `__init__.py`.

A file in this directory is NOT run just because it is here. CLV lists it,
and nothing more, until you name it in the `plugins` line of

    settings.conf

under [log_viewer]:

    plugins = my_plugin, another_plugin

Names are the file (or directory) name without the `.py`, separated by
commas, and matched without regard to case.

A plugin is trusted code
------------------------

A plugin is Python that CLV imports into its own process. It runs with your
privileges and can read every file you can read, including every log CLV has
open. The plugin interfaces bound what CLV *asks* of a plugin; they do not
bound what a plugin *can do*, and CLV does not sandbox one.

Install a plugin the way you would install any other program: from someone
you have reason to trust, after reading it if you can. The trust model, and
what to look at when reviewing a third-party plugin, are written out in
CLV's `clv/plugins/AGENTS.md`.

Worked examples
---------------

Nine complete, commented plugins -- one for every interface CLV publishes --
each walking through what its kind of plugin has to declare and why:

    examples/nginx_error.py   teaches CLV to read nginx's error log, a format
                              the built-in matchers do not recognise
    examples/field_regex.py   adds `svc~^web[0-9]+` - a regex against one
                              field - and `age<60`, seconds since the line
                              was written
    examples/watch_alerts.py  adds a `burst` watch rule kind - "five of these
                              within a minute" - and a sink that appends every
                              watch hit to a file you name. The sink ships
                              inert: it delivers nothing until its
                              [plugin:watch_alerts] section gives it a path
    examples/cluster_rules.py teaches `c` to fold repeats it could not: a
                              Kubernetes pod suffix and an ANSI colour run are
                              normalised away, and one field of your choosing
                              keeps two streams in separate clusters
    examples/timeline_marks.py marks deploys on the timeline (`b`) and scales
                              its bars by bytes rather than by lines. Ships
                              inert: it marks nothing until its
                              [plugin:timeline_marks] section lists moments
    examples/commands.py      adds two commands to `C`: one sets the query to
                              errors-only, one opens a panel that writes the
                              filtered lines to a file you name. Its key is
                              yours to pick, in [plugin:commands]
    examples/redact_secrets.py hides the value beside `password=`, `token=`
                              and friends wherever the line is shown - the
                              pane, the detail pane, an export, the clipboard.
                              Edit the list in [plugin:redact_secrets]
    examples/html_report.py   adds an HTML report to `Ctrl+E`: one
                              self-contained file carrying the query that
                              produced it, ready to attach to a ticket
    examples/container_logs.py offers every running container as a source,
                              tailing live through podman or docker. Ships
                              inert: it runs nothing until its
                              [plugin:container_logs] section says enabled

Nothing in `examples/` is listed or run: it is one directory down, and CLV
only looks here. To use one, copy it up and name it:

    cp examples/field_regex.py .

then add `field_regex` to the `plugins` line in settings.conf. Copying is
also how you start your own - it is a better starting point than an empty
file.
"""

#: Subdirectory of the plugin directory the worked examples are written to.
#:
#: **One level down, and that is the whole design of it.** Written into the
#: plugin directory itself an example would be *discovered*, so a fresh install
#: with nothing installed would report "1 not enabled" forever — and the plugin
#: count, which is supposed to mean "plugins someone installed", would be
#: counting one that CLV installed. ``pkgutil.iter_modules`` does not descend,
#: and a directory with no ``__init__.py`` is not a package, so nothing here is
#: listed, discovered or importable. Copying a file up one level is what turns
#: an example into an installed plugin — which is the same act as installing any
#: other, and is the sentence the README here ends on.
PLUGIN_EXAMPLES_DIR = "examples"

#: Worked examples written into that directory on first run, as
#: ``{filename: module}``.
SEEDED_EXAMPLES: dict[str, str] = {
    "nginx_error.py": "clv.examples.nginx_error",
    "field_regex.py": "clv.examples.field_regex",
    "watch_alerts.py": "clv.examples.watch_alerts",
    "cluster_rules.py": "clv.examples.cluster_rules",
    "timeline_marks.py": "clv.examples.timeline_marks",
    "commands.py": "clv.examples.commands",
    "redact_secrets.py": "clv.examples.redact_secrets",
    "html_report.py": "clv.examples.html_report",
    "container_logs.py": "clv.examples.container_logs",
}


def _seed_plugin_examples(target: Path) -> None:
    """Write the worked examples into *target*, never over one already there.

    Written from the packaged module's source rather than shipped as a data file
    so there is exactly one copy in the tree: the example is imported by the
    suite and parsed against a fixture, so it cannot rot into something that no
    longer loads while still being handed to every new operator.

    **Only when absent.** An operator who edited the example, or deleted it
    because they did not want it, gets to keep that decision — rewriting it on
    every start would undo both. Never raises: seeding an example is the least
    important thing that happens at startup.
    """

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    bundled = bundled_examples_dir()
    for filename, module in SEEDED_EXAMPLES.items():
        destination = target / filename
        if destination.exists():
            continue
        source = _example_source(bundled / filename, module)
        if source is None:
            continue
        try:
            destination.write_text(source, encoding="utf-8")
        except OSError:
            continue


def _example_source(bundled: Path, module: str) -> Optional[str]:
    """The text of one worked example, from the bundle or from the import.

    The file first, because that is the only one of the two that works in a
    frozen build -- see :func:`bundled_examples_dir`. The import second, because
    a source checkout that has not been packaged has the module and may not have
    thought about the data path, and because it is what this did before.

    ``None`` rather than a raise: a missing example is a missing example, and a
    startup that failed over one would be a far worse bug than the one this is
    fixing.
    """

    try:
        if bundled.is_file():
            return bundled.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        return inspect.getsource(importlib.import_module(module))
    except Exception:  # noqa: BLE001 - a stripped build has no source
        return None


def ensure_user_plugin_dir() -> Optional[Path]:
    """Create the user plugin directory and its README, returning the path.

    Separate from :func:`ensure_user_settings_file` rather than folded into it,
    which would have been the shorter edit and the wrong one: that function
    returns early when ``settings.conf`` already exists, so every installation
    that predates this directory would never get it.

    Never raises. A read-only or unwritable ``$HOME`` means no user plugins,
    which is a valid state and not worth a startup failure.
    """

    target = user_plugin_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    _seed_plugin_examples(target / PLUGIN_EXAMPLES_DIR)

    readme = target / "README.txt"
    if not readme.exists():
        try:
            readme.write_text(PLUGIN_DIR_README, encoding="utf-8")
        except OSError:
            pass
    return target


def _names_sources(path: Path) -> bool:
    try:
        return _text_names_sources(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def _text_names_sources(text: str) -> bool:
    """Whether *text* would give the viewer somewhere to look.

    Split out from :func:`_names_sources` so the upgrade path can apply the same
    guard to a merged document it has not written yet: handing the operator a
    settings file with an empty ``log_dirs`` is worse than leaving theirs alone.
    """

    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return False
    if CONFIG_SECTION not in parser:
        return False
    return bool(parser[CONFIG_SECTION].get("log_dirs", "").strip())


def get_config_file() -> Optional[Path]:
    user = ensure_user_settings_file()
    if user:
        return user
    repo = repo_config_path()
    return repo if repo.exists() else None


class _EmptySection:
    """Stand-in for a missing ``[log_viewer]`` section, so reads return defaults."""

    @staticmethod
    def get(_option: str, fallback=None):
        return fallback

    @staticmethod
    def getint(_option: str, fallback=None):
        return fallback

    @staticmethod
    def getboolean(_option: str, fallback=None):
        return fallback


_EMPTY = _EmptySection()


def _clamp(value: int, option: str) -> int:
    default, minimum, maximum = _LIMITS[option]
    if not isinstance(value, int):
        return default
    return max(minimum, min(value, maximum))


def _read_int(section, option: str) -> int:
    default = _LIMITS[option][0]
    try:
        raw = section.getint(option, fallback=default)
    except (ValueError, AttributeError):
        return default
    return _clamp(raw if raw is not None else default, option)


def _read_bool(section, option: str, default: bool) -> bool:
    try:
        value = section.getboolean(option, fallback=default)
    except (ValueError, AttributeError):
        return default
    return default if value is None else bool(value)


def _read_globs(section, option: str, default):
    raw = section.get(option, fallback=None)
    if raw is None:
        return default
    parts = tuple(piece.strip() for piece in raw.split(",") if piece.strip())
    # An explicitly empty value means "no filtering", which is meaningful for
    # include_globs and must not silently fall back to the default. For a host
    # section that distinction is also inherit-vs-override: the default passed
    # in is `None`, and only an absent option can return it.
    return parts


def _split_list(raw: str) -> list[str]:
    """The comma-separated shape ``log_dirs`` uses, stripped of quotes.

    Stripping stays out of the ref layer: a trailing space is a legal filename
    component, so it may be tidied away from a config *line* and never from a
    ref.
    """

    return [
        text
        for text in (piece.strip().strip('"').strip("'") for piece in raw.split(","))
        if text
    ]


def _read_plugin_list(section, issues: list[ConfigIssue]) -> tuple[str, ...]:
    """Parse the ``plugins`` enable-list, reporting each name it cannot use.

    Per-entry rather than all-or-nothing: this follows :func:`parse_log_dirs`,
    which drops and reports the one entry it cannot read and keeps the rest.
    Voiding the whole list on a single stray character would silently disable a
    working set of plugins, which is the failure an operator is least equipped
    to diagnose from the outside.

    Matching is case-insensitive. A settings file is operator prose, and
    ``Redact`` where the file on disk is ``redact.py`` is a typo class rather
    than an intent -- so the name is casefolded here and compared casefolded by
    the loader.
    """

    raw = section.get("plugins", fallback=None)
    if raw is None:
        return ()

    names: list[str] = []
    for text in _split_list(raw):
        # A plugin name is a module name, so it is exactly what Python will
        # accept as one. Rejecting `my-plugin`, `foo.bar` and `../evil` here
        # means the loader never builds an import path out of operator text.
        if not text.isidentifier():
            issues.append(
                ConfigIssue(
                    "plugins",
                    f"{text!r} is not a usable plugin name: name the file "
                    "without its .py, using letters, digits and underscores",
                )
            )
            continue
        if text.startswith("_"):
            issues.append(
                ConfigIssue(
                    "plugins",
                    f"{text!r} is not loadable: CLV skips modules whose name "
                    "starts with an underscore",
                )
            )
            continue
        names.append(text.casefold())

    # Order-preserving de-duplication, as `persist_log_sources` does for the
    # source list: naming a plugin twice is a harmless mistake, not an issue.
    return tuple(dict.fromkeys(names))


#: What to tell an operator whose ``log_dirs`` entry read as an identifier:
#: what it was taken for, and where that kind of source actually comes from.
#:
#: One entry per scheme because the two have different answers — the journal is
#: offered by its own source, a remote root belongs to a host section — and a
#: generic "that is a scheme" would leave the operator to guess which.
_SCHEME_ANSWERS: dict[str, tuple[str, str]] = {
    "journal": (
        "a journald identifier",
        "The journal is offered by the journald source when enable_journald is "
        "true, not by log_dirs.",
    ),
    "ssh": (
        "a remote source identifier",
        "Remote roots belong to a host's own log_dirs, inside an [ssh:<name>] "
        "section.",
    ),
}


def parse_log_dirs(
    raw: str, issues: Optional[list[ConfigIssue]] = None
) -> list[SourceRef]:
    """Turn the comma-separated ``log_dirs`` value into absolute source refs.

    Relative entries are resolved against the working directory rather than
    discarded, so ``log_dirs = ./logs`` behaves the way it reads. This is the
    **user-input** boundary — ``normalize_ref`` expands and absolutises what a
    person typed, and leaves an identifier such as ``journal:all`` alone rather
    than inventing ``$CWD/journal:all`` for it.

    Splitting on ``,`` is why a ref string may not contain one. A local filename
    with a comma in it is already mangled here, and has been since this function
    was written; that is not made worse, and not fixed, by refs.

    **An entry that reads as a registered scheme is refused**, and this is the
    debt ``SSH_TODO.md`` Phase 1 recorded and left for Phase 3 to pay. A
    *relative* directory literally named ``journal:archive`` is not unreachable
    — ``Path`` still resolves it against the working directory — but it is the
    one entry ``normalize_ref`` hands back **unpinned**, so unlike every sibling
    it means a different place depending on where CLV was launched from. It
    works, right up until someone starts the viewer somewhere else, which is
    worse than a clean refusal.

    Every scheme entry is refused rather than only those that shadow a real
    directory, and that is what keeps this a rule instead of a heuristic:
    nothing legitimately puts a scheme ref in the global ``log_dirs``. The
    journal comes from its provider's ``discover()``; a remote root comes from a
    host section. CLV cannot write one back here either — ``sources.check_access``
    rejects a scheme ref as missing, so ``persist_log_sources`` never sees one.

    An **absolute** path of the same name still parses: ``is_local`` short
    circuits on ``is_absolute()`` before the scheme regex is ever consulted.
    """

    entries: list[SourceRef] = []
    seen: set[str] = set()
    for text in _split_list(raw):
        scheme = scheme_of(text)
        if scheme is not None:
            if issues is not None:
                noun, answer = _SCHEME_ANSWERS.get(
                    scheme, (f"a {scheme} identifier", "")
                )
                sentences = [
                    f"{text!r} reads as {noun}, not a path.",
                    answer,
                    "If you meant a directory of that name, give its absolute path.",
                ]
                issues.append(
                    ConfigIssue("log_dirs", " ".join(part for part in sentences if part))
                )
            continue
        ref = normalize_ref(text)
        key = format_ref(ref)
        if key in seen:
            continue
        seen.add(key)
        entries.append(ref)
    return entries


# --- remote hosts -----------------------------------------------------------


def validate_port(raw: str) -> tuple[Optional[int], Optional[str]]:
    """``(port, complaint)`` for something typed or read as a port.

    Shared by the parser and the host dialog so the two cannot describe the same
    typo differently. Deliberately not clamped: clamping 70000 to 65535 would
    quietly connect somewhere the operator never named, so an impossible port is
    reported and the host is refused.
    """

    low, high = _PORT_RANGE
    text = (raw or "").strip()
    if not text:
        return _DEFAULT_SSH_PORT, None
    try:
        port = int(text)
    except ValueError:
        return None, f"port {text!r} is not a number; give one in {low}-{high}."
    if not low <= port <= high:
        return None, f"port {port} is outside {low}-{high}."
    return port, None


def validate_identity_file(raw: str) -> tuple[Optional[Path], Optional[str]]:
    """``(path, warning)``. A missing key file warns; it never refuses.

    The host may still work: ssh-agent commonly already holds the key and
    ``~/.ssh/config`` may name another. Refusing here would lose a working
    machine to a stale line, which Requirement 7 forbids more strongly than it
    asks for strictness — so the second element is a *warning* at both call
    sites, and a dialog that treated it as blocking would be wrong.
    """

    text = (raw or "").strip()
    if not text:
        return None, None
    candidate = Path(text).expanduser()
    if candidate.is_file():
        return candidate, None
    return candidate, (
        f"identity_file '{candidate}' does not exist; the connection "
        "will fall back to ssh-agent and ~/.ssh/config."
    )


def validate_remote_dirs(raw: str) -> tuple[tuple[str, ...], list[str]]:
    """``(dirs, complaints)`` for a host's roots, as paths on *that* machine.

    Validated for absoluteness and nothing else — there is nothing on this
    machine to check them against. A bare relative path means the SSH user's
    home directory on some shells and the login directory on others; ``~/logs``
    says the same thing unambiguously, so it is accepted and the ambiguous form
    is refused.
    """

    dirs: list[str] = []
    complaints: list[str] = []
    for text in _split_list(raw):
        if not text.startswith(("/", "~")):
            complaints.append(
                f"log_dirs entry {text!r} is relative; give an absolute path "
                "on the remote host, or a ~-relative one."
            )
            continue
        if text not in dirs:
            dirs.append(text)
    return tuple(dirs), complaints


def validate_host_name(name: str, existing: Iterable[str] = ()) -> Optional[str]:
    """What is wrong with *name* as a ``[ssh:<name>]`` suffix, or ``None``.

    The dialog's rule rather than the parser's, and the wording differs on
    purpose: the parser is describing a *section* it found in a file and says so
    ("has no host name; use [ssh:<name>]"), while this is describing a field
    someone is typing into. The two share the shape of the rule, not the
    sentence, because a message that reads correctly in both places reads
    naturally in neither.

    ``[`` and ``]`` are refused because a name carrying one produces a header
    ``configparser`` reads as a different section than the one intended, and
    surrounding whitespace because ``_parse_hosts`` strips it — so ``[ssh: web01 ]``
    and ``[ssh:web01]`` are one host with two spellings, and the file would not
    round trip.
    """

    if not name.strip():
        return "Give the host a name."
    if name != name.strip():
        return "A host name cannot start or end with a space."
    if any(character in name for character in "[]\n"):
        return "A host name cannot contain [ or ]."
    if name in set(existing):
        return f"A host named {name!r} is already configured."
    return None


def host_options(host: RemoteHost) -> list[tuple[str, str]]:
    """*host* as the ``key = value`` lines a ``[ssh:<name>]`` section carries.

    The exact inverse of :func:`_parse_host`, pinned by a round-trip test —
    a serialiser that drifts from its parser writes a file that reads back as a
    different machine.

    Only non-default values are emitted. A section listing all eleven options at
    their defaults is noise in a file whose whole design is that an absent option
    inherits, and the operator has to read it every time they open the file.
    """

    options: list[tuple[str, str]] = []
    if host.host and host.host != host.name:
        options.append(("host", host.host))
    if host.user:
        options.append(("user", host.user))
    if host.port != _DEFAULT_SSH_PORT:
        options.append(("port", str(host.port)))
    if host.identity_file is not None:
        options.append(("identity_file", str(host.identity_file)))
    options.append(("log_dirs", ", ".join(host.log_dirs)))
    if host.include_globs is not None:
        options.append(("include_globs", ", ".join(host.include_globs)))
    if host.exclude_globs is not None:
        options.append(("exclude_globs", ", ".join(host.exclude_globs)))
    if host.max_files is not None:
        options.append(("max_files", str(host.max_files)))
    if host.max_buffer_lines is not None:
        options.append(("max_buffer_lines", str(host.max_buffer_lines)))
    if host.correct_clock_skew:
        options.append(("correct_clock_skew", "true"))
    if not host.enabled:
        options.append(("enabled", "false"))
    return options


def _read_port(section, issues: list[ConfigIssue], origin: str) -> Optional[int]:
    """The host's port, or ``None`` meaning the section cannot be used.

    Deliberately not ``_read_int``: that clamps, and clamping 70000 to 65535
    would quietly connect somewhere the operator never named. A port outside the
    range is a typo, and a typo is reported.
    """

    port, complaint = validate_port(section.get("port", fallback="") or "")
    if complaint is not None:
        issues.append(ConfigIssue(origin, complaint))
    return port


def _read_optional_int(
    section, option: str, issues: list[ConfigIssue], origin: str
) -> Optional[int]:
    """A per-host budget override, clamped, or ``None`` to inherit the global."""

    raw = (section.get(option, fallback="") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        issues.append(
            ConfigIssue(
                origin,
                f"{option} = {raw!r} is not a number; the global value is used.",
                "warning",
            )
        )
        return None
    return _clamp(value, option)


def _read_remote_dirs(
    raw: str, issues: list[ConfigIssue], origin: str
) -> tuple[str, ...]:
    """The host's roots, as strings on *that* machine.

    Validated for absoluteness and nothing else. A bare relative path would mean
    the SSH user's home directory on some shells and the login directory on
    others; ``~/logs`` says the same thing unambiguously, so it is accepted and
    the ambiguous form is refused.
    """

    dirs, complaints = validate_remote_dirs(raw)
    issues.extend(ConfigIssue(origin, complaint) for complaint in complaints)
    return dirs


def _read_identity_file(
    section, issues: list[ConfigIssue], origin: str
) -> Optional[Path]:
    """The named key file, warning rather than refusing when it is not there.

    A warning because the host may still work: ssh-agent commonly already holds
    the key, and ``~/.ssh/config`` may name another. Refusing here would lose a
    working machine to a stale line, which Requirement 7 forbids more strongly
    than it asks for strictness.
    """

    candidate, warning = validate_identity_file(
        section.get("identity_file", fallback="") or ""
    )
    if warning is not None:
        issues.append(ConfigIssue(origin, warning, "warning"))
    return candidate


def _parse_host(
    name: str, section, issues: list[ConfigIssue], origin: str
) -> Optional[RemoteHost]:
    """One ``[ssh:<name>]`` section, or ``None`` if it cannot be used.

    Everything that makes the host unusable — no name, an impossible port, no
    roots — skips it and says why. Everything recoverable warns and keeps it.
    Nothing raises: one malformed section may not cost the operator the rest of
    their configuration.
    """

    for option in section:
        answer = _REFUSED_HOST_KEYS.get(option.lower())
        if answer is not None:
            issues.append(
                ConfigIssue(origin, f"{option} is not a supported option. {answer}")
            )

    port = _read_port(section, issues, origin)
    if port is None:
        return None

    log_dirs = _read_remote_dirs(section.get("log_dirs", fallback="") or "", issues, origin)
    if not log_dirs:
        issues.append(
            ConfigIssue(
                origin,
                "no usable log_dirs; name at least one absolute folder or file "
                "on the remote host.",
            )
        )
        return None

    user = (section.get("user", fallback="") or "").strip() or None

    return RemoteHost(
        name=name,
        # An absent `host` means the section name, which is exactly how a
        # ~/.ssh/config `Host` alias already reads. `host` is the override for
        # when CLV's name for a machine is not the address to reach it at.
        host=(section.get("host", fallback="") or "").strip() or name,
        user=user,
        port=port,
        identity_file=_read_identity_file(section, issues, origin),
        log_dirs=log_dirs,
        enabled=_read_bool(section, "enabled", True),
        correct_clock_skew=_read_bool(section, "correct_clock_skew", False),
        include_globs=_read_globs(section, "include_globs", None),
        exclude_globs=_read_globs(section, "exclude_globs", None),
        max_files=_read_optional_int(section, "max_files", issues, origin),
        max_buffer_lines=_read_optional_int(section, "max_buffer_lines", issues, origin),
    )


def _parse_plugin_sections(
    parser, issues: list[ConfigIssue]
) -> dict[str, dict[str, str]]:
    """Read every ``[plugin:<name>]`` section, skipping the ones it cannot use.

    Modelled on :func:`_parse_hosts`, and for the same reason: a malformed
    section costs itself and nothing else. A settings file is the operator's,
    and one bad header must not be able to void the plugin next to it -- still
    less to stop CLV starting.

    The two name checks are ``_read_plugin_list``'s, deliberately. A name that
    the enable-list would refuse is a name the loader will never import, so a
    section for it can only ever configure nothing; catching it here means the
    operator is told once, in terms that match what they wrote.
    """

    found: dict[str, dict[str, str]] = {}
    for raw_section in parser.sections():
        if not raw_section.startswith(PLUGIN_SECTION_PREFIX):
            continue
        origin = f"[{raw_section}]"
        name = raw_section[len(PLUGIN_SECTION_PREFIX) :].strip()
        if not name:
            issues.append(
                ConfigIssue(origin, "has no plugin name; use [plugin:<name>].")
            )
            continue
        if not name.isidentifier() or name.startswith("_"):
            issues.append(
                ConfigIssue(
                    origin,
                    f"{name!r} is not a usable plugin name: name the plugin as "
                    "its file is named, without the .py",
                )
            )
            continue
        key = name.casefold()
        if key in found:
            issues.append(
                ConfigIssue(origin, f"{name!r} is already configured; skipped.")
            )
            continue
        try:
            found[key] = dict(parser[raw_section])
        except Exception as exc:  # pragma: no cover - defensive
            issues.append(ConfigIssue(origin, f"could not be read: {exc}"))
    return found


def plugin_settings_for(config: "LogConfig") -> dict[str, dict[str, str]]:
    """The settings each plugin should be handed, legacy keys folded in.

    Takes the whole :class:`LogConfig` rather than the parser, and that is what
    makes the aliasing correct rather than merely convenient: ``app.py`` turns
    the journal on with ``replace(self._config, enable_journald=True)`` and
    never re-reads the file, so an alias resolved at parse time would hand the
    plugin the value from disk while the drawer showed the operator the value in
    memory. Resolved from the config object, both agree by construction.

    Copies, so mutating the result cannot reach into ``LogConfig``. The registry
    then owns these dicts for the session and mutates them in place -- see
    :meth:`~clv.plugins.PluginRegistry.refresh_settings`.
    """

    settings = {
        name: dict(values) for name, values in config.plugin_settings.items()
    }
    for name, aliases in _LEGACY_PLUGIN_KEYS.items():
        section = settings.setdefault(name, {})
        for key, option in aliases.items():
            if key in section:
                continue
            value = getattr(config, option, None)
            if value is None:
                continue
            section[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return settings


def _parse_hosts(parser, issues: list[ConfigIssue]) -> tuple[RemoteHost, ...]:
    hosts: list[RemoteHost] = []
    claimed: set[str] = set()
    for raw_section in parser.sections():
        if not raw_section.startswith(SSH_SECTION_PREFIX):
            continue
        origin = f"[{raw_section}]"
        name = raw_section[len(SSH_SECTION_PREFIX) :].strip()
        if not name:
            issues.append(
                ConfigIssue(origin, "has no host name; use [ssh:<name>].")
            )
            continue
        if name in claimed:
            issues.append(
                ConfigIssue(origin, f"a host named {name!r} is already configured; skipped.")
            )
            continue
        try:
            host = _parse_host(name, parser[raw_section], issues, origin)
        except Exception as exc:  # pragma: no cover - defensive
            issues.append(ConfigIssue(origin, f"could not be read: {exc}"))
            continue
        if host is not None:
            claimed.add(name)
            hosts.append(host)
    return tuple(hosts)


def _read_parser(
    resolved: Optional[Path], issues: list[ConfigIssue]
) -> configparser.ConfigParser:
    """Parse the settings file, surviving a duplicate rather than discarding it.

    The strict read comes first, so a well-formed file behaves exactly as it
    always has. A duplicate section or option used to raise, be swallowed here,
    and cost the operator **every setting in the file** — one repeated
    ``[ssh:web01]`` and the whole configuration silently became defaults. It is
    re-read non-strict instead (last definition wins) and the duplicate is
    reported by name.
    """

    parser = configparser.ConfigParser()
    if resolved is None:
        return parser
    try:
        parser.read(resolved)
        return parser
    except (configparser.DuplicateSectionError, configparser.DuplicateOptionError) as exc:
        issues.append(
            ConfigIssue(
                str(resolved),
                f"{exc.message.splitlines()[0] if exc.message else exc}; "
                "the last definition wins.",
            )
        )
    except configparser.Error as exc:
        issues.append(ConfigIssue(str(resolved), f"could not be parsed: {exc}"))
        return configparser.ConfigParser()
    except OSError as exc:
        issues.append(ConfigIssue(str(resolved), f"could not be read: {exc}"))
        return configparser.ConfigParser()

    lenient = configparser.ConfigParser(strict=False)
    try:
        lenient.read(resolved)
    except (OSError, configparser.Error):
        return configparser.ConfigParser()
    return lenient


def load_config(path: Optional[Path] = None) -> LogConfig:
    """Load configuration, falling back to defaults for anything unusable."""

    issues: list[ConfigIssue] = []
    resolved = path if path is not None else get_config_file()
    parser = _read_parser(resolved, issues)

    section = parser[CONFIG_SECTION] if CONFIG_SECTION in parser else _EMPTY

    log_dirs = parse_log_dirs(section.get("log_dirs", fallback="") or "", issues)

    discovery = DiscoverySettings(
        include_globs=_read_globs(section, "include_globs", ()),
        exclude_globs=_read_globs(section, "exclude_globs", DEFAULT_EXCLUDE_GLOBS),
        follow_symlinks=_read_bool(section, "follow_symlinks", False),
        skip_binary=_read_bool(section, "skip_binary", True),
        max_files=_read_int(section, "max_files"),
        group_rotated=_read_bool(section, "group_rotated", True),
    )

    config = LogConfig(
        log_dirs=log_dirs,
        discovery=discovery,
        max_buffer_lines=_read_int(section, "max_buffer_lines"),
        default_show_lines=_read_int(section, "default_show_lines"),
        refresh_hz=_read_int(section, "refresh_hz"),
        min_show_lines=_read_int(section, "min_show_lines"),
        show_step=_read_int(section, "show_step"),
        csv_max_rows=_read_int(section, "csv_max_rows"),
        csv_max_cols=_read_int(section, "csv_max_cols"),
        tree_width=_read_int(section, "tree_width"),
        clipboard_max_bytes=_read_int(section, "clipboard_max_bytes"),
        watch_rate_limit=_read_int(section, "watch_rate_limit"),
        cluster_lookback=_read_int(section, "cluster_lookback"),
        plugin_time_budget_ms=_read_int(section, "plugin_time_budget_ms"),
        plugin_read_budget_ms=_read_int(section, "plugin_read_budget_ms"),
        plugin_sink_timeout_ms=_read_int(section, "plugin_sink_timeout_ms"),
        plugin_host_timeout_ms=_read_int(section, "plugin_host_timeout_ms"),
        watch_bell=_read_bool(section, "watch_bell", False),
        enable_journald=_read_bool(section, "enable_journald", False),
        plugins=_read_plugin_list(section, issues),
        plugin_settings=_parse_plugin_sections(parser, issues),
        enable_ssh=_read_bool(section, "enable_ssh", False),
        hosts=_parse_hosts(parser, issues),
        issues=tuple(issues),
    )

    # default_show_lines must not exceed what the buffer can hold.
    if config.default_show_lines > config.max_buffer_lines:
        config.default_show_lines = config.max_buffer_lines
    if config.min_show_lines > config.max_buffer_lines:
        config.min_show_lines = config.max_buffer_lines
    return config
