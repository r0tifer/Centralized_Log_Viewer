"""The command line: subcommands that divert, and a bare ``clv`` that does not.

The compatibility question is the whole risk of this layer, so it is the first
test in the file. ``clv`` with no arguments launched the viewer before there was
an argv layer at all and must keep doing it exactly — ``PLUGIN_TODO.md``
Requirement 13 — and the two config flags that predate the subcommands have to
keep behaving through both the new entry point and the old name.

The rest is about two properties that are easy to assert and easy to lose:
``clv doctor`` must work without a terminal, and ``clv plugin list`` must not be
able to run the code it is listing.
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import clv
from clv import cli
from clv.plugins import PLUGIN_PATH_ENV, discover_user_plugins
from clv.services.config import default_config_text, user_config_path

# --- helpers -----------------------------------------------------------------

#: A filter stage, named after the module it is in so a report can be read.
_STAGE = """\
from clv.api import FilterStage

class Stage(FilterStage):
    name = "{name}"

    def apply(self, entry, context):
        return entry
"""


@pytest.fixture
def user_root(tmp_path, monkeypatch):
    """A plugin root on ``CLV_PLUGIN_PATH``, the documented development door.

    The same fixture ``tests/test_plugins.py`` uses, and for the same reason:
    ``conftest.py`` already points ``XDG_CONFIG_HOME`` at a temp tree, so the
    real user plugin directory is isolated for free, and the tests reach the
    loader the way an author does.
    """

    known = set(sys.modules)
    roots: list[Path] = []

    def make(name: str = "root") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        roots.append(root)
        monkeypatch.setenv(PLUGIN_PATH_ENV, os.pathsep.join(str(r) for r in roots))
        importlib.invalidate_caches()
        return root

    yield make

    for name in [n for n in sys.modules if n not in known]:
        del sys.modules[name]


def _plugin(root: Path, module: str, body: str | None = None) -> Path:
    path = root / f"{module}.py"
    path.write_text(body or _STAGE.format(name=module), encoding="utf-8")
    return path


def _enable(*names: str) -> None:
    """Name *names* in the enable list.

    Appended rather than templated: ``conftest.py`` writes a settings file with
    exactly one section, so the end of the file is inside ``[log_viewer]``.
    """

    with user_config_path().open("a", encoding="utf-8") as handle:
        handle.write(f"plugins = {', '.join(names)}\n")


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- bare clv ----------------------------------------------------------------


def test_bare_clv_launches_the_viewer(capsys, monkeypatch) -> None:
    """The compatibility test, and the reason this phase is riskier than it looks.

    Asserted without taking the terminal: ``main`` is split from ``run`` for
    exactly this, and the app is imported inside the branch so the patch lands
    on the class the branch will use.
    """

    from clv.app import LogViewerApp

    launched: list[bool] = []
    monkeypatch.setattr(LogViewerApp, "run", lambda self: launched.append(True))

    assert cli.main([]) == cli.EXIT_OK
    assert launched == [True]
    # Nothing printed on the way past: a viewer that scrolled a report off the
    # top of its own first screen would be a regression nobody asked for.
    assert capsys.readouterr().out == ""


def test_a_recognised_subcommand_does_not_launch_the_viewer(capsys, monkeypatch) -> None:
    from clv.app import LogViewerApp

    def refuse(self):  # pragma: no cover - the assertion is that this is unused
        raise AssertionError("doctor started a screen")

    monkeypatch.setattr(LogViewerApp, "run", refuse)

    assert cli.main(["doctor"]) == cli.EXIT_OK
    assert "clv " in capsys.readouterr().out


# --- the flags that predate the subcommands ----------------------------------


def test_version_prints_and_exits(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert f"clv {clv.__version__}" in capsys.readouterr().out


def test_help_exits_zero_and_names_every_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])

    assert exit_info.value.code == 0
    printed = capsys.readouterr().out
    for name in cli.SUBCOMMANDS:
        assert name in printed


def test_print_default_config_is_unchanged(capsys) -> None:
    code, printed, _ = _run(capsys, "--print-default-config")

    assert code == cli.EXIT_OK
    assert printed == default_config_text()


def test_the_old_name_still_works(capsys) -> None:
    """``clv.app.main`` is what two config flags' tests and docs already call.

    A shim rather than a rename, so nothing outside this phase had to move; a
    test rather than a comment, because a shim with no caller is the kind of
    thing a later cleanup deletes.
    """

    from clv.app import main as shim

    assert shim(["--print-default-config"]) == cli.EXIT_OK
    assert capsys.readouterr().out == default_config_text()


def test_every_top_level_option_is_a_flag() -> None:
    """Pins the assumption the positional scan rests on.

    ``_reserved_source`` skips anything starting with ``-`` and treats the next
    token as the subcommand slot. That is only safe while no top-level option
    consumes a value: add ``--config PATH`` and ``clv --config x`` would be
    reported as an attempt to open ``x`` as a log. This fails first instead.
    """

    for action in cli.build_parser()._actions:
        if not action.option_strings:
            continue
        assert action.nargs == 0 or isinstance(
            action, (argparse._HelpAction, argparse._VersionAction)
        ), f"{action.option_strings} takes a value — see _reserved_source"


# --- usage errors ------------------------------------------------------------


def test_an_unknown_subcommand_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["frobnicate"])

    assert exit_info.value.code == cli.EXIT_USAGE
    error = capsys.readouterr().err
    assert "invalid choice" in error
    # The real commands are listed, so the message is a correction rather than
    # only a refusal.
    for name in cli.SUBCOMMANDS:
        assert name in error


def test_clv_plugin_alone_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["plugin"])

    assert exit_info.value.code == cli.EXIT_USAGE
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argument",
    ["/var/log/syslog", "./app.log", "~/app.log", "logs/app.log"],
)
def test_a_bare_path_reports_the_reserved_behaviour(capsys, argument: str) -> None:
    """Declined by name, because it is the most natural thing to try.

    Distinct from the invalid-choice message above: the two are different
    mistakes, and telling someone who typed a path to "choose from doctor,
    plugin" answers a question they did not ask.
    """

    code, _, error = _run(capsys, argument)

    assert code == cli.EXIT_USAGE
    assert "not supported" in error
    assert "log_dirs" in error
    assert "invalid choice" not in error


def test_a_path_that_exists_is_recognised_without_a_separator(
    capsys, tmp_path, monkeypatch
) -> None:
    """A bare filename in the working directory is still a source, not a typo."""

    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.log").write_text("hello\n", encoding="utf-8")

    code, _, error = _run(capsys, "app.log")

    assert code == cli.EXIT_USAGE
    assert "not supported" in error


def test_a_flag_before_a_subcommand_is_not_mistaken_for_a_path(capsys, monkeypatch) -> None:
    from clv.app import LogViewerApp

    monkeypatch.setattr(LogViewerApp, "run", lambda self: None)

    assert cli.main(["--upgrade-config"]) == cli.EXIT_OK
    assert "not supported" not in capsys.readouterr().err


# --- clv doctor --------------------------------------------------------------


def test_doctor_on_a_machine_with_no_plugins(capsys) -> None:
    """Requirement 10: nothing installed says so, and adds no empty table.

    Bundled plugins are still reported — they are installed, in the only sense
    CLV has — so this asserts the *user* directory is quiet rather than that the
    section is absent.
    """

    code, printed, _ = _run(capsys, "doctor")

    assert code == cli.EXIT_OK
    assert f"clv {clv.__version__}" in printed
    assert str(user_config_path()) in printed
    assert "plugins:" in printed
    assert "not enabled" not in printed


def test_doctor_reports_every_state_and_still_exits_zero(capsys, user_root) -> None:
    """The four states an operator has to tell apart, in one report.

    Exit 0 is the assertion that matters as much as the text: a broken plugin is
    what this command exists to report, not a reason for it to fail. A support
    tool that exited non-zero on the problem it was run to diagnose would be
    treated as broken itself.
    """

    root = user_root()
    _plugin(root, "works")
    _plugin(root, "unnamed")
    _plugin(root, "explodes", "import definitely_not_a_module\n")
    _plugin(
        root,
        "futuristic",
        _STAGE.format(name="futuristic").replace(
            'name = "futuristic"', 'name = "futuristic"\n    requires_clv = ">=99.0"'
        ),
    )
    _enable("works", "explodes", "futuristic", "typo_nothing_here")

    code, printed, _ = _run(capsys, "doctor")

    assert code == cli.EXIT_OK
    assert "works — loaded" in printed
    assert "unnamed — not enabled" in printed
    assert "explodes — failed" in printed
    assert "futuristic — incompatible" in printed
    # The row Phase 4 found by hand: a name in settings.conf matching nothing.
    assert "typo_nothing_here" in printed

    # Each one says what to do about it, which is the difference between a
    # report and a list.
    assert "No module named 'definitely_not_a_module'" in printed
    assert "requires CLV >=99.0, running" in printed
    assert "add `unnamed` to `plugins`" in printed


def test_doctor_names_the_interfaces_a_plugin_supplied(capsys, user_root) -> None:
    root = user_root()
    _plugin(root, "works")
    _enable("works")

    _, printed, _ = _run(capsys, "doctor")

    assert "supplies: filter" in printed


def test_doctor_lists_the_search_roots_in_order(capsys, user_root) -> None:
    first, second = user_root("first"), user_root("second")
    _plugin(first, "one")

    _, printed, _ = _run(capsys, "doctor")

    lines = [line for line in printed.splitlines() if line.startswith("  ")]
    roots = [line for line in lines if str(first) in line or str(second) in line]
    assert str(first) in roots[0] and "1 plugin" in roots[0]
    assert str(second) in roots[1] and "empty" in roots[1]


def test_doctor_reports_a_settings_problem(capsys) -> None:
    """The same channel as the plugin report, for the same reason ``app.py``
    gives both the same colour: both are something the operator wrote."""

    with user_config_path().open("a", encoding="utf-8") as handle:
        handle.write("[ssh:web01]\nport = 99999\n")

    code, printed, _ = _run(capsys, "doctor")

    assert code == cli.EXIT_OK
    assert "settings problems:" in printed
    assert "web01" in printed


def test_doctor_says_which_build_it_is(capsys) -> None:
    """"The feature is missing" and "the build is old" look identical from the
    UI, which is the whole reason ``--version`` exists; a report pasted into an
    issue needs the same fact plus the interpreter under it."""

    _, printed, _ = _run(capsys, "doctor")

    assert "source checkout" in printed
    assert sys.version.split()[0] in printed


_PURITY_PROBE = """
import contextlib
import io
import sys

