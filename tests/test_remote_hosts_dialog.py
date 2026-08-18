"""The host dialog, and the settings file it is trusted with.

Two things are being defended here and they pull in opposite directions. The
dialog has to be able to *change* ``settings.conf`` — that is the whole feature —
and it must not be able to damage it. So the round-trip assertions are on the
full file text rather than on what ``configparser`` makes of it: a parse-level
assertion passes just as happily when every comment in the file has been thrown
away, and the shipped file is two thirds prose.

Nothing here touches a network. The one thing in the dialog that would is
injected, and there is a test asserting it is only ever called by a button press.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input, OptionList, Static

from clv.app import LogViewerApp
from clv.services.config import RemoteHost, load_config
from clv.widgets.remote_hosts_dialog import ProbeResult, RemoteHostsDialog


def _run(scenario) -> None:
    asyncio.run(scenario())


WEB01 = RemoteHost(
    name="web01",
    host="web01.internal",
    user="ops",
    log_dirs=("/var/log", "/srv/app/logs"),
)
DB02 = RemoteHost(name="db02", host="10.0.0.12", log_dirs=("/var/log/postgresql",))


COMMENTED = """\
# CLV settings. Hand-written, and full of things worth keeping.

[log_viewer]
# How often the tail redraws.
refresh_hz = 2
enable_ssh = true

# ---------------------------------------------------------------------------
# Remote sources over SSH
# ---------------------------------------------------------------------------

[ssh:web01]
# Reached through the bastion; see ~/.ssh/config.
host = web01.internal
user = ops
log_dirs = /var/log, /srv/app/logs
# Noisy box, so it gets its own budget.
max_files = 4000

[ssh:db02]
host = 10.0.0.12
log_dirs = /var/log/postgresql
"""


def _hint(screen) -> str:
    return str(screen.query_one("#host-hint", Static).render())


# --- the dialog on its own --------------------------------------------------


def test_the_list_shows_each_host_with_what_is_known_about_it() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                RemoteHostsDialog(
                    [WEB01, DB02], statuses={"web01": "connected"}
                )
            )
            await pilot.pause()

            options = app.screen.query_one("#host-list", OptionList)
            rows = [str(option.prompt) for option in options.options]

            assert "web01" in rows[0] and "connected" in rows[0]
            # db02 has never been contacted, and a brand new SSHConnection
            # reports `connected` optimistically — so "reachable" and "never
            # tried" would otherwise render identically.
            assert "db02" in rows[1] and "not tried" in rows[1]

    _run(scenario)


def test_adding_a_host_returns_it_and_an_untouched_dialog_returns_nothing() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            results: list[object] = []
            app.push_screen(RemoteHostsDialog(), callback=results.append)
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#host-name", Input).has_focus

            screen.query_one("#host-name", Input).value = "web09"
            screen.query_one("#host-dirs", Input).value = "/var/log"
            await pilot.press("enter")
            await pilot.pause()
            assert not screen.editing, "saving closes the editor"

            await pilot.press("escape")
            await pilot.pause()

            assert results == [(RemoteHost(name="web09", host="web09", log_dirs=("/var/log",)),)]

            # And a dialog that was only looked at reports nothing changed, so
            # the app can skip rewriting the file and reloading every source.
            results.clear()
            app.push_screen(RemoteHostsDialog([WEB01]), callback=results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert results == [None]

    _run(scenario)


def test_validation_is_reported_where_it_was_typed_and_nothing_is_saved() -> None:
    """A toast would be the wrong channel: the field that is wrong is on screen."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01]))
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen

            for name, dirs, port, expected in (
                ("", "/var/log", "", "Give the host a name."),
                ("web01", "/var/log", "", "already configured"),
                ("web09", "/var/log", "70000", "outside 1-65535"),
                ("web09", "logs", "", "is relative"),
                ("web09", "", "", "at least one absolute folder"),
            ):
                screen.query_one("#host-name", Input).value = name
                screen.query_one("#host-dirs", Input).value = dirs
                screen.query_one("#host-port", Input).value = port
                await pilot.press("enter")
                await pilot.pause()

                assert expected in _hint(screen), expected
                assert "-warning" in screen.query_one("#host-hint", Static).classes
                assert screen.editing, "a refused save keeps the editor open"
                assert screen.hosts == (WEB01,), "and changes nothing"

    _run(scenario)


