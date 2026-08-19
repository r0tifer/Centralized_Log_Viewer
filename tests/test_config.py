"""Settings resolution, validation and the packaged-app template lookup.

The second half of this file is the ``[ssh:<name>]`` schema. It has no network
in it and never will: this module parses host records and exposes them, and the
thing that connects to one does not exist until ``SSH_TODO.md`` Phase 4.

Two of those tests are the schema *enforcing* a requirement rather than
describing one. ``password`` and ``sudo`` are refused at the point of parsing,
and asserted absent from every field of ``RemoteHost`` — walked reflectively, so
a field added later cannot quietly become the place one lands.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from clv.services.config import (
    CURRENT_CONFIG_VERSION,
    DEFAULT_SETTINGS_TEMPLATE,
    ConfigIssue,
    LogConfig,
    RemoteHost,
    bundled_config_path,
    config_version_of,
    ensure_user_settings_file,
    host_options,
    load_config,
    parse_log_dirs,
    user_config_path,
    validate_host_name,
    validate_identity_file,
    validate_port,
    validate_remote_dirs,
)
from clv.services.discovery import DEFAULT_EXCLUDE_GLOBS, DiscoverySettings


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# --- template location ------------------------------------------------------


def test_bundled_template_is_found_via_meipass_when_frozen(tmp_path, monkeypatch) -> None:
    """PyInstaller extracts data files to sys._MEIPASS, not beside the source."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundled_config_path() == tmp_path / "settings.conf"


def test_bundled_template_falls_back_to_the_source_tree(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    path = bundled_config_path()

    assert path.name == "settings.conf"
    # Development checkout: settings.conf sits beside the clv package.
    assert (path.parent / "clv").is_dir()


def test_shipped_template_is_usable(monkeypatch) -> None:
    """The real settings.conf must name sources, or first run is empty."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    template = bundled_config_path()

    assert template.exists()
    config = load_config(template)
    assert config.log_dirs, "shipped settings.conf must name at least one source"


# --- first run --------------------------------------------------------------


def test_first_run_creates_a_user_settings_file() -> None:
    target = user_config_path()
    if target.exists():
        target.unlink()

    created = ensure_user_settings_file()

    assert created == target
    assert target.exists()
    assert load_config(target).log_dirs


def test_a_template_naming_no_sources_is_not_copied(tmp_path, monkeypatch) -> None:
    """An empty log_dirs would hand the user a viewer with nothing in it."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    _write(tmp_path / "settings.conf", "[log_viewer]\nlog_dirs =\n")

    target = user_config_path()
    if target.exists():
        target.unlink()

    created = ensure_user_settings_file()

    assert created == target
    # Falls back to the built-in defaults rather than the empty template.
    assert created.read_text(encoding="utf-8") == DEFAULT_SETTINGS_TEMPLATE
    assert load_config(created).log_dirs


def test_the_builtin_fallback_template_is_stamped_too(tmp_path, monkeypatch) -> None:
    """The fallback writes a real settings file, so it needs a real version.

    An unstamped fallback would read as version 0 and be "upgraded" on the very
    next install, replacing a file written seconds earlier.
    """
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    _write(tmp_path / "settings.conf", "[log_viewer]\nlog_dirs =\n")
    target = user_config_path()
    if target.exists():
        target.unlink()

    created = ensure_user_settings_file()

    assert created.read_text(encoding="utf-8") == DEFAULT_SETTINGS_TEMPLATE
    assert config_version_of(created) == CURRENT_CONFIG_VERSION


# --- parsing and validation -------------------------------------------------


def test_log_dirs_accepts_folders_files_and_relative_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()

    parsed = parse_log_dirs(" /var/log , rel , /srv/app.log , ")

    assert Path("/var/log") in parsed
    assert tmp_path / "rel" in parsed  # relative resolves against cwd
    assert Path("/srv/app.log") in parsed


def test_duplicate_log_dirs_are_collapsed() -> None:
    assert len(parse_log_dirs("/var/log, /var/log")) == 1


def test_values_are_clamped_not_trusted(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\nrefresh_hz = 9999\n"
            "max_buffer_lines = 0\ncsv_max_cols = -5\ntree_width = 1\n",
        )
    )

    assert config.refresh_hz <= 60
    assert config.max_buffer_lines >= 100
    assert config.csv_max_cols >= 1
    assert config.tree_width >= 20


