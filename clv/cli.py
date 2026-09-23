"""CLV's command line: subcommands, and a bare ``clv`` that never changes.

Bare ``clv`` launches the TUI and is the only behaviour here that is promised
forever (``PLUGIN_TODO.md`` Requirement 13). Everything else is a *recognised*
subcommand diverting before the screen is built; anything unrecognised is a
usage error rather than a guess.

**Nothing a plugin supplies reaches this module.** :data:`SUBCOMMANDS` is a
closed literal, because an installed file must not be able to change what a
shell command does -- the whole point of an enable list is undone if dropping a
plugin in place redefines ``clv``. A plugin that wants to be invoked implements
``Command`` and is reached from ``C`` inside the running viewer, where the
operator is present. ``PluginRegistry.add`` reports an author who assumed
otherwise; see ``_CLI_ATTRIBUTES`` there.

**Textual is imported on the TUI branch and nowhere else.** ``clv doctor`` has
to work over a pipe, in a CI step, and in a bug report, so it may not need a
terminal to say why a plugin did not load. Pinned by a test that imports this
module out of process and asserts ``textual`` never arrives.

**Two hazards worth knowing before editing this file or its entry point.**

*A frozen build's spawned children arrive here as argv.* ``multiprocessing``'s
spawn method starts a child by re-running ``sys.executable``, which in a bundle
is the CLV binary, invoked as ``clv --multiprocessing-fork …``. That is not a
command line anyone typed and this parser would reject it with a usage error,
which would break plugin isolation in exactly the build it matters in.
``clv/__main__.py`` intercepts it with ``freeze_support()`` **before** importing
this module, and the ordering is asserted against the source in
``tests/test_plugin_isolation.py``.

*The positional scan below assumes no top-level option takes a value.* All
three are flags, so the first token that is not an option is the subcommand
slot. ``test_cli.py`` pins that rather than trusting it: add a value-taking
top-level option and the test fails there, instead of ``clv --config x`` being
reported as an attempt to open ``x`` as a log.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence, TextIO

from . import __version__
from .services.config import (
    default_config_text,
    ensure_user_plugin_dir,
    get_config_file,
    load_config,
    plugin_settings_for,
    user_config_path,
)
from .services.config_upgrade import describe as describe_upgrade
from .services.config_upgrade import upgrade_user_settings

if TYPE_CHECKING:  # pragma: no cover - annotations only
    # Under `from __future__ import annotations` these cost nothing at runtime,
    # which is the point: `clv doctor` must not import the plugin layer merely
    # to be type-annotated, and a report helper should still say what it takes.
    from .plugins import PluginRegistry, PluginStatus
    from .services.config import LogConfig

#: Every subcommand CLV has, and the complete list. Closed and literal: see the
#: module docstring. ``clv`` with none of these launches the viewer.
SUBCOMMANDS = ("doctor", "plugin")

#: What ``clv plugin`` accepts, and the complete list. Closed for the same
#: reason :data:`SUBCOMMANDS` is, and asserted against the parser's own choices
#: by ``tests/test_cli.py`` so a later change that fed it a registry fails there
#: however reasonable it looked.
#:
#: Alphabetical rather than grouped: this is a lookup table, and the ``--help``
#: output an operator scans is ordered by the parser, not by this.
PLUGIN_SUBCOMMANDS = ("info", "install", "list", "remove", "trust", "verify")

#: Exit codes, as ``PLUGIN_TODO.md`` Phase 14 defines them. 2 is argparse's own
#: convention for a usage error and is what it already exits with, so this
#: agrees with the parser rather than overriding it.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

_RESERVED_SOURCE_NOTE = """\
clv: opening a source from the command line is not supported.

