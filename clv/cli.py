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

#: What ``clv plugin`` accepts. ``PLUGIN_TODO.md`` Phase 15 adds ``info``,
#: ``install``, ``remove``, ``verify`` and ``trust`` to this tuple.
PLUGIN_SUBCOMMANDS = ("list",)

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
        help="inspect installed plugins",
        description="Inspect what is installed in your plugin directories.",
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
        return handler(sys.stdout)

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


def _doctor(stream: TextIO) -> int:
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

    rows = registry.status()
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


def _plugin_list(stream: TextIO) -> int:
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
            print(f"  {entry.name} — {state} ({kind})", file=stream)

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


def run() -> None:  # pragma: no cover - script entry point
    raise SystemExit(main())