def test_clipboard_cap_is_read_and_clamped(tmp_path) -> None:
    """A cap of zero would make `y` a no-op; a huge one would be dropped by tmux."""

    assert (
        load_config(
            _write(
                tmp_path / "settings.conf",
                "[log_viewer]\nlog_dirs = /var/log\nclipboard_max_bytes = 4096\n",
            )
        ).clipboard_max_bytes
        == 4096
    )

    clamped = load_config(
        _write(
            tmp_path / "low.conf",
            "[log_viewer]\nlog_dirs = /var/log\nclipboard_max_bytes = 0\n",
        )
    )
    assert clamped.clipboard_max_bytes >= 1024

    default = load_config(_write(tmp_path / "none.conf", "[log_viewer]\nlog_dirs = /var/log\n"))
    assert default.clipboard_max_bytes == LogConfig().clipboard_max_bytes


def test_garbage_values_fall_back_to_defaults(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\nrefresh_hz = banana\n"
            "follow_symlinks = maybe\n",
        )
    )

    assert config.refresh_hz == LogConfig().refresh_hz
    assert config.discovery.follow_symlinks is False


def test_a_missing_section_yields_defaults(tmp_path) -> None:
    config = load_config(_write(tmp_path / "settings.conf", "[something_else]\nx = 1\n"))

    assert config.log_dirs == []
    assert config.max_buffer_lines == LogConfig().max_buffer_lines


def test_unreadable_file_does_not_raise(tmp_path) -> None:
    assert isinstance(load_config(tmp_path / "does-not-exist.conf"), LogConfig)


def test_discovery_globs_round_trip(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n"
            "include_globs = *.log, syslog*\nexclude_globs = *.gz\n"
            "follow_symlinks = true\nskip_binary = false\n",
        )
    )

    assert config.discovery.include_globs == ("*.log", "syslog*")
    assert config.discovery.exclude_globs == ("*.gz",)
    assert config.discovery.follow_symlinks is True
    assert config.discovery.skip_binary is False


def test_omitted_excludes_keep_the_safe_defaults(tmp_path) -> None:
    config = load_config(
        _write(tmp_path / "settings.conf", "[log_viewer]\nlog_dirs = /var/log\n")
    )

    assert config.discovery.exclude_globs == DEFAULT_EXCLUDE_GLOBS
    # An empty include list means "every text file", which is the default.
    assert config.discovery.include_globs == ()


def test_show_lines_cannot_exceed_the_buffer(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n"
            "max_buffer_lines = 200\ndefault_show_lines = 5000\n",
        )
    )

    assert config.default_show_lines <= config.max_buffer_lines


# --- log_dirs and registered schemes ----------------------------------------


def test_a_scheme_shadowed_relative_log_dir_is_refused_with_the_fix(
    tmp_path, monkeypatch
) -> None:
    """The debt Phase 1 pinned, now paid.

    ``journal:archive`` as a *relative* entry is not unreachable — ``Path``
    resolves it against the working directory — but it is the one ``log_dirs``
    entry that comes back unpinned, so it means a different place depending on
    where CLV was launched from. That is worse than a refusal, because it works
    until someone starts the viewer somewhere else.

    Inverts ``test_refs.test_a_scheme_shadowed_relative_dir_is_left_unpinned``.
    """

    (tmp_path / "journal:archive").mkdir()
    (tmp_path / "ordinary").mkdir()
    monkeypatch.chdir(tmp_path)

    issues: list[ConfigIssue] = []
    parsed = parse_log_dirs("journal:archive, ordinary", issues)

    assert parsed == [tmp_path / "ordinary"], "only the shadowed entry is dropped"
    assert len(issues) == 1
    assert issues[0].origin == "log_dirs"
    assert "journal:archive" in issues[0].message
    assert "absolute path" in issues[0].message


