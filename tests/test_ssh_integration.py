"""The opt-in suite that touches a real host. Skipped by default, always.

``tests/test_ssh_source.py`` proves the transport against fake runners and the
local shell, which is where every command is asserted as a string and where
Requirement 14 — *the suite never touches a network* — is kept. What that cannot
do is catch the thing Requirement 5 is about: **a fake runner will happily
return whatever fixture it was given**, so a BusyBox ``find`` with no
``-printf`` and a BSD ``stat`` with different format letters both pass a
fixture and fail in the field.

This file is the answer, and it is deliberately not part of any phase gate.

Running it
----------

``tests/containers/run.sh`` does the whole thing — builds a throwaway image,
generates a keypair, pins the host key, runs this file, tears it all down::

    tests/containers/run.sh alpine     # BusyBox — the profile that matters
    tests/containers/run.sh gnu        # GNU coreutils — the control

Run **both**. Alpine is the one with something to prove; the GNU run is what
makes an Alpine failure readable as a portability gap rather than a CLV bug.

``CLV_TEST_SSH_PROFILE`` is set per image and asserts the probe reached that
conclusion, so an Alpine run that silently detected ``gnu`` is a failure rather
than a pass that proved nothing.

To point it at a host of your own instead, set ``CLV_TEST_SSH_HOST`` and
friends by hand (``_HOST``, ``_PORT``, ``_USER``, ``_DIR``, ``_IDENTITY``,
``_KNOWN_HOSTS``, ``_PROFILE``). The host key must be trusted and the key usable
without a passphrase prompt — that is not a limitation of these tests, it is the
product's auth model, and a run that needed a password should fail.

**On ``known_hosts``.** OpenSSH does not honour ``$HOME`` — it reads the file
from the passwd entry — so the throwaway ``HOME`` ``conftest`` provides cannot
redirect it, and CLV will never pass ``UserKnownHostsFile`` because a test
asserts it never appears in an argv. ``run.sh`` therefore trusts the container's
key in the real file and removes it again, which is what an operator does by
hand; the entry is keyed to an ephemeral port and the file is backed up first.
That is the *only* thing this suite touches outside a scratch directory.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from clv.plugins.sources.ssh import (
    RemoteBackend,
    SSHConnection,
    quote_all,
)
from clv.services.backend import ClassifyRequest
from clv.services.config import RemoteHost
from clv.services.discovery import DiscoverySettings, discover
from clv.services.reader import SourceReader
from clv.services.refs import RemoteRef

pytestmark = pytest.mark.remote_integration


def _host() -> RemoteHost:
    name = os.environ.get("CLV_TEST_SSH_HOST")
    if not name:
        pytest.skip("set CLV_TEST_SSH_HOST to run the remote integration suite")
    identity = os.environ.get("CLV_TEST_SSH_IDENTITY")
    return RemoteHost(
        name="itest",
        host=name,
        user=os.environ.get("CLV_TEST_SSH_USER") or None,
        port=int(os.environ.get("CLV_TEST_SSH_PORT", "22")),
        identity_file=Path(identity) if identity else None,
    )


@pytest.fixture
def connection(tmp_path):
    conn = SSHConnection(_host(), socket_dir=str(tmp_path))
    yield conn
    conn.close()


@pytest.fixture
def backend(connection) -> RemoteBackend:
    return RemoteBackend(connection)


@pytest.fixture
def scratch(backend, connection):
    """A throwaway directory on the remote, removed afterwards.

    Created over the same transport under test, which is itself a small
    end-to-end check: if quoting or framing were broken, nothing below would
    even set up.
    """

    base = os.environ.get("CLV_TEST_SSH_DIR", "/tmp")
    path = f"{base}/clv-itest-{uuid.uuid4().hex[:12]}"
    connection.run(f"mkdir -p {quote_all(path)}")
    try:
        yield RemoteRef.build("itest", path)
    finally:
        connection.run(f"rm -rf {quote_all(path)}")


# --------------------------------------------------------------------------
# The probe: the whole reason this file exists
# --------------------------------------------------------------------------


def test_the_probe_reaches_the_profile_the_image_actually_has(connection) -> None:
    """Requirement 5, against a real shell rather than a fixture.

    Set ``CLV_TEST_SSH_PROFILE`` and this becomes an assertion instead of an
    observation — which is what makes an Alpine run meaningful.
    """

    facts = connection.facts()

    expected = os.environ.get("CLV_TEST_SSH_PROFILE")
    if expected:
        assert facts.profile.name == expected, (
            f"probed {facts.profile.name!r} on an image declared {expected!r}"
        )
    assert facts.uname


def test_the_clock_is_measured_and_plausible(connection) -> None:
    """A midpoint measurement, so latency does not read as skew."""

    facts = connection.facts()

    assert abs(facts.skew.total_seconds()) < 86_400, (
        "a day of skew is a broken measurement, not a broken clock"
    )
    assert -86_400 < facts.utc_offset.total_seconds() < 86_400


# --------------------------------------------------------------------------
# Reading, against whatever utilities the image really has
# --------------------------------------------------------------------------


def test_a_walk_finds_what_was_written(backend, connection, scratch) -> None:
    connection.run(
        f"mkdir -p {quote_all(str(scratch.path) + '/nested')}; "
        f"printf 'alpha\\n' > {quote_all(str(scratch.path) + '/a.log')}; "
        f"printf 'bravo\\nbravo\\n' > {quote_all(str(scratch.path) + '/nested/b.log')}"
    )

    entries = {entry.ref.name: entry for entry in backend.walk(scratch)}

    assert sorted(entries) == ["a.log", "b.log"]
    assert entries["a.log"].size == 6


def test_a_hostile_filename_survives_a_real_remote(backend, connection, scratch) -> None:
    """The injection table, but with the far side being a real login shell."""

    hostile = "a b'c$(touch pwned);d"
    target = f"{scratch.path}/{hostile}"
    connection.run(f"printf 'alpha\\n' > {quote_all(target)}")

    names = [entry.ref.name for entry in backend.walk(scratch)]

    assert names == [hostile]
    probe = connection.run(
        f"if [ -e {quote_all(str(scratch.path) + '/pwned')} ]; then echo yes; fi"
    )
    assert probe.strip() != "yes"


def test_a_bounded_tail_reads_the_end(backend, connection, scratch) -> None:
    """Requirement 2 over the network: a bounded tail, never a ``cat``."""

    target = f"{scratch.path}/big.log"
    connection.run(
        f"i=0; while [ $i -lt 2000 ]; do echo \"line $i\"; i=$((i+1)); done "
        f"> {quote_all(target)}"
    )
    ref = RemoteRef.build("itest", target)

    result = SourceReader(ref, max_lines=10, backend=backend).prime()

    assert result.lines[-1] == "line 1999"
    assert len(result.lines) == 10
    assert result.truncated is True


def test_rotation_is_detected_by_inode_where_the_image_has_one(
    backend, connection, scratch
) -> None:
    """The reason the shell transport was chosen over SFTP.

    ``stat`` returns an inode in one round trip, so a log rotated within the
    same second to the same size is still seen as a new file. An image whose
    ``stat`` CLV cannot read declares ``stable_identity`` False and is skipped
    here rather than asserted against.
    """

    if not backend.capabilities.stable_identity:
        pytest.skip("this image has no stat CLV can read; degradation is by design")

    target = f"{scratch.path}/rotating.log"
    connection.run(f"printf 'aaaaa\\n' > {quote_all(target)}")
    ref = RemoteRef.build("itest", target)

    first = backend.identity(ref)
    connection.run(f"rm -f {quote_all(target)}; printf 'bbbbb\\n' > {quote_all(target)}")
    second = backend.identity(ref)

    assert first is not None and second is not None
    assert first != second, "a same-size rotation went unnoticed"


def test_classify_returns_bytes_and_the_verdict_is_made_here(
    backend, connection, scratch
) -> None:
    """The UTF-16 case, which a remote NUL test would get wrong."""

    text = f"{scratch.path}/text.log"
    binary = f"{scratch.path}/binary.log"
    connection.run(f"printf 'alpha\\n' > {quote_all(text)}")
    connection.run(f"printf 'a\\000b' > {quote_all(binary)}")

    results = backend.classify(
        [
            ClassifyRequest(ref=RemoteRef.build("itest", text), head_bytes=8192),
            ClassifyRequest(ref=RemoteRef.build("itest", binary), head_bytes=8192),
        ]
    )

    assert results[RemoteRef.build("itest", text)].head == b"alpha\n"
    assert b"\x00" in results[RemoteRef.build("itest", binary)].head


def test_discovery_costs_the_same_whatever_the_tree_size(
    backend, connection, scratch
) -> None:
    """Requirement 4, against a shell that is really enumerating files.

    Asserted as **invariance** rather than as a magic number, because a fixed
    ceiling is a number someone eventually just raises. A tree four times the
    size must cost the *same* commands; the day a per-file round trip returns,
    the second count is four times the first and this fails by a mile.

    The absolute ceiling below is a second, weaker guard: one ``kind``, one
    ``access``, one ``find``, one ``classify`` batch. The one-time capability
    probe is excluded by warming it first — it happens once per connection ever,
    not once per root.
    """

    class _Resolver:
        def for_ref(self, ref):
            return backend

    connection.facts()  # the probe is per connection, not per discovery

    def cost(directory: str, count: int) -> int:
        target = f"{scratch.path}/{directory}"
        connection.run(
            f"mkdir -p {quote_all(target)}; i=0; "
            f"while [ $i -lt {count} ]; do printf 'x\\n' > "
            f"{quote_all(target)}/f$i.log; i=$((i+1)); done"
        )
        root = RemoteRef.build("itest", target)
        before = len(connection.commands)
        report = discover([root], DiscoverySettings(), backends=_Resolver())
        assert report.file_count == count
        return len(connection.commands) - before

    small = cost("small", 30)
    large = cost("large", 120)

    assert small == large, (
        f"30 files cost {small} commands and 120 cost {large}; "
        "that is a per-file round trip"
    )
    assert large <= 4, f"discovery of one root cost {large} commands"


# --------------------------------------------------------------------------
# The whole app, against a real host
# --------------------------------------------------------------------------


def _settings(host: RemoteHost, root: str) -> str:
    identity = f"identity_file = {host.identity_file}\n" if host.identity_file else ""
    return (
        "[log_viewer]\n"
        "enable_ssh = true\n"
        "refresh_hz = 10\n"
        f"\n[ssh:{host.name}]\n"
        f"host = {host.host}\n"
        f"port = {host.port}\n"
        + (f"user = {host.user}\n" if host.user else "")
        + identity
        + f"log_dirs = {root}\n"
    )


def test_the_app_discovers_opens_and_tails_a_remote_log(connection, scratch) -> None:
    """The smoke test, driven headlessly instead of by hand.

    Everything from ``settings.conf`` to a tailing pane: the config parses into
    a ``RemoteHost``, ``build_backends`` produces a resolver, discovery walks the
    remote root in a worker, the tree gains a node for the log, selecting it
    opens through ``_begin_remote_open``, and appending to the file on the remote
    shows up in the pane.

    **And the event loop is never blocked.** Requirement 3's whole point is that
    none of the above stalls the UI, so the poll that drives the pane is timed:
    a round trip would show up here as tens of milliseconds and does not.
    """

    import asyncio
    import time as _time

    from clv.app import LogViewerApp
    from clv.services.config import load_config, user_config_path

    target = f"{scratch.path}/app.log"
    connection.run(
        f"printf 'line 0\\nline 1\\n' > {quote_all(target)}"
    )

    settings = user_config_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(_settings(_host(), str(scratch.path)), encoding="utf-8")

    async def scenario() -> None:
        app = LogViewerApp(config=load_config())
        async with app.run_test() as pilot:
            await pilot.pause()

            discovered = {ref.name for ref in app._file_refs}
            assert "app.log" in discovered, f"discovery found {discovered}"

            ref = next(r for r in app._file_refs if r.name == "app.log")
            assert app._select_source(ref, announce=False) is True
            for _ in range(60):
                await pilot.pause()
                if app._session.primary is not None:
                    break
            assert [e.message for e in app._session.entries][-2:] == [
                "line 0", "line 1"
            ]

            connection.run(f"printf 'line 2\\n' >> {quote_all(target)}")

            # The poll the tail timer runs, timed. This is the assertion
            # Requirement 3 exists for.
            slowest = 0.0
            for _ in range(80):
                started = _time.perf_counter()
                app._poll_tail()
                slowest = max(slowest, _time.perf_counter() - started)
                if "line 2" in [e.message for e in app._session.entries]:
                    break
                await asyncio.sleep(0.05)

            assert "line 2" in [e.message for e in app._session.entries], (
                "the remote log did not tail"
            )
            assert slowest < 0.05, (
                f"a poll took {slowest * 1000:.0f} ms — that is a round trip on "
                "the event loop"
            )

    asyncio.run(scenario())


def test_the_app_leaves_no_ssh_process_behind(connection, scratch) -> None:
    """No leaked ``tail -F``, and no control socket, after the app closes.

    A persisted multiplex socket is a live authenticated connection any local
    process running as this user can ride, so "it expires in a minute" is not
    the answer — and a follow left running is a connection held open for it.
    """

    import asyncio
    import subprocess as _sp

    from clv.app import LogViewerApp
    from clv.services.config import load_config, user_config_path

    target = f"{scratch.path}/app.log"
    connection.run(f"printf 'line 0\\n' > {quote_all(target)}")

    settings = user_config_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(_settings(_host(), str(scratch.path)), encoding="utf-8")

    sockets: list[str] = []

    async def scenario() -> None:
        app = LogViewerApp(config=load_config())
        async with app.run_test() as pilot:
            await pilot.pause()
            ref = next(r for r in app._file_refs if r.name == "app.log")
            app._select_source(ref, announce=False)
            for _ in range(60):
                await pilot.pause()
                if app._session.primary is not None:
                    break
            for backend in app._backends.backends.values():
                sockets.append(backend.connection.socket)

    asyncio.run(scenario())

    assert sockets, "the app never opened a connection"
    for socket_path in sockets:
        assert not os.path.exists(socket_path), (
            f"a multiplex socket outlived the app: {socket_path}"
        )

    # `-x tail` matches the *process*, not any command line that happens to
    # mention it — this test's own invocation contains the pattern.
    running = _sp.run(
        ["pgrep", "-x", "tail", "-f", f"tail -F -c .* {scratch.path}"],
        capture_output=True,
        check=False,
    )
    if running.returncode == 0:
        pids = running.stdout.decode().split()
        detail = []
        for pid in pids:
            try:
                detail.append(
                    Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                )
            except OSError:
                detail.append(f"{pid} (gone)")
        raise AssertionError("a follow outlived the app:\n  " + "\n  ".join(detail))


# --------------------------------------------------------------------------
# The socket
# --------------------------------------------------------------------------


def test_the_multiplex_socket_is_private_and_is_removed(tmp_path) -> None:
    """A leaked socket is a rideable authenticated connection."""

    conn = SSHConnection(_host(), socket_dir=str(tmp_path))
    conn.run("echo hi")

    assert os.path.exists(conn.socket)
    assert (os.stat(conn.socket).st_mode & 0o077) == 0

    conn.close()
    assert not os.path.exists(conn.socket)
