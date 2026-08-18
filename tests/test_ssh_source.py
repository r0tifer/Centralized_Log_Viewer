"""Remote sources over SSH: the transport, the backend, and the two live risks.

**No network, no SSH server, no loopback.** Two fakes stand in for one, and the
split is deliberate:

* :func:`fixture_runner` returns canned output and records every argv. This is
  what asserts commands *as strings* — a change to an argv is then visible in
  review rather than only against a real host — and it is what the security
  table runs against.
* :func:`shell_transport` executes the generated script with the local ``sh``
  against a ``tmp_path``. Nothing leaves the machine, but the scripts are really
  run, which is what lets :class:`TestRemoteBackend` inherit the whole of
  ``BackendContract`` from ``tests/test_backend.py`` unedited. That is the
  mechanism Phase 2 built and this is the phase that spends it: parity becomes a
  passing test instead of a claim.

  It exercises whichever command profile the machine running the suite has. The
  other profiles are covered by fake fixtures here and by the opt-in
  ``remote_integration`` suite against real GNU and Alpine images.

The two risks with their own sections at the bottom are the ones the plan calls
highest-severity: **command injection** through a configured path, and **a round
trip per file** turning a 400-file ``/var/log`` into an unusable one.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from clv.plugins.sources import ssh
from clv.plugins.sources.ssh import (
    PROFILES,
    Profile,
    RemoteBackend,
    RemoteFollowReader,
    RemoteResolver,
    SSHConnection,
    SSHError,
    quote_all,
)
from clv.services.backend import (
    LOCAL,
    RECONNECT_ATTEMPTS,
    ClassifyRequest,
    backoff_for,
    blocking_methods,
    cheap_only,
    PROTOCOL_METHODS,
)
from clv.services.config import RemoteHost
from clv.services.discovery import DiscoverySettings, discover
from clv.services.reader import SourceReader, open_reader
from clv.services.refs import RemoteRef, ref_key
from clv.services.session import SourceBuffer

from test_backend import BackendContract, Workspace


HOST = RemoteHost(name="web01", host="web01.internal", user="ops", port=22)


# ==========================================================================
# The two fakes
# ==========================================================================


def _script_of(argv: list[str]) -> str:
    """The remote script out of a complete ssh argv: always the last element."""

    return argv[-1]


def fixture_runner(output: str = "", *, stderr: str = "", returncode: int = 0):
    """A runner that answers with *output* and records what it was asked.

    The framing is applied here rather than being faked away, so a test that
    checks banner handling can put junk in front of it and everything else still
    goes through the same unframing the real transport does.
    """

    calls: list[list[str]] = []

    def run(argv: list[str], timeout: int):
        calls.append(argv)
        script = _script_of(argv)
        start = script.split(None, 2)[2].split(";")[0].strip()
        token = start.removeprefix("__clv_").removesuffix("_start")
        framed = (
            f"__clv_{token}_start\n{output}"
            + ("" if not output or output.endswith("\n") else "\n")
            + f"__clv_{token}_end {returncode}\n"
        )
        return framed.encode(), stderr, 0

    run.calls = calls  # type: ignore[attr-defined]
    return run


def shell_transport(cwd: Optional[Path] = None):
    """A ``(runner, spawn)`` pair that runs the script with the local ``sh``.

    Not a network and not an SSH server — Requirement 14 holds — but the shell
    is real, so quoting bugs, sentinel framing, ``od`` encoding and the profile
    fallbacks are all genuinely exercised. This is what makes the contract suite
    below mean something.
    """

    def run(argv: list[str], timeout: int):
        completed = subprocess.run(
            ["/bin/sh", "-c", _script_of(argv)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
        return (
            completed.stdout,
            completed.stderr.decode("utf-8", errors="replace"),
            completed.returncode,
        )

    def spawn(argv, **kwargs):
        kwargs.pop("env", None)
        kwargs.pop("text", None)
        return subprocess.Popen(
            ["/bin/sh", "-c", _script_of(argv)], cwd=cwd, **kwargs
        )

    return run, spawn


needs_shell = pytest.mark.skipif(
    not os.path.exists("/bin/sh"), reason="the shell transport needs /bin/sh"
)


# ==========================================================================
# The contract suite, against a backend that shells out
# ==========================================================================


@needs_shell
class TestRemoteBackend(BackendContract):
    """Every assertion ``LocalBackend`` satisfies, satisfied over a transport.

    Not one of them is edited, which is the whole point of writing them against
    the protocol in Phase 2. What changes is two fixtures.
    """

    @pytest.fixture
    def backend(self, tmp_path: Path) -> RemoteBackend:
        run, spawn = shell_transport()
        return RemoteBackend(
            SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
        )

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Workspace:
        """A real tree, addressed as though it were on another machine.

        The remote path *is* the local one, because the fake transport runs the
        script here — so nothing has to translate paths and the scripts under
        test are the ones that would be sent.
        """

        root = tmp_path / "tree"
        root.mkdir()

        def ref(relative: str) -> RemoteRef:
            return RemoteRef.build(HOST.name, str(root / relative))

        def write(relative: str, text: str) -> RemoteRef:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return ref(relative)

        def mkdir(relative: str) -> RemoteRef:
            (root / relative).mkdir(parents=True, exist_ok=True)
            return ref(relative)

        def remove(relative: str) -> None:
            target = root / relative
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

        def rename(source: str, destination: str) -> None:
            (root / source).rename(root / destination)

        return Workspace(
            root=RemoteRef.build(HOST.name, str(root)),
            write=write,
            mkdir=mkdir,
            remove=remove,
            ref=ref,
            rename=rename,
        )

    # --- the two contract assertions that cannot hold as written ------------

    def test_walk_does_not_pay_for_what_the_caller_never_asks_for(
        self, backend, workspace
    ) -> None:
        """Overridden: remote laziness is client-side and cannot be observed
        by deleting files.

        The inherited version pulls one entry, deletes half the tree, and counts
        what still arrives — an eager walk captured everything before the
        deletion. That works because ``os.scandir`` is pulled synchronously by
        the consumer.

        A remote walk is one ``find`` running on the **far side of a pipe**. It
        enumerates at its own pace regardless of how fast this end reads, and
        over a ten-file tree it finishes before the deletion happens. Asserting
        otherwise would be asserting something about the network's timing.

        What laziness has to mean here instead, and what is checked:

        * the walker is a true iterator, so nothing was materialised into a list;
        * abandoning it **terminates the remote command** rather than leaving a
          ``find`` enumerating someone else's filesystem — which is what
          ``max_files`` does every time it fires, and which is the real cost
          this contract exists to bound.
        """

        for index in range(5):
            workspace.write(f"a/f{index}.log", "x\n")
            workspace.write(f"z/g{index}.log", "x\n")

        walker = backend.walk(workspace.root)

        assert iter(walker) is walker
        assert next(walker).ref.name.endswith(".log")

        before = len(backend.connection.commands)
        walker.close()
        assert len(backend.connection.commands) == before, (
            "closing a walk must not issue another command"
        )

    def test_walk_order_is_the_remote_filesystem_s_own(
        self, backend, workspace
    ) -> None:
        """Stated rather than left to be discovered.

        ``LocalBackend.walk`` sorts, and ``max_files`` truncation is defined by
        that order. ``find`` returns directory order instead, and sorting it
        remotely would mean buffering the whole enumeration — giving up the
        laziness above to gain an ordering that ``discover`` throws away anyway,
        since it sorts the report before anyone sees it.

        What that costs is worth being precise about: over a tree larger than
        ``max_files``, *which* subset survives is the remote's directory order
        rather than alphabetical. It is stable between rescans of an unchanged
        tree, and the truncation is reported either way.
        """

        for name in ("charlie.log", "alpha.log", "bravo.log"):
            workspace.write(name, "x\n")

        names = [entry.ref.name for entry in backend.walk(workspace.root)]

        assert sorted(names) == ["alpha.log", "bravo.log", "charlie.log"]

    # --- and the one about a cheap call with nothing cheap to say -----------

    def test_the_cheap_methods_still_work_under_the_guard(
        self, backend, workspace
    ) -> None:
        """Overridden, and this is the single departure from the contract.

        The inherited version writes a file and immediately asserts
        ``stat(...) is not None`` under ``cheap_only()``. For a local backend
        that is a syscall. For this one there is **no cheap true answer about a
        file it has never measured** — the honest reply is "I would have to ask,
        and asking here is the frozen UI this guard exists to prevent".

        So the assertion is replaced by the stronger pair it stands for, rather
        than deleted or quietly satisfied by blocking:

        * a source that has been measured — which on the real path is every
          source, because ``prime()`` opens it before ``poll()`` ever runs —
          answers from the cache;
        * one that has not answers ``None``, which the protocol already permits,
          instead of going to the wire.
        """

        log = workspace.write("a.log", "alpha\n")
        unmeasured = workspace.write("never-opened.log", "bravo\n")

        backend.refresh(log)  # what prime() does, from a worker

        with cheap_only():
            assert backend.stat(log) is not None
            assert backend.identity(log) is not None
            assert backend.stat(unmeasured) is None, (
                "a cold stat under the guard went to the network"
            )

    # --- and what only a remote backend can be asked ------------------------

    def test_it_declares_the_costs_local_does_not(self, backend) -> None:
        """The reason the guard exists: almost everything here is a round trip."""

        assert backend.capabilities.blocking == frozenset(
            {"walk", "list_dir", "kind", "access", "open", "classify"}
        )
        assert LOCAL.capabilities.blocking == frozenset()

    def test_the_access_hint_never_recommends_sudo(self, backend) -> None:
        """Requirement 11, at the point where the local answer would be wrong.

        "Re-launch with sudo" is not merely unhelpful for a file on another
        machine — it recommends the one thing CLV refuses to do anywhere.
        """

        hint = backend.capabilities.access_hint
        assert "sudo" not in hint.lower()
        assert "adm" in hint


# ==========================================================================
# stat: the member that is guaranteed cheap and backed by a network
# ==========================================================================


@needs_shell
def test_stat_serves_the_cache_under_the_poll_guard(tmp_path: Path) -> None:
    """The Requirement 3 resolution, asserted from both sides.

    ``stat`` is ``GUARANTEED_CHEAP`` because ``poll()`` calls it on the event
    loop twice a second per merged source. It is also the honest answer to "did
    this grow?". Both hold because it answers *who is asking*.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )
    ref = RemoteRef.build(HOST.name, str(log))

    first = backend.stat(ref)
    assert first is not None and first.size == len("alpha\n")

    log.write_text("alpha\nbravo\n", encoding="utf-8")

    with cheap_only():
        cached = backend.stat(ref)
    assert cached is not None
    assert cached.size == len("alpha\n"), "the guard must not have gone to the wire"

    assert backend.stat(ref).size == len("alpha\nbravo\n")