import clv.cli

with contextlib.redirect_stdout(io.StringIO()):
    clv.cli.main(["doctor"])

leaked = sorted(
    name
    for name in sys.modules
    if name == "clv.app"
    or name.split(".")[0] in {"textual", "rich"}
    or name.startswith("clv.widgets")
)
print("LEAKED:" + ",".join(leaked))
"""


def test_doctor_needs_no_terminal_and_no_ui(tmp_path) -> None:
    """"Send me the output of clv doctor" has to work over a pipe and in CI.

    Out of process on purpose, for the reason ``tests/test_api_surface.py``
    gives about ``clv.api``: in-process this would pass whenever an earlier test
    had already imported a widget, which is every session, and would go on
    passing long after the property had broken.
    """

    result = subprocess.run(
        [sys.executable, "-c", _PURITY_PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    marker = next(
        line for line in result.stdout.splitlines() if line.startswith("LEAKED:")
    )
    leaked = [name for name in marker[len("LEAKED:"):].split(",") if name]
    assert not leaked, "clv doctor dragged in the UI layer: " + ", ".join(leaked)


def test_doctor_leaves_no_isolated_child_behind(capsys, user_root) -> None:
    """A plugin isolated in settings.conf spawns its host during the *load*.

    So a report that only read the registry would leave a live child and a
    ``clv-plugin-*`` temp directory behind every time it ran. ``stop_hosts()``
    is what this asserts exists and is called, and it is deliberately not
    ``shutdown()``: ``start()`` never ran, so there is no ``setup()`` to undo.
    """

    root = user_root()
    _plugin(
        root,
        "contained",
        """\
