"""Reading OpenSSH's config: what counts as a machine, and what is only a note.

Assertions are on candidates and notes rather than on internal state, because
those two are the entire product of this module — the picker shows one and
prints the other, and nothing else ever looks at a scan.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clv.services import config as config_module
from clv.services.config import host_options
from clv.widgets.remote_hosts_dialog import EDITABLE_KEYS
from clv.services.ssh_config import (
    DEFAULT_LOG_DIRS,
    MAX_INCLUDE_DEPTH,
    as_remote_host,
    read_ssh_config,
    ssh_config_path,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _names(scan) -> list[str]:
    return [host.name for host in scan.hosts]


def test_a_plain_host_block_becomes_one_candidate(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host web01\n  HostName 10.0.0.9\n  User ops\n  Port 2222\n",
        )
    )

    (host,) = scan.hosts
    assert (host.name, host.hostname, host.user, host.port) == (
        "web01",
        "10.0.0.9",
        "ops",
        2222,
    )
    assert scan.notes == ()


def test_keys_are_case_insensitive_and_accept_equals_or_whitespace(
    tmp_path: Path,
) -> None:
    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "HOST web01\n  hostname=10.0.0.9\n  UsEr   ops\n  Port = 2222\n",
        )
    )

    (host,) = scan.hosts
    assert (host.hostname, host.user, host.port) == ("10.0.0.9", "ops", 2222)


def test_a_host_line_with_several_patterns_imports_the_first_and_records_the_rest(
    tmp_path: Path,
) -> None:
    """One machine, one section. Three aliases would be three duplicate trees."""

    scan = read_ssh_config(
        _write(tmp_path / "config", "Host web01 web01.dc1 w1\n  HostName 10.0.0.9\n")
    )

    (host,) = scan.hosts
    assert host.name == "web01"
    assert host.also == ("web01.dc1", "w1")
    assert "also web01.dc1, w1" in host.summary()


def test_wildcard_and_negated_patterns_are_never_candidates(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host *\n  User root\n\n"
            "Host web?\n  User root\n\n"
            "Host !bastion prod-01\n  HostName 10.0.0.1\n\n"
            "Host 10.*.*.* fallback\n  User ops\n",
        )
    )

    assert _names(scan) == ["prod-01", "fallback"]


def test_a_wildcard_block_does_not_leak_its_keys_into_named_hosts(
    tmp_path: Path,
) -> None:
    """CLV does not emulate OpenSSH's cross-block inheritance, and need not.

    The import keeps the *alias* as the destination, so ``ssh`` applies
    ``Host *`` itself at connect time. A second opinion here could only drift
    from the one that actually connects.
    """

    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host *\n  User root\n  Port 2222\n\nHost web01\n  HostName 10.0.0.9\n",
        )
    )

    (host,) = scan.hosts
    assert host.user is None
    assert host.port == 22


def test_include_is_expanded_relative_to_the_including_file_and_globbed(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "conf.d" / "a.conf", "Host alpha\n  HostName 10.0.0.1\n")
    _write(tmp_path / "conf.d" / "b.conf", "Host beta\n  HostName 10.0.0.2\n")
    scan = read_ssh_config(
        _write(tmp_path / "config", "Include conf.d/*.conf\n\nHost web01\n")
    )

    assert _names(scan) == ["alpha", "beta", "web01"]


def test_an_include_inside_a_host_block_continues_that_block(tmp_path: Path) -> None:
    """OpenSSH processes Include inline, so this is not a pre-pass."""

    _write(tmp_path / "extra", "  HostName 10.0.0.9\n  User ops\n")
    scan = read_ssh_config(
        _write(tmp_path / "config", "Host web01\n  Include extra\n")
    )

    (host,) = scan.hosts
    assert (host.name, host.hostname, host.user) == ("web01", "10.0.0.9", "ops")


def test_an_include_cycle_is_a_note_not_a_recursion_error(tmp_path: Path) -> None:
    _write(tmp_path / "config", "Include loop\nHost web01\n")
    _write(tmp_path / "loop", "Include config\n")

    scan = read_ssh_config(tmp_path / "config")

    assert _names(scan) == ["web01"]
    assert any("included more than once" in note for note in scan.notes)


def test_include_nesting_stops_at_the_documented_depth(tmp_path: Path) -> None:
    for index in range(MAX_INCLUDE_DEPTH + 3):
        _write(tmp_path / f"f{index}", f"Include f{index + 1}\nHost h{index}\n")
    _write(tmp_path / "config", "Include f0\n")

    scan = read_ssh_config(tmp_path / "config")

    assert any("nested more than" in note for note in scan.notes)


def test_match_blocks_are_skipped_and_counted_once(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host web01\n  HostName 10.0.0.9\n\n"
            "Match host db*\n  User admin\n\n"
            "Match exec true\n  User other\n",
        )
    )

    assert _names(scan) == ["web01"]
    assert sum("Match block" in note for note in scan.notes) == 1
    assert "2 Match block(s)" in " ".join(scan.notes)


def test_full_line_comments_are_dropped_and_a_trailing_hash_stays_literal(
    tmp_path: Path,
) -> None:
    """The binary that connects treats a mid-line ``#`` literally; so do we."""

    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "# Host commented\n  # indented comment\nHost web01\n  User ops#1\n",
        )
    )

    (host,) = scan.hosts
    assert host.name == "web01"
    assert host.user == "ops#1"


def test_quoted_arguments_survive(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            'Host "quoted host"\n  HostName "q.example"\n',
        )
    )

    (host,) = scan.hosts
    assert (host.name, host.hostname) == ("quoted host", "q.example")