def test_delete_arms_rather_than_stacking_a_second_modal() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01, DB02]))
            await pilot.pause()
            screen = app.screen

            await pilot.press("d")
            await pilot.pause()
            assert "again" in _hint(screen)
            assert screen.hosts == (WEB01, DB02), "one press only arms"

            await pilot.press("d")
            await pilot.pause()
            assert screen.hosts == (DB02,)

    _run(scenario)


def test_space_toggles_a_host_without_opening_the_editor() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01]))
            await pilot.pause()

            await pilot.press("space")
            await pilot.pause()

            assert app.screen.hosts[0].enabled is False
            assert not app.screen.editing

    _run(scenario)


def test_editing_carries_the_options_the_dialog_does_not_offer() -> None:
    """Per-host budgets and globs are not on screen, so they must not be lost by
    passing through an edit of something that is."""

    tuned = RemoteHost(
        name="web01",
        host="web01.internal",
        log_dirs=("/var/log",),
        max_files=4000,
        include_globs=("*.log",),
        correct_clock_skew=True,
    )

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([tuned]))
            await pilot.pause()
            screen = app.screen

            await pilot.press("enter")
            await pilot.pause()
            screen.query_one("#host-address", Input).value = "10.0.0.9"
            await pilot.press("enter")
            await pilot.pause()

            assert screen.hosts[0].host == "10.0.0.9"
            assert screen.hosts[0].max_files == 4000
            assert screen.hosts[0].include_globs == ("*.log",)
            assert screen.hosts[0].correct_clock_skew is True

    _run(scenario)


# --- Test connection --------------------------------------------------------


def test_the_probe_runs_only_when_the_operator_asks_for_it() -> None:
    """Requirement 8's carve-out, held to exactly one trigger.

    Opening the dialog, moving through the list, editing and dismissing must all
    connect to nothing. The exception this feature takes to "no ssh process until
    enable_ssh is true" is *a button press*, so a probe that fired on anything
    else would be a different, much larger exception.
    """

    async def scenario() -> None:
        calls: list[RemoteHost] = []

        def probe(host: RemoteHost) -> ProbeResult:
            calls.append(host)
            return ProbeResult(True, "connected · gnu · Linux")

        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01, DB02], probe=probe))
            await pilot.pause()
            screen = app.screen

            await pilot.press("down")
            await pilot.press("up")
            await pilot.press("a")
            await pilot.pause()
            screen.query_one("#host-name", Input).value = "web09"
            screen.query_one("#host-dirs", Input).value = "/var/log"
            await pilot.press("enter")
            await pilot.pause()
            assert calls == [], "nothing so far was a request to connect"

            await pilot.press("t")
            await pilot.pause()
            await pilot.pause()

            assert [host.name for host in calls] == ["web01"]
            assert "connected · gnu" in _hint(screen)

    _run(scenario)


def test_a_failing_probe_reports_the_reason_verbatim_and_warns() -> None:
    async def scenario() -> None:
        def probe(host: RemoteHost) -> ProbeResult:
            return ProbeResult(False, "web01 refused the connection on port 22")

        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01], probe=probe))
            await pilot.pause()

            await pilot.press("t")
            await pilot.pause()
            await pilot.pause()

            screen = app.screen
            assert "refused the connection" in _hint(screen)
            assert "-warning" in screen.query_one("#host-hint", Static).classes

    _run(scenario)


def test_a_probe_that_raises_is_reported_rather_than_crashing_the_dialog() -> None:
    async def scenario() -> None:
        def probe(host: RemoteHost) -> ProbeResult:
            raise OSError("ssh: Could not resolve hostname web01")

        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01], probe=probe))
            await pilot.pause()

            await pilot.press("t")
            await pilot.pause()
            await pilot.pause()

            assert "Could not resolve hostname" in _hint(app.screen)

    _run(scenario)


def test_testing_from_the_editor_tests_what_was_just_typed() -> None:
    """The saved record answers a different question than the one being asked."""

    async def scenario() -> None:
        seen: list[tuple[str, int]] = []

        def probe(host: RemoteHost) -> ProbeResult:
            seen.append((host.host, host.port))
            return ProbeResult(True, "connected")

        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01], probe=probe))
            await pilot.pause()
            screen = app.screen

            await pilot.press("enter")
            await pilot.pause()
            screen.query_one("#host-address", Input).value = "10.0.0.9"
            screen.query_one("#host-port", Input).value = "2222"
            screen.query_one("#host-test", type(screen.query_one("#host-test"))).press()
            await pilot.pause()
            await pilot.pause()

            assert seen == [("10.0.0.9", 2222)]

    _run(scenario)