def test_an_absolute_path_of_the_same_name_still_works(tmp_path, monkeypatch) -> None:
    """`is_local` short-circuits on is_absolute() before the scheme regex runs."""

    shadowed = tmp_path / "journal:archive"
    shadowed.mkdir()
    monkeypatch.chdir(tmp_path)

    issues: list[ConfigIssue] = []

    assert parse_log_dirs(str(shadowed), issues) == [shadowed]
    assert issues == []


def test_an_ssh_log_dir_entry_names_the_host_section(tmp_path, monkeypatch) -> None:
    """Each scheme gets its own answer, because each has a different one."""

    monkeypatch.chdir(tmp_path)
    issues: list[ConfigIssue] = []

    assert parse_log_dirs("ssh:web01/var/log", issues) == []
    assert "[ssh:<name>]" in issues[0].message


def test_parse_log_dirs_still_works_without_an_issue_list() -> None:
    """The parameter is optional; the refusal must not require a collector."""

    assert parse_log_dirs("journal:archive, /var/log") == [Path("/var/log")]


# --- remote host sections ---------------------------------------------------


SSH_CONFIG = """\
[log_viewer]
log_dirs = /var/log
enable_ssh = true
include_globs = *.log
max_files = 500
max_buffer_lines = 5000

[ssh:web01]
host = web01.internal
user = ops
port = 2222
log_dirs = /var/log, /srv/app/logs
include_globs = *.log, syslog*
max_files = 2000
max_buffer_lines = 1000
correct_clock_skew = true

[ssh:db02]
log_dirs = /var/log/postgresql
"""


def test_multiple_hosts_parse_with_per_host_overrides(tmp_path) -> None:
    config = load_config(_write(tmp_path / "settings.conf", SSH_CONFIG))

    assert config.issues == ()
    assert [host.name for host in config.hosts] == ["web01", "db02"]

    web01 = config.host("web01")
    assert web01 == RemoteHost(
        name="web01",
        host="web01.internal",
        user="ops",
        port=2222,
        log_dirs=("/var/log", "/srv/app/logs"),
        include_globs=("*.log", "syslog*"),
        max_files=2000,
        max_buffer_lines=1000,
        correct_clock_skew=True,
    )


def test_an_absent_host_option_falls_back_to_the_section_name(tmp_path) -> None:
    """`[ssh:web01]` means the machine `web01`, exactly as a ssh_config alias does.

    An operator whose ~/.ssh/config already has a `Host web01` block should not
    have to say so twice; `host` is the override for when CLV's name for a
    machine is not the address to reach it at.
    """

    config = load_config(_write(tmp_path / "settings.conf", SSH_CONFIG))

    assert config.host("db02").host == "db02"
    assert config.host("web01").host == "web01.internal"


def test_per_host_settings_fall_back_to_the_global_ones(tmp_path) -> None:
    config = load_config(_write(tmp_path / "settings.conf", SSH_CONFIG))
    base = config.discovery

    inherits = config.host("db02").discovery_settings(base)
    assert inherits == base, "an override-free host is the global settings, unchanged"
    assert config.host("db02").buffer_lines(config.max_buffer_lines) == 5000

    overrides = config.host("web01").discovery_settings(base)
    assert overrides.include_globs == ("*.log", "syslog*")
    assert overrides.max_files == 2000
    # Untouched keys still come from the global settings.
    assert overrides.exclude_globs == base.exclude_globs
    assert overrides.follow_symlinks == base.follow_symlinks
    assert config.host("web01").buffer_lines(config.max_buffer_lines) == 1000