@needs_shell
def test_poll_issues_no_command_at_all(tmp_path: Path) -> None:
    """The regression test this whole design exists for.

    Five remote logs polled at 2 Hz on a 60 ms link is 600 ms of frozen UI per
    second. The guarantee is not "fast": it is *no command*.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    run, spawn = shell_transport()
    connection = SSHConnection(
        HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path)
    )
    backend = RemoteBackend(connection)
    ref = RemoteRef.build(HOST.name, str(log))

    buffer = SourceBuffer(
        ref,
        max_lines=100,
        reader=SourceReader(ref, max_lines=100, backend=backend),
    )
    buffer.prime()

    before = len(connection.commands)
    for _ in range(5):
        buffer.poll()

    assert len(connection.commands) == before, (
        "poll() reached the network; that is a frozen UI at refresh_hz"
    )


# ==========================================================================
# Command construction, per profile
# ==========================================================================


def _backend_with(profile: Profile, output: str = "") -> tuple[RemoteBackend, list]:
    run = fixture_runner(output)
    connection = SSHConnection(HOST, runner=run, socket_dir="/tmp")
    connection._facts = ssh.HostFacts(profile=profile)  # probe already done
    return RemoteBackend(connection), run.calls  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["gnu", "busybox", "bsd"])
def test_a_walk_is_one_command_whatever_the_profile(name: str) -> None:
    """Requirement 4 holds on every profile, not only where ``-printf`` exists.

    A BusyBox host loses ``-printf`` and gets ``-exec stat … {} +``, which
    batches: still one command, still one round trip. What it must never
    degrade to is a stat per file.
    """

    backend, calls = _backend_with(PROFILES[name])
    list(backend.walk(RemoteRef.build("web01", "/var/log")))

    assert len(calls) == 1
    script = _script_of(calls[0])
    assert "find" in script and "-type f" in script
    if name == "gnu":
        assert "-printf" in script
    else:
        assert "-exec stat" in script and "{} +" in script


def test_the_gnu_walk_is_nul_terminated() -> None:
    """So a newline inside a filename is a filename and not two files."""

    backend, calls = _backend_with(PROFILES["gnu"])
    list(backend.walk(RemoteRef.build("web01", "/var/log")))

    assert "\\0" in _script_of(calls[0])


def test_include_globs_are_pushed_down_when_they_are_expressible() -> None:
    """A ``/var/log`` full of archives should not cross the wire to be dropped."""

    run = fixture_runner()
    connection = SSHConnection(HOST, runner=run, socket_dir="/tmp")
    connection._facts = ssh.HostFacts(profile=PROFILES["gnu"])
    backend = RemoteBackend(connection, include_globs=("*.log", "syslog*"))

    list(backend.walk(RemoteRef.build("web01", "/var/log")))

    script = _script_of(run.calls[0])  # type: ignore[attr-defined]
    assert "-name '*.log'" in script
    assert "-name 'syslog*'" in script


def test_a_path_shaped_glob_is_not_pushed_down() -> None:
    """``find -name`` matches a basename and cannot express ``nginx/*.log``.

    Pushing it down would silently filter on the wrong thing, so it is left to
    this side where ``matched_glob`` already tests the root-relative path.
    """

    run = fixture_runner()
    connection = SSHConnection(HOST, runner=run, socket_dir="/tmp")
    connection._facts = ssh.HostFacts(profile=PROFILES["gnu"])
    backend = RemoteBackend(connection, include_globs=("nginx/*.log",))

    list(backend.walk(RemoteRef.build("web01", "/var/log")))

    assert "-name" not in _script_of(run.calls[0])  # type: ignore[attr-defined]


def test_a_ranged_read_uses_dd_where_it_exists_and_tail_where_it_does_not() -> None:
    """The documented degradation, pinned. Both are one round trip."""

    gnu, gnu_calls = _backend_with(PROFILES["gnu"])
    gnu.read_range(RemoteRef.build("web01", "/var/log/syslog"), 4096, 1024)
    assert "dd if=" in _script_of(gnu_calls[0])
    assert "iflag=skip_bytes,count_bytes" in _script_of(gnu_calls[0])

    bsd, bsd_calls = _backend_with(PROFILES["bsd"])
    bsd.read_range(RemoteRef.build("web01", "/var/log/syslog"), 4096, 1024)
    script = _script_of(bsd_calls[0])
    assert "tail -c +4097" in script and "head -c 1024" in script


def test_a_host_with_no_stat_declares_no_stable_identity() -> None:
    """Rotation detection degrades to the shrink-only path Phase 2 built.

    Declared rather than discovered: ``SourceReader`` reads
    ``capabilities.stable_identity`` and says what it is doing.
    """

    backend, _calls = _backend_with(Profile(name="posix"))

    assert backend.capabilities.stable_identity is False
    assert PROFILES["gnu"].stable_identity is True


# ==========================================================================
# The probe
# ==========================================================================


def test_the_probe_is_one_command_and_carries_the_clock() -> None:
    """Capability and clock together, because each alone is a handshake.

    The skew and the UTC offset are captured now and spent by Phase 6: a merged
    view across machines with disagreeing clocks must never present a confident
    ordering it cannot justify.
    """

    run = fixture_runner(
        "Linux\nprintf=yes\nstatc=yes\nstatf=no\ndd=yes\ndate=4000000000 -0500\n"
    )
    connection = SSHConnection(HOST, runner=run, socket_dir="/tmp")

    facts = connection.facts()

    assert len(run.calls) == 1  # type: ignore[attr-defined]
    assert facts.profile.name == "gnu"
    assert facts.utc_offset.total_seconds() == -5 * 3600
    assert facts.skew.total_seconds() != 0
    connection.facts()
    assert len(run.calls) == 1, "the probe must not repeat per operation"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Linux\nprintf=yes\nstatc=yes\nstatf=no\ndd=yes\n", "gnu"),
        ("Linux\nprintf=no\nstatc=yes\nstatf=no\ndd=yes\n", "busybox"),
        ("FreeBSD\nprintf=no\nstatc=no\nstatf=yes\ndd=no\n", "bsd"),
        ("SomeOS\nprintf=no\nstatc=no\nstatf=no\ndd=no\n", "posix"),
    ],
)
def test_the_profile_is_probed_never_assumed(output: str, expected: str) -> None:
    """Requirement 5. Alpine is a first-class target, not an edge case."""

    connection = SSHConnection(HOST, runner=fixture_runner(output), socket_dir="/tmp")

    assert connection.facts().profile.name == expected


def test_a_busybox_host_without_dd_loses_only_the_ranged_read() -> None:
    """One absent capability must not cascade into a different profile."""

    connection = SSHConnection(
        HOST,
        runner=fixture_runner("Linux\nprintf=no\nstatc=yes\nstatf=no\ndd=no\n"),
        socket_dir="/tmp",
    )
    profile = connection.facts().profile

    assert profile.name == "busybox"
    assert profile.dd_skip_bytes is False
    assert profile.stable_identity is True


# ==========================================================================
# Shell noise
# ==========================================================================


def test_a_banner_before_the_output_is_not_a_filename() -> None:
    """A data-integrity problem rather than a cosmetic one.

    An MOTD, a legal banner or a ``.bashrc`` echo lands in ``find`` output
    looking exactly like a path, and a phantom source in the tree is worse than
    an ugly one.
    """

    # NUL-terminated, because that is what a GNU walk really emits — and what
    # lets a newline inside a filename be part of the filename.
    noisy = fixture_runner("2048\t1\t2\t/var/log/real.log\0")

    def with_banner(argv, timeout):
        stdout, stderr, code = noisy(argv, timeout)
        return (
            b"Welcome to web01!\nUnauthorised access is prohibited.\n" + stdout,
            stderr,
            code,
        )

    connection = SSHConnection(HOST, runner=with_banner, socket_dir="/tmp")
    connection._facts = ssh.HostFacts(profile=PROFILES["gnu"])
    backend = RemoteBackend(connection)

    names = [entry.ref.name for entry in backend.walk(RemoteRef.build("web01", "/var/log"))]

    assert names == ["real.log"]


def test_output_that_never_reached_the_sentinel_is_an_error_not_an_empty_read() -> None:
    """Requirement 7, at the seam where it would otherwise be lost.

    A remote pane that goes quiet because the link dropped is the single worst
    outcome of this feature. "Produced nothing" and "stopped early" must not be
    the same value.
    """

    def truncated(argv, timeout):
        return b"__clv_deadbeef_start\npartial", "Connection reset by peer", 255

    connection = SSHConnection(HOST, runner=truncated, socket_dir="/tmp")

    with pytest.raises(SSHError):
        connection.run("echo hi")


@pytest.mark.parametrize(
    ("stderr", "fragment"),
    [
        ("Host key verification failed.", "Connect once by hand"),
        ("REMOTE HOST IDENTIFICATION HAS CHANGED!", "CHANGED"),
        ("ops@web01: Permission denied (publickey).", "ssh-agent"),
        ("ssh: Could not resolve hostname web01", "does not resolve"),
        ("connect to host web01 port 22: Connection refused", "nothing is listening"),
    ],
)
def test_each_failure_reads_as_its_own_fact(stderr: str, fragment: str) -> None:
    """Five different problems, five different instructions.

    Folding them into "connection failed" is what makes a remote source feel
    unfixable, and the host-key cases are the ones ``BatchMode`` makes
    reportable at all instead of a hang.
    """

    def failing(argv, timeout):
        return b"", stderr, 255

    connection = SSHConnection(HOST, runner=failing, socket_dir="/tmp")

    with pytest.raises(SSHError) as caught:
        connection.run("echo hi")
    assert fragment in str(caught.value)
    assert "web01" in str(caught.value)


# ==========================================================================
# Security
# ==========================================================================


def _every_argv(backend: RemoteBackend, calls: list) -> list[list[str]]:
    root = RemoteRef.build("web01", "/var/log")
    list(backend.walk(root))
    backend.kind(root)
    backend.access(root, os.R_OK)
    backend.refresh(root / "syslog")
    backend.classify([ClassifyRequest(ref=root / "syslog", head_bytes=8192)])
    backend.read_range(root / "syslog", 0, 64)
    return list(calls)


@pytest.mark.parametrize("name", ["gnu", "busybox", "bsd"])
def test_no_argv_ever_weakens_host_key_checking_or_escalates(name: str) -> None:
    """Requirements 10 and 11, as a test rather than as a review comment.

    Not behind a flag, not "for testing". An unknown host key is an unreachable
    host with a message saying so — never a disabled check.
    """

    backend, calls = _backend_with(PROFILES[name])
    banned = (
        "stricthostkeychecking=no",
        "userknownhostsfile",
        "sudo",
        "doas",
        "pkexec",
        "password",
    )

    for argv in _every_argv(backend, calls):
        joined = " ".join(argv).lower()
        for forbidden in banned:
            assert forbidden not in joined, f"{forbidden!r} reached an argv"


@pytest.mark.parametrize("name", ["gnu", "busybox", "bsd"])
def test_every_argv_carries_batchmode(name: str) -> None:
    """The single flag that stops CLV hanging invisibly inside the TUI.

    Without it the first connection to an unknown host writes a confirmation
    prompt to a stdin nobody is reading.
    """

    backend, calls = _backend_with(PROFILES[name])

    for argv in _every_argv(backend, calls):
        assert "BatchMode=yes" in argv
        assert "-T" in argv


HOSTILE_PATHS = [
    "/var/log/two words",
    "/var/log/it's",
    '/var/log/say "hi"',
    "/var/log/$(reboot)",
    "/var/log/`reboot`",
    "/var/log/;reboot",
    "/var/log/a|b",
    "/var/log/a&b",
    "/var/log/*",
    "/var/log/../etc",
    "/var/log/\\backslash",
    "/var/log/new\nline",
    "/var/log/-dashed",
    "/var/log/$HOME",
    "/var/log/${IFS}",
]


@pytest.mark.parametrize("hostile", HOSTILE_PATHS)
def test_a_hostile_path_is_data_and_never_code(hostile: str) -> None:
    """The highest-severity risk in the plan, table-driven.

    ``ssh`` joins its argv with spaces and hands the result to a login shell, so
    a path is re-parsed on the far side. :func:`quote_all` is the only way one
    enters a script; this asserts that what comes back out of ``sh`` is the
    original string, byte for byte.
    """

    quoted = quote_all(hostile)

    completed = subprocess.run(
        ["/bin/sh", "-c", f"printf '%s' {quoted}"],
        capture_output=True,
        check=True,
    )

    assert completed.stdout.decode() == hostile


@needs_shell
@pytest.mark.parametrize("hostile", ["two words", "it's", "$(touch pwned)", "a;b"])
def test_a_hostile_filename_survives_a_real_walk(tmp_path: Path, hostile: str) -> None:
    """The same risk from the other end: a file *named* hostilely is found."""

    root = tmp_path / "tree"
    root.mkdir()
    (root / hostile).write_text("alpha\n", encoding="utf-8")

    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )

    names = [
        entry.ref.name
        for entry in backend.walk(RemoteRef.build(HOST.name, str(root)))
    ]

    assert names == [hostile]
    assert not (tmp_path / "pwned").exists()
    assert not (root / "pwned").exists()


# ==========================================================================
# Consent
# ==========================================================================


def test_nothing_is_spawned_without_the_opt_in(monkeypatch, tmp_path: Path) -> None:
    """Requirement 8, asserted at the spawn point as the journald suite does.

    A *network* subprocess raises the consent bar rather than lowering it, so
    with ``enable_ssh`` false the resolver is not merely inert — it is the local
    backend, and there is nothing to be inert.
    """

    spawned: list[object] = []
    monkeypatch.setattr(
        ssh.subprocess, "Popen", lambda *a, **k: spawned.append(a) or None
    )
    monkeypatch.setattr(
        ssh.subprocess, "run", lambda *a, **k: spawned.append(a) or None
    )

    assert ssh.enabled() is False
    assert not spawned


def test_a_resolver_routes_local_refs_to_the_local_backend(tmp_path: Path) -> None:
    resolver = RemoteResolver([HOST], local=LOCAL, socket_dir=str(tmp_path))

    assert resolver.for_ref(Path("/var/log/syslog")) is LOCAL
    assert isinstance(resolver.for_ref(RemoteRef.build("web01", "/var/log")), RemoteBackend)


def test_a_ref_naming_an_unconfigured_host_does_not_invent_a_connection(
    tmp_path: Path,
) -> None:
    """A starred ref for a host the operator removed from ``settings.conf``.

    Reported as missing by the local backend rather than connected to. Phase 6
    gives it a message pointing at the host dialog; what matters now is that it
    does not silently open something.
    """

    resolver = RemoteResolver([HOST], local=LOCAL, socket_dir=str(tmp_path))

    assert resolver.for_ref(RemoteRef.build("gone", "/var/log")) is LOCAL
    assert resolver.backends == {}


def test_a_resolver_opens_one_connection_per_host_not_per_ref(tmp_path: Path) -> None:
    resolver = RemoteResolver([HOST], local=LOCAL, socket_dir=str(tmp_path))

    first = resolver.for_ref(RemoteRef.build("web01", "/var/log/a.log"))
    second = resolver.for_ref(RemoteRef.build("web01", "/var/log/b.log"))

    assert first is second


# ==========================================================================
# The ControlMaster socket
# ==========================================================================


def test_the_socket_path_is_a_hash_not_the_host_name(tmp_path: Path) -> None:
    """``sun_path`` is about 104 bytes, and overflowing it fails *silently*.

    Multiplexing simply stops and every command pays a full handshake, with
    nothing saying why. A long FQDN under a long ``XDG_RUNTIME_DIR`` is an
    ordinary configuration, not a contrived one.
    """

    long_host = RemoteHost(
        name="a" * 90, host="very.long.fully.qualified.example.internal" * 2
    )
    connection = SSHConnection(long_host, socket_dir=str(tmp_path))

    assert len(connection.socket) < 100
    assert "a" * 90 not in connection.socket


def test_two_hosts_do_not_share_a_socket(tmp_path: Path) -> None:
    first = SSHConnection(HOST, socket_dir=str(tmp_path))
    second = SSHConnection(
        RemoteHost(name="db02", host="10.0.0.12"), socket_dir=str(tmp_path)
    )

    assert first.socket != second.socket


def test_the_socket_directory_is_private(monkeypatch, tmp_path: Path) -> None:
    """A multiplex socket is a live authenticated connection.

    Any local process running as this user can ride one, so the directory is
    ``0700`` and the socket is torn down rather than left to expire.
    """

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    directory = ssh.control_socket_dir()

    assert (os.stat(directory).st_mode & 0o777) == 0o700


def test_closing_removes_the_socket_and_asks_the_master_to_exit(
    tmp_path: Path,
) -> None:
    """Not left to ``ControlPersist``.

    "It expires in a minute" is a worse answer than "it is gone" when the reason
    CLV is closing may be that the operator is walking away from the machine.
    """

    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(argv)
        return b"", "", 0

    connection = SSHConnection(HOST, runner=runner, socket_dir=str(tmp_path))
    Path(connection.socket).write_bytes(b"")

    connection.close()

    assert not Path(connection.socket).exists()
    assert any("-O" in argv and "exit" in argv for argv in calls)


def test_closing_a_connection_that_never_connected_does_nothing(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    connection = SSHConnection(
        HOST, runner=lambda argv, timeout: calls.append(argv) or (b"", "", 0),
        socket_dir=str(tmp_path),
    )

    connection.close()
    connection.close()

    assert calls == []


# ==========================================================================
# The round-trip budget
# ==========================================================================


@needs_shell
def test_discovering_two_hundred_files_is_a_bounded_number_of_commands(
    tmp_path: Path,
) -> None:
    """Requirement 4, as a test rather than as a review comment.

    A round trip per file is what makes an ``sshfs`` mount slow, and the binary
    sniff is where it would silently come back: ``skip_reason`` reads 8 KB per
    candidate. This fails loudly the day someone reintroduces a per-file call.
    """

    root = tmp_path / "tree"
    root.mkdir()
    for index in range(200):
        (root / f"app{index:03d}.log").write_text("alpha\n", encoding="utf-8")

    run, spawn = shell_transport()
    connection = SSHConnection(
        HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path)
    )
    backend = RemoteBackend(connection)
    resolver = RemoteResolver([HOST], local=LOCAL, socket_dir=str(tmp_path))
    resolver._backends["web01"] = backend

    report = discover(
        [RemoteRef.build(HOST.name, str(root))],
        DiscoverySettings(),
        backends=resolver,
    )

    assert report.file_count == 200
    assert len(connection.commands) <= 6, (
        f"200 files cost {len(connection.commands)} commands; "
        "that is a per-file round trip"
    )


@needs_shell
def test_a_binary_file_is_skipped_without_being_fetched(tmp_path: Path) -> None:
    """The verdict is computed here, from bytes measured there.

    A NUL test written in ``sh`` would be four characters and would reject every
    UTF-16 export — PowerShell's normal output — because UTF-16 encodes ASCII
    with a NUL beside every character. The rule stays in
    ``reader.looks_binary_block``.
    """

    root = tmp_path / "tree"
    root.mkdir()
    (root / "text.log").write_text("alpha\n", encoding="utf-8")
    (root / "binary.log").write_bytes(b"\x00\x01\x02binary\x00")
    (root / "utf16.log").write_text("alpha\nbravo\n", encoding="utf-16")

    run, spawn = shell_transport()
    connection = SSHConnection(
        HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path)
    )
    resolver = RemoteResolver([HOST], local=LOCAL, socket_dir=str(tmp_path))
    resolver._backends["web01"] = RemoteBackend(connection)

    report = discover(
        [RemoteRef.build(HOST.name, str(root))],
        DiscoverySettings(),
        backends=resolver,
    )

    found = {item.path.name for item in report.files}
    assert found == {"text.log", "utf16.log"}
    assert report.skipped_unsupported == 1


@pytest.mark.parametrize(
    "name", ["empty.gz", "header-only.gz", "garbage.gz", "good.gz", "big.gz"]
)
def test_the_batched_probe_agrees_with_the_one_it_replaced(
    tmp_path: Path, name: str
) -> None:
    """Requirement 13, at the one place batching could have changed a verdict.

    ``compressed.probe`` opens the file and decompresses; ``probe_block`` gets a
    sample from a batch. They must reach the same answer on every shape,
    including the two that are arguably lenient — an empty ``.gz`` and one that
    is only a header both read as valid, because ``gzip`` yields ``b""`` at EOF
    rather than raising. *Matching* is the requirement here; improving on it
    would be a behaviour change this phase is not allowed to make.

    ``big.gz`` is the case ``complete`` exists for: incompressible content, so
    the member is far longer than the sample and the decompressor runs out of
    input part way through a perfectly good file.
    """

    import gzip
    import os as _os

    from clv.services.compressed import PROBE_SIZE, probe, probe_block

    bodies = {
        "empty.gz": b"",
        "header-only.gz": gzip.compress(b"x" * 100)[:12],
        "garbage.gz": b"not gzip at all",
        "good.gz": gzip.compress(b"line\n" * 20),
        "big.gz": gzip.compress(_os.urandom(200_000)),
    }
    target = tmp_path / name
    target.write_bytes(bodies[name])
    sample = bodies[name][:PROBE_SIZE]

    assert probe_block(
        target, sample, complete=len(bodies[name]) <= PROBE_SIZE
    ) is probe(target)


# ==========================================================================
# Reading
# ==========================================================================


@needs_shell
def test_a_remote_log_reads_its_bounded_tail(tmp_path: Path) -> None:
    """The feature, end to end through the ordinary reader.

    Nothing about ``SourceReader`` changed. It asks a backend for a seekable
    handle and steps backwards, and the handle happens to be on another machine.
    """

    log = tmp_path / "syslog"
    log.write_text("".join(f"line {index}\n" for index in range(500)), encoding="utf-8")

    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )
    ref = RemoteRef.build(HOST.name, str(log))

    result = SourceReader(ref, max_lines=10, backend=backend).prime()

    assert result.lines[-1] == "line 499"
    assert len(result.lines) == 10
    assert result.truncated is True


@needs_shell
def test_a_bounded_tail_is_one_ranged_read(tmp_path: Path) -> None:
    """The window exists so a backwards walk costs one round trip, not sixteen.

    ``read_last_lines`` steps backwards in 64 KiB chunks. Served naively that is
    a fetch per chunk; served from a window it is one. Priming also opens the
    file more than once — to sniff the encoding, then to read — and the cache
    living on the *backend* rather than on the handle is what stops each of
    those refetching the same bytes.
    """

    log = tmp_path / "syslog"
    log.write_text("".join(f"line {index}\n" for index in range(500)), encoding="utf-8")

    run, spawn = shell_transport()
    connection = SSHConnection(
        HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path)
    )
    backend = RemoteBackend(connection)
    ref = RemoteRef.build(HOST.name, str(log))
    connection.facts()  # probe once, so the count below is about reading

    before = len(connection.commands)
    SourceReader(ref, max_lines=10, backend=backend).prime()
    issued = [_script_of(argv) for argv in connection.commands[before:]]
    ranged = [
        script
        for script in issued
        if f"dd if={str(log)}" in script or "tail -c +" in script
    ]

    assert len(ranged) == 1, f"the backwards walk cost {len(ranged)} fetches"
    # The rest are `stat`s, one per open. Pinned so the cost of opening a remote
    # source is a number someone has to change deliberately rather than drift.
    assert len(issued) <= 5, issued


@needs_shell
def test_a_remote_compressed_member_reads_through_the_handle(tmp_path: Path) -> None:
    """Half of ``/var/log`` is ``.gz``, and it is reachable because Phase 2
    made the decompressors take a handle rather than a path."""

    import gzip

    log = tmp_path / "syslog.1.gz"
    with gzip.open(log, "wb") as handle:
        handle.write(b"".join(f"line {index}\n".encode() for index in range(50)))

    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )
    ref = RemoteRef.build(HOST.name, str(log))

    from clv.services.reader import open_reader

    result = open_reader(ref, max_lines=5, backend=backend).prime()

    assert result.lines[-1] == "line 49"


@needs_shell
def test_a_remote_file_refuses_to_be_written_to(tmp_path: Path) -> None:
    """Read-only, always. The one thing this feature must never learn to do."""

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")

    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )
    handle = backend.open(RemoteRef.build(HOST.name, str(log)))

    with pytest.raises(Exception):
        handle.write(b"nope")
    with pytest.raises(Exception):
        backend.open(RemoteRef.build(HOST.name, str(log)), "wb")


# ==========================================================================
# Following
# ==========================================================================


def _follow(tmp_path: Path, log: Path, *, max_lines: int = 100):
    """A tailer over *log*, with the transport that really runs the script."""

    run, spawn = shell_transport()
    connection = SSHConnection(
        HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path)
    )
    backend = RemoteBackend(connection)
    ref = RemoteRef.build(HOST.name, str(log))
    return connection, backend, RemoteFollowReader(
        ref, max_lines=max_lines, backend=backend
    )


def _drain_until(reader, wanted: int, *, tries: int = 100) -> list[str]:
    """Poll until *wanted* lines have arrived, or give up.

    ``tail`` is a real process here, so its output arrives when the kernel says
    so rather than when the test would like. Polling in a loop is what a
    ``set_interval`` timer does anyway.
    """

    collected: list[str] = []
    for _ in range(tries):
        collected.extend(reader.poll().lines)
        if len(collected) >= wanted:
            break
        time.sleep(0.05)
    return collected


@needs_shell
def test_a_remote_log_tails(tmp_path: Path) -> None:
    """The feature 4b exists for: the pane keeps up with a growing remote log."""

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    _connection, _backend, reader = _follow(tmp_path, log)
    try:
        assert reader.prime().lines == ["line 0"]

        with log.open("a", encoding="utf-8") as handle:
            handle.write("line 1\nline 2\n")
            handle.flush()

        assert _drain_until(reader, 2) == ["line 1", "line 2"]
    finally:
        reader.close()


@needs_shell
def test_following_issues_no_command_at_all(tmp_path: Path) -> None:
    """The Requirement 3 regression test, now against a live tailer.

    Five remote logs polled at 2 Hz on a 60 ms link would be 600 ms of frozen UI
    per second. The guarantee is not "fast" — it is *no command*, however many
    lines arrive.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    connection, _backend, reader = _follow(tmp_path, log)
    try:
        reader.prime()
        before = len(connection.commands)

        with log.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"line {index}\n" for index in range(1, 40)))
            handle.flush()
        _drain_until(reader, 39)
        for _ in range(10):
            reader.poll()

        assert len(connection.commands) == before, (
            "poll() reached the network while following"
        )
    finally:
        reader.close()


@needs_shell
def test_the_follow_starts_where_the_bounded_read_stopped(tmp_path: Path) -> None:
    """No line delivered twice, none skipped.

    The reason the follow is ``tail -c +<offset+1>`` rather than ``tail -n``:
    the prime already read to a known byte, and the tailer resumes from exactly
    it. An off-by-one here duplicates the last line of history or loses the
    first new one, and both look like a parser bug from the outside.
    """

    log = tmp_path / "syslog"
    log.write_text("".join(f"old {index}\n" for index in range(50)), encoding="utf-8")
    _connection, _backend, reader = _follow(tmp_path, log, max_lines=5)
    try:
        primed = reader.prime()
        assert primed.lines == [f"old {index}" for index in range(45, 50)]

        with log.open("a", encoding="utf-8") as handle:
            handle.write("new 0\nnew 1\n")
            handle.flush()

        assert _drain_until(reader, 2) == ["new 0", "new 1"]
    finally:
        reader.close()


@needs_shell
def test_a_partial_line_is_held_until_the_rest_arrives(tmp_path: Path) -> None:
    """Half a log line is not a log line.

    Emitting it would put a truncated line in the pane and then contradict it a
    tick later — and the parser would have already assigned it a timestamp and a
    level from whatever fragment it saw.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    _connection, _backend, reader = _follow(tmp_path, log)
    try:
        reader.prime()

        with log.open("a", encoding="utf-8") as handle:
            handle.write("incomp")
            handle.flush()
        for _ in range(6):
            assert reader.poll().lines == []
            time.sleep(0.05)

        with log.open("a", encoding="utf-8") as handle:
            handle.write("lete\n")
            handle.flush()

        assert _drain_until(reader, 1) == ["incomplete"]
    finally:
        reader.close()


@needs_shell
def test_the_stat_cache_follows_the_log_so_poll_stays_cheap(tmp_path: Path) -> None:
    """What makes 4a's cache-serving ``stat`` useful rather than merely cheap.

    Under the poll guard ``stat`` never goes to the wire. Without the tailer
    feeding it, the cached size would be whatever the last worker refresh saw
    and a growing log would look static to anything that asks.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    _connection, backend, reader = _follow(tmp_path, log)
    try:
        reader.prime()
        ref = RemoteRef.build(HOST.name, str(log))
        with cheap_only():
            before = backend.stat(ref).size

        with log.open("a", encoding="utf-8") as handle:
            handle.write("line 1\n")
            handle.flush()
        _drain_until(reader, 1)

        with cheap_only():
            after = backend.stat(ref)
        assert after.size > before
    finally:
        reader.close()


@needs_shell
def test_a_remote_rotated_set_tails_its_live_head(tmp_path: Path) -> None:
    """The assertion whose absence let a whole feature ship silent.

    ``RotatedSetReader`` used to open its live head with ``open_reader``
    directly, which always yields a ``SourceReader`` — and a ``SourceReader``
    asks the backend "did this grow?" on every poll. That is free locally and a
    round trip remotely, so the poll guard answers it from a cache that only a
    *follow* reader refreshes. A remote rotated set therefore showed its history
    and then never updated again, for ever.

    Rotated sets are the ordinary shape of ``/var/log``, so this was most of the
    remote feature. It went unnoticed because every rotated-set test was local
    and every remote test was unrotated — the gap was exactly in the corner
    neither covered.
    """

    from clv.services.rotation import RotatedSetReader, group_rotated

    (tmp_path / "app.log").write_text("live 1\n", encoding="utf-8")
    (tmp_path / "app.log.1").write_text("older\n", encoding="utf-8")

    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )

    def factory(path, **kwargs):
        kwargs.pop("backend", None)
        if isinstance(path, RemoteRef):
            return RemoteFollowReader(path, backend=backend, **kwargs)
        return open_reader(path, **kwargs)

    sets, _singles = group_rotated(
        [
            RemoteRef.build(HOST.name, str(tmp_path / "app.log")),
            RemoteRef.build(HOST.name, str(tmp_path / "app.log.1")),
        ]
    )
    reader = RotatedSetReader(
        sets[0], max_lines=50, backend=backend, reader_factory=factory
    )
    try:
        assert reader.prime().lines == ["older", "live 1"]
        assert isinstance(reader._live, RemoteFollowReader), (
            "the live head is not a follow reader; it cannot tail"
        )

        with (tmp_path / "app.log").open("a", encoding="utf-8") as handle:
            handle.write("live 2\n")
            handle.flush()

        arrived: list[str] = []
        for _ in range(60):
            with cheap_only():  # exactly what SourceBuffer.poll does
                arrived += reader.poll().lines
            if arrived:
                break
            time.sleep(0.05)

        assert arrived == ["live 2"]
    finally:
        reader.close()


def test_a_local_rotated_set_still_opens_the_way_it_always_did(tmp_path: Path) -> None:
    """The other half: the injected factory must not move local behaviour.

    ``RotatedSetReader``'s default factory *is* ``open_reader``, so a local set
    gets exactly the reader it got before.
    """

    from clv.services.reader import SourceReader
    from clv.services.rotation import RotatedSetReader, group_rotated

    (tmp_path / "app.log").write_text("live 1\n", encoding="utf-8")
    (tmp_path / "app.log.1").write_text("older\n", encoding="utf-8")

    sets, _singles = group_rotated(
        [tmp_path / "app.log", tmp_path / "app.log.1"]
    )
    reader = RotatedSetReader(sets[0], max_lines=50)

    assert isinstance(reader._live, SourceReader)
    assert reader.prime().lines == ["older", "live 1"]


@needs_shell
def test_closing_leaves_no_tail_running(tmp_path: Path) -> None:
    """A leaked ``tail -F`` per source switch must not be arrangeable.

    The session calls ``close()`` on every switch and again at shutdown, so this
    pins the half that is this class's to keep.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    _connection, backend, reader = _follow(tmp_path, log)
    reader.prime()
    process = reader._process
    assert process is not None and process.poll() is None

    reader.close()

    process.wait(timeout=5)
    assert process.poll() is not None
    reader.close()  # idempotent


@needs_shell
def test_a_banner_never_reaches_the_pane_as_log_lines(tmp_path: Path) -> None:
    """The follow's version of the MOTD problem, and the worse version.

    A one-shot command's banner corrupts a *file list*. A follow's banner is
    parsed and displayed as **log lines** — phantom entries carrying whatever
    timestamp and level the parser read out of a welcome message. The opening
    sentinel is what separates them, found incrementally because a follow never
    completes and so can never be unframed whole.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")

    run, real_spawn = shell_transport()

    def noisy_spawn(argv, **kwargs):
        # A login shell that greets you before running anything of ours.
        script = _script_of(argv)
        banner = "Welcome to web01!\nUnauthorised access is prohibited.\n"
        noisy = list(argv[:-1]) + [f"printf '%s' {shlex.quote(banner)}; {script}"]
        return real_spawn(noisy, **kwargs)

    connection = SSHConnection(
        HOST, runner=run, spawn=noisy_spawn, socket_dir=str(tmp_path)
    )
    backend = RemoteBackend(connection)
    reader = RemoteFollowReader(
        RemoteRef.build(HOST.name, str(log)), max_lines=100, backend=backend
    )
    try:
        reader.prime()
        with log.open("a", encoding="utf-8") as handle:
            handle.write("line 1\n")
            handle.flush()

        assert _drain_until(reader, 1) == ["line 1"]
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("tail: '/var/log/syslog' has appeared;  following new file", True),
        ("tail: /var/log/syslog: file truncated", True),
        ("tail: '/var/log/syslog' has become inaccessible", True),
        ("tail: file has been replaced", True),
        ("tail: something nobody has ever printed", False),
        ("", False),
    ],
)
def test_a_rotation_diagnostic_is_recognised_where_it_can_be(
    diagnostic: str, expected: bool
) -> None:
    """Best effort, and benign when it misses.

    There is no portable machine-readable signal for "the file I was following
    was replaced", so this matches what the three implementations actually
    print. An unrecognised spelling costs the **redraw notice** and nothing
    else — ``tail -F`` has already reopened and the lines keep flowing, so no
    line is ever lost to this.
    """

    class _Process:
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(diagnostic.encode())

        def poll(self):
            return None

    reader = RemoteFollowReader(
        RemoteRef.build("web01", "/var/log/syslog"),
        max_lines=10,
        backend=object(),  # never reached: nothing here touches the backend
    )
    reader._process = _Process()
    reader._check_stderr()

    assert reader._rotated is expected


def test_the_follow_command_resumes_at_the_primed_offset() -> None:
    """Asserted as a string, so a change to it is visible in review.

    ``-F`` rather than ``-f`` is the load-bearing letter: it reopens the name
    when the file is replaced, which is what makes a remote ``logrotate``
    survivable instead of a pane that silently stops updating.
    """

    reader = RemoteFollowReader(
        RemoteRef.build("web01", "/var/log/sys log"),
        max_lines=10,
        backend=object(),
    )
    reader._offset = 4096

    command = reader.command()

    assert command.startswith("tail -F -c +4097 '/var/log/sys log' &")
    assert " -f " not in command
    # The watcher that stops the remote tail when the connection goes. Without
    # it an idle log leaves a `tail` running on the operator's server forever,
    # because a closed pipe is only noticed on the next write.
    assert "cat >/dev/null" in command
    assert "kill $__clv_t" in command
    # In the *foreground*. POSIX reassigns a backgrounded command's stdin to
    # /dev/null, so `{ cat ...; } &` would read EOF immediately and kill the
    # follow before it produced a line.
    assert "} & cat >/dev/null" in command


# ==========================================================================
# No leaked processes
# ==========================================================================


@needs_shell
def test_abandoning_a_walk_does_not_leave_a_find_running(tmp_path: Path) -> None:
    """``max_files`` abandons a walk part way through, every time it fires.

    A ``find`` left enumerating someone else's filesystem is both rude and a
    connection held open, so the generator's teardown kills it.
    """

    root = tmp_path / "tree"
    root.mkdir()
    for index in range(50):
        (root / f"f{index:03d}.log").write_text("x\n", encoding="utf-8")

    spawned: list[subprocess.Popen] = []
    run, real_spawn = shell_transport()

    def spawn(argv, **kwargs):
        process = real_spawn(argv, **kwargs)
        spawned.append(process)
        return process

    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )

    walker = backend.walk(RemoteRef.build(HOST.name, str(root)))
    next(walker)
    walker.close()

    assert spawned
    for process in spawned:
        process.wait(timeout=5)
        assert process.poll() is not None


# ==========================================================================
# The protocol itself
# ==========================================================================


def test_the_remote_backend_implements_every_protocol_method() -> None:
    """Declared costs, derived from the marks, refusing anything unmarked."""

    declared = blocking_methods(RemoteBackend)

    assert declared == frozenset(PROTOCOL_METHODS) - {
        "stat",
        "identity",
        "reachability",
    }
    for name in PROTOCOL_METHODS:
        assert callable(getattr(RemoteBackend, name, None)), name


def test_the_transport_module_registers_no_plugin() -> None:
    """It is a **backend**, not a source provider, and says so.

    A ``LogSourceProvider`` hands back a ``ProviderSource``, which is
    deliberately not a path and which starring, glob filtering and rotated-set
    grouping all skip by design. A remote log has to be none of that. Without
    an explicit decline the loader falls through to ``__all__`` and reports
    ``RemoteBackend`` as "does not implement a CLV plugin interface" — a message
    about the wrong problem.
    """

    from clv.plugins import load_plugins

    assert ssh.register() == []
    registry = load_plugins(include_entry_points=False)
    assert not registry.errors


# ==========================================================================
# The app, wired
# ==========================================================================


def test_the_resolver_is_the_local_backend_when_the_switch_is_off() -> None:
    """Requirement 13, in one line.

    Not "a resolver that happens to route everything locally" — ``LOCAL``
    itself, the same object the local path has always used. There is nothing to
    connect and nothing to spawn because there is nothing.
    """

    from clv.app import build_backends, remote_roots
    from clv.services.config import LogConfig

    config = LogConfig(enable_ssh=False, hosts=(HOST,))

    assert build_backends(config) is LOCAL
    assert remote_roots(config) == []


def test_the_resolver_appears_only_when_a_host_is_enabled() -> None:
    from clv.app import build_backends, remote_roots
    from clv.services.config import LogConfig

    disabled = RemoteHost(name="db02", host="10.0.0.12", enabled=False)
    config = LogConfig(
        enable_ssh=True,
        hosts=(
            RemoteHost(name="web01", host="web01.internal", log_dirs=("/var/log",)),
            disabled,
        ),
    )

    assert isinstance(build_backends(config), RemoteResolver)
    assert [str(root) for root in remote_roots(config)] == ["ssh:web01/var/log"]


def test_remote_log_dirs_are_never_resolved_against_this_machine(
    tmp_path: Path, monkeypatch
) -> None:
    """The corruption the ref boundary exists to prevent.

    ``RemoteHost.log_dirs`` is a tuple of strings because those are paths on
    *another* machine; running one through ``normalize_ref`` would pin it to
    this one's working directory.
    """

    from clv.app import remote_roots
    from clv.services.config import LogConfig

    monkeypatch.chdir(tmp_path)
    config = LogConfig(
        enable_ssh=True,
        hosts=(RemoteHost(name="web01", host="w", log_dirs=("/var/log",)),),
    )

    root = remote_roots(config)[0]

    assert str(root) == "ssh:web01/var/log"
    assert str(tmp_path) not in str(root)


# ==========================================================================
# Parity: a remote source is a source
# ==========================================================================


def test_the_ref_union_admits_both_implementations_and_no_provider_source() -> None:
    """What excludes a journal unit from starring is the type, and still is.

    ``isinstance(data, Path)`` meant "is this a source?" for exactly as long as
    ``Path`` was the only implementation. The union is what kept that guarantee
    while the set of ref types grew — and widening it to "anything
    source-shaped" is what would let a ``ProviderSource`` into someone's
    ``session.json`` as a path that does not exist.
    """

    from clv.plugins import ProviderSource
    from clv.services.refs import is_source_ref
    from clv.storage import SavedView

    assert is_source_ref(Path("/var/log/syslog"))
    assert is_source_ref(RemoteRef.build("web01", "/var/log/syslog"))

    assert not is_source_ref(ProviderSource(Path("journal:all"), "System journal"))
    assert not is_source_ref(SavedView(name="n"))
    assert not is_source_ref(None)


def test_a_remote_ref_groups_into_a_rotated_set() -> None:
    """``group_rotated`` is pure name arithmetic and should need nothing.

    Confirmed rather than assumed — "should be free" is how a gap ships. The one
    thing that did need widening is ``RotatedSet.__contains__``, which is what
    the membership assertion below is really testing.
    """

    from clv.services.rotation import group_rotated

    members = [
        RemoteRef.build("web01", "/var/log/app.log"),
        RemoteRef.build("web01", "/var/log/app.log.1"),
        RemoteRef.build("web01", "/var/log/app.log.2.gz"),
    ]

    sets, singles = group_rotated(members)

    assert singles == []
    assert len(sets) == 1
    assert sets[0].head == members[0]
    assert [ref.name for ref in sets[0].paths] == [
        "app.log", "app.log.1", "app.log.2.gz"
    ]
    for member in members:
        assert member in sets[0], "RotatedSet.__contains__ still narrows to Path"


def test_two_hosts_with_the_same_path_group_separately() -> None:
    """A rotated set is a log on *one* machine, not a name across a fleet."""

    from clv.services.rotation import group_rotated

    sets, _singles = group_rotated(
        [
            RemoteRef.build("web01", "/var/log/app.log"),
            RemoteRef.build("web01", "/var/log/app.log.1"),
            RemoteRef.build("web02", "/var/log/app.log"),
            RemoteRef.build("web02", "/var/log/app.log.1"),
        ]
    )

    assert len(sets) == 2
    assert {rotated.head.node for rotated in sets} == {"web01", "web02"}


def test_a_remote_ref_survives_the_starred_set_round_trip(tmp_path: Path) -> None:
    """Starring persists a ref string and rebuilds it on the next launch.

    The regression Phase 1 wrote its guard for, now against a starred *remote*
    source rather than a stub.
    """

    from clv.services.refs import parse_ref, ref_key
    from clv.storage import SessionState, StateStore

    ref = RemoteRef.build("web01", "/var/log/syslog")
    store = StateStore(tmp_path / "session.json")
    store.save(SessionState(starred=(ref_key(ref),)))

    restored = store.load()

    assert restored.starred == ("ssh:web01/var/log/syslog",)
    assert parse_ref(restored.starred[0]) == ref


# ==========================================================================
# The time rule
# ==========================================================================


def _merged_session(*members):
    """A session of pre-filled buffers. No readers, no IO — ordering only.

    ``(facts, [(timestamp, message)])`` per member. Building the buffers by hand
    is what keeps these tests about the *merge* rather than about parsing.
    """

    from clv.services.parsing import LogEntry
    from clv.services.session import SourceBuffer, SourceSession

    session = SourceSession(max_lines=100)
    buffers = []
    for index, (facts, rows) in enumerate(members):
        buffer = SourceBuffer(
            Path(f"/log/{index}"), max_lines=100, facts=facts, tag_origin=True
        )
        for stamp, message in rows:
            buffer.entries.append(
                LogEntry(raw=message, message=message, timestamp=stamp)
            )
        buffer.revision += 1
        buffers.append(buffer)
    session.install_many(buffers)
    return session


def test_two_hosts_in_different_zones_interleave_correctly() -> None:
    """**The regression this whole item exists for.**

    Syslog carries no offset at all. A naive ``10:00:00`` on a UTC host and a
    naive ``06:00:00`` on a UTC-4 host are the *same instant*, and the old rule
    — drop every offset the moment any stamp is naive — orders them four hours
    apart. Nothing on screen suggests anything is amiss, and the operator reads
    causation out of the interleaving.

    Without the fix this assertion fails by exactly the zone difference.
    """

    from clv.services.session import SourceFacts

    utc = timezone.utc
    east = timezone(timedelta(hours=-4))

    session = _merged_session(
        (
            SourceFacts(node="utc-host", zone=utc),
            [
                (datetime(2026, 8, 17, 10, 0, 0), "utc first"),
                (datetime(2026, 8, 17, 10, 0, 2), "utc third"),
            ],
        ),
        (
            SourceFacts(node="east-host", zone=east),
            [
                (datetime(2026, 8, 17, 6, 0, 1), "east second"),
                (datetime(2026, 8, 17, 6, 0, 3), "east fourth"),
            ],
        ),
    )

    assert [entry.message for entry in session.entries] == [
        "utc first",
        "east second",
        "utc third",
        "east fourth",
    ]


def test_an_all_local_merge_orders_exactly_as_it_did_before() -> None:
    """Requirement 13, at the one place the time rule could have moved it.

    Every local buffer shares one zone, so the new branch is never entered and
    the old rule runs untouched. Pinned rather than reasoned about, because
    "single-zone sets are unaffected" is the entire safety argument.
    """

    from clv.services.session import local_facts

    facts = local_facts(None)
    session = _merged_session(
        (facts, [
            (datetime(2026, 8, 17, 10, 0, 0), "a1"),
            (datetime(2026, 8, 17, 10, 0, 2), "a2"),
        ]),
        (facts, [
            (datetime(2026, 8, 17, 10, 0, 1), "b1"),
            (datetime(2026, 8, 17, 10, 0, 3), "b2"),
        ]),
    )

    assert [entry.message for entry in session.entries] == ["a1", "b1", "a2", "b2"]
    assert session._spans_zones() is False


def test_hosts_in_the_same_zone_take_the_unchanged_path() -> None:
    """A fleet in one datacentre is the common case and must cost nothing new."""

    from clv.services.session import SourceFacts

    zone = timezone(timedelta(hours=1))
    session = _merged_session(
        (SourceFacts(node="web01", zone=zone), [(datetime(2026, 8, 17, 10, 0), "a")]),
        (SourceFacts(node="web02", zone=zone), [(datetime(2026, 8, 17, 10, 1), "b")]),
    )

    assert session._spans_zones() is False
    assert [entry.message for entry in session.entries] == ["a", "b"]


def test_a_single_source_never_takes_the_cross_zone_path() -> None:
    """One machine has one clock; there is nothing to reconcile."""

    from clv.services.session import SourceFacts

    session = _merged_session(
        (
            SourceFacts(node="web01", zone=timezone.utc),
            [(datetime(2026, 8, 17, 10, 0), "only")],
        ),
    )

    assert session._spans_zones() is False


def test_skew_is_reported_but_not_applied_by_default() -> None:
    """Honest by default, correct when asked.

    A clock four seconds fast is a fact to surface, not one to quietly paper
    over — the operator may want to fix the clock rather than have CLV hide it.
    """

    from clv.services.session import SourceFacts

    session = _merged_session(
        (
            SourceFacts(node="web01", zone=timezone.utc, skew=timedelta(seconds=4)),
            [(datetime(2026, 8, 17, 10, 0), "a")],
        ),
        (
            SourceFacts(node="db02", zone=timezone.utc),
            [(datetime(2026, 8, 17, 10, 1), "b")],
        ),
    )

    assert session.skew_spread() == timedelta(seconds=4)
    assert session.corrected is False
    assert session._spans_zones() is False, "reporting skew must not reorder anything"


def test_skew_is_applied_when_the_host_asked_for_it() -> None:
    """And when it is, the ordering follows and the pane can say so.

    ``web01`` runs four seconds fast, so its ``10:00:04`` is really ``10:00:00``
    and belongs *before* ``db02``'s ``10:00:02``.
    """

    from clv.services.session import SourceFacts

    session = _merged_session(
        (
            SourceFacts(
                node="web01",
                zone=timezone.utc,
                skew=timedelta(seconds=4),
                correct_skew=True,
            ),
            [(datetime(2026, 8, 17, 10, 0, 4), "web01 line")],
        ),
        (
            SourceFacts(node="db02", zone=timezone.utc),
            [(datetime(2026, 8, 17, 10, 0, 2), "db02 line")],
        ),
    )

    assert session.corrected is True
    assert [entry.message for entry in session.entries] == [
        "web01 line",
        "db02 line",
    ]


def test_the_raw_timestamp_is_never_rewritten() -> None:
    """The correction lives in the sort key and nowhere else.

    A displayed stamp that differs from the raw log text with no explanation
    would be a worse failure than the misordering this fixes.
    """

    from clv.services.session import SourceFacts

    written = datetime(2026, 8, 17, 10, 0, 4)
    session = _merged_session(
        (
            SourceFacts(
                node="web01",
                zone=timezone.utc,
                skew=timedelta(seconds=4),
                correct_skew=True,
            ),
            [(written, "web01 line")],
        ),
        (
            SourceFacts(node="db02", zone=timezone(timedelta(hours=2))),
            [(datetime(2026, 8, 17, 12, 0, 2), "db02 line")],
        ),
    )

    entry = next(e for e in session.entries if e.message == "web01 line")
    assert entry.timestamp == written
    assert entry.timestamp.tzinfo is None


# ==========================================================================
# The node field
# ==========================================================================


def test_node_is_carried_and_host_is_untouched() -> None:
    """``node`` is where CLV read it; ``host`` is what the log says about itself.

    Both present, both meaning their own thing. That separation is what lets
    ``node:web01`` be added without changing what a single saved query already
    matched.
    """

    from clv.services.session import NODE_FIELD, SourceBuffer, SourceFacts
    from clv.services.reader import TailRead

    buffer = SourceBuffer(
        RemoteRef.build("web01", "/var/log/syslog"),
        max_lines=10,
        facts=SourceFacts(node="web01"),
    )
    entries = buffer._feed(
        TailRead(lines=["Aug 17 10:00:00 db-primary sshd[1]: hello"], offset=0)
    )

    assert entries[0].fields[NODE_FIELD] == "web01"
    assert entries[0].fields["host"] == "db-primary", "host must keep its meaning"


def test_a_local_source_gains_no_node_key() -> None:
    """Requirement 13 at the per-line level: local entries are untouched."""

    from clv.services.session import NODE_FIELD, SourceBuffer
    from clv.services.reader import TailRead

    buffer = SourceBuffer(Path("/var/log/syslog"), max_lines=10)
    entries = buffer._feed(
        TailRead(lines=["Aug 17 10:00:00 db-primary sshd[1]: hello"], offset=0)
    )

    assert NODE_FIELD not in entries[0].fields


def test_node_is_a_query_term_and_host_still_means_what_it_meant() -> None:
    """``node:web01 status>=500`` is the most obvious query in the feature.

    Without ``node`` in the vocabulary it would land in ``hidden_missing_field``
    or be searched for as plain text.
    """

    from clv.services.query import NORMALISED_FIELD_KEYS, parse_query

    assert "node" in NORMALISED_FIELD_KEYS
    assert "host" in NORMALISED_FIELD_KEYS

    # The vocabulary the app hands the parser: the normalised keys, plus
    # whatever the open buffer actually carries.
    parsed = parse_query("node:web01 status>=500", NORMALISED_FIELD_KEYS)

    assert {term.key for term in parsed.terms} == {"node", "status"}
    assert parsed.text.strip() == ""

    # And the query that must not have changed meaning: `host` is still a term,
    # still about what the log says about itself.
    host_query = parse_query("host:db-primary", NORMALISED_FIELD_KEYS)
    assert {term.key for term in host_query.terms} == {"host"}


def test_per_host_budgets_are_applied_and_named(tmp_path: Path) -> None:
    """One noisy host must not consume the whole allowance silently.

    Globally, the first host walked eats ``max_files`` and the rest come back
    empty with nothing saying why. Per-host budgets plus a per-root truncation
    line is what makes that legible.
    """

    from clv.services.config import LogConfig
    from clv.services.discovery import DiscoveryReport

    host = RemoteHost(name="web01", host="w", log_dirs=("/var/log",), max_files=2)
    config = LogConfig(enable_ssh=True, hosts=(host,))

    resolved = host.discovery_settings(DiscoverySettings(max_files=5000))

    assert resolved.max_files == 2
    assert config.host("web01") is host

    report = DiscoveryReport(truncated=True, truncated_roots=[RemoteRef.build("web01", "/var/log")])
    assert "Reached its file limit: ssh:web01/var/log" in report.summary_lines()


# ==========================================================================
# Reachability — the phase that exists for a pane that went quiet
# ==========================================================================
#
# The hazard is a deliberate existing behaviour: `SourceBuffer.poll` swallows
# `OSError` because a source that vanished mid-session is not worth taking the
# pane down for. Exactly right for a rotated local file, exactly wrong for a
# dropped SSH connection — where it renders as a log that simply stopped.


@pytest.mark.parametrize(
    ("stderr", "status", "fragment", "forbidden"),
    [
        (
            "/bin/sh: find: not found",
            127,
            "missing a utility",
            "ssh-agent",
        ),
        (
            "tail: /var/log/secure: Permission denied",
            1,
            "Add the user to a group",
            "ssh-agent",
        ),
        (
            "stat: /var/log/nope: No such file or directory",
            1,
            "not there on that host",
            "ssh-agent",
        ),
    ],
)
def test_a_failing_remote_command_is_not_reported_as_an_auth_failure(
    stderr: str, status: int, fragment: str, forbidden: str
) -> None:
    """The second hint table, and why there has to be one.

    `Permission denied` means two completely different things depending on which
    side said it. From ``ssh`` it means the key was refused and the answer is
    ssh-agent; from the remote shell it means the SSH user cannot read a file and
    the answer is a group or an ACL. One table would report the second as the
    first and send the operator to their agent for an hour over a file mode.
    """

    connection = SSHConnection(
        HOST, runner=fixture_runner(stderr=stderr, returncode=status), socket_dir="/tmp"
    )

    with pytest.raises(SSHError) as caught:
        connection.run("find /var/log")

    message = str(caught.value)
    assert fragment in message
    assert "web01" in message
    assert forbidden not in message
    # The command failed; the *host* answered. Reporting this as unreachable
    # would start a reconnect against a connection that is working perfectly.
    assert connection.reachability.ok


def test_a_transport_failure_is_recorded_as_unreachable_with_a_backoff() -> None:
    """A frame that never closed is the link, and the link is reachability."""

    def refused(argv, timeout):
        return b"", "connect to host web01 port 22: Connection refused", 255

    connection = SSHConnection(HOST, runner=refused, socket_dir="/tmp")
    assert connection.reachability.ok

    with pytest.raises(SSHError):
        connection.run("echo hi")

    state = connection.reachability
    assert state.state == "unreachable"
    assert "nothing is listening" in state.reason
    assert state.attempts == 1
    assert not state.exhausted
    # A deadline in the future, so nothing retries on the next tick.
    assert not state.may_attempt(time.monotonic())


def test_the_backoff_is_bounded_rather_than_one_attempt_per_tick() -> None:
    """N failures produce N *bounded* attempts, and then it stops.

    The requirement this pins is negative: a reconnect driven from the poll tick
    would be `refresh_hz` connection attempts a second at a host that is down —
    the fork bomb with a nice name that `journald` names in its own poll. Each
    failure has to buy a longer wait, and the schedule has to end.
    """

    def refused(argv, timeout):
        return b"", "connect to host web01 port 22: Connection refused", 255

    connection = SSHConnection(HOST, runner=refused, socket_dir="/tmp")

    waits = []
    for expected in range(1, RECONNECT_ATTEMPTS + 1):
        before = time.monotonic()
        with pytest.raises(SSHError):
            connection.run("echo hi")
        state = connection.reachability
        assert state.attempts == expected
        waits.append(round(state.retry_at - before))

    assert waits == [backoff_for(n) for n in range(1, RECONNECT_ATTEMPTS + 1)]
    assert waits == sorted(waits), "each failure must buy a longer wait"
    assert connection.reachability.exhausted
    # Spent: nothing further is attempted automatically, at any time.
    assert not connection.reachability.may_attempt(time.monotonic() + 10_000)


def test_a_working_command_clears_a_previous_failure() -> None:
    """A host that came back is not still down. Recovery needs no gesture."""

    outcomes = iter(
        [
            (b"", "connect to host web01 port 22: Connection refused", 255),
            None,
        ]
    )
    canned = fixture_runner("ok")

    def flaky(argv, timeout):
        nxt = next(outcomes, None)
        return canned(argv, timeout) if nxt is None else nxt

    connection = SSHConnection(HOST, runner=flaky, socket_dir="/tmp")
    with pytest.raises(SSHError):
        connection.run("echo hi")
    assert not connection.reachability.ok

    assert connection.run("echo hi").strip() == "ok"
    assert connection.reachability.ok
    assert connection.reachability.attempts == 0


def test_a_reload_forgets_the_backoff_because_asking_is_a_reason_to_try() -> None:
    """`Ctrl+R` means "try now", and waiting out an interrupted backoff is rude."""

    def refused(argv, timeout):
        return b"", "connect to host web01 port 22: Connection refused", 255

    connection = SSHConnection(HOST, runner=refused, socket_dir="/tmp")
    with pytest.raises(SSHError):
        connection.run("echo hi")
    assert not connection.reachability.may_attempt(time.monotonic())

    connection.reset()

    assert connection.reachability.ok
    assert connection.reachability.may_attempt(time.monotonic())


def test_a_disabled_host_is_a_third_state_and_never_attempted() -> None:
    """Off is not down. Nothing is spawned for a host the operator switched off."""

    connection = SSHConnection(
        RemoteHost(name="db02", host="db02.internal", enabled=False),
        runner=fixture_runner(),
        socket_dir="/tmp",
    )

    state = connection.reachability
    assert state.state == "disabled"
    assert not state.may_attempt(time.monotonic())
    # And a reload does not quietly turn it back on.
    connection.reset()
    assert connection.reachability.state == "disabled"


def test_reachability_is_cheap_and_answers_under_the_poll_guard() -> None:
    """Requirement 3, extended to the new member.

    The status line reads this on every render and the log pane on every empty
    result. A version that probed would be the frozen UI in a new place, so this
    asserts the strong form: not "fast", but *no command at all*.
    """

    runner = fixture_runner()
    connection = SSHConnection(HOST, runner=runner, socket_dir="/tmp")
    backend = RemoteBackend(connection)

    with cheap_only():
        assert backend.reachability().ok
        assert LOCAL.reachability().ok

    assert runner.calls == []


@needs_shell
def test_a_walk_that_drops_mid_stream_is_a_short_tree_not_a_quiet_one(
    tmp_path: Path,
) -> None:
    """The named phase-5 hazard, as a test.

    A remote walk yields nothing when its link goes, exactly as a local one does
    when a directory will not list — so from inside the walk they are the same
    event. What must differ is what the *report* says: a host that went away
    cannot be allowed to shrink the tree in silence.
    """

    root = tmp_path / "logs"
    root.mkdir()
    (root / "syslog").write_text("hello\n", encoding="utf-8")

    run, spawn = shell_transport()

    def truncating_spawn(argv, **kwargs):
        # A `find` whose output stops before the closing sentinel: a connection
        # that died part way through enumerating the tree.
        kwargs.pop("env", None)
        kwargs.pop("text", None)
        script = _script_of(argv)
        marker = script.split(None, 2)[2].split(";")[0].strip()
        return subprocess.Popen(
            ["/bin/sh", "-c", f"printf '%s\\n' {marker}; printf 'partial'"],
            **kwargs,
        )

    connection = SSHConnection(
        HOST, runner=run, spawn=truncating_spawn, socket_dir=str(tmp_path)
    )
    backend = RemoteBackend(connection)
    ref = RemoteRef.build(HOST.name, str(root))

    # The walk still yields nothing and still does not raise -- `LocalBackend`
    # behaves identically and the protocol says so.
    assert list(backend.walk(ref)) == []
    # What is new: the backend now knows, so the caller can report it.
    assert not backend.reachability().ok
    assert "web01" in backend.reachability().reason


@needs_shell
def test_a_complete_walk_leaves_the_host_reachable(tmp_path: Path) -> None:
    """The other direction, so the check above cannot pass by always failing."""

    root = tmp_path / "logs"
    root.mkdir()
    (root / "syslog").write_text("hello\n", encoding="utf-8")

    run, spawn = shell_transport()
    backend = RemoteBackend(
        SSHConnection(HOST, runner=run, spawn=spawn, socket_dir=str(tmp_path))
    )
    ref = RemoteRef.build(HOST.name, str(root))

    assert [entry.ref.name for entry in backend.walk(ref)] == ["syslog"]
    assert backend.reachability().ok


@needs_shell
def test_a_mid_session_drop_produces_a_notice_rather_than_silence(
    tmp_path: Path,
) -> None:
    """**The regression this phase exists for.**

    Before Phase 5 the branch below closed the reader and returned an ordinary
    empty read — indistinguishable from a log with nothing new to say. A pane
    that goes quiet because the link dropped is Requirement 7's single worst
    outcome, and it looked exactly like a pane that was up to date.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    connection, backend, reader = _follow(tmp_path, log)
    try:
        assert reader.prime().lines == ["line 0"]
        # An ordinary quiet poll says nothing, which is what must stay true.
        assert reader.poll().problem == ""

        # The link goes. Killing the local process is what a dropped connection
        # looks like from this side.
        reader._process.kill()
        reader._process.wait(timeout=5)

        for _ in range(100):
            result = reader.poll()
            if result.problem:
                break
            time.sleep(0.05)

        assert result.problem, "a dropped follow must say so"
        assert "web01" in result.problem
        # And the backend agrees, so the status line has something to show for
        # as long as it lasts rather than only at the instant it happened.
        assert not backend.reachability().ok
    finally:
        reader.close()


@needs_shell
def test_a_stoppage_is_reported_once_not_on_every_tick(tmp_path: Path) -> None:
    """A toast twice a second is a worse failure than the silence it replaces."""

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    _connection, _backend, reader = _follow(tmp_path, log)
    try:
        reader.prime()
        reader._process.kill()
        reader._process.wait(timeout=5)

        problems = []
        for _ in range(20):
            problems.append(reader.poll().problem)
            time.sleep(0.02)

        assert len([problem for problem in problems if problem]) == 1
    finally:
        reader.close()


@needs_shell
def test_a_recovered_source_resumes_without_losing_its_buffer(
    tmp_path: Path,
) -> None:
    """Reconnecting must not cost the operator what was already on screen.

    This is why reconnection is `resume()` and not a re-prime: `SourceBuffer.
    prime` clears the buffer before refilling it, so going back through it would
    throw away everything visible in order to re-fetch most of it over the link
    that has only just come back. Resuming at the stored offset delivers no line
    twice and skips none.
    """

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")
    _connection, _backend, reader = _follow(tmp_path, log)
    buffer = SourceBuffer(reader.path, max_lines=100, reader=reader)
    try:
        buffer.prime()
        assert [entry.message for entry in buffer.entries] == ["line 0"]
        offset_before = reader.offset

        reader._process.kill()
        reader._process.wait(timeout=5)
        for _ in range(100):
            if buffer.poll().problem:
                break
            time.sleep(0.05)

        # Lines written while the link was down are not lost either: the resumed
        # follow starts at the byte the last one stopped at.
        with log.open("a", encoding="utf-8") as handle:
            handle.write("line 1\n")
            handle.flush()

        reader.resume()
        assert reader.offset == offset_before

        for _ in range(100):
            buffer.poll()
            if len(buffer.entries) >= 2:
                break
            time.sleep(0.05)

        assert [entry.message for entry in buffer.entries] == ["line 0", "line 1"]
    finally:
        reader.close()


@needs_shell
def test_resuming_delivers_no_line_twice(tmp_path: Path) -> None:
    """Requirement 2, across a reconnection: not one line more, not one less."""

    log = tmp_path / "syslog"
    log.write_text("".join(f"line {n}\n" for n in range(5)), encoding="utf-8")
    _connection, _backend, reader = _follow(tmp_path, log)
    try:
        assert len(reader.prime().lines) == 5
        reader._process.kill()
        reader._process.wait(timeout=5)
        for _ in range(100):
            if reader.poll().problem:
                break
            time.sleep(0.05)

        reader.resume()
        with log.open("a", encoding="utf-8") as handle:
            handle.write("line 5\n")
            handle.flush()

        assert _drain_until(reader, 1) == ["line 5"]
    finally:
        reader.close()


def test_a_follow_that_never_started_leaves_the_host_marked_unreachable() -> None:
    """4a kept the reason in a list. Phase 5 is where it becomes visible.

    A bounded read that worked followed by a follow that did not is a *readable*
    source that is not live, and the pane must stop claiming otherwise.
    """

    connection = SSHConnection(HOST, runner=fixture_runner(), socket_dir="/tmp")
    backend = RemoteBackend(connection)
    ref = RemoteRef.build(HOST.name, "/var/log/syslog")

    backend.note_follow_failure(ref, OSError("web01: could not start ssh"))

    assert backend.follow_errors == [(ref, "web01: could not start ssh")]
    assert not backend.reachability().ok
    assert "web01" in backend.reachability().reason


# --- discovery reports it, so a down host does not shrink the tree ---------


@needs_shell
def test_an_unreachable_host_is_named_rather_than_missing(tmp_path: Path) -> None:
    """Requirement 7: a host that is down must read differently from one with
    no matching files. Both produce an empty branch; only one is a problem."""

    def refused(argv, timeout):
        return b"", "ssh: Could not resolve hostname web01", 255

    connection = SSHConnection(HOST, runner=refused, socket_dir=str(tmp_path))
    backend = RemoteBackend(connection)

    class OneHost:
        def for_ref(self, ref):
            return backend

    root = RemoteRef.build(HOST.name, "/var/log")
    report = discover([root], DiscoverySettings(), backends=OneHost())

    assert report.unreadable_roots == [root]
    assert "does not resolve" in report.root_reasons[root]
    summary = "\n".join(report.summary_lines())
    assert "Could not read source: ssh:web01/var/log" in summary
    assert "does not resolve" in summary


def test_a_local_root_that_is_simply_absent_gains_no_invented_reason(
    tmp_path: Path,
) -> None:
    """The other side of the same rule: a missing local path is not a network
    problem and must not be dressed up as one."""

    missing = tmp_path / "nope"
    report = discover([missing], DiscoverySettings())

    assert report.unreadable_roots == [missing]
    assert report.root_reasons == {}
    assert "Could not read source: " in "\n".join(report.summary_lines())


# --- the resolver keeps what still works ----------------------------------


def test_a_reload_keeps_a_connection_whose_host_did_not_change(
    tmp_path: Path,
) -> None:
    """`Ctrl+R` used to close every master and pay a full handshake per host.

    Over five connections that is most of the wait, and all of it spent on hosts
    whose settings had not moved.
    """

    resolver = RemoteResolver(
        [HOST], local=LOCAL, runner=fixture_runner(), socket_dir=str(tmp_path)
    )
    backend = resolver.for_ref(RemoteRef.build("web01", "/var/log"))

    kept, closed = resolver.reconcile([HOST])

    assert (kept, closed) == (["web01"], [])
    assert resolver.for_ref(RemoteRef.build("web01", "/var/log")) is backend


def test_a_reload_drops_a_connection_whose_host_was_edited(tmp_path: Path) -> None:
    """A connection belongs to the configuration that named it."""

    resolver = RemoteResolver(
        [HOST], local=LOCAL, runner=fixture_runner(), socket_dir=str(tmp_path)
    )
    backend = resolver.for_ref(RemoteRef.build("web01", "/var/log"))
    moved = RemoteHost(name="web01", host="web01.dr.internal", user="ops", port=22)

    kept, closed = resolver.reconcile([moved])

    assert (kept, closed) == ([], ["web01"])
    assert resolver.for_ref(RemoteRef.build("web01", "/var/log")) is not backend


def test_a_reload_drops_a_connection_the_config_no_longer_names(
    tmp_path: Path,
) -> None:
    """A host that was removed must not keep a live authenticated socket open."""

    resolver = RemoteResolver(
        [HOST], local=LOCAL, runner=fixture_runner(), socket_dir=str(tmp_path)
    )
    resolver.for_ref(RemoteRef.build("web01", "/var/log"))

    kept, closed = resolver.reconcile([])

    assert (kept, closed) == ([], ["web01"])
    assert resolver.backends == {}


def test_a_reload_resets_the_backoff_on_a_host_that_was_down(
    tmp_path: Path,
) -> None:
    """The gesture that means impatience must not be answered with a wait."""

    def refused(argv, timeout):
        return b"", "connect to host web01 port 22: Connection refused", 255

    resolver = RemoteResolver(
        [HOST], local=LOCAL, runner=refused, socket_dir=str(tmp_path)
    )
    backend = resolver.for_ref(RemoteRef.build("web01", "/var/log"))
    with pytest.raises(SSHError):
        backend.connection.run("echo hi")
    assert not backend.reachability().ok

    resolver.reconcile([HOST])

    assert backend.reachability().ok


# --- the flag that makes a down host a fact rather than a wait -------------


def test_every_argv_bounds_the_connect_it_is_waiting_on() -> None:
    """Without this a blackholed host costs the whole command timeout.

    Most of a minute of a pane that looks broken, when the answer was knowable in
    ten seconds. It weakens no verification — it only bounds how long CLV
    believes in a silence.
    """

    connection = SSHConnection(HOST, runner=fixture_runner(), socket_dir="/tmp")
    argv = connection.base_argv()

    assert f"ConnectTimeout={ssh.CONNECT_TIMEOUT}" in argv
    assert ssh.CONNECT_TIMEOUT < ssh.COMMAND_TIMEOUT
    # And the security posture is unchanged by the addition.
    joined = " ".join(argv)
    assert "StrictHostKeyChecking" not in joined
    assert "UserKnownHostsFile" not in joined


# ==========================================================================
# The app: a host being down costs the local tree nothing
# ==========================================================================


def _down_runner(delay: float = 0.0):
    """A runner for a host that is not answering, optionally slowly.

    The delay is what makes the ordering assertions mean something: a host that
    fails instantly cannot demonstrate that the local tree did not wait for it.
    """

    def refused(argv, timeout):
        if delay:
            time.sleep(delay)
        return b"", "connect to host web01 port 22: Connection refused", 255

    return refused


def _settings_with_a_host(root: Path) -> str:
    return (
        "[log_viewer]\n"
        f"log_dirs = {root}\n"
        "enable_ssh = true\n"
        "\n[ssh:web01]\n"
        "host = web01.internal\n"
        "user = ops\n"
        "log_dirs = /var/log\n"
    )


def _app_with_a_down_host(tmp_path: Path, *, delay: float = 0.0):
    """A configured app whose one host refuses, wired to fakes rather than ssh."""

    from clv.app import LogViewerApp
    from clv.services.config import load_config, user_config_path

    root = tmp_path / "local"
    root.mkdir()
    (root / "app.log").write_text("2026-08-11 10:00:00 - INFO - local\n", encoding="utf-8")

    settings = user_config_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(_settings_with_a_host(root), encoding="utf-8")

    config = load_config()
    app = LogViewerApp(config=config)
    app._backends = RemoteResolver(
        config.hosts,
        local=LOCAL,
        runner=_down_runner(delay),
        socket_dir=str(tmp_path / "sockets"),
    )
    return app, root


def test_a_host_that_is_down_never_delays_the_local_tree(tmp_path: Path) -> None:
    """The local tree is built and shown before a host has even answered.

    One pass used to mean every local log waited behind every configured
    machine's connect timeout — three unreachable hosts and the operator watched
    an empty panel for a minute for files that were on their own disk.

    Asserted by *holding the host open* rather than by timing anything: the
    remote walk is blocked on an event this test controls, so "the local tree
    arrived first" is a fact about ordering rather than a race against a clock.
    """

    import asyncio
    import threading

    released = threading.Event()
    released.set()

    def held(argv, timeout):
        released.wait(timeout=10)
        return b"", "connect to host web01 port 22: Connection refused", 255

    from clv.app import LogViewerApp
    from clv.services.config import load_config, user_config_path

    root = tmp_path / "local"
    root.mkdir()
    (root / "app.log").write_text(
        "2026-08-11 10:00:00 - INFO - local\n", encoding="utf-8"
    )
    settings = user_config_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(_settings_with_a_host(root), encoding="utf-8")

    config = load_config()
    app = LogViewerApp(config=config)
    app._backends = RemoteResolver(
        config.hosts, local=LOCAL, runner=held, socket_dir=str(tmp_path / "sockets")
    )

    installs: list[int] = []
    original = app._install_report

    async def recording(report):
        installs.append(report.file_count)
        await original(report)

    app._install_report = recording  # type: ignore[method-assign]

    async def scenario() -> None:
        async with app.run_test(size=(150, 40)):
            from clv.app import remote_roots
            from clv.services import SourceManager

            app._source_manager = SourceManager(
                [root, *remote_roots(app._config)], []
            )
            # The mount already ran a rescan, which left the host inside its
            # backoff. Clear it so this pass really does contact the host --
            # a skipped host would prove nothing about waiting for one.
            for backend in app._backends.backends.values():
                backend.connection.reset()

            released.clear()
            installs.clear()
            scan = asyncio.ensure_future(app._rescan())
            try:
                for _ in range(200):
                    if installs:
                        break
                    await asyncio.sleep(0.02)

                assert installs == [1], (
                    "the local tree must be built and shown while the host is "
                    "still being waited on"
                )
            finally:
                released.set()
                await scan

            # And the host's results are folded in afterwards, in a second pass.
            assert len(installs) == 2

    asyncio.run(scenario())


def test_an_unreachable_host_leaves_the_local_sources_fully_usable(
    tmp_path: Path,
) -> None:
    """Requirement 7 and the startup rule together.

    The host is named as a problem, the local log is still there to open, and
    neither fact contaminates the other.
    """

    import asyncio

    app, root = _app_with_a_down_host(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(150, 40)) as pilot:
            from clv.app import remote_roots
            from clv.services import SourceManager

            app._source_manager = SourceManager(
                [root, *remote_roots(app._config)], []
            )
            await app._rescan()
            await pilot.pause()

            report = app._report
            assert report is not None
            # The local file is present and openable.
            assert [item.path.name for item in report.files] == ["app.log"]
            assert app._select_source(root / "app.log", announce=False) is True
            assert [e.message for e in app._session.entries] == ["local"]

            # And the host is *named*, not silently absent.
            summary = "\n".join(report.summary_lines())
            assert "ssh:web01/var/log" in summary
            assert "nothing is listening" in summary

    asyncio.run(scenario())


def test_a_reload_with_a_host_down_completes_and_refreshes_the_rest(
    tmp_path: Path,
) -> None:
    """`Ctrl+R` must not block on a machine that is not there.

    The second walk is *skipped* rather than retried, because the backoff from
    the first has not expired — so the reload costs a local walk and nothing
    else, and still says what it could not refresh.
    """

    import asyncio

    app, root = _app_with_a_down_host(tmp_path, delay=0.4)

    async def scenario() -> None:
        async with app.run_test(size=(150, 40)) as pilot:
            from clv.app import remote_roots
            from clv.services import SourceManager

            app._source_manager = SourceManager(
                [root, *remote_roots(app._config)], []
            )
            await app._rescan()
            await pilot.pause()
            assert not app._backends.for_ref(
                RemoteRef.build("web01", "/var/log")
            ).reachability().ok

            # The reload resets the backoff — asking is a reason to try — so
            # this one *does* attempt the host, once, and no more.
            connection = app._backends.backends["web01"].connection
            before = len(connection.commands)
            started = time.monotonic()
            await app.action_reload_sources()
            await pilot.pause()
            elapsed = time.monotonic() - started

            # One attempt's worth of timeout, not a retry storm.
            assert elapsed < 4 * 0.4, f"the reload took {elapsed:.2f}s"
            assert len(connection.commands) - before <= 3
            # The local sources came back regardless.
            assert app._report is not None
            assert [item.path.name for item in app._report.files] == ["app.log"]

    asyncio.run(scenario())


def test_a_skipped_host_costs_no_command_at_all(tmp_path: Path) -> None:
    """A host inside its backoff is reported from what is known, not re-probed.

    Spending a connect timeout to learn again what CLV was told a second ago is
    the thing that makes a rescan with a dead host feel broken.
    """

    import asyncio

    app, root = _app_with_a_down_host(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(150, 40)) as pilot:
            from clv.app import remote_roots
            from clv.services import SourceManager

            app._source_manager = SourceManager(
                [root, *remote_roots(app._config)], []
            )
            await app._rescan()
            await pilot.pause()

            connection = app._backends.backends["web01"].connection
            # Deepen the backoff so the window under test is seconds rather
            # than the first step's one second, which a rescan can outlast.
            for _ in range(3):
                connection.note_failure("web01: still down.")
            assert not connection.reachability.may_attempt(time.monotonic())
            before = len(connection.commands)

            await app._rescan()
            await pilot.pause()

            assert len(connection.commands) == before, (
                "a host inside its backoff must not be probed again"
            )
            # And it is still reported rather than quietly dropped.
            summary = "\n".join(app._report.summary_lines())
            assert "ssh:web01/var/log" in summary
            assert "still down" in summary

    asyncio.run(scenario())


@needs_shell
def test_the_pane_says_a_source_stopped_rather_than_showing_nothing(
    tmp_path: Path,
) -> None:
    """The three channels, end to end, and none of them is a log row.

    `_notify` stopped writing into the pane because copy mode copied what it
    wrote, so a fabricated "connection lost" line is not an option — it would be
    a line the source never produced, in the one place CLV promises not to
    invent one. What the operator gets instead is a toast now, the empty-pane
    explanation while the pane is empty, and a status-line segment throughout.
    """

    import asyncio

    from clv.app import LogViewerApp

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()

            _connection, backend, reader = _follow(tmp_path, log)
            ref = reader.path
            buffer = SourceBuffer(ref, max_lines=100, reader=reader)
            buffer.prime()
            app._session.install(buffer)
            app._backends = RemoteResolver(
                [HOST], local=LOCAL, runner=fixture_runner(), socket_dir=str(tmp_path)
            )
            app._backends._backends["web01"] = backend

            assert app._unreachable_detail() == ""

            reader._process.kill()
            reader._process.wait(timeout=5)
            for _ in range(100):
                app._poll_tail()
                if app._source_problems:
                    break
                await asyncio.sleep(0.05)

            detail = app._unreachable_detail()
            assert detail, "the pane must say the source stopped"
            assert "web01" in detail
            # A bounded attempt was queued rather than one per poll tick.
            assert len(app._reconnects) == 1
            assert not app._reconnects[ref_key(ref)].exhausted
            reader.close()

    asyncio.run(scenario())


@needs_shell
def test_the_reconnect_gives_up_and_says_so_rather_than_trying_forever(
    tmp_path: Path,
) -> None:
    """Bounded, and the boundary is visible.

    A host that has been gone this long is not coming back because CLV asked
    again, and an open-ended retry is a stream of ``ssh`` processes at a machine
    the operator may have taken down deliberately. What is *not* acceptable is
    stopping quietly: the pane has to say it has stopped, and say what resumes
    it, or the operator is back to a log that went silent for no stated reason.
    """

    import asyncio

    from clv.app import LogViewerApp

    log = tmp_path / "syslog"
    log.write_text("line 0\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()

            _connection, backend, reader = _follow(tmp_path, log)
            key = ref_key(reader.path)
            buffer = SourceBuffer(reader.path, max_lines=100, reader=reader)
            buffer.prime()
            app._session.install(buffer)
            reader.close()

            # A resume that always fails, so the schedule runs to its end
            # without a real host being involved.
            def always_fails() -> None:
                raise SSHError("web01: the host did not answer.")

            reader.resume = always_fails  # type: ignore[method-assign]

            app._schedule_reconnect(buffer, "web01: the host did not answer.")
            for _ in range(RECONNECT_ATTEMPTS + 1):
                record = app._reconnects.get(key)
                if record is None or record.exhausted:
                    break
                if record.timer is not None:
                    record.timer.stop()
                    record.timer = None
                await app._finish_reconnect(key, buffer)

            record = app._reconnects[key]
            assert record.exhausted
            assert record.attempts == RECONNECT_ATTEMPTS
            # And it is on screen, naming the way back.
            assert "Ctrl+R" in app._source_problems[key]
            assert "Ctrl+R" in app._unreachable_detail()

    asyncio.run(scenario())


def test_a_local_reader_is_never_scheduled_for_reconnection(tmp_path: Path) -> None:
    """There is nothing to reconnect *to*.

    A local file is either there on the next poll or it is not, so the absence
    of ``resume`` is the answer rather than a ref-type check at the call site —
    and a timer against a local log would be pure waste.
    """

    import asyncio

    from clv.app import LogViewerApp

    path = tmp_path / "app.log"
    path.write_text("2026-08-11 10:00:00 - INFO - hi\n", encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            buffer = app._session.open_single(path)

            app._schedule_reconnect(buffer, "something went wrong")

            assert app._reconnects == {}

    asyncio.run(scenario())