# --- the settings file ------------------------------------------------------


def _app_on(config_path: Path) -> LogViewerApp:
    app = LogViewerApp(config=load_config(config_path))
    app._settings_path = config_path
    return app


def test_an_edit_cycle_leaves_a_hand_written_file_otherwise_intact(
    tmp_path: Path,
) -> None:
    """**The gate.** Add one host, edit another, remove a third, and every
    comment, blank line and per-host budget in the file is exactly where it was.

    Asserted as full text. The rule that makes it hold is that the writeback only
    ever touches the six keys the dialog offers — it never regenerates a section
    from the record, which would take the comments and the budget with it.
    """

    config = tmp_path / "settings.conf"
    config.write_text(COMMENTED, encoding="utf-8")
    app = _app_on(config)

    app._write_hosts(
        (
            RemoteHost(
                name="web01",
                host="10.0.0.9",
                user="ops",
                port=2222,
                log_dirs=("/var/log",),
                max_files=4000,
            ),
            RemoteHost(name="web09", host="web09", log_dirs=("/var/log",)),
        )
    )

    # Written out in full rather than built by string surgery, because the
    # point of the assertion is every byte that did *not* move. Note where
    # `port` lands: after the section's last real content line, which is what
    # keeps a new key from being wedged in above the operator's comment.
    assert config.read_text(encoding="utf-8") == """\
# CLV settings. Hand-written, and full of things worth keeping.

[log_viewer]
# How often the tail redraws.
refresh_hz = 2
enable_ssh = true

# ---------------------------------------------------------------------------
# Remote sources over SSH
# ---------------------------------------------------------------------------

[ssh:web01]
# Reached through the bastion; see ~/.ssh/config.
host = 10.0.0.9
user = ops
log_dirs = /var/log
# Noisy box, so it gets its own budget.
max_files = 4000
port = 2222

[ssh:web09]
log_dirs = /var/log
"""


def test_a_refused_key_survives_an_edit_and_keeps_warning(tmp_path: Path) -> None:
    """An operator whose `password =` line vanished during an unrelated edit
    would reasonably conclude CLV had accepted and stored it. That is the one
    impression this project may never give."""

    config = tmp_path / "settings.conf"
    config.write_text(
        "[log_viewer]\nenable_ssh = true\n\n"
        "[ssh:web01]\nhost = web01.internal\npassword = hunter2\n"
        "log_dirs = /var/log\n",
        encoding="utf-8",
    )
    app = _app_on(config)
    assert any("password" in str(issue) for issue in app._config.issues)

    app._write_hosts((RemoteHost(name="web01", host="10.0.0.9", log_dirs=("/var/log",)),))

    text = config.read_text(encoding="utf-8")
    assert "password = hunter2" in text
    assert "host = 10.0.0.9" in text
    assert any("password" in str(issue) for issue in load_config(config).issues)


def test_a_section_the_parser_skipped_is_shown_and_never_rewritten(
    tmp_path: Path,
) -> None:
    """A host with an impossible port never reaches `config.hosts`, so the one
    place an operator would go to fix it was the one place it was invisible —
    and a dialog that cannot see a section must not be what deletes it."""

    config = tmp_path / "settings.conf"
    original = (
        "[log_viewer]\nenable_ssh = true\n\n"
        "[ssh:broken]\nhost = broken.internal\nport = 70000\nlog_dirs = /var/log\n\n"
        "[ssh:web01]\nhost = web01.internal\nlog_dirs = /var/log\n"
    )
    config.write_text(original, encoding="utf-8")
    app = _app_on(config)

    assert [host.name for host in app._config.hosts] == ["web01"]
    skipped = [str(i) for i in app._config.issues if i.origin.startswith("[ssh:")]
    assert skipped and "70000" in skipped[0]

    # The dialog hands back only what it was given, so `broken` is in neither the
    # keep list nor the remove list, and the diff never names it.
    app._write_hosts((RemoteHost(name="web01", host="web01.internal", log_dirs=("/var/log",)),))

    assert config.read_text(encoding="utf-8") == original

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog(app._config.hosts, skipped=skipped))
            await pilot.pause()

            rows = [
                str(option.prompt)
                for option in app.screen.query_one("#host-list", OptionList).options
            ]
            assert any("70000" in row for row in rows), "the skipped host is visible"

    _run(scenario)