def test_the_first_value_wins_for_a_repeated_key(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host web01\n  HostName first.example\n  HostName second.example\n",
        )
    )

    assert scan.hosts[0].hostname == "first.example"


def test_a_missing_config_is_a_note_not_an_exception(tmp_path: Path) -> None:
    scan = read_ssh_config(tmp_path / "nothing-here")

    assert scan.hosts == ()
    assert any("No SSH configuration" in note for note in scan.notes)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_an_unreadable_include_costs_only_itself(tmp_path: Path) -> None:
    secret = _write(tmp_path / "secret", "Host hidden\n")
    secret.chmod(0o000)
    scan = read_ssh_config(
        _write(tmp_path / "config", "Include secret\n\nHost web01\n")
    )

    try:
        assert _names(scan) == ["web01"]
        assert any("Could not read" in note for note in scan.notes)
    finally:
        secret.chmod(0o600)


def test_an_impossible_port_is_reported_through_validate_port(tmp_path: Path) -> None:
    """Refused, never clamped — the same rule ``settings.conf`` applies."""

    scan = read_ssh_config(
        _write(tmp_path / "config", "Host web01\n  Port 70000\n")
    )

    (host,) = scan.hosts
    assert host.port is None
    assert any("70000" in note and "65535" in note for note in scan.notes)


def test_a_missing_identity_file_warns_rather_than_refuses(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(tmp_path / "config", "Host web01\n  IdentityFile ~/.ssh/absent_key\n")
    )

    (host,) = scan.hosts
    assert host.identity_file is not None
    assert host.identity_warning is not None
    assert "ssh-agent" in host.identity_warning


def test_the_path_is_resolved_at_call_time(tmp_path: Path, monkeypatch) -> None:
    """The guard that keeps this suite off the developer's real machine.

    ``~/.ssh/config`` expanded at import time would sail straight past the
    autouse ``isolated_environment`` fixture and read the actual file.
    """

    home = tmp_path / "elsewhere"
    _write(home / ".ssh" / "config", "Host from-the-fake-home\n")
    monkeypatch.setenv("HOME", str(home))

    assert ssh_config_path() == home / ".ssh" / "config"
    assert _names(read_ssh_config()) == ["from-the-fake-home"]


def test_without_drops_names_already_configured(tmp_path: Path) -> None:
    scan = read_ssh_config(
        _write(tmp_path / "config", "Host web01\n\nHost db02\n\nHost cache03\n")
    )

    trimmed = scan.without({"web01", "cache03"})

    assert _names(trimmed) == ["db02"]
    assert trimmed.notes == scan.notes


# --- turning a candidate into a host ----------------------------------------


def test_an_imported_host_writes_only_log_dirs(tmp_path: Path) -> None:
    """The alias is the destination, and that is the whole contract.

    Writing ``host = 10.0.0.9`` would stop ``ssh`` matching the operator's own
    ``Host`` block and silently lose ProxyJump, per-host keys and known_hosts.
    """

    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host web01\n  HostName 10.0.0.9\n  User ops\n  Port 2222\n"
            "  IdentityFile /dev/null\n",
        )
    )

    host, complaint = as_remote_host(scan.hosts[0], DEFAULT_LOG_DIRS)

    assert complaint is None
    assert host is not None
    assert host.name == host.host == "web01"
    assert dict(host_options(host)) == {"log_dirs": "/var/log"}


def test_an_empty_log_dir_is_refused_before_the_write(tmp_path: Path) -> None:
    """The same rule ``config._parse_host`` applies after — applied earlier."""

    scan = read_ssh_config(_write(tmp_path / "config", "Host web01\n"))

    host, complaint = as_remote_host(scan.hosts[0], "")

    assert host is None
    assert "log directory" in (complaint or "")


def test_a_relative_log_dir_is_refused_in_the_shared_wording(tmp_path: Path) -> None:
    scan = read_ssh_config(_write(tmp_path / "config", "Host web01\n"))

    host, complaint = as_remote_host(scan.hosts[0], "logs")

    assert host is None
    assert "relative" in (complaint or "")


def test_an_alias_the_schema_would_not_accept_is_refused(tmp_path: Path) -> None:
    scan = read_ssh_config(_write(tmp_path / "config", 'Host "we[b]01"\n'))

    host, complaint = as_remote_host(scan.hosts[0], DEFAULT_LOG_DIRS)

    assert host is None
    assert "[" in (complaint or "")


def test_a_name_already_configured_is_refused(tmp_path: Path) -> None:
    scan = read_ssh_config(_write(tmp_path / "config", "Host web01\n"))

    host, complaint = as_remote_host(
        scan.hosts[0], DEFAULT_LOG_DIRS, existing={"web01"}
    )

    assert host is None
    assert "already configured" in (complaint or "")


def test_no_refused_key_can_be_smuggled_in(tmp_path: Path) -> None:
    """Structural, not a blocklist: there is no field for these to land in."""

    scan = read_ssh_config(
        _write(
            tmp_path / "config",
            "Host web01\n  Password hunter2\n  Sudo yes\n  PasswordAuthentication yes\n"
            "  SendEnv LANG\n",
        )
    )

    host, complaint = as_remote_host(scan.hosts[0], DEFAULT_LOG_DIRS)

    assert complaint is None
    written = set(dict(host_options(host)))
    assert written <= set(EDITABLE_KEYS)
    assert not written & set(config_module._REFUSED_HOST_KEYS)