Start clv and add the source from the tree with `a`, or name its folder in
`log_dirs` in your settings file ({settings}). Either way it is remembered,
which a command-line argument would not be."""


def build_parser() -> argparse.ArgumentParser:
    """The whole command line, in one place.

    Separate from :func:`main` so a test can inspect the parser itself -- that
    every top-level option is a flag, and that :data:`SUBCOMMANDS` is what it
    was built from.
    """

    parser = argparse.ArgumentParser(
        prog="clv",
        description="Centralized Log Viewer — a terminal log viewer.",
        epilog=(
            "With no arguments, clv launches the viewer. "
            "Exit codes: 0 success, 1 failure, 2 usage error."
        ),
    )
    # Not decoration: an operator running a stale bundle has no way to tell, and
    # "the feature is missing" and "the build is old" look identical from the UI.
    parser.add_argument(
        "--version", action="version", version=f"clv {__version__}"
    )
    parser.add_argument(
        "--print-default-config",
        action="store_true",
        help=(
            "print the shipped, fully commented settings file and exit. This is "
            "the reference a newer version documents; --upgrade-config is how to "
            "fold it into the file you already have."
        ),
    )
    parser.add_argument(
        "--upgrade-config",
        action="store_true",
        help=(
            "rewrite your settings file from the shipped template, keeping your "
            "values and hosts, after saving the previous one alongside it. Does "
            "nothing if it is already current. The installer runs this for you."
        ),
    )

    # Optional by default, which is what keeps bare `clv` parsing rather than
    # erroring — Requirement 13, and the first test in tests/test_cli.py.
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = commands.add_parser(
        "doctor",
        help="report this build, its settings and every plugin, then exit",
        description=(
            "Print what CLV is, what it read, and what every plugin did — "
            "without starting the viewer. Exits 0 even when a plugin failed: "
            "a broken plugin is what this command is for reporting, not a "
            "reason for it to fail."
        ),
    )
    doctor.set_defaults(handler=_doctor)

    plugin = commands.add_parser(
        "plugin",
        help="install, inspect, verify and remove plugins",
        description=(
            "Manage what is in your plugin directories. No command here imports "
            "a plugin, so none of them can run the code they are describing."
        ),
    )
    # Required, so `clv plugin` alone is a usage error rather than a silence.
    plugin_commands = plugin.add_subparsers(
        dest="plugin_command", metavar="<command>", required=True
    )

    plugin_list = plugin_commands.add_parser(
        "list",
        help="list installed plugins without importing any of them",
        description=(
            "List what is in your plugin directories. Nothing is imported, so "
            "listing a plugin cannot run it."
        ),
    )
    plugin_list.set_defaults(handler=_plugin_list)

    plugin_info = plugin_commands.add_parser(
        "info",
        help="what one plugin declares, and where it came from",
        description=(
            "Everything CLV knows about one plugin without running it: its "
            "manifest, where it was installed from, who signed it, and its own "
            "settings section if it has one."
        ),
    )
    plugin_info.add_argument("name", help="the plugin's name, as `plugin list` prints it")
    plugin_info.set_defaults(handler=_plugin_info)

    plugin_install = plugin_commands.add_parser(
        "install",
        help="install a plugin from a path, a tar archive or an https URL",
        description=(
            "Verify a plugin and copy it into your plugin directory. It does "
            "NOT enable it: installing a plugin is not consent to run it, and "
            "this prints the line to add when you decide it is."
        ),
        epilog=(
            "An archive or a URL has to carry a clv-plugin.toml declaring the "
            "sha256 of every file. A .py or a directory on this machine does "
            "not — that is the same act as copying it in by hand."
        ),
    )
    plugin_install.add_argument(
        "source", help="a .py file, a directory, a .tar.gz, or an https:// URL"
    )
    plugin_install.add_argument(
        "--force",
        action="store_true",
        help="replace a plugin of the same name that is already installed",
    )
    plugin_install.add_argument(
        "--sha256",
        metavar="DIGEST",
        help=(
            "the archive's expected sha256, as published beside the download. "
            "Checked before anything is unpacked — it is the one number that "
            "did not come from inside the archive."
        ),
    )
    plugin_install.set_defaults(handler=_plugin_install)

    plugin_remove = plugin_commands.add_parser(
        "remove",
        help="delete an installed plugin's files",
        description=(
            "Delete the plugin and stop it being enabled. Its [plugin:<name>] "
            "settings section is kept, so reinstalling does not mean setting it "
            "up again."
        ),
    )
    plugin_remove.add_argument("name", help="the plugin to remove")
    plugin_remove.add_argument(
        "--purge",
        action="store_true",
        help="also delete its [plugin:<name>] section from your settings file",
    )
    plugin_remove.set_defaults(handler=_plugin_remove)

    plugin_verify = plugin_commands.add_parser(
        "verify",
        help="re-check installed plugins against their manifests",
        description=(
            "Re-hash every file a plugin declared and re-check its signature "
            "against the keys you trust now. With no name, everything that was "
            "installed with a manifest."
        ),
    )
    plugin_verify.add_argument(
        "name", nargs="*", help="the plugins to check; omit for all of them"
    )
    plugin_verify.set_defaults(handler=_plugin_verify)

    plugin_trust = plugin_commands.add_parser(
        "trust",
        help="trust a key that signs plugins, or list the ones you trust",
        description=(
            "CLV ships no keys and trusts nobody by default. A signature counts "
            "for something only once you have put its signer here."
        ),
        epilog=(
            'Typically: clv plugin trust "alice@example.com '
            '$(cat alice.pub)". The format is OpenSSH\'s own allowed_signers.'
        ),
    )
    plugin_trust.add_argument(
        "signer",
        nargs="?",
        help='a line of the form "<who> <key-type> <key>"',
    )
    plugin_trust.add_argument(
        "--list",
        action="store_true",
        dest="list_signers",
        help="print the signers you trust and exit",
    )
    plugin_trust.add_argument(
        "--remove",
        metavar="WHO",
        help="stop trusting every key belonging to WHO",
    )
    plugin_trust.set_defaults(handler=_plugin_trust)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the command line and either print something or run the viewer.

    Split out from :func:`run` so it is testable: ``run`` is the console-script
    entry point and cannot be called from a test without taking the terminal.

    The two config flags exist for the operator who has just upgraded.
    ``--print-default-config`` is read-only and always has been: it prints the
    reference so they can read what a newer build documents.
    ``--upgrade-config`` is the one place in CLV that rewrites their settings
    file, and it does so only because they asked -- the launch path still never
    touches it. A plain ``diff`` between the two files is mostly noise, since
    they are ordered and commented differently, which is why the merge is a
    command rather than a suggestion.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)

    # Before the parser, which would report a path as an invalid choice and list
    # the subcommands at someone who asked to open a log.
    reserved = _reserved_source(arguments)
    if reserved is not None:
        print(reserved, file=sys.stderr)
        return EXIT_USAGE

    args = build_parser().parse_args(arguments)

    if args.print_default_config:
        sys.stdout.write(default_config_text())
        return EXIT_OK

    if args.upgrade_config:
        result = upgrade_user_settings()
        stream = sys.stdout if result.ok else sys.stderr
        print(describe_upgrade(result), file=stream)
        return EXIT_OK if result.ok else EXIT_FAILURE

    handler = getattr(args, "handler", None)
    if handler is not None:
        return handler(args, sys.stdout)

    # Imported here, not at module scope: everything above has to work over a
    # pipe, and a command that prints a report should not pay for a UI toolkit
    # or require a terminal to load one.
    from .app import LogViewerApp

    LogViewerApp().run()
    return EXIT_OK


def _reserved_source(arguments: Sequence[str]) -> Optional[str]:
    """The reserved-argument notice, when *arguments* opens with a path.

    ``clv /var/log/syslog`` is the most natural thing to try and the least
    obvious thing to have declined, so it is declined *by name*. Making it work
    here would put source selection in the argv layer, where nobody looking for
    it would find it, and leave it unremembered between runs -- so it belongs to
    a source-selection change, not to this one.

    Anything else unrecognised falls through to argparse, which reports an
    invalid choice and lists the real commands. Two different mistakes, two
    different messages, both exit 2.
    """

    for token in arguments:
        if token.startswith("-"):
            # Safe only because every top-level option is a flag; see the
            # module docstring, and the test that keeps it true.
            continue
        if token in SUBCOMMANDS:
            return None
        if not token:
            # `Path("")` is `Path(".")`, which exists, so an empty argument would
            # otherwise be reported as an attempt to open the working directory.
            # It is nonsense either way; argparse's invalid choice says so better.
            return None
        looks_like_a_path = (
            os.sep in token
            or (os.altsep is not None and os.altsep in token)
            or token.startswith(("~", "."))
            or Path(token).exists()
        )
        if not looks_like_a_path:
            return None
        settings = get_config_file() or user_config_path()
        return _RESERVED_SOURCE_NOTE.format(settings=settings)
    return None


def _doctor(args: argparse.Namespace, stream: TextIO) -> int:
    """Load the plugins, report everything, and start no screen.

    Loads exactly the way ``LogViewerApp.on_mount`` does, because a report of
    what *would* happen is worth nothing if it is assembled differently from
    what does. That includes honouring ``isolated = true``, which costs a
    subprocess during the load -- so the host is stopped again on the way out.

    What it deliberately does **not** do is call ``PluginRegistry.start()``. A
    diagnostic must not run ``setup()`` and start acquiring sockets, files and
    connections on the operator's behalf; the loader declines to for the same
    reason. A plugin that fails in ``setup()`` therefore shows here as loaded,
    and it is the one thing this report cannot tell you.
    """

    from .plugins import load_plugins, plugin_search_roots

    config = load_config()
    # Parity with `LogViewerApp.__init__`, so `clv doctor` on a new machine
    # names a plugin directory that exists rather than one that would.
    try:
        ensure_user_plugin_dir()
    except OSError:
        pass

    print(f"clv {__version__}", file=stream)
    build = "frozen bundle" if getattr(sys, "frozen", False) else "source checkout"
    print(
        f"build: {build}, Python {sys.version.split()[0]} on {sys.platform}",
        file=stream,
    )
    print(f"settings: {get_config_file() or user_config_path()}", file=stream)

    registry = load_plugins(
        enabled=config.plugins,
        settings=plugin_settings_for(config),
        host_timeout_ms=config.plugin_host_timeout_ms,
    )
    try:
        _report_roots(stream, plugin_search_roots(), registry)
        _report_config_issues(stream, config)
        _report_plugins(stream, registry)
    finally:
        # Not `shutdown()`: `start()` never ran, so there is no `setup()` to
        # undo and claiming otherwise would call `teardown()` on plugins that
        # were never set up. What there is to clean up is a child process and
        # its temp directory, from a plugin the operator isolated in
        # settings.conf — that door pays its handshake during the load.
        registry.stop_hosts()
    return EXIT_OK


def _report_roots(
    stream: TextIO, roots: Sequence[Path], registry: "PluginRegistry"
) -> None:
    """The plugin search roots, in order, each with what was found in it.

    Counted from ``registry.discovered`` rather than by walking again: the
    report has to describe the listing CLV actually used, and a second walk
    could disagree with the first.
    """

    print("", file=stream)
    if not roots:
        # No `$HOME` to resolve a config directory in. Rare, and worth saying
        # out loud rather than printing an empty heading.
        print("plugin roots: none (no config directory could be resolved)", file=stream)
        return
    print("plugin roots:", file=stream)
    for index, root in enumerate(roots, start=1):
        found = sum(1 for entry in registry.discovered if entry.root == root)
        if not root.is_dir():
            note = "does not exist"
        elif found:
            note = f"{found} plugin{'s' if found != 1 else ''}"
        else:
            note = "empty"
        print(f"  {index}. {root} — {note}", file=stream)


def _report_config_issues(stream: TextIO, config: "LogConfig") -> None:
    """Whatever CLV could not honour in the settings file.

    Same channel as the plugin report and for the same reason ``app.py`` puts
    them in the same colour: both are something the operator wrote that CLV
    read and declined, and a support report that showed one but not the other
    would send people looking in the wrong file.
    """

    if not config.issues:
        return
    print("", file=stream)
    print(f"settings problems: {len(config.issues)}", file=stream)
    for issue in config.issues:
        label = "warning" if issue.severity == "warning" else "problem"
        print(f"  {label}: {issue}", file=stream)


def _report_plugins(stream: TextIO, registry: "PluginRegistry") -> None:
    """One block per installable unit, from ``PluginRegistry.status()``.

    The rows, their states, their details and their order are all that method's,
    not this one's. It exists precisely so this report and the ``P`` dialog
    cannot come to two different conclusions about the same plugin -- its own
    docstring says it is built where it is so it can be asserted without a
    screen, and this is the caller that takes it up on that.
    """

    from .plugins.manifest import annotate, integrity_problems

    # The same call the `P` dialog makes, so the report and the dialog cannot
    # reach two conclusions about where one plugin came from.
    rows = annotate(registry.status())
    print("", file=stream)
    if not rows:
        # Requirement 10: a build with no plugins has nothing to say, and says
        # nothing rather than printing an empty table.
        print("plugins: none installed", file=stream)
        return

    counted: dict[str, int] = {}
    for row in rows:
        counted[row.state] = counted.get(row.state, 0) + 1
    # `isolated` included, unlike the drawer's summary line: a contained plugin
    # is a fact about how this build is running, and a report that dropped it
    # would make a subprocess invisible to the one command meant to find it.
    summary = ", ".join(
        f"{counted[state]} {state}"
        for state in ("loaded", "isolated", "not enabled", "incompatible", "failed")
        if counted.get(state)
    )
    print(f"plugins: {summary}", file=stream)

    for row in rows:
        print(f"  {row.name} — {row.state}", file=stream)
        print(f"      {row.source}: {row.origin}", file=stream)
        if row.kinds:
            print(f"      supplies: {', '.join(row.kinds)}", file=stream)
        # No line at all when there are none, rather than "supplies: nothing":
        # a plugin that failed to import or was never named has not declined to
        # supply anything, CLV simply has not asked it yet, and a verdict on its
        # behalf is the sort of thing an author reads as a bug report.
        if row.reads_content:
            # The single fact an operator most needs before enabling something,
            # and knowable from the declaration without running it.
            print(
                "      reads your log lines and delivers them wherever it is "
                "configured to send them",
                file=stream,
            )
        if row.provenance:
            print(f"      {row.provenance}", file=stream)
        if row.source == "user":
            # Re-hashed here and not in `annotate`, which the dialog also calls:
            # this is the command an operator is told to run when something is
            # wrong, so a tamper only `clv plugin verify` could find would be a
            # check that effectively does not exist. The dialog stays free of it
            # because opening a dialog should not cost work proportional to what
            # is installed.
            for problem in integrity_problems(row.name):
                print(f"      ⚠ does not match its manifest: {problem}", file=stream)
        if row.detail:
            for line in row.detail.splitlines():
                print(f"      {line}", file=stream)
        hint = _hint_for(row)
        if hint:
            print(f"      {hint}", file=stream)

    note = registry.errors.overflow_note
    if note:
        print(f"  ({note})", file=stream)


def _hint_for(row: "PluginStatus") -> str:
    """What to do about this row, where the state alone does not say.

    Only where `status()` leaves `detail` empty and the answer is not guessable:
    a plugin sitting in a directory unnamed looks identical to a working one
    from the filesystem, and "not enabled" does not tell anyone which file to
    edit.
    """

    if row.state != "not enabled" or row.detail or row.source != "user":
        return ""
    return f"add `{row.name}` to `plugins` in your settings file to run it"


def _plugin_list(args: argparse.Namespace, stream: TextIO) -> int:
    """List what is installed, and import none of it.

    The promise is the output's last line, and it is the reason this command
    does not go through ``load_plugins``: listing a plugin must not be able to
    run one, so the enumeration is the import-free
    ``plugins.discover_user_plugins`` and nothing here consults the registry.
    Pinned by a test whose plugin writes a sentinel file at import time.

    Bundled plugins are out of scope. They are not installed, cannot be removed,
    and are not named in the enable list -- ``clv doctor`` is what reports them.
    """

    from .plugins import discover_user_plugins, plugin_search_roots
    from .plugins.manifest import read_record

    config = load_config()
    roots = plugin_search_roots()
    scan = discover_user_plugins(roots, config.plugins)

    for root in roots:
        entries = [entry for entry in scan.plugins if entry.root == root]
        print(f"{root}", file=stream)
        if not entries:
            print("  (nothing installed here)", file=stream)
            continue
        for entry in sorted(entries, key=lambda item: item.name.casefold()):
            if entry.shadowed_by is not None:
                state = f"shadowed by {entry.shadowed_by}"
            elif entry.enabled:
                state = "enabled"
            else:
                state = "not enabled"
            kind = "package" if entry.is_package else "module"
            record = read_record(entry.name)
            # Appended rather than restructured: this line's shape is what
            # `tests/test_cli.py` reads, and a version is extra information
            # about a row rather than a different row.
            version = f" {record.version}" if record and record.version else ""
            print(f"  {entry.name}{version} — {state} ({kind})", file=stream)

    missing = [error for error in scan.errors if error.category == "missing"]
    if missing:
        print("", file=stream)
        for error in missing:
            print(f"{error.origin}: {error.message}", file=stream)

    installed = len(scan.plugins)
    enabled = sum(1 for entry in scan.plugins if entry.enabled)
    print("", file=stream)
    print(
        f"{installed} installed, {enabled} enabled. Nothing was imported.",
        file=stream,
    )
    return EXIT_OK


# --- plugin info -------------------------------------------------------------


def _plugin_info(args: argparse.Namespace, stream: TextIO) -> int:
    """Everything CLV knows about one plugin, having run none of it.

    Assembled from three import-free sources -- the filesystem scan, the
    installation record, and the settings file -- because the whole value of
    this command is that an operator can read it about a plugin they have not
    decided to trust yet.
    """

    from .plugins import discover_user_plugins, plugin_search_roots
    from .plugins.manifest import describe_record, read_record, stored_signature

    config = load_config()
    scan = discover_user_plugins(plugin_search_roots(), config.plugins)
    name = args.name
    found = next(
        (
            entry
            for entry in scan.plugins
            if entry.name.casefold() == name.casefold()
        ),
        None,
    )
    record = read_record(name)

    if found is None and record is None:
        print(
            f"clv: {name} is not installed. `clv plugin list` shows what is.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    print(f"{name}", file=stream)
    if found is not None:
        kind = "package" if found.is_package else "module"
        # The file, not the module name: a path printed in a report is one
        # somebody will paste into `ls`.
        located = found.root / (
            found.name if found.is_package else f"{found.name}.py"
        )
        print(f"  installed: {located} ({kind})", file=stream)
        if found.shadowed_by is not None:
            print(f"  shadowed by: {found.shadowed_by}", file=stream)
        print(f"  enabled: {'yes' if found.enabled else 'no'}", file=stream)
    else:
        # A record with no files beside it. Worth its own sentence: it is what
        # a half-finished `rm` looks like, and the fix is `clv plugin remove`.
        print("  installed: no — a record remains but the files are gone", file=stream)

    if record is None:
        print(
            "  origin: copied in by hand — CLV has no record of it arriving, "
            "so there is nothing to verify it against",
            file=stream,
        )
    else:
        if record.version:
            print(f"  version: {record.version}", file=stream)
        print(f"  origin: {describe_record(record)}", file=stream)
        print(f"  installed at: {record.installed_at}", file=stream)
        if record.author:
            print(f"  author: {record.author}", file=stream)
        if record.homepage:
            print(f"  homepage: {record.homepage}", file=stream)
        if record.description:
            print(f"  description: {record.description}", file=stream)
        if record.kinds:
            # The manifest's claim, and said as one: CLV has not imported
            # anything and cannot confirm it. `clv doctor` is what reports what
            # a plugin actually supplied.
            print(f"  declares: {', '.join(record.kinds)}", file=stream)
        for label, constraint in (
            ("requires plugin API", record.requires_api),
            ("requires clv", record.requires_clv),
        ):
            if constraint:
                print(f"  {label}: {constraint}", file=stream)
        if record.files:
            count = len(record.files)
            print(
                f"  files: {count} declared — run `clv plugin verify {name}` "
                f"to re-check {'them' if count != 1 else 'it'}",
                file=stream,
            )
        # Resolved now rather than read off the record, which froze at install.
        # One plugin, one `ssh-keygen` call, and the operator asked about this
        # one specifically — the dialog cannot afford the same and says so.
        live = stored_signature(name)
        print(f"  signature now: {_signature_line(live)}", file=stream)

    _report_plugin_section(stream, config, name)
    return EXIT_OK


def _report_plugin_section(stream: TextIO, config: "LogConfig", name: str) -> None:
    """The plugin's own ``[plugin:<name>]`` settings, if it has any.

    Printed because the commonest reason a correctly installed plugin does
    nothing is that it is waiting for a setting -- the shipped ``watch_alerts``
    example is deliberately inert until its section names a file -- and that
    fact lives in a different file from everything else here.
    """

    settings = config.plugin_settings.get(name.casefold())
    print("", file=stream)
    if not settings:
        print(f"no [plugin:{name}] section in your settings file.", file=stream)
        return
    print(f"[plugin:{name}]", file=stream)
    for key, value in sorted(settings.items()):
        print(f"  {key} = {value}", file=stream)


# --- plugin install ----------------------------------------------------------


def _plugin_install(args: argparse.Namespace, stream: TextIO) -> int:
    """Install, and say in as many words that nothing has been enabled.

    The last two lines of the output are the phase's Requirement 2 made
    visible. An install command that quietly enabled what it installed would
    make "a file in the plugin directory is inert until you name it" false at
    the one moment it is most tempting to break it, so this prints the line to
    add and leaves the adding to the operator.
    """

    from .plugins.install import InstallError, install
    from .plugins.manifest import ManifestError

    try:
        result = install(args.source, force=args.force, expected_sha256=args.sha256)
    except (InstallError, ManifestError) as exc:
        # Both are already phrased for whoever ran the command. Printing the
        # exception is the whole handler, because a traceback here would be a
        # report about CLV to somebody holding a broken tarball.
        print(f"clv: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    version = f" {result.version}" if result.version else ""
    verb = "replaced" if result.replaced else "installed"
    print(f"{verb} {result.name}{version} at {result.destination}", file=stream)
    if result.manifested:
        count = len(result.record.files)
        print(
            f"  checksums: {count} file{'s' if count != 1 else ''} verified",
            file=stream,
        )
    else:
        print(
            "  checksums: none — installed from a path with no manifest, so "
            "`clv plugin verify` will have nothing to compare against",
            file=stream,
        )
    print(f"  signature: {_signature_line(result.signature)}", file=stream)
    for warning in result.warnings:
        print(f"  warning: {warning}", file=stream)

    print("", file=stream)
    print(f"{result.name} is installed and NOT enabled.", file=stream)
    print(
        f"Add it to `plugins` in {get_config_file() or user_config_path()}:",
        file=stream,
    )
    print(f"    plugins = {result.name}", file=stream)
    print("then restart clv. Plugins are imported once, at startup.", file=stream)
    return EXIT_OK


def _signature_line(signature) -> str:
    """One line for a signature state, detail included where there is one."""

    if signature.state == "verified":
        return f"verified — signed by {signature.signer}"
    return f"{signature.state} — {signature.detail}"


# --- plugin remove -----------------------------------------------------------


def _plugin_remove(args: argparse.Namespace, stream: TextIO) -> int:
    """Delete a plugin, and report the two things that did not happen.

    Both asymmetries get a line. The name comes out of ``plugins``, because
    leaving it would report the plugin as named-but-missing on every launch
    from here on -- accurate, and indistinguishable from a bug to whoever just
    ran this. The ``[plugin:<name>]`` section stays, because it is what the
    operator wrote and a reinstall should not mean setting it up again.
    """

    from .plugins.install import InstallError, remove

    try:
        result = remove(args.name, purge=args.purge)
    except InstallError as exc:
        print(f"clv: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except OSError as exc:
        print(f"clv: {args.name} could not be removed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    for path in result.removed:
        print(f"removed {path}", file=stream)
    if result.disabled:
        print(f"removed {result.name} from `plugins` in your settings file", file=stream)
    if result.section_purged:
        print(f"removed the [plugin:{result.name}] section", file=stream)
    elif result.section_kept:
        print(
            f"kept [{result.section_kept}] in "
            f"{get_config_file() or user_config_path()} — its settings are "
            f"yours, and reinstalling will pick them up again. `--purge` "
            f"removes it.",
            file=stream,
        )
    return EXIT_OK


# --- plugin verify -----------------------------------------------------------


def _plugin_verify(args: argparse.Namespace, stream: TextIO) -> int:
    """Re-check installed plugins, and exit 1 if anything did not match.

    Both halves are re-run, and the signature half is the one worth knowing
    about: it is checked against the keys trusted *now*, not the keys trusted
    at install. Adding a signer turns an ``untrusted`` plugin into a verified
    one without reinstalling it, which is the entire reason to trust keys
    rather than files.
    """

    from .plugins.install import verify

    results = verify(args.name)
    if not results:
        print(
            "nothing to verify: no plugin here was installed with a manifest. "
            "`clv plugin list` shows what is installed.",
            file=stream,
        )
        return EXIT_OK

    failed = 0
    for result in results:
        if result.ok:
            checked = (
                f"{result.checked} file{'s' if result.checked != 1 else ''} "
                f"{'match' if result.checked != 1 else 'matches'}"
                if result.checked
                else "no files declared"
            )
            print(f"{result.name}: ok — {checked}", file=stream)
        else:
            failed += 1
            print(f"{result.name}: FAILED", file=stream)
            for problem in result.problems:
                print(f"    {problem}", file=stream)
        print(f"    signature: {_signature_line(result.signature)}", file=stream)

    if failed:
        print("", file=stream)
        print(
            f"{failed} of {len(results)} did not match what was installed. A "
            f"plugin you edited yourself will say this too — if you did not "
            f"edit it, reinstall it from a source you trust.",
            file=stream,
        )
        return EXIT_FAILURE
    return EXIT_OK


# --- plugin trust ------------------------------------------------------------


def _plugin_trust(args: argparse.Namespace, stream: TextIO) -> int:
    """Add, list or remove a signer the operator trusts.

    CLV ships no trust root and never will: a bundled key would make CLV the
    arbiter of which plugins are legitimate, which is the hosted-index
    commitment arriving through a side door. Everything this file contains, the
    operator put there.
    """

    from .plugins.manifest import (
        ManifestError,
        add_trusted_signer,
        parse_signer_line,
        read_trusted_signers,
        remove_trusted_signer,
        trust_store_path,
    )

    store = trust_store_path()

    if args.remove:
        dropped = remove_trusted_signer(args.remove)
        if not dropped:
            print(f"clv: no trusted key belongs to {args.remove}.", file=sys.stderr)
            return EXIT_FAILURE
        print(f"stopped trusting {args.remove} ({dropped} key(s))", file=stream)
        print(
            "Plugins already installed keep working; they will report as "
            "untrusted next time you verify them.",
            file=stream,
        )
        return EXIT_OK

    if args.list_signers or args.signer is None:
        signers = read_trusted_signers()
        if not signers:
            print(f"no plugin signers trusted ({store} is empty or absent).", file=stream)
            print(
                'Add one with: clv plugin trust "alice@example.com '
                '$(cat alice.pub)"',
                file=stream,
            )
            return EXIT_OK
        print(f"{store}", file=stream)
        for signer in signers:
            print(f"  {signer.principal} — {signer.key_type}", file=stream)
        return EXIT_OK

    try:
        signer = parse_signer_line(args.signer)
    except ManifestError as exc:
        print(f"clv: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        added = add_trusted_signer(signer)
    except OSError as exc:
        print(f"clv: {store} could not be written: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if not added:
        print(f"{signer.principal} was already trusted; nothing changed.", file=stream)
        return EXIT_OK
    print(f"trusting {signer.principal} ({signer.key_type})", file=stream)
    print(f"  written to {store}", file=stream)
    print(
        "Plugins signed by this key will now verify. Run `clv plugin verify` to "
        "re-check what is already installed.",
        file=stream,
    )
    return EXIT_OK


def run() -> None:  # pragma: no cover - script entry point
    raise SystemExit(main())