def test_clearing_a_field_removes_its_line_rather_than_writing_a_default(
    tmp_path: Path,
) -> None:
    config = tmp_path / "settings.conf"
    config.write_text(
        "[log_viewer]\nenable_ssh = true\n\n"
        "[ssh:web01]\nhost = web01.internal\nuser = ops\nport = 2222\n"
        "log_dirs = /var/log\n",
        encoding="utf-8",
    )
    app = _app_on(config)

    app._write_hosts((RemoteHost(name="web01", host="web01", log_dirs=("/var/log",)),))

    assert config.read_text(encoding="utf-8") == (
        "[log_viewer]\nenable_ssh = true\n\n[ssh:web01]\nlog_dirs = /var/log\n"
    )
    assert load_config(config).host("web01").port == 22


# --- layout and reachability ------------------------------------------------


def test_the_dialog_fits_eighty_columns_with_the_editor_open() -> None:
    """The tallest state over an overfilled list, which is the worst case."""

    hosts = [
        RemoteHost(name=f"web{index:02d}", host=f"web{index:02d}", log_dirs=("/var/log",))
        for index in range(12)
    ]

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog(hosts))
            await pilot.pause()

            dialog = app.screen.query_one("#remote-hosts-dialog")
            assert dialog.region.right <= 80
            assert dialog.region.bottom <= 24

            # The list is visible, and painting, while it is the thing on screen.
            listing = app.screen.query_one("#host-list").region
            assert listing.width > 0 and listing.height > 0
            assert listing.bottom <= 24

            await pilot.press("a")
            await pilot.pause()
            # It gives way to the editor deliberately — six fields, four lines of
            # instructions and the buttons do not fit beside it in 24 rows.
            assert app.screen.query_one("#host-list").region.height == 0
            for widget_id in ("#host-editor", "#host-hint", "#dialog-actions"):
                region = app.screen.query_one(widget_id).region
                assert region.width > 0 and region.height > 0, widget_id
                assert region.bottom <= 24, widget_id
                assert region.right <= 80, widget_id
            # Including the bottom border of the dialog itself: the buttons being
            # on screen is not enough if the frame around them is clipped.
            assert app.screen.query_one("#remote-hosts-dialog").region.bottom <= 24

    _run(scenario)


def test_every_control_is_reachable_from_the_keyboard() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01]))
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            reached = set()
            for _ in range(14):
                await pilot.press("tab")
                await pilot.pause()
                focused = app.focused
                if focused is not None and focused.id:
                    reached.add(focused.id)

            assert {
                "host-name",
                "host-address",
                "host-user",
                "host-port",
                "host-identity",
                "host-dirs",
            } <= reached
            assert {"host-add", "host-test", "host-close"} <= reached

    _run(scenario)


def test_R_opens_the_dialog() -> None:
    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Focus off the query input first: a single-letter binding is a
            # character while an Input has focus, which the suite already
            # records as correct-and-intended for `a`, `t`, `s` and `f`.
            app.set_focus(app.log_panel)
            await pilot.pause()
            await pilot.press("R")
            # Twice: the action spawns a worker that awaits `push_screen`, so
            # the screen is not on the stack until that coroutine has run.
            await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, RemoteHostsDialog)

    _run(scenario)