def test_an_explicitly_empty_glob_list_is_an_override_not_an_absence(tmp_path) -> None:
    """`include_globs =` means "every file", which is not the same as inheriting."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\ninclude_globs = *.log\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\ninclude_globs =\n",
        )
    )

    host = config.host("web01")
    assert host.include_globs == ()
    assert host.discovery_settings(config.discovery).include_globs == ()


def test_hosts_parse_but_stay_inert_when_ssh_is_disabled(tmp_path) -> None:
    """Parsing is not connecting, so a mistake is reported the launch it is made."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n[ssh:web01]\nlog_dirs = /var/log\n",
        )
    )

    assert config.enable_ssh is False, "the master switch defaults off"
    assert [host.name for host in config.hosts] == ["web01"]


def test_a_disabled_host_still_parses(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\nenabled = false\n",
        )
    )

    assert config.host("web01").enabled is False
    assert config.issues == ()


def test_a_config_with_no_ssh_sections_is_unchanged(tmp_path) -> None:
    """The Requirement 13 shape: nothing about a local config moved."""

    config = load_config(
        _write(tmp_path / "settings.conf", "[log_viewer]\nlog_dirs = /var/log\n")
    )

    assert config.hosts == ()
    assert config.issues == ()
    assert config.enable_ssh is False


# --- host validation --------------------------------------------------------


def _issue_text(config: LogConfig) -> str:
    return "\n".join(str(issue) for issue in config.issues)


def test_a_bad_port_skips_the_host_and_says_the_range(tmp_path) -> None:
    """Reported, not clamped: 65535 is not what the operator asked to connect to."""

    for value in ("0", "70000", "banana", "-1"):
        config = load_config(
            _write(
                tmp_path / f"port-{value}.conf",
                "[log_viewer]\nlog_dirs = /var/log\n\n"
                f"[ssh:web01]\nlog_dirs = /var/log\nport = {value}\n",
            )
        )

        assert config.hosts == (), f"port {value} should skip the host"
        assert "1-65535" in _issue_text(config)


def test_a_host_with_no_log_dirs_is_skipped_and_reported(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n[ssh:web01]\nhost = web01\n",
        )
    )

    assert config.hosts == ()
    assert "log_dirs" in _issue_text(config)


def test_a_relative_remote_log_dir_is_refused(tmp_path) -> None:
    """`logs` means one thing under one shell and another under the next."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n"
            "[ssh:web01]\nlog_dirs = logs, /var/log\n",
        )
    )

    assert config.host("web01").log_dirs == ("/var/log",)
    assert "relative" in _issue_text(config)


def test_a_tilde_remote_log_dir_is_accepted(tmp_path) -> None:
    """It is unambiguous, and it must not be expanded against *this* machine."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n[ssh:web01]\nlog_dirs = ~/logs\n",
        )
    )

    assert config.host("web01").log_dirs == ("~/logs",)
    assert config.issues == ()


def test_a_section_with_no_name_is_skipped(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n[ssh:]\nlog_dirs = /var/log\n",
        )
    )

    assert config.hosts == ()
    assert "[ssh:<name>]" in _issue_text(config)