from clv.api import Exporter, ExportResult

class Ship(Exporter):
    name = "contained"
    format_name = "contained"
    label = "Contained"

    def export(self, entries, context):
        return ExportResult(ok=True, message="done")
""",
    )
    _enable("contained")
    with user_config_path().open("a", encoding="utf-8") as handle:
        handle.write("\n[plugin:contained]\nisolated = true\n")

    before = set(Path("/tmp").glob("clv-plugin-*")) if Path("/tmp").is_dir() else set()

    code, printed, _ = _run(capsys, "doctor")

    assert code == cli.EXIT_OK
    assert "contained — isolated" in printed
    # The honest half of the sentence reaches the CLI too, not only the dialog.
    assert "still runs as you" in printed

    after = set(Path("/tmp").glob("clv-plugin-*")) if Path("/tmp").is_dir() else set()
    assert after - before == set(), "clv doctor left an isolation host's workdir behind"


# --- clv plugin list ---------------------------------------------------------


def test_plugin_list_names_what_is_installed(capsys, user_root) -> None:
    root = user_root()
    _plugin(root, "works")
    _plugin(root, "unnamed")
    _enable("works")

    code, printed, _ = _run(capsys, "plugin", "list")

    assert code == cli.EXIT_OK
    assert "works — enabled" in printed
    assert "unnamed — not enabled" in printed
    assert "2 installed, 1 enabled" in printed


def test_plugin_list_imports_nothing(capsys, user_root, tmp_path) -> None:
    """Requirement 2's teeth at the CLI, and Phase 15 inherits this assertion.

    Every ``clv plugin`` command must be safe to run against a directory
    containing something hostile, because "let me see what is installed" is
    precisely what an operator does *before* deciding to trust it. The sentinel
    is the only assertion here that cannot be softened.
    """

    root = user_root()
    sentinel = tmp_path / "ran.txt"
    _plugin(
        root,
        "sneaky",
        f"from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported', encoding='utf-8')\n"
        + _STAGE.format(name="sneaky"),
    )
    # Named in the enable list, so consent is not what is doing the work here:
    # the command simply never imports anything, enabled or not.
    _enable("sneaky")

    code, printed, _ = _run(capsys, "plugin", "list")

    assert code == cli.EXIT_OK
    assert "sneaky — enabled" in printed
    assert not sentinel.exists(), "clv plugin list imported a plugin"
    assert "Nothing was imported." in printed


def test_plugin_list_reports_an_absent_name(capsys, user_root) -> None:
    user_root()
    _enable("typo_nothing_here")

    _, printed, _ = _run(capsys, "plugin", "list")

    assert "typo_nothing_here" in printed
    assert "was not found" in printed


def test_plugin_list_reports_shadowing(capsys, user_root) -> None:
    first, second = user_root("first"), user_root("second")
    _plugin(first, "twice")
    _plugin(second, "twice")
    _enable("twice")

    _, printed, _ = _run(capsys, "plugin", "list")

    assert "twice — enabled" in printed
    assert "shadowed by" in printed


def test_plugin_list_says_when_a_root_is_empty(capsys, user_root) -> None:
    user_root()

    code, printed, _ = _run(capsys, "plugin", "list")

    assert code == cli.EXIT_OK
    assert "(nothing installed here)" in printed
    assert "0 installed, 0 enabled" in printed


def test_a_package_directory_is_listed_as_a_package(capsys, user_root) -> None:
    root = user_root()
    package = root / "bundlish"
    package.mkdir()
    (package / "__init__.py").write_text(
        _STAGE.format(name="bundlish"), encoding="utf-8"
    )

    _, printed, _ = _run(capsys, "plugin", "list")

    assert "bundlish — not enabled (package)" in printed


def test_the_scan_and_the_loader_see_the_same_listing(user_root) -> None:
    """One walk, not two.

    ``clv plugin list`` and the loader have to agree about what is installed, or
    a plugin can be in one listing and not the other with nothing to say which
    is wrong. They agree by construction — ``_load_user_roots`` consumes this
    scan — and this is the test that keeps it that way.
    """

    from clv.plugins import load_plugins, plugin_search_roots

    root = user_root()
    _plugin(root, "works")
    _plugin(root, "unnamed")

    scan = discover_user_plugins(plugin_search_roots(), ["works"])
    registry = load_plugins(
        clv_version=clv.__version__, include_local=False, include_entry_points=False,
        enabled=["works"],
    )

    assert [entry.name for entry in scan.plugins] == [
        entry.name for entry in registry.discovered
    ]
    assert [entry.enabled for entry in scan.plugins] == [
        entry.enabled for entry in registry.discovered
    ]


# --- plugins do not add subcommands ------------------------------------------


def test_the_subcommand_table_is_closed() -> None:
    """Requirement 13, structurally.

    The parser is built from a literal tuple. Nothing plugin-supplied may reach
    it, so this asserts the parser's choices are exactly that tuple — a future
    change that fed it a registry list, however reasonable it looked, fails
    here.
    """

    parser = cli.build_parser()
    subparsers = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subparsers) == 1
    assert tuple(subparsers[0].choices) == cli.SUBCOMMANDS

    import clv.api as api
    import clv.plugins as plugins

    # No published name and no registry field is a plausible source of
    # subcommands, so there is nothing for a loader to have wired in.
    assert not {"SUBCOMMANDS", "subcommands"} & set(api.__all__)
    assert not hasattr(plugins.PluginRegistry(), "subcommands")


def test_a_plugin_declaring_a_subcommand_is_reported_but_still_loads(
    user_root, capsys
) -> None:
    """The author who assumed otherwise gets a sentence, not a silence.

    Without this, ``subcommand = "ship"`` loads, works, and simply never becomes
    a subcommand — the failure hardest to diagnose from outside CLV's source.
    The attribute is refused; the plugin is not, because a declaration CLV
    cannot honour is no reason to withdraw the ones it can.
    """

    root = user_root()
    _plugin(
        root,
        "ambitious",
        _STAGE.format(name="ambitious").replace(
            'name = "ambitious"', 'name = "ambitious"\n    subcommand = "ship"'
        ),
    )
    _enable("ambitious")

    code, printed, _ = _run(capsys, "doctor")

    assert code == cli.EXIT_OK
    assert "declares subcommand" in printed
    assert "implement Command instead" in printed
    # Still in service: it is a working filter stage that declared one thing
    # CLV will not do.
    assert "supplies: filter" in printed


def test_the_run_shim_exits_with_mains_code(monkeypatch) -> None:
    monkeypatch.setattr(cli, "main", lambda argv=None: 3)

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == 3