def test_removing_the_host_of_the_open_log_says_so_rather_than_no_such_file() -> None:
    """`action_reload_sources` re-selects a remote ref unconditionally — its
    guard short-circuits on `not is_local(...)` — and with the host gone the
    resolver hands the ref to the local backend, which reports it *missing*. So
    an operator who deleted `web01` got "no such file" for a path that is fine,
    on a machine they removed on purpose.

    Driven through the real action rather than a pushed screen, because the whole
    sequence under test is the one that runs after the dialog dismisses: write
    the file, notice the open source lost its machine, then reload.
    """

    from clv.services.config import get_config_file, user_config_path
    from clv.services.refs import RemoteRef

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            # The settings file the app will actually re-read on reload, which
            # conftest has already pointed at a temp directory.
            settings = get_config_file() or user_config_path()
            # `isolated_environment` is session-scoped, so this file is shared
            # with every other test in the run. Restored below.
            original = settings.read_text(encoding="utf-8")
            settings.write_text(
                original
                + "\nenable_ssh = true\n\n"
                "[ssh:web01]\nhost = web01.internal\nlog_dirs = /var/log\n",
                encoding="utf-8",
            )
            app._config = load_config(settings)
            app._settings_path = settings
            assert [host.name for host in app._config.hosts] == ["web01"]

            app._selected_source = RemoteRef.build("web01", "/var/log/syslog")
            notices: list[str] = []
            app._notify = lambda message, severity="information": notices.append(
                f"{severity}:{message}"
            )

            app.action_remote_hosts()
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, RemoteHostsDialog)

            await pilot.press("d")
            await pilot.press("d")
            await pilot.press("escape")
            for _ in range(6):
                await pilot.pause()

            try:
                assert "[ssh:web01]" not in settings.read_text(encoding="utf-8")
                assert app._config.hosts == ()
                closed = [n for n in notices if "no longer configured" in n]
                assert closed, notices
                assert closed[0].startswith("warning:")
                assert "web01-syslog" in closed[0]
                assert app._selected_source is None
            finally:
                settings.write_text(original, encoding="utf-8")

    _run(scenario)


def test_the_dialog_says_when_the_hosts_it_is_editing_are_switched_off() -> None:
    """Adding a host and getting no sources is the "control that quietly does
    nothing" failure, one level up. The switch stays the only writer of
    `enable_ssh`; this is a status line and a pointer to it."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01], enabled=False))
            await pilot.pause()

            assert "Remote sources are off" in _hint(app.screen)
            assert "press f" in _hint(app.screen)

            app.pop_screen()
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([WEB01], enabled=True))
            await pilot.pause()

            assert "Remote sources are off" not in _hint(app.screen)

            # And with no hosts yet there is nothing being switched off, so the
            # first-run dialog is not scolding anyone.
            app.pop_screen()
            await pilot.pause()
            app.push_screen(RemoteHostsDialog([], enabled=False))
            await pilot.pause()

            assert "Remote sources are off" not in _hint(app.screen)

    _run(scenario)


def test_the_editor_explains_how_to_add_a_host_without_wrapping() -> None:
    """Placeholders are not instructions, and a wrapped instruction costs rows.

    Every line here is written to fit the dialog's text width. That is not
    fussiness: each line that wraps costs a second row, and four wrapped lines
    pushed the Close button off a 24-row terminal — which is how this dialog
    first rendered.
    """

    from clv.widgets.remote_hosts_dialog import RemoteHostsDialog as Dialog

    lines = Dialog.EDIT_HINT.splitlines()

    assert len(lines) == 4
    for line in lines:
        assert len(line) <= 72, f"{line!r} is {len(line)} columns and will wrap"

    body = Dialog.EDIT_HINT
    assert "* required" in body, "which fields are mandatory is not guessable"
    assert "Example" in body, "a worked example is the fastest instruction"
    assert "web01.internal" in body
    assert "/var/log, /srv/app/logs" in body, "the comma is the part people miss"
    assert "no passwords" in body.lower()


def test_the_required_fields_are_marked_and_the_examples_are_not_truncated() -> None:
    """`~/.ssh/id_ed25519` rendered as `~/.ssh/id_ed2551` in an 18-column field —
    an example that is not merely clipped but wrong, and copied by hand."""

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(RemoteHostsDialog())
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen

            marked = {
                str(label.render())
                for label in screen.query(".editor-label")
            }
            assert "Name *" in marked and "Log dirs *" in marked

            # Asserted against what is actually *painted*, not against a
            # computed column budget: the first version of this test allowed
            # `region.width - 4` and happily passed the very placeholder that
            # rendered as `~/.ssh/id_ed2551`.
            strips = app.screen._compositor.render_strips()
            painted = "\n".join(
                "".join(segment.text for segment in strip) for strip in strips
            )
            for field_id in ("#host-name", "#host-address", "#host-user",
                             "#host-port", "#host-identity", "#host-dirs"):
                placeholder = screen.query_one(field_id, Input).placeholder
                assert placeholder in painted, (
                    f"{field_id} shows {placeholder!r} clipped, so the example an "
                    f"operator copies is not the one that was written"
                )

    _run(scenario)