def test_a_missing_identity_file_warns_but_keeps_the_host(tmp_path) -> None:
    """ssh-agent may already hold the key, so a stale line must not cost a machine."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n[ssh:web01]\n"
            f"log_dirs = /var/log\nidentity_file = {tmp_path / 'nope'}\n",
        )
    )

    assert config.host("web01") is not None, "the host survives"
    assert [issue.severity for issue in config.issues] == ["warning"]
    assert "ssh-agent" in _issue_text(config)


def test_a_present_identity_file_is_expanded_and_silent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    key = tmp_path / ".ssh" / "id_ed25519"
    key.parent.mkdir(parents=True)
    key.write_text("not really a key", encoding="utf-8")

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n[ssh:web01]\n"
            "log_dirs = /var/log\nidentity_file = ~/.ssh/id_ed25519\n",
        )
    )

    assert config.host("web01").identity_file == key
    assert config.issues == ()


def test_a_bad_per_host_budget_warns_and_inherits(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\nmax_files = lots\n",
        )
    )

    host = config.host("web01")
    assert host.max_files is None
    assert host.discovery_settings(config.discovery).max_files == config.discovery.max_files
    assert [issue.severity for issue in config.issues] == ["warning"]


def test_a_per_host_budget_is_clamped(tmp_path) -> None:
    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\nmax_buffer_lines = 0\n",
        )
    )

    assert config.host("web01").max_buffer_lines >= 100


def test_a_duplicate_host_section_does_not_cost_the_rest_of_the_file(tmp_path) -> None:
    """The regression the non-strict re-read exists for.

    A repeated section used to raise inside configparser, be swallowed whole,
    and hand back all-defaults — so one duplicated header silently discarded
    every setting the operator had written, log_dirs included.
    """

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\nrefresh_hz = 5\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\n\n"
            "[ssh:web01]\nlog_dirs = /srv/logs\n",
        )
    )

    assert config.log_dirs == [Path("/var/log")], "the rest of the file survived"
    assert config.refresh_hz == 5
    assert config.issues, "and the duplicate is named"
    assert len(config.hosts) == 1


def test_two_sections_naming_the_same_host_are_reported(tmp_path) -> None:
    """`[ssh:web01]` and `[ssh: web01]` are distinct to configparser, not to CLV."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\n\n"
            "[ssh: web01]\nlog_dirs = /srv/logs\n",
        )
    )

    assert [host.name for host in config.hosts] == ["web01"]
    assert config.host("web01").log_dirs == ("/var/log",)
    assert "already configured" in _issue_text(config)


def test_no_malformed_section_ever_raises(tmp_path) -> None:
    """The rule the whole of config.py follows, stated as a test."""

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            "[log_viewer]\nlog_dirs = /var/log\n\n"
            "[ssh:]\n\n"
            "[ssh:a]\nport = nope\n\n"
            "[ssh:b]\nlog_dirs =\n\n"
            "[ssh:c]\nlog_dirs = relative\nmax_files = huge\n\n"
            "[ssh:d]\nlog_dirs = /var/log\nenabled = perhaps\n",
        )
    )

    assert isinstance(config, LogConfig)
    assert config.log_dirs == [Path("/var/log")]
    assert [host.name for host in config.hosts] == ["d"]
    assert config.host("d").enabled is True, "an unreadable bool falls back, as elsewhere"


# --- requirements enforced at the schema ------------------------------------


def _remote_host_values(host: RemoteHost) -> list[str]:
    """Every field's value as text.

    Walked reflectively rather than named, so a field added to RemoteHost later
    cannot quietly become the place a refused value lands.
    """

    return [str(getattr(host, entry.name)) for entry in dataclasses.fields(host)]


def test_no_password_key_is_accepted(tmp_path) -> None:
    """Requirement 9, enforced where adding one would feel helpful."""

    for option in ("password", "passphrase", "password_file"):
        config = load_config(
            _write(
                tmp_path / f"{option}.conf",
                "[log_viewer]\nlog_dirs = /var/log\n\n"
                f"[ssh:web01]\nlog_dirs = /var/log\n{option} = hunter2\n",
            )
        )

        host = config.host("web01")
        assert host is not None, "the key is dropped, not the machine"
        assert not any("hunter2" in value for value in _remote_host_values(host))
        assert not hasattr(host, option)
        assert "ssh-agent" in _issue_text(config)


def test_no_sudo_key_is_accepted(tmp_path) -> None:
    """Requirement 11, same treatment and the same assertion."""

    for option in ("sudo", "use_sudo", "doas", "pkexec", "become"):
        config = load_config(
            _write(
                tmp_path / f"{option}.conf",
                "[log_viewer]\nlog_dirs = /var/log\n\n"
                f"[ssh:web01]\nlog_dirs = /var/log\n{option} = true\n",
            )
        )

        host = config.host("web01")
        assert host is not None
        assert not hasattr(host, option)
        assert "never escalates privilege" in _issue_text(config)
        assert "adm" in _issue_text(config), "and says what to do instead"


def test_remote_host_has_no_credential_or_privilege_field() -> None:
    """The absence itself, asserted — the schema is the enforcement point."""

    names = {entry.name for entry in dataclasses.fields(RemoteHost)}

    assert not names & {
        "password",
        "passphrase",
        "password_file",
        "sudo",
        "use_sudo",
        "doas",
        "pkexec",
        "become",
    }


# --- the phase gate ---------------------------------------------------------


def test_ssh_sections_load_with_no_ssh_plugin_present(tmp_path) -> None:
    """Phase 3's gate: the schema stands on its own.

    There is no SSH source in this build and will not be until Phase 4. Parsing
    a host must not import, require or imply one.
    """

    config = load_config(_write(tmp_path / "settings.conf", SSH_CONFIG))

    assert config.enable_ssh is True
    assert len(config.hosts) == 2
    assert config.issues == ()


def test_the_shipped_template_documents_ssh_and_still_parses(monkeypatch) -> None:
    """The template and the parser cannot drift into disagreeing."""

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    template = bundled_config_path()
    text = template.read_text(encoding="utf-8")

    assert "enable_ssh = false" in text
    assert "# [ssh:web01]" in text
    # The two absences the file must explain rather than merely have.
    assert "no password option" in text
    assert "no sudo option" in text

    config = load_config(template)
    assert config.enable_ssh is False
    assert config.hosts == (), "the example block is commented out"
    assert config.issues == ()


def test_a_skipped_host_is_named_in_the_log_panel(tmp_path) -> None:
    """"Reported through the same channel as plugin errors", proven not claimed.

    A host section skipped in silence is a machine that vanishes from the tree
    with no explanation, which is the outcome Requirement 7 exists to forbid.
    Driven through the real app so the assertion is about what an operator sees
    rather than about a list of dataclasses.
    """

    import asyncio

    from clv.app import LogViewerApp

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "alpha.log").write_text("2026-08-16 09:00:00 INFO up\n", encoding="utf-8")

    config = load_config(
        _write(
            tmp_path / "settings.conf",
            f"[log_viewer]\nlog_dirs = {logs}\nenable_ssh = true\n\n"
            "[ssh:web01]\nlog_dirs = /var/log\n\n"
            "[ssh:db02]\nlog_dirs = /var/log\nport = 70000\n",
        )
    )
    assert [host.name for host in config.hosts] == ["web01"]

    async def scenario() -> None:
        app = LogViewerApp(config=config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            written = "\n".join(str(row.renderable) for row in app.log_panel.rows)

        assert "[ssh:db02]" in written, "the skipped host is named"
        assert "1-65535" in written, "and so is the reason"
        assert "[ssh:web01]" not in written, "the working host says nothing"

    asyncio.run(scenario())


def test_the_builtin_template_documents_ssh_too(tmp_path) -> None:
    """DEFAULT_SETTINGS_TEMPLATE is what a first run gets when the file is absent."""

    assert "enable_ssh = false" in DEFAULT_SETTINGS_TEMPLATE
    assert "[ssh:web01]" in DEFAULT_SETTINGS_TEMPLATE

    config = load_config(_write(tmp_path / "settings.conf", DEFAULT_SETTINGS_TEMPLATE))

    assert config.enable_ssh is False
    assert config.hosts == ()
    assert config.issues == ()


# --- the serialiser and the validators the host dialog shares with the parser


def _round_trip(tmp_path, host: RemoteHost) -> RemoteHost:
    """Write *host* the way the dialog does, and read it back the way CLV does."""

    lines = [f"[ssh:{host.name}]"]
    lines += [f"{key} = {value}" for key, value in host_options(host)]
    path = tmp_path / "settings.conf"
    path.write_text(
        "[log_viewer]\nenable_ssh = true\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.issues == (), config.issues
    return config.host(host.name)


def test_a_fully_populated_host_survives_the_serialiser(tmp_path) -> None:
    """``host_options`` is the inverse of ``_parse_host``, and a test says so.

    A serialiser that drifts from its parser writes a file that reads back as a
    different machine — and the dialog is the only thing that would ever notice,
    by which point the operator's settings file is already wrong.
    """

    key = tmp_path / "id_ed25519"
    key.write_text("not really a key", encoding="utf-8")
    host = RemoteHost(
        name="web01",
        host="web01.internal",
        user="ops",
        port=2222,
        identity_file=key,
        log_dirs=("/var/log", "/srv/app/logs"),
        enabled=False,
        correct_clock_skew=True,
        include_globs=("*.log", "syslog*"),
        exclude_globs=("*.gz",),
        max_files=2000,
        max_buffer_lines=1000,
    )

    assert _round_trip(tmp_path, host) == host


def test_an_all_defaults_host_serialises_to_one_line(tmp_path) -> None:
    """An absent option inherits, so writing the defaults back is noise.

    The operator reads this file; eleven options at their default values is ten
    lines of nothing between them and the one that matters.
    """

    host = RemoteHost(name="web01", host="web01", log_dirs=("/var/log",))

    assert host_options(host) == [("log_dirs", "/var/log")]
    assert _round_trip(tmp_path, host) == host


def test_the_validators_say_what_the_parser_says(tmp_path) -> None:
    """One wording per fault, so the dialog and a config error cannot disagree."""

    assert validate_port("") == (22, None)
    assert validate_port("2222") == (2222, None)
    assert validate_port("nope") == (
        None,
        "port 'nope' is not a number; give one in 1-65535.",
    )
    assert validate_port("70000") == (None, "port 70000 is outside 1-65535.")

    dirs, complaints = validate_remote_dirs("/var/log, logs, ~/app, /var/log")
    assert dirs == ("/var/log", "~/app")
    assert complaints == [
        "log_dirs entry 'logs' is relative; give an absolute path on the "
        "remote host, or a ~-relative one."
    ]

    assert validate_identity_file("") == (None, None)
    missing, warning = validate_identity_file(str(tmp_path / "absent"))
    assert missing == tmp_path / "absent"
    assert warning is not None and "ssh-agent" in warning

    present = tmp_path / "id_ed25519"
    present.write_text("x", encoding="utf-8")
    assert validate_identity_file(str(present)) == (present, None)


def test_a_host_name_must_round_trip_through_its_own_section_header() -> None:
    """``[ssh: web01 ]`` and ``[ssh:web01]`` are one host with two spellings.

    ``_parse_hosts`` strips the suffix, so a padded name parses to the same host
    and writes back to a header the file never had. A bracket is worse: the
    header means a different section than the one intended.
    """

    assert validate_host_name("web01") is None
    assert validate_host_name("") == "Give the host a name."
    assert validate_host_name("   ") == "Give the host a name."
    assert validate_host_name(" web01") == (
        "A host name cannot start or end with a space."
    )
    assert validate_host_name("web[01]") == "A host name cannot contain [ or ]."
    assert validate_host_name("web01", ["db02", "web01"]) == (
        "A host named 'web01' is already configured."
    )
    assert validate_host_name("web01", ["db02"]) is None


# --- the shipped file against the documented defaults ------------------------
#
# `settings.conf` is two thirds prose and README carries a table of the same
# options. Nothing mechanically tied the three together, so a default could be
# changed in `config.py`, left alone in the template, and documented as a fourth
# value in README without anything failing. These are the tie.


def _shipped_template(monkeypatch) -> Path:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    return bundled_config_path()


def test_the_shipped_file_parses_into_the_documented_defaults(monkeypatch) -> None:
    """Every value README's settings table names, read off the real file.

    Asserted against literals rather than against `LogConfig()` defaults on
    purpose: comparing the parser to itself would pass while the template and
    the documentation both said something else.
    """

    config = load_config(_shipped_template(monkeypatch))

    assert config.discovery.follow_symlinks is False
    assert config.discovery.skip_binary is True
    assert config.discovery.max_files == 5000
    assert config.discovery.group_rotated is True
    assert config.max_buffer_lines == 5000
    assert config.default_show_lines == 500
    assert config.min_show_lines == 10
    assert config.show_step == 50
    assert config.refresh_hz == 2
    assert config.tree_width == 38
    assert config.csv_max_rows == 20
    assert config.csv_max_cols == 10
    assert config.clipboard_max_bytes == 65536
    assert config.watch_rate_limit == 60
    assert config.watch_bell is False
    assert config.cluster_lookback == 200
    assert config.enable_journald is False
    assert config.enable_ssh is False


def test_the_shipped_file_names_no_hosts_and_reports_nothing(monkeypatch) -> None:
    """A first run connects to nothing, and says nothing about a clean file.

    The `[ssh:...]` examples below `enable_ssh` are commented out. If one were
    ever uncommented by accident, this is what would notice — and an issue
    reported off the shipped file would mean CLV starts by complaining about
    configuration the operator has not written yet.
    """

    config = load_config(_shipped_template(monkeypatch))

    assert config.hosts == ()
    assert config.issues == ()


def _uncomment_example(template: str, name: str) -> str:
    """The commented ``[ssh:<name>]`` block from the shipped file, uncommented.

    The block is documentation, so nothing parses it and it can drift from the
    schema silently — an option renamed in `config.py` would leave the example
    telling operators to write a line CLV now refuses. Running the real parser
    over it is what closes that.
    """

    lines = template.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"# [ssh:{name}]")
    block = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped.startswith("#"):
            break
        body = stripped[1:].strip()
        if not body:
            break
        block.append(body)
    return "[log_viewer]\nlog_dirs = /var/log\n\n" + "\n".join(block) + "\n"


def test_the_commented_host_example_still_parses(tmp_path, monkeypatch) -> None:
    """Uncommented, the README example is a working host with every option set.

    One warning is expected and is the point: the example names a key file the
    machine running this does not have, and an absent `identity_file` *warns and
    keeps* the host rather than skipping it, because ssh-agent commonly already
    holds the key. Nothing here may be an error.
    """

    template = _shipped_template(monkeypatch).read_text(encoding="utf-8")
    body = _uncomment_example(template, "web01")

    config = load_config(_write(tmp_path / "settings.conf", body))

    assert [issue for issue in config.issues if issue.severity != "warning"] == []
    assert [issue.origin for issue in config.issues] == ["[ssh:web01]"]
    assert "identity_file" in config.issues[0].message
    assert len(config.hosts) == 1
    host = config.hosts[0]
    assert host.name == "web01"
    assert host.host == "web01.internal"
    assert host.user == "ops"
    assert host.port == 22
    assert host.identity_file == Path("~/.ssh/id_ed25519").expanduser()
    assert host.log_dirs == ("/var/log", "/srv/app/logs")
    assert host.include_globs == ("*.log", "syslog*")
    assert host.max_files == 2000
    assert host.max_buffer_lines == 2000
    assert host.correct_clock_skew is False
    assert host.enabled is True


def test_the_second_commented_host_example_still_parses(tmp_path, monkeypatch) -> None:
    """The minimal example: a name, an address and a directory, and nothing else.

    It is the shape the README calls "a complete host", so it has to be one.
    """

    template = _shipped_template(monkeypatch).read_text(encoding="utf-8")
    body = _uncomment_example(template, "db02")

    config = load_config(_write(tmp_path / "settings.conf", body))

    assert config.issues == ()
    assert len(config.hosts) == 1
    assert config.hosts[0].name == "db02"
    assert config.hosts[0].host == "10.0.0.12"
    assert config.hosts[0].log_dirs == ("/var/log/postgresql",)
