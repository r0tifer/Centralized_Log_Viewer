"""Log folders on another machine, read over the operator's own SSH.

A viewer called *Centralized* Log Viewer that can only read the machine it runs
on has centralised nothing. This module is the transport that fixes that: a
:class:`RemoteBackend` implementing :mod:`clv.services.backend`'s protocol over
a multiplexed ``ssh`` connection, so a remote folder is discovered, listed,
opened and filtered by exactly the code that does it locally.

**Nothing here is a ``ProviderSource``.** A remote root reaches ``SourceManager``
as an ordinary root and builds the same nested folder tree a local one does.
That is the whole reason this work is a backend plus a ref type rather than a
provider plugin: a ``ProviderSource`` is deliberately *not* a path, and starring,
glob filtering and rotated-set grouping all skip one by design. A remote log
that cannot be starred has not met the goal.

What lives here is the part that spawns a process. :class:`RemoteRef` — what a
remote source *is* — lives in ``clv/services/refs.py``, because ``parse_ref``
decodes ``session.json`` before any plugin is imported.

Why the shell transport
-----------------------

An SFTP client returns ``SFTPAttributes``, which carries **no inode**.
``SourceReader`` detects rotation by comparing an opaque identity that is
``(st_dev, st_ino)`` locally; without an inode that degrades to a
``(size, mtime)`` heuristic which misfires on a log rotated within the same
second. ``stat -c '%d %i %s %Y'`` over a shell returns device, inode, size and
mtime in **one** round trip, so rotation detection keeps full fidelity — the
dependency-heavy option is the one that would have given that up.

It also costs no new runtime dependency, and inherits ``~/.ssh/config``
wholesale: ``ProxyJump``, per-host keys, ``known_hosts``, agent forwarding.

The round-trip budget
---------------------

Round trips are a bounded resource, and a per-file one is what makes an
``sshfs`` mount slow at 400 files. So:

* Discovery of a root is **one** command — a single ``find``, streamed.
* The text/binary sniff, which is 8 KB *per candidate* locally, is one command
  per batch of :data:`~clv.services.discovery.CLASSIFY_BATCH` through the
  backend's ``classify`` member. The verdict is not computed remotely: the
  bytes come back and ``reader.looks_binary_block`` decides, because a NUL test
  written in ``sh`` would reject every UTF-16 export CLV goes out of its way to
  read.
* ``stat`` answers from a cache when it is called from the event loop, and only
  goes to the wire from a worker. See the next section.
* A bounded tail read is one ranged fetch, not one per 64 KB chunk.

``poll()`` must never perform a round trip
------------------------------------------

``SourceBuffer.poll`` runs on a ``set_interval`` timer at ``refresh_hz`` — twice
a second, per merged source, on the event loop. ``stat`` and ``identity`` are
``GUARANTEED_CHEAP`` on **every** backend, and a network round trip is not
cheap, so five remote logs on a 60 ms link would be 600 ms of frozen UI per
second.

The resolution is that :meth:`RemoteBackend.stat` serves its cache when it is
running under ``backend.cheap_only()`` — which is precisely the guard ``poll``
enters — and refreshes off the wire when it is not. A worker thread is
unaffected because the guard is thread-local. That is not a trick: it is the
honest reading of a contract that says *this call must be cheap here*, and it
is what lets the backend contract suite see a real answer while the event loop
sees a free one.

Authentication, and why ``BatchMode`` is load-bearing
-----------------------------------------------------

Agent and key files only. There is no password field in the config schema, the
dialog, ``SessionState`` or memory, and ``-o BatchMode=yes`` is what turns that
from a policy into a mechanism: it converts every interactive prompt into a
clean non-zero exit with usable stderr. Without it, the first connection to an
unknown host writes ``Are you sure you want to continue connecting?`` to a stdin
nobody is reading, and **CLV hangs invisibly inside the TUI**.

It is also what lets host-key verification stay on. ``StrictHostKeyChecking=no``
and ``UserKnownHostsFile=/dev/null`` appear nowhere, not behind a flag and not
"for testing"; an unknown host key is an unreachable host with a message saying
to verify it once by hand.

Command profiles: the remote is not assumed to be GNU
-----------------------------------------------------

``find -printf``, ``stat -c`` and ``dd iflag=skip_bytes`` are GNU extensions
that BusyBox and BSD do not have, and Alpine is a first-class target rather than
an edge case. One probe at connect selects a :class:`Profile`, and every
fallback is explicit:

===================  ==============================  ==============================
Capability           Present                         Absent
===================  ==============================  ==============================
``find -printf``     one command, NUL-delimited      ``find … -exec stat … {} +``
``stat`` inode       real rotation detection         ``stable_identity`` False
``dd`` byte offsets  one ranged read                 ``tail -c +N | head -c M``
===================  ==============================  ==============================

Two hazards the shell puts in the way
-------------------------------------

**Shell noise is a data-integrity problem, not cosmetics.** A login shell may
print an MOTD, a legal banner or a ``.bashrc`` echo, and that text lands in
``find`` output as phantom filenames. Every script is framed with a random
sentinel; anything before it is discarded, and a missing closing sentinel means
*truncated*, which is a different fact from *empty*. stderr is captured
separately and never merged.

**Command injection through a configured path is the live risk.** ``ssh`` joins
its argv with spaces and hands the result to the remote login shell, so a script
is built as a single argv element with every operator-supplied byte passed
through :func:`shlex.quote` first. A path containing ``$(reboot)`` is data.
:func:`quote_all` is the only way a path enters a script, and a table-driven
test in ``tests/test_ssh_source.py`` holds it there.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from io import UnsupportedOperation
from pathlib import PurePosixPath
from typing import IO, Any, Iterator, Optional, Sequence

from ...services.backend import (
    BackendCapabilities,
    BackendStat,
    ClassifyRequest,
    ClassifyResult,
    RefKind,
    WalkEntry,
    blocking,
    blocking_methods,
    cheap,
    in_cheap_only,
)
from ...services.config import RemoteHost, load_config
from ...services.reader import (
    DEFAULT_MAX_READ_BYTES,
    UTF8,
    TailRead,
    detect_file_encoding,
    read_last_lines,
)
from ...services.refs import RemoteRef, SourceRef
from .journald import child_environment

__all__ = [
    "CONTROL_PERSIST",
    "PROFILES",
    "Profile",
    "RemoteBackend",
    "RemoteFile",
    "RemoteFollowReader",
    "RemoteResolver",
    "SSHConnection",
    "SSHError",
    "control_socket_dir",
    "enabled",
    "quote_all",
    "ssh_path",
]


# ---------------------------------------------------------------------------
# Consent and the binary
# ---------------------------------------------------------------------------


def ssh_path() -> str:
    """The ``ssh`` to run. Falls back to the bare name so ``PATH`` still applies."""

    return shutil.which("ssh") or "ssh"


def enabled() -> bool:
    """Whether the operator has opted in, read fresh every time.

    Not cached, for the reason ``journald.enabled`` is not: the Advanced drawer
    writes ``enable_ssh`` to the settings file and the next rescan has to see it
    without a restart.

    A *network* subprocess raises the consent bar rather than lowering it, so
    this is checked before anything is spawned, on every entry point, and never
    once at import.
    """

    try:
        return bool(getattr(load_config(), "enable_ssh", False))
    except Exception:  # noqa: BLE001 - a broken config must not break discovery
        return False


class SSHError(OSError):
    """A remote operation failed, with what the remote said about it.

    An ``OSError`` because every caller in ``discovery``, ``reader`` and
    ``session`` is already written to expect one from a read that did not work;
    making this a new exception type would mean a remote failure escaping
    guards a local one is caught by.
    """

    def __init__(self, message: str, *, stderr: str = "", returncode: int = 0) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# Quoting — the security boundary
# ---------------------------------------------------------------------------


def quote_all(*values: object) -> str:
    """POSIX-quote *values* into one space-separated fragment.

    The **only** way an operator-supplied byte enters a script. ``shlex.quote``
    emits a single-quoted string with ``'`` escaped as ``'\\''``, which no shell
    metacharacter survives — so a log directory named ``/var/log/$(reboot)`` is
    a directory name and nothing else.

    Spelled as a function rather than inlined at each call so the test that
    proves it can name one thing, and so a call site that forgot it is visible
    as an f-string interpolation rather than as an absent import.
    """

    return " ".join(shlex.quote(str(value)) for value in values)


def _remote_path(ref: SourceRef) -> str:
    """The path *ref* names on its own machine, unquoted.

    A :class:`RemoteRef` is ``ssh:web01/var/log`` and the remote knows it as
    ``/var/log``; sending the string form would look for a directory called
    ``ssh:web01``.
    """

    if isinstance(ref, RemoteRef):
        return str(ref.path)
    return str(ref)


# ---------------------------------------------------------------------------
# Command profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    """Which argv set a host gets, and what it gives up.

    Built from a probe, never assumed. The point is that a non-GNU remote
    degrades along a path that is written down and tested, rather than failing
    obscurely at the first ``find: unrecognized: -printf``.
    """

    name: str
    #: ``find -printf`` with NUL-terminated records: one command, and a newline
    #: in a filename survives.
    find_printf: bool = False
    #: The ``stat`` format string yielding ``dev inode size mtime``, or ``None``
    #: when this host has no ``stat`` CLV can read — which costs inode-based
    #: rotation detection and nothing else.
    stat_format: Optional[str] = None
    #: ``dd iflag=skip_bytes,count_bytes``. Without it a ranged read is
    #: ``tail -c +N | head -c M``, which is POSIX and works everywhere.
    dd_skip_bytes: bool = False

    @property
    def stable_identity(self) -> bool:
        return self.stat_format is not None


#: The three shapes worth having names for. A host that matches none of them
#: gets :data:`_POSIX`, which uses only what POSIX guarantees.
PROFILES: dict[str, Profile] = {
    "gnu": Profile(
        name="gnu",
        find_printf=True,
        stat_format="%d %i %s %Y",
        dd_skip_bytes=True,
    ),
    "busybox": Profile(
        name="busybox",
        find_printf=False,
        stat_format="%d %i %s %Y",
        dd_skip_bytes=True,
    ),
    "bsd": Profile(
        name="bsd",
        find_printf=False,
        stat_format="%d %i %z %m",
        dd_skip_bytes=False,
    ),
}

_POSIX = Profile(name="posix")


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What one probe learned, beyond which argv set to use.

    :attr:`skew` and :attr:`utc_offset` are captured now and *spent* by Phase 6:
    a merged view across machines with disagreeing clocks must never present a
    confident ordering it cannot justify, and a naive syslog stamp from a UTC
    host and one from an EST host are five hours of silent misordering. Measured
    here because this is the round trip that was already being made.
    """

    profile: Profile = _POSIX
    uname: str = ""
    #: Remote clock minus local clock, latency-corrected.
    skew: timedelta = timedelta(0)
    #: The remote's UTC offset, so a naive stamp from it can be made aware.
    utc_offset: timedelta = timedelta(0)

    @property
    def tzinfo(self) -> timezone:
        return timezone(self.utc_offset)


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------


#: How long a multiplex master outlives its last client.
#:
#: Short on purpose. A persisted socket is a **live authenticated connection**
#: that any local process running as this user can ride, so leaving one behind
#: after CLV exits is a real exposure. The teardown below is explicit and
#: tested; this is the backstop for a CLV that was killed rather than quit.
CONTROL_PERSIST = 60

#: Seconds one bounded command may take before it is a failure rather than a
#: slow link. Generous because a first connection may negotiate a ProxyJump.
COMMAND_TIMEOUT = 45

_SENTINEL_RE = re.compile(rb"^__clv_(?P<token>[0-9a-f]{16})_(?P<kind>start|end)")


def control_socket_dir() -> str:
    """Where multiplex sockets live, created ``0700``.

    ``$XDG_RUNTIME_DIR`` when there is one — it is already per-user and
    ``0700`` — falling back to the temp directory with the mode set explicitly.
    """

    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    directory = os.path.join(base, "clv-ssh")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - a filesystem that will not say
        pass
    return directory


class SSHConnection:
    """One multiplexed connection to one configured host.

    Holds no socket of its own: ``ControlMaster=auto`` means the first command
    opens the master and the rest ride it, so a connection object that has run
    nothing has connected to nothing. That is what makes constructing one during
    config load safe.
    """

    def __init__(
        self,
        host: RemoteHost,
        *,
        spawn=subprocess.Popen,
        runner=None,
        socket_dir: Optional[str] = None,
    ) -> None:
        self.host = host
        #: Injected so every command can be asserted as a string in a test,
        #: exactly as the journald suite injects `runner=` and `spawn=`.
        self._spawn = spawn
        self._runner = runner
        self._socket_dir = socket_dir
        self._socket: Optional[str] = None
        self._facts: Optional[HostFacts] = None
        self._token = hashlib.blake2b(
            f"{host.name}\0{os.getpid()}\0{time.time_ns()}".encode(),
            digest_size=8,
        ).hexdigest()
        #: Commands issued, so a test can assert a round-trip budget rather than
        #: a reviewer having to count call sites.
        self.commands: list[list[str]] = []

    # --- the socket ---------------------------------------------------------

    @property
    def socket(self) -> str:
        """The ``ControlPath``, from a hash rather than from the host name.

        ``sun_path`` is about 104 bytes. A host named after a long FQDN under a
        long ``XDG_RUNTIME_DIR`` overflows it, and the failure mode is *silent*
        loss of multiplexing — every command pays a full handshake and nothing
        says why.
        """

        if self._socket is None:
            digest = hashlib.blake2b(
                "\0".join(
                    (
                        self.host.name,
                        self.host.host,
                        self.host.user or "",
                        str(self.host.port),
                    )
                ).encode(),
                digest_size=8,
            ).hexdigest()
            directory = self._socket_dir or control_socket_dir()
            self._socket = os.path.join(directory, f"m-{digest}")
        return self._socket

    # --- the argv -----------------------------------------------------------

    def base_argv(self) -> list[str]:
        """The flags every invocation carries, and the three that matter.

        ``BatchMode`` is the one that turns "agent and keys only" into a
        mechanism; ``-T`` means nothing tries to render a prompt; and
        ``LogLevel=ERROR`` keeps the client's own chatter out of the framed
        output. What is *absent* is as deliberate: no
        ``StrictHostKeyChecking``, no ``UserKnownHostsFile``, ever.
        """

        argv = [
            ssh_path(),
            "-T",
            "-o", "BatchMode=yes",
            "-o", "LogLevel=ERROR",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self.socket}",
            "-o", f"ControlPersist={CONTROL_PERSIST}",
        ]
        if self.host.port:
            argv += ["-p", str(self.host.port)]
        if self.host.identity_file is not None:
            argv += ["-i", str(self.host.identity_file)]
        if self.host.user:
            argv += ["-l", self.host.user]
        argv.append(self.host.host)
        return argv

    def command(self, script: str) -> list[str]:
        """*script*, framed, as a complete argv.

        The script is **one** argv element. ``ssh`` joins what follows the
        destination with spaces and hands it to the remote login shell, so
        splitting it across elements would let a space inside a quoted path
        become an argument boundary on the other side.
        """

        return [*self.base_argv(), self.frame(script)]

    @property
    def start_marker(self) -> bytes:
        """The opening sentinel, for a caller draining a stream incrementally.

        A one-shot command is unframed whole by :func:`_unframe`. A **follow**
        cannot be: it never completes, so its consumer has to find the marker as
        the bytes arrive and treat everything after it as data. Exposed rather
        than reached for, because the token is per connection.
        """

        return f"__clv_{self._token}_start".encode()

    @property
    def end_marker(self) -> str:
        """The closing sentinel, for a follow that outlives its own ``tail``.

        A one-shot command's frame is consumed by :func:`_unframe`. A follow's
        cannot be — but it still *arrives*, because the follow script does not
        ``exec`` (the shell has to outlive ``tail`` to clean up after it), so
        the trailing ``printf`` runs when the remote command finally exits. The
        drain filters it out rather than showing the operator a sentinel as a
        log line, and takes it as the signal that the far end is done.
        """

        return f"__clv_{self._token}_end"

    def frame(self, script: str) -> str:
        """Wrap *script* so banner text can be told from output.

        A login shell may print an MOTD before anything of ours runs. Without
        the opening sentinel that text is indistinguishable from the first line
        of ``find`` output — which is to say, from a filename. The closing
        sentinel carries ``$?`` and its absence means the output stopped early,
        which is what a dropped link looks like and is *not* the same as a
        command that legitimately produced nothing.
        """

        start = f"__clv_{self._token}_start"
        end = f"__clv_{self._token}_end"
        return f"printf '%s\\n' {start}; {script}\nprintf '%s %s\\n' {end} $?"

    # --- running ------------------------------------------------------------

    def run(self, script: str, *, timeout: int = COMMAND_TIMEOUT) -> str:
        """Run one bounded command and return its framed stdout.

        Raises :class:`SSHError` on a transport failure, a non-zero remote exit,
        or output that never reached the closing sentinel.
        """

        argv = self.command(script)
        self.commands.append(argv)
        runner = self._runner or _run
        try:
            stdout, stderr, returncode = runner(argv, timeout)
        except SSHError:
            raise
        except Exception as exc:  # noqa: BLE001 - an injected runner is test code
            raise SSHError(f"{self.host.name}: could not run ssh: {exc}") from exc

        body, remote_status, complete = _unframe(stdout, self._token)
        if not complete:
            raise SSHError(
                _describe_failure(self.host, stderr, returncode),
                stderr=stderr,
                returncode=returncode,
            )
        if remote_status:
            raise SSHError(
                f"{self.host.name}: remote command exited {remote_status}"
                + (f": {_first_line(stderr)}" if stderr.strip() else ""),
                stderr=stderr,
                returncode=remote_status,
            )
        return body

    def stream(self, script: str, *, separator: bytes) -> Iterator[bytes]:
        """Records from a command, yielded as they arrive off the pipe.

        The lazy counterpart to :meth:`run`, and the reason ``walk`` can honour
        the protocol's laziness contract: a consumer that stops at ``max_files``
        has not paid for the rest of the tree, and closing the generator kills
        the remote ``find`` rather than waiting for it to finish enumerating a
        filesystem nobody is going to look at.

        Falls back to :meth:`run` when only a canned ``runner`` was injected —
        that is the fixture path, where output is a fixed string and laziness is
        not what is being tested. Real spawning is what the contract suite uses.
        """

        if self._runner is not None and self._spawn is subprocess.Popen:
            for record in self.run(script).split(separator.decode()):
                if record.strip():
                    yield record.encode()
            return

        argv = self.command(script)
        self.commands.append(argv)
        process = self._spawn(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            env=child_environment(),
        )
        try:
            yield from _stream_records(process.stdout, self._token, separator)
        finally:
            _terminate(process)

    def follow(self, script: str):
        """Spawn a persistent command, stdout non-blocking. Used by the tailer.

        **stdin is a pipe and is held open**, which is the mechanism the remote
        side uses to notice that CLV has gone away — see
        :meth:`RemoteFollowReader.command`. ``DEVNULL`` would look tidier and
        would kill the follow instantly, since the remote watcher would see EOF
        before ``tail`` had produced a line.
        """

        argv = self.command(script)
        self.commands.append(argv)
        process = self._spawn(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=child_environment(),
        )
        # **Both** streams, not just stdout. `tail` writes its rotation
        # diagnostic to stderr, and a blocking read of an empty stderr on the
        # event loop is the same freeze this whole design exists to avoid — the
        # more insidious version, because it only happens when nothing is
        # wrong.
        for stream in (
            getattr(process, "stdout", None),
            getattr(process, "stderr", None),
        ):
            if stream is None:
                continue
            try:
                os.set_blocking(stream.fileno(), False)
            except (OSError, AttributeError, ValueError):
                # A fake process in a test, or a platform without it: the drain
                # tolerates a blocking read returning everything at once.
                pass
        return process

    # --- the probe ----------------------------------------------------------

    @property
    def probed(self) -> bool:
        """Whether the one probe has already happened. Never triggers it."""

        return self._facts is not None

    def facts(self) -> HostFacts:
        """Everything one probe learned, measured once and kept.

        **One** round trip for the shell identity, the three capability tests
        and the clock. They are combined not to be clever but because each of
        them separately is a handshake an operator would feel on a slow link.
        """

        if self._facts is None:
            self._facts = self._probe()
        return self._facts

    def _probe(self) -> HostFacts:
        script = (
            "uname -s 2>/dev/null || echo unknown; "
            "(find . -maxdepth 0 -printf '' >/dev/null 2>&1 && echo printf=yes) "
            "|| echo printf=no; "
            "(stat -c '%d' . >/dev/null 2>&1 && echo statc=yes) || echo statc=no; "
            "(stat -f '%d' . >/dev/null 2>&1 && echo statf=yes) || echo statf=no; "
            "(dd if=/dev/null iflag=skip_bytes >/dev/null 2>&1 && echo dd=yes) "
            "|| echo dd=no; "
            "date +'date=%s'"
        )
        before = time.time()
        body = self.run(script)
        after = time.time()

        values: dict[str, str] = {}
        uname = ""
        for line in body.splitlines():
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
            elif line and not uname:
                uname = line

        profile = _select_profile(uname, values)
        skew, offset = _measure_clock(values.get("date", ""), before, after)
        return HostFacts(
            profile=profile, uname=uname, skew=skew, utc_offset=offset
        )

    # --- teardown -----------------------------------------------------------

    def close(self) -> None:
        """Tear the multiplex master down explicitly. Safe to call twice.

        Not left to ``ControlPersist``. The socket is a live authenticated
        connection for as long as it exists, and "it expires in a minute" is a
        worse answer than "it is gone" when the reason CLV is closing may be
        that the operator is walking away from the machine.
        """

        socket_path = self._socket
        self._facts = None
        if socket_path is None or not os.path.exists(socket_path):
            self._socket = None
            return
        try:
            (self._runner or _run)(
                [*self.base_argv()[:-1], "-O", "exit", self.host.host], 5
            )
        except Exception:  # noqa: BLE001 - the master may already be gone
            pass
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        self._socket = None


def _select_profile(uname: str, values: dict[str, str]) -> Profile:
    """Pick the argv set from what the probe saw, never from a guess."""

    if values.get("printf") == "yes":
        base = PROFILES["gnu"]
    elif values.get("statf") == "yes" and values.get("statc") != "yes":
        base = PROFILES["bsd"]
    elif uname and uname.lower().startswith(("darwin", "freebsd", "openbsd", "netbsd")):
        base = PROFILES["bsd"]
    elif values.get("statc") == "yes":
        base = PROFILES["busybox"]
    else:
        base = _POSIX

    changes: dict[str, Any] = {}
    if base.stat_format is not None:
        has_stat = values.get("statc") == "yes" if base.name != "bsd" else values.get("statf") == "yes"
        if not has_stat:
            # No readable `stat`: rotation detection degrades to the shrink-only
            # path Phase 2 already built, and `capabilities.stable_identity`
            # says so rather than the reader pretending.
            changes["stat_format"] = None
    if values.get("dd") != "yes":
        changes["dd_skip_bytes"] = False
    return replace(base, **changes) if changes else base


def _measure_clock(
    raw: str, before: float, after: float
) -> tuple[timedelta, timedelta]:
    """Remote clock offset and UTC offset, from ``date +'%s %z'``.

    Local time is sampled either side of the command and the **midpoint** used,
    so a 200 ms link does not read as 100 ms of clock skew. The measurement is
    reported by Phase 6 whether or not correction is enabled — a set whose hosts
    disagree should say by how much, and one that agrees should say nothing.
    """

    parts = raw.split()
    if not parts:
        return timedelta(0), timedelta(0)
    try:
        remote = float(parts[0])
    except ValueError:
        return timedelta(0), timedelta(0)

    skew = timedelta(seconds=remote - (before + after) / 2)

    offset = timedelta(0)
    if len(parts) > 1 and len(parts[1]) >= 5:
        sign = -1 if parts[1][0] == "-" else 1
        try:
            hours = int(parts[1][1:3])
            minutes = int(parts[1][3:5])
        except ValueError:
            return skew, timedelta(0)
        offset = sign * timedelta(hours=hours, minutes=minutes)
    return skew, offset


# ---------------------------------------------------------------------------
# Framing, and telling one failure from another
# ---------------------------------------------------------------------------


def _unframe(stdout: bytes, token: str) -> tuple[str, int, bool]:
    """``(body, remote exit status, reached the end)``.

    Everything before the opening sentinel is discarded — that is the MOTD, the
    legal banner and the ``.bashrc`` echo, any of which would otherwise be read
    as a filename. Reaching the closing sentinel is what separates "produced
    nothing" from "stopped early", and Requirement 7 turns on that distinction:
    a remote pane that goes quiet because the link dropped is the single worst
    outcome of this feature.
    """

    started = False
    lines: list[bytes] = []
    status = 0
    complete = False

    for raw in stdout.splitlines():
        match = _SENTINEL_RE.match(raw)
        if match is not None and match.group("token").decode() == token:
            if match.group("kind") == b"start":
                started = True
                lines.clear()
                continue
            complete = True
            tail = raw.split()
            if len(tail) > 1:
                try:
                    status = int(tail[1])
                except ValueError:
                    status = 0
            break
        if started:
            lines.append(raw)

    body = b"\n".join(lines)
    if lines:
        body += b"\n"
    return body.decode("utf-8", errors="replace"), status, complete


def _stream_records(
    stdout: Optional[IO[bytes]], token: str, separator: bytes
) -> Iterator[bytes]:
    """The framing rules of :func:`_unframe`, applied to a live pipe.

    Same three jobs — discard the banner, split records, stop at the closing
    sentinel — done incrementally so a caller that stops early has not waited
    for a ``find`` over the whole filesystem. The sentinels are always
    newline-terminated even when the records are NUL-terminated, which is what
    lets a GNU walk survive a newline inside a filename.
    """

    if stdout is None:
        return

    start = f"__clv_{token}_start".encode()
    end = f"__clv_{token}_end".encode()
    buffer = b""
    started = False

    while True:
        chunk = stdout.read(65536)
        if not chunk:
            break
        buffer += chunk

        if not started:
            marker = buffer.find(start)
            if marker < 0:
                # Still inside the banner. Keep only enough to match a sentinel
                # split across two reads.
                buffer = buffer[-len(start) :]
                continue
            newline = buffer.find(b"\n", marker)
            if newline < 0:
                continue
            buffer = buffer[newline + 1 :]
            started = True

        while True:
            cut = buffer.find(separator)
            if cut < 0:
                break
            record, buffer = buffer[:cut], buffer[cut + len(separator) :]
            if record.startswith(end):
                return
            if record.strip():
                yield record

    if started and buffer.strip() and not buffer.startswith(end):
        for trailing in buffer.split(separator):
            if trailing.strip() and not trailing.startswith(end):
                yield trailing


def _terminate(process: Any) -> None:
    """Stop a streamed command. Safe on one that already finished.

    Called from the generator's ``finally``, so abandoning a walk part way
    through — which is exactly what ``max_files`` does — does not leave a
    ``find`` running on someone else's machine.
    """

    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    except Exception:  # noqa: BLE001 - the process may already be gone
        pass
    finally:
        for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass


#: stderr fragment → what an operator should actually do about it.
#:
#: Ordered, and matched as substrings, because ``ssh`` does not offer a stable
#: machine-readable reason. Each of these is a *different* fact — Requirement 7
#: — and folding them into "connection failed" is what makes a remote source
#: feel unfixable.
_FAILURE_HINTS: tuple[tuple[str, str], ...] = (
    (
        "host key verification failed",
        "the host key is not trusted. Connect once by hand to verify it: "
        "CLV never disables host key checking.",
    ),
    (
        "remote host identification has changed",
        "the host key has CHANGED. Verify why before doing anything else, "
        "then update ~/.ssh/known_hosts by hand.",
    ),
    (
        "permission denied",
        "authentication was refused. CLV uses ssh-agent and key files only — "
        "load your key with ssh-add, or point identity_file at one.",
    ),
    (
        "could not resolve hostname",
        "the host name does not resolve.",
    ),
    ("connection refused", "nothing is listening on that port."),
    ("connection timed out", "the host did not answer."),
    ("no route to host", "the host is unreachable from here."),
    (
        "operation timed out",
        "the host did not answer.",
    ),
)


def _describe_failure(host: RemoteHost, stderr: str, returncode: int) -> str:
    """Name the host, say what happened, and say what to do about it."""

    lowered = stderr.lower()
    for fragment, hint in _FAILURE_HINTS:
        if fragment in lowered:
            return f"{host.name} ({host.host}): {hint}"
    detail = _first_line(stderr)
    if detail:
        return f"{host.name} ({host.host}): {detail}"
    return (
        f"{host.name} ({host.host}): the connection produced no output "
        f"(ssh exited {returncode})."
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _run(argv: list[str], timeout: int) -> tuple[bytes, str, int]:
    """Run one bounded ssh command as ``(stdout, stderr, returncode)``.

    stdin is closed rather than inherited: with ``BatchMode`` nothing should
    prompt, and a command that somehow tries to must fail rather than block on a
    terminal the TUI owns.
    """

    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        raise SSHError(f"ssh timed out after {timeout}s") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise SSHError(f"ssh could not be run: {exc}") from exc
    return (
        completed.stdout or b"",
        (completed.stderr or b"").decode("utf-8", errors="replace"),
        completed.returncode,
    )


# ---------------------------------------------------------------------------
# A seekable handle over a file on another machine
# ---------------------------------------------------------------------------


#: Bytes fetched per ranged read.
#:
#: Sized against the caller that matters. ``read_last_lines`` seeks to the end
#: and steps backwards in 64 KiB chunks until it has enough newlines; a window
#: this size answers a typical bounded tail — a few thousand lines — in **one**
#: round trip instead of sixteen. Larger would pull history nobody asked for
#: across a slow link, which is the pressure ``max_buffer_lines`` exists to
#: relieve rather than to create.
WINDOW = 1024 * 1024

#: Windows kept at once. Two, because the backwards walk touches the tail and
#: then usually the window before it; a third is history and this is a viewer,
#: not a cache. Requirement 12 — no remote log content ever touches disk —
#: applies to memory in spirit: what is held is bounded and small.
WINDOW_CACHE = 2


class RemoteFile(IO[bytes]):
    """A read-only, seekable binary handle over a file on another machine.

    Seekability is not incidental. ``read_last_lines`` seeks to the end and
    steps backwards, and ``zipfile.ZipFile`` refuses a non-seekable stream
    outright — so a remote ``.gz`` or ``.ods`` would be unreachable without it.
    The protocol requires it in as many words.

    What makes that affordable is that a seek costs nothing: only a ``read``
    fetches, and it fetches a :data:`WINDOW` around what was asked for, so a
    caller stepping backwards in 64 KiB chunks pays one round trip rather than
    sixteen. **Nothing is written to disk** — Requirement 12 — and at most
    :data:`WINDOW_CACHE` windows are held.
    """

    def __init__(self, backend: "RemoteBackend", ref: SourceRef, size: int) -> None:
        self._backend = backend
        self._ref = ref
        self._size = size
        self._position = 0
        self._closed = False

    # --- the parts callers actually use -------------------------------------

    def read(self, size: int = -1) -> bytes:
        self._check_open()
        if size is None or size < 0:
            size = max(0, self._size - self._position)
        if size == 0 or self._position >= self._size:
            return b""
        size = min(size, self._size - self._position)

        chunks: list[bytes] = []
        wanted = size
        while wanted > 0:
            window = self._window_for(self._position)
            start = self._position - window[0]
            piece = window[1][start : start + wanted]
            if not piece:
                break
            chunks.append(piece)
            self._position += len(piece)
            wanted -= len(piece)
        return b"".join(chunks)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_open()
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._position + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:  # pragma: no cover - the stdlib never passes anything else
            raise ValueError(f"invalid whence: {whence}")
        self._position = max(0, target)
        return self._position

    def tell(self) -> int:
        self._check_open()
        return self._position

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def __enter__(self) -> "RemoteFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- fetching -----------------------------------------------------------

    def _window_for(self, position: int) -> tuple[int, bytes]:
        """The window containing *position*, fetching it if need be.

        The cache belongs to the **backend**, not to this handle, and that is
        what makes a bounded tail one round trip rather than three. Priming a
        source opens the file more than once — once to sniff its encoding, once
        to read backwards — and a per-handle cache would refetch the same window
        for each. ``refresh`` drops a file's windows the moment its size or
        identity changes, so a handle can never serve bytes from a previous
        version of a rotated log.
        """

        start = (position // WINDOW) * WINDOW
        cached = self._backend.cached_window(self._ref, start)
        if cached is None:
            cached = self._backend.read_range(
                self._ref, start, min(WINDOW, self._size - start)
            )
            self._backend.cache_window(self._ref, start, cached)
        return start, cached

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed file")

    # --- the rest of the IO surface, refused rather than half-implemented ----

    def write(self, data: Any) -> int:
        raise UnsupportedOperation("CLV never writes to a remote host")

    def writelines(self, lines: Any) -> None:
        raise UnsupportedOperation("CLV never writes to a remote host")

    def truncate(self, size: Optional[int] = None) -> int:
        raise UnsupportedOperation("CLV never writes to a remote host")

    def fileno(self) -> int:
        raise UnsupportedOperation("a remote file has no local descriptor")

    def isatty(self) -> bool:
        return False

    def flush(self) -> None:
        return None

    def readline(self, limit: int = -1) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = self.read(4096 if limit < 0 else min(4096, limit))
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunks.append(chunk[: newline + 1])
                self.seek(-(len(chunk) - newline - 1), os.SEEK_CUR)
                break
            chunks.append(chunk)
            if limit >= 0:
                limit -= len(chunk)
                if limit <= 0:
                    break
        return b"".join(chunks)

    def readlines(self, hint: int = -1) -> list[bytes]:
        return self.read().splitlines(keepends=True)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


REMOTE_ACCESS_HINT = (
    "CLV reads as the SSH user and never escalates privilege. Add that user to "
    "the log group (adm, systemd-journal) or set an ACL on the path."
)

#: Records per ``classify`` command. The batch discovery hands over is already
#: bounded by ``discovery.CLASSIFY_BATCH``; this is the second guard, so a
#: caller that batches differently still cannot build an argv past ``ARG_MAX``.
CLASSIFY_CHUNK = 250

#: The delimiter between a walk record's fields. Tab, and the path is **last**,
#: so a tab inside a filename is recoverable by splitting a fixed number of
#: times. On a GNU host the records themselves are NUL-terminated, which solves
#: the newline case outright; elsewhere a newline in a filename is the one shape
#: that cannot be represented, and it is reported rather than silently mangled.
FIELD = "\t"


class RemoteBackend:
    """:class:`~clv.services.backend.SourceBackend`, over one SSH connection.

    Every method's cost is declared, and two of them are declared ``@cheap``
    while being backed by a network — which is the interesting part of this
    class and is explained on :meth:`stat`.
    """

    def __init__(
        self,
        connection: SSHConnection,
        *,
        include_globs: Sequence[str] = (),
    ) -> None:
        self._connection = connection
        #: Pushed down into ``find`` where a pattern is expressible as one, so
        #: a root full of rotated archives does not cross the wire to be
        #: discarded on this side.
        self._include_globs = tuple(include_globs)
        #: ref → last measurement. Populated by `walk` and by any refresh, and
        #: what `stat` answers with when it is not allowed to block.
        self._stats: dict[SourceRef, BackendStat] = {}
        #: (ref, window start) → bytes. Shared across handles for one file, and
        #: invalidated in `refresh` the moment that file changes.
        self._windows: dict[tuple[SourceRef, int], bytes] = {}
        #: Follows that could not be started, kept rather than raised — see
        #: :meth:`note_follow_failure`.
        self.follow_errors: list[tuple[SourceRef, str]] = []

    # --- identity of the backend itself -------------------------------------

    @property
    def connection(self) -> SSHConnection:
        return self._connection

    @property
    def node(self) -> str:
        return self._connection.host.name

    @property
    def profile(self) -> Profile:
        return self._connection.facts().profile

    @property
    def capabilities(self) -> BackendCapabilities:
        """What this host can do — without probing it from the event loop.

        ``stable_identity`` genuinely depends on the probe, and the probe is a
        round trip. Asked before one has happened *and* from inside the guard,
        this reports the conservative answer rather than blocking: an unknown
        identity degrades rotation detection to the shrink-only test, which is
        the safe direction to be wrong in and exactly what Phase 2 built the
        fallback for.
        """

        if self._connection.probed or not in_cheap_only():
            stable = self.profile.stable_identity
        else:
            stable = False
        return BackendCapabilities(
            name=f"ssh:{self.node}",
            blocking=_REMOTE_BLOCKING,
            stable_identity=stable,
            access_hint=REMOTE_ACCESS_HINT,
        )

    # --- guaranteed cheap ---------------------------------------------------

    @cheap
    def stat(self, ref: SourceRef) -> Optional[BackendStat]:
        """Size, mtime and identity — from the cache when that is all that is allowed.

        ``stat`` is ``GUARANTEED_CHEAP`` on every backend because ``poll()``
        calls it from the event loop at ``refresh_hz``, and a round trip there
        is a frozen UI twice a second per merged source. It is also the honest
        answer to "did this file grow?", which cannot be known without asking.

        Both are satisfied by asking *who wants to know*. Under
        ``backend.cheap_only()`` — the guard ``SourceBuffer.poll`` enters — this
        returns the last measurement and costs nothing. Outside it, on a worker
        or in a test, it goes to the wire and returns the truth. The guard is
        thread-local, so a worker is never held back by one the event loop
        happens to be inside.

        What keeps the cached answer *useful* rather than merely cheap is that
        a followed source has its cache refreshed by the tailer, so growth is
        seen without anyone paying for a poll.
        """

        if in_cheap_only():
            return self._stats.get(ref)
        return self.refresh(ref)

    @cheap
    def identity(self, ref: SourceRef) -> object | None:
        info = self.stat(ref)
        return None if info is None else info.identity

    # --- may block ----------------------------------------------------------

    @blocking
    def refresh(self, ref: SourceRef) -> Optional[BackendStat]:
        """Measure *ref* for real and update the cache.

        Named rather than folded into :meth:`stat` so a worker can be explicit
        about paying for a round trip, and so the ``@blocking`` mark applies to
        the call that actually is one.
        """

        fmt = self.profile.stat_format
        path = _remote_path(ref)
        if fmt is None:
            # No usable `stat`: size from `wc -c` and nothing for identity,
            # which `capabilities.stable_identity` already declares so the
            # reader degrades knowingly rather than guessing.
            script = f"wc -c < {quote_all(path)} 2>/dev/null"
            try:
                size = int(self._connection.run(script).strip())
            except (SSHError, ValueError):
                return self._forget(ref)
            return self._remember(ref, BackendStat(size=size, mtime_ns=0))

        flag = "-f" if self.profile.name == "bsd" else "-c"
        script = f"stat {flag} {shlex.quote(fmt)} {quote_all(path)}"
        try:
            info = _parse_stat(self._connection.run(script))
        except SSHError:
            return self._forget(ref)
        return self._forget(ref) if info is None else self._remember(ref, info)

    def _remember(self, ref: SourceRef, info: BackendStat) -> BackendStat:
        """Record a measurement, dropping cached bytes if the file moved on.

        The invalidation point. A rotated log keeps its name and gets a new
        inode, and serving a window fetched from the *previous* file would show
        yesterday's lines under today's name — the one failure a cache like this
        can cause that is worse than having no cache.
        """

        previous = self._stats.get(ref)
        if previous is not None and (
            previous.size != info.size or previous.identity != info.identity
        ):
            self._drop_windows(ref)
        self._stats[ref] = info
        return info

    def _forget(self, ref: SourceRef) -> None:
        self._stats.pop(ref, None)
        self._drop_windows(ref)
        return None

    @blocking
    def walk(
        self,
        root: SourceRef,
        *,
        follow_symlinks: bool = False,
        seen: set[object] | None = None,
    ) -> Iterator[WalkEntry]:
        """Every file beneath *root*, from **one** command, streamed.

        One ``find`` rather than a listing plus a stat per file: Requirement 4,
        and the difference between a usable ``/var/log`` and an unusable one.
        Size and identity come back *with* the entry for the same reason.

        Streamed rather than collected, so ``max_files`` bounds the work and not
        just the output — the laziness the protocol requires and the backend
        contract suite checks by deleting part of the tree mid-walk.

        A root that does not exist or will not list yields nothing rather than
        raising, matching ``LocalBackend``: the caller reports an unreadable
        *root*, and a directory that vanished mid-walk is not worth taking a
        pass down for.
        """

        for record in self._walk_records(root, follow_symlinks):
            entry = self._entry_from(record, root)
            if entry is None:
                continue
            if entry.identity is not None and seen is not None and follow_symlinks:
                # Cycle guard, shared across roots so two overlapping ones do
                # not walk the subtree twice. Only reachable with -L, which is
                # the only way a directory can contain itself.
                if entry.identity in seen:
                    continue
                seen.add(entry.identity)
            if not entry.unreadable:
                self._stats[entry.ref] = BackendStat(
                    size=entry.size, mtime_ns=0, identity=entry.identity
                )
            yield entry

    @blocking
    def list_dir(self, ref: SourceRef) -> Iterator[SourceRef]:
        """One level, no recursion, and it **raises**.

        The opposite of :meth:`walk`, and deliberately: ``sources.check_access``
        exists to report a listing that was refused, and a version of this that
        swallowed the error would lose the message entirely. An ACL or an
        SELinux label can refuse a listing that ``access`` permits, which is why
        the check lists for real.
        """

        path = _remote_path(ref)
        # `ls -A` rather than a `find -maxdepth 1` dance: this member has to
        # *report* the failure, and `ls` is what says "Permission denied" on
        # stderr for it. A missing path exits 2 before `ls` runs, so the two
        # cases stay distinguishable in `_listing_error`.
        script = (
            f"if [ ! -e {quote_all(path)} ]; then exit 2; fi; "
            f"ls -A {quote_all(path)}"
        )
        try:
            body = self._connection.run(script)
        except SSHError as exc:
            raise _listing_error(exc, ref) from exc
        base = ref if isinstance(ref, RemoteRef) else RemoteRef.build(self.node, path)
        for name in body.splitlines():
            if name.strip():
                yield base / name

    @blocking
    def kind(self, ref: SourceRef) -> RefKind:
        """What *ref* is, in **one** call.

        A remote ``exists``/``is_file``/``is_dir`` triple is three round trips
        to answer one question, which is why the protocol has this member at all.
        ``denied`` is kept apart from ``missing``: one is a permission to fix
        and the other is a path to correct, and they are different instructions.
        """

        path = _remote_path(ref)
        quoted = quote_all(path)
        script = (
            f"if [ -d {quoted} ]; then echo dir; "
            f"elif [ -f {quoted} ]; then echo file; "
            f"elif [ -e {quoted} ]; then echo other; "
            f"else echo absent; fi"
        )
        try:
            body = self._connection.run(script)
        except SSHError:
            return "denied"
        answer = body.strip()
        if answer in ("dir", "file", "other"):
            return answer  # type: ignore[return-value]
        if answer == "absent":
            # `[ -e ]` is false both for a path that is not there and for one
            # inside a directory this user may not traverse. One more question,
            # and only when the answer was ambiguous.
            return "missing" if self._parent_is_searchable(path) else "denied"
        return "other"

    @blocking
    def access(self, ref: SourceRef, mode: int) -> bool:
        """``os.access`` semantics, as ``test`` expresses them."""

        tests = []
        if mode & os.R_OK:
            tests.append("-r")
        if mode & os.W_OK:
            tests.append("-w")
        if mode & os.X_OK:
            tests.append("-x")
        if not tests:
            tests.append("-e")
        quoted = quote_all(_remote_path(ref))
        condition = " -a ".join(f"{flag} {quoted}" for flag in tests)
        try:
            body = self._connection.run(f"if [ {condition} ]; then echo y; fi")
        except SSHError:
            return False
        return body.strip() == "y"

    @blocking
    def open(self, ref: SourceRef, mode: str = "rb") -> IO[bytes]:
        """A seekable handle. Raises ``OSError`` for anything unreadable."""

        if "w" in mode or "a" in mode or "+" in mode:
            raise UnsupportedOperation("CLV never writes to a remote host")
        info = self.refresh(ref)
        if info is None:
            raise SSHError(f"{ref}: could not be read on {self.node}")
        return RemoteFile(self, ref, info.size)

    @blocking
    def classify(
        self, requests: Sequence[ClassifyRequest]
    ) -> dict[SourceRef, ClassifyResult]:
        """Readability and leading bytes for a whole batch, in one command.

        **This is the member that decides whether the feature is usable.** The
        text/binary sniff reads 8 KB per candidate; done the obvious way that is
        one round trip per file, which is exactly what makes an ``sshfs`` mount
        slow at 400 files and precisely the trap Requirement 4 names.

        What crosses the wire is the *bytes*, hex-encoded, not a verdict. A
        remote NUL test would be four characters of ``sh`` and would reject
        every UTF-16 export — PowerShell's normal output — because UTF-16
        encodes ASCII with a NUL beside every character. The rule that handles
        that lives in ``reader.looks_binary_block`` and stays there.

        ``od`` rather than ``base64``: it is POSIX, present on BusyBox and BSD,
        and needs no ``-d``/``-D`` spelling fork.
        """

        results: dict[SourceRef, ClassifyResult] = {}
        batch = [request for request in requests if request is not None]
        for start in range(0, len(batch), CLASSIFY_CHUNK):
            chunk = batch[start : start + CLASSIFY_CHUNK]
            results.update(self._classify_chunk(chunk))
        return results

    # --- what a follower tells the backend ----------------------------------

    def note_followed_size(self, ref: SourceRef, size: int) -> None:
        """Record that a tailer has read up to *size* bytes of *ref*.

        This is what makes 4a's cache-serving :meth:`stat` **useful** rather
        than merely cheap. Under the poll guard ``stat`` answers from the cache
        and never goes to the wire; without this the cached size would be
        whatever the last worker-driven refresh saw, and a growing log would
        look static to anything that asks. The tailer already knows how many
        bytes it consumed, so the answer costs nothing to keep current.

        Identity is carried through unchanged: bytes arriving on an open follow
        are by definition the *same* file, and inventing a new identity here
        would look like a rotation to ``SourceReader``.
        """

        previous = self._stats.get(ref)
        if previous is None:
            self._stats[ref] = BackendStat(size=size, mtime_ns=0)
            return
        if size <= previous.size:
            return
        self._stats[ref] = BackendStat(
            size=size, mtime_ns=previous.mtime_ns, identity=previous.identity
        )

    def note_follow_failure(self, ref: SourceRef, exc: BaseException) -> None:
        """A follow could not be started. Recorded, never raised.

        The bounded read has already succeeded by the time this can happen, so
        the operator has their log; what failed is only liveness. Turning that
        into an exception would make a readable source unopenable over
        something that costs a refresh key.

        Phase 5 owns turning this into something the pane says. Until then it is
        kept rather than discarded, so the reason exists to be reported.
        """

        self.follow_errors.append((ref, str(exc)))

    # --- the window cache ---------------------------------------------------

    def cached_window(self, ref: SourceRef, start: int) -> Optional[bytes]:
        return self._windows.get((ref, start))

    def cache_window(self, ref: SourceRef, start: int, data: bytes) -> None:
        if len(self._windows) >= WINDOW_CACHE:
            # Oldest out. `dict` preserves insertion order, which is the whole
            # of the eviction policy this needs — and the bound is what keeps
            # "no remote log content touches disk" from becoming "it lives in
            # memory instead".
            self._windows.pop(next(iter(self._windows)))
        self._windows[(ref, start)] = data

    def _drop_windows(self, ref: SourceRef) -> None:
        for key in [key for key in self._windows if key[0] == ref]:
            self._windows.pop(key, None)

    # --- ranged reads -------------------------------------------------------

    @blocking
    def read_range(self, ref: SourceRef, offset: int, size: int) -> bytes:
        """*size* bytes of *ref* starting at *offset*, in one command.

        ``dd`` where the remote has GNU byte offsets, and ``tail -c +N | head -c
        M`` where it does not — which is POSIX and therefore works on the
        BusyBox and BSD hosts the probe found. Both are one round trip; the
        fallback simply reads more on the remote side to get there.
        """

        if size <= 0:
            return b""
        quoted = quote_all(_remote_path(ref))
        if self.profile.dd_skip_bytes:
            body = (
                f"dd if={quoted} bs={WINDOW} iflag=skip_bytes,count_bytes "
                f"skip={offset} count={size} 2>/dev/null"
            )
        else:
            body = f"tail -c +{offset + 1} {quoted} 2>/dev/null | head -c {size}"
        # Hex, for the same reason `classify` uses it: the framing is line
        # based, and raw log bytes would carry newlines — and could carry the
        # sentinel's own alphabet — straight through it.
        #
        # The trailing `echo` is load-bearing rather than tidy. `tr -d ' \n'`
        # strips every newline including the last, so without it the closing
        # sentinel lands on the same line as the payload and the framing never
        # sees it: a perfectly good read reports as a dropped connection.
        script = f"{{ {body} ; }} | od -An -v -tx1 | tr -d ' \\n'; echo"
        return _unhex(self._connection.run(script))

    # --- internals ----------------------------------------------------------

    def _parent_is_searchable(self, path: str) -> bool:
        parent = str(PurePosixPath(path).parent)
        try:
            body = self._connection.run(
                f"if [ -x {quote_all(parent)} ]; then echo y; fi"
            )
        except SSHError:
            return False
        return body.strip() == "y"

    def _find_argv(self, root: SourceRef, follow_symlinks: bool) -> str:
        """The one command a walk is.

        ``-name`` is pushed down where the operator's include globs are
        expressible as one — a single path component with no separator — so a
        ``/var/log`` full of ``.gz`` archives is filtered on the remote rather
        than transferred and discarded here. A glob containing ``/`` matches
        against the root-relative path, which ``find -name`` cannot express, so
        it is left to this side rather than pushed down wrongly.
        """

        follow = "-L " if follow_symlinks else ""
        quoted = quote_all(_remote_path(root))
        pushdown = ""
        expressible = [glob for glob in self._include_globs if "/" not in glob]
        if expressible and len(expressible) == len(self._include_globs):
            names = " -o ".join(f"-name {shlex.quote(glob)}" for glob in expressible)
            pushdown = f" \\( {names} \\)"

        if self.profile.find_printf:
            return (
                f"find {follow}{quoted} -type f{pushdown} "
                f"-printf '%s{FIELD}%D{FIELD}%i{FIELD}%p\\0' 2>/dev/null"
            )
        fmt = self.profile.stat_format
        if fmt is None:
            return f"find {follow}{quoted} -type f{pushdown} -print 2>/dev/null"
        flag = "-f" if self.profile.name == "bsd" else "-c"
        # `-exec ... +` batches: still one command and one round trip, which is
        # what the requirement is about. It is not one process per file.
        order = "%z\t%d\t%i\t%N" if self.profile.name == "bsd" else "%s\t%d\t%i\t%n"
        return (
            f"find {follow}{quoted} -type f{pushdown} "
            f"-exec stat {flag} {shlex.quote(order)} {{}} + 2>/dev/null"
        )

    def _walk_records(
        self, root: SourceRef, follow_symlinks: bool
    ) -> Iterator[str]:
        """Records from the one walk command, as they arrive.

        Streamed rather than collected, and that is a contract rather than an
        optimisation: ``max_files`` has to bound *work*, and a walk that
        buffered the whole of ``find /`` before yielding anything would satisfy
        the round-trip budget while breaking the laziness one.

        A root that will not list yields nothing. ``LocalBackend.walk`` swallows
        the same failure for the same reason — the caller reports an unreadable
        *root*, and the difference between "the root is gone" and "a
        subdirectory below it is" is not the walk's to make.
        """

        separator = b"\0" if self.profile.find_printf else b"\n"
        try:
            for record in self._connection.stream(
                self._find_argv(root, follow_symlinks), separator=separator
            ):
                yield record.decode("utf-8", errors="replace")
        except SSHError:
            return

    def _entry_from(self, record: str, root: SourceRef) -> Optional[WalkEntry]:
        """One walk record → a :class:`WalkEntry`, or ``None`` if unparseable."""

        if self.profile.find_printf or self.profile.stat_format is not None:
            fields = record.split(FIELD, 3)
            if len(fields) != 4:
                return None
            raw_size, raw_dev, raw_ino, path = fields
            ref = RemoteRef.build(self.node, path)
            try:
                size = int(raw_size)
            except ValueError:
                return WalkEntry(ref=ref, size=0, unreadable=True)
            identity: object | None = None
            if self.profile.stable_identity:
                try:
                    identity = (int(raw_dev), int(raw_ino))
                except ValueError:
                    identity = None
            return WalkEntry(ref=ref, size=size, identity=identity)

        # No `stat` at all: the name is everything the walk could learn. Size
        # comes from `classify`'s readability pass or from the open, and
        # `stable_identity` is already False.
        return WalkEntry(ref=RemoteRef.build(self.node, record), size=0)

    def _classify_chunk(
        self, chunk: Sequence[ClassifyRequest]
    ) -> dict[SourceRef, ClassifyResult]:
        """One command over up to :data:`CLASSIFY_CHUNK` files.

        Each file produces three framed lines — a marker, the readability
        verdict, and the hex of its head — so the parse cannot be confused by a
        filename, however hostile, appearing in the output. The index is the
        key, not the path.
        """

        if not chunk:
            return {}

        lines = []
        for index, request in enumerate(chunk):
            path = quote_all(_remote_path(request.ref))
            want = max(0, request.head_bytes)
            lines.append(
                f"echo 'F {index}'; "
                f"if [ -r {path} ] && [ -f {path} ]; then echo 'R 1'; "
                + (
                    f"head -c {want + 1} {path} 2>/dev/null | od -An -v -tx1 "
                    f"| tr -d ' \\n'; echo; "
                    if want
                    else "echo; "
                )
                + "else echo 'R 0'; echo; fi"
            )
        body = self._connection.run("; ".join(lines))

        results: dict[SourceRef, ClassifyResult] = {}
        current: Optional[int] = None
        readable = False
        for line in body.splitlines():
            if line.startswith("F "):
                current = _int_or_none(line[2:])
                readable = False
                continue
            if line.startswith("R "):
                readable = line[2:].strip() == "1"
                continue
            if current is None or current >= len(chunk):
                continue
            request = chunk[current]
            if not readable:
                results[request.ref] = ClassifyResult(readable=False)
                current = None
                continue
            head = _unhex(line.strip())
            results[request.ref] = ClassifyResult(
                readable=True,
                head=head[: request.head_bytes],
                complete=len(head) <= request.head_bytes,
            )
            current = None
        return results


def _parse_stat(body: str) -> Optional[BackendStat]:
    """``"2049 1234 512 1755300000"`` → a :class:`BackendStat`."""

    fields = body.split()
    if len(fields) < 4:
        return None
    try:
        dev, ino, size, mtime = (int(field) for field in fields[:4])
    except ValueError:
        return None
    return BackendStat(
        size=size, mtime_ns=mtime * 1_000_000_000, identity=(dev, ino)
    )


def _listing_error(exc: SSHError, ref: SourceRef) -> OSError:
    """Translate a failed listing into the exception ``check_access`` expects.

    ``sources.check_access`` distinguishes ``PermissionError`` from
    ``FileNotFoundError`` and says something different for each, which is the
    whole reason ``list_dir`` raises rather than skipping.
    """

    lowered = (exc.stderr or str(exc)).lower()
    if "permission denied" in lowered or exc.returncode == 3:
        return PermissionError(str(exc))
    if "no such file" in lowered or exc.returncode == 2:
        return FileNotFoundError(str(exc))
    return exc


def _int_or_none(text: str) -> Optional[int]:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _unhex(text: str) -> bytes:
    """``od -An -tx1`` output → bytes. Tolerant of an odd tail.

    A stray character means the sample was cut off mid-byte, which is a short
    read rather than a reason to lose the whole file: the head is a heuristic
    input, and half a byte fewer changes no verdict.
    """

    cleaned = "".join(character for character in text if character in "0123456789abcdefABCDEF")
    if len(cleaned) % 2:
        cleaned = cleaned[:-1]
    try:
        return bytes.fromhex(cleaned)
    except ValueError:  # pragma: no cover - the filter above makes this unreachable
        return b""


# ---------------------------------------------------------------------------
# Resolving a ref to the backend that answers for it
# ---------------------------------------------------------------------------


#: Derived once at import: the marks do not change at runtime, and deriving
#: them per `capabilities` access would walk the class on every tree row.
_REMOTE_BLOCKING = blocking_methods(RemoteBackend)


# ---------------------------------------------------------------------------
# Following a remote log
# ---------------------------------------------------------------------------


#: What ``tail`` says on its own stderr when the file it was following was
#: replaced. Matched as substrings, lowercased, because there is no portable
#: machine-readable signal and the three implementations word it differently.
#:
#: Best effort **by design**, and the degradation is benign: an unrecognised
#: spelling means lines keep flowing from the new file with no reload notice,
#: never that lines are lost. Requirement 2 is not at risk here; only the
#: redraw is.
_ROTATION_NOTICES: tuple[str, ...] = (
    "has appeared",              # GNU: "...has appeared;  following new file"
    "following new file",
    "has become inaccessible",   # GNU, when it goes the other way first
    "file truncated",            # GNU and BusyBox, on `> file`
    "has been replaced",
    "file has been replaced",    # BSD
)


class RemoteFollowReader:
    """A remote log, followed by a persistent ``tail -F``.

    The reader ``poll()`` was designed around. ``SourceBuffer.poll`` runs on a
    ``set_interval`` timer at ``refresh_hz`` — twice a second, per merged
    source, on the event loop — so the one thing this class must never do is a
    round trip. It does not: the connection is opened once at ``prime()`` and
    everything after that is draining bytes that have already arrived.

    Structurally identical to :class:`~clv.plugins.sources.journald.JournalReader`,
    including the non-blocking stdout, the partial-line remainder and the
    explicit ``close()``, because that class already solved every part of this
    for a subprocess-backed stream.

    **Priming is a bounded backwards read, not ``tail -n``.** ``tail -n N -F``
    would be one round trip instead of two and is tempting for exactly that
    reason — but it is bounded by *lines*, so a remote file whose "lines" are
    enormous (minified JSON, a file with no newlines at all) transfers without
    limit. Requirement 3 of ``AGENTS.md`` does not relax with distance. So the
    initial read goes through ``read_last_lines`` exactly as a local source
    does, byte-bounded by ``max_bytes``, and the follow then starts at
    ``-c +<offset+1>`` — the byte the prime stopped at, so no line is delivered
    twice and none is skipped. ``tail -c +N`` is POSIX and works on all three
    command profiles.
    """

    RELOAD_NOTICE = "{name} was rotated; reloaded."

    def __init__(
        self,
        path: SourceRef,
        *,
        max_lines: int,
        backend: "RemoteBackend",
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        encoding: Any = None,
    ) -> None:
        self.path = path
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._backend = backend
        self._process: Any = None
        self._remainder = ""
        self._byte_remainder = b""
        self._offset = 0
        self._encoding = UTF8
        #: Set by a drain that saw `tail` report the file was replaced, and
        #: consumed by the next `poll()` so the pane redraws once.
        self._rotated = False
        #: Whether the opening sentinel has been seen, after which every byte
        #: is log. Latches; see `_past_the_banner`.
        self._started = False
        #: Bytes seen before it, held only long enough to match a marker split
        #: across two reads.
        self._banner = b""
        #: Set when the closing sentinel arrives: the remote command has ended,
        #: whether because `tail` exited or because the link went.
        self._finished = False

    @property
    def offset(self) -> int:
        return self._offset

    # --- the command --------------------------------------------------------

    def command(self) -> str:
        """The remote script this reader follows with.

        ``-F`` rather than ``-f``: ``-F`` reopens the name when it is replaced,
        which is what makes a remote ``logrotate`` survivable instead of a pane
        that silently stops updating.

        **The rest of it exists because killing the local ``ssh`` is not enough
        to stop the remote ``tail``.** When the connection goes, ``sshd`` closes
        the command's stdout — but a process only finds out about a closed pipe
        when it next *writes* to it, and a ``tail`` on an idle log never writes
        again. It sits there forever. Every source switch would leave one behind
        on the operator's server, which is a worse leak than any local one:
        CLV's whole claim is that it installs nothing and leaves nothing
        running.

        So the script watches its own stdin, which ``sshd`` *does* close
        promptly, and covers both directions:

        * the connection drops → ``cat`` sees EOF → the shell kills ``tail`` and
          exits;
        * ``tail`` exits on its own → the poller notices and kills the shell, so
          ``poll()`` sees a dead process and says so rather than waiting forever
          on a stream that will never produce anything.

        **``cat`` must be in the foreground.** POSIX says a backgrounded command
        in a non-interactive shell gets its stdin reassigned to ``/dev/null``, so
        a watcher written as ``{ cat …; } &`` reads EOF *immediately* and kills
        the follow before it has produced a line. That is a one-character
        difference between working and useless, which is why it is written down
        here rather than left to be rediscovered.

        No ``exec``, because the shell has to outlive ``tail`` to clean up after
        it. The cost is that the framing's closing sentinel arrives on exit;
        :meth:`_drain` filters it.
        """

        target = quote_all(_remote_path(self.path))
        return (
            f"tail -F -c +{self._offset + 1} {target} & __clv_t=$!; "
            "{ while kill -0 $__clv_t 2>/dev/null; do sleep 5; done; "
            "kill $$ 2>/dev/null; } & "
            "cat >/dev/null 2>&1; kill $__clv_t 2>/dev/null"
        )

    # --- the contract -------------------------------------------------------

    def prime(self) -> TailRead:
        """Bounded backwards read, then start following from where it ended."""

        self.close()

        self._encoding = detect_file_encoding(self.path, backend=self._backend)
        result = read_last_lines(
            self.path,
            self._max_lines,
            max_bytes=self._max_bytes,
            encoding=self._encoding,
            backend=self._backend,
        )
        self._offset = result.offset
        self._remainder = ""
        self._byte_remainder = b""
        self._rotated = False
        self._started = False
        self._banner = b""
        self._finished = False

        try:
            self._process = self._backend.connection.follow(self.command())
        except OSError as exc:
            # The bounded read already succeeded, so the operator gets the log;
            # what they lose is the tail. Raising here would turn a readable
            # source into an unopenable one over a failure that only costs
            # liveness.
            self._process = None
            self._backend.note_follow_failure(self.path, exc)
        return result

    def poll(self) -> TailRead:
        """Whatever has arrived since the last call. **Never** a round trip."""

        if self._process is None:
            return TailRead(lines=[], offset=self._offset)
        if self._finished or self._process.poll() is not None:
            # `tail` exited. Draining once more catches what it wrote on the
            # way out; re-running it every tick would be the fork bomb with a
            # nice name that `journald` names in its own poll.
            lines = self._drain()
            rotated, self._rotated = self._rotated, False
            self.close()
            return TailRead(lines=lines, offset=self._offset, rotated=rotated)

        lines = self._drain()
        rotated, self._rotated = self._rotated, False
        if rotated:
            # The name points at a new file, so the offset this reader was
            # counting is meaningless and the buffer it filled describes the
            # previous one. Re-prime, exactly as `SourceReader.poll` does.
            result = self.prime()
            return TailRead(
                lines=result.lines,
                offset=result.offset,
                truncated=result.truncated,
                rotated=True,
            )
        return TailRead(lines=lines, offset=self._offset)

    # --- draining -----------------------------------------------------------

    def _drain(self) -> list[str]:
        """Read what is buffered on stdout, and notice what stderr said.

        The bytes are decoded with the encoding the prime sniffed, holding back
        a character split across two polls: decoding those alone would emit
        U+FFFD for a character that arrives intact a fraction of a second later.
        ``SourceReader._read_from`` holds the same remainder for the same
        reason.
        """

        self._check_stderr()

        stdout = getattr(self._process, "stdout", None)
        if stdout is None:
            return []
        try:
            chunk = stdout.read()
        except (BlockingIOError, ValueError):
            return []
        if not chunk:
            return []
        if isinstance(chunk, str):  # pragma: no cover - a text-mode fake
            chunk = chunk.encode("utf-8")

        chunk = self._past_the_banner(chunk)
        if not chunk:
            return []

        # Counted before decoding: the offset is a byte position, and it is what
        # keeps the backend's cheap `stat` honest for the poll that follows.
        self._offset += len(chunk)

        data = self._byte_remainder + chunk
        usable = len(data) - len(data) % self._encoding.unit_size
        self._byte_remainder = data[usable:]
        text = self._remainder + data[:usable].decode(
            self._encoding.name, errors="replace"
        )

        lines = text.splitlines()
        if not text.endswith(("\n", "\r")):
            # Hold the partial line: half a log line is not a log line, and it
            # would otherwise be parsed and then contradicted a tick later.
            self._remainder = lines.pop() if lines else text
        else:
            self._remainder = ""

        # The frame's closing sentinel, which arrives when the remote shell
        # finally exits. Showing it to the operator as a log line would be a
        # small lie in the one place CLV promises never to tell one; the token
        # is a random 16 hex digits, so a real log line cannot collide with it.
        end = self._backend.connection.end_marker
        if any(line.startswith(end) for line in lines):
            self._finished = True
            lines = [line for line in lines if not line.startswith(end)]

        self._backend.note_followed_size(self.path, self._offset)
        return lines

    def _past_the_banner(self, chunk: bytes) -> bytes:
        """Drop everything up to and including the opening sentinel.

        **This matters more for a follow than for a one-shot command.** A login
        shell's MOTD, legal banner or ``.bashrc`` echo arrives on the same pipe
        as the log, and without this it would be *parsed and displayed as log
        lines* — phantom entries in the pane, with whatever timestamp and level
        the parser managed to read out of a welcome message.

        Done incrementally rather than by :func:`_unframe`, because a follow
        never completes and so can never be unframed whole. Once the marker has
        been seen everything after it is data, which is why the flag latches.
        """

        if self._started:
            return chunk

        self._banner += chunk
        marker = self._backend.connection.start_marker
        position = self._banner.find(marker)
        if position < 0:
            # Keep only enough to match a marker split across two reads.
            self._banner = self._banner[-len(marker) :]
            return b""
        newline = self._banner.find(b"\n", position)
        if newline < 0:
            return b""

        remainder = self._banner[newline + 1 :]
        self._banner = b""
        self._started = True
        return remainder

    def _check_stderr(self) -> None:
        """Look for ``tail``'s own rotation diagnostic. Never blocks."""

        stderr = getattr(self._process, "stderr", None)
        if stderr is None:
            return
        try:
            chunk = stderr.read()
        except (BlockingIOError, ValueError):
            return
        if not chunk:
            return
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        lowered = chunk.lower()
        if any(notice in lowered for notice in _ROTATION_NOTICES):
            self._rotated = True

    # --- teardown -----------------------------------------------------------

    def close(self) -> None:
        """Stop following. Safe to call twice, and on a dead process.

        **Closing stdin comes first**, and is what actually stops the remote
        ``tail``: it is the EOF the watcher in :meth:`command` is waiting for.
        Terminating the local ``ssh`` alone leaves a ``tail`` on an idle log
        running on someone else's machine indefinitely, because a closed pipe is
        only noticed on the next write.
        """

        process, self._process = self._process, None
        if process is not None:
            stdin = getattr(process, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:  # noqa: BLE001 - already gone is fine
                    pass
        _terminate(process)

    def __del__(self) -> None:  # pragma: no cover - a backstop, not the path
        # The session closes readers explicitly; this only catches one that
        # somehow never reached it.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


class RemoteResolver:
    """Routes a ref to its host's backend, and everything else to the local one.

    A resolver rather than a backend because the root list is *mixed*: one entry
    is a folder on this machine and the next is a folder on another, and no
    single backend can answer for both.

    **Nothing is constructed until a remote ref actually arrives**, and nothing
    is spawned until a command is run — so holding one of these with
    ``enable_ssh`` off costs a dictionary. ``app.build_resolver`` does not even
    do that: it hands back ``LOCAL`` itself.
    """

    def __init__(
        self,
        hosts: Sequence[RemoteHost],
        *,
        local,
        include_globs: Sequence[str] = (),
        spawn=subprocess.Popen,
        runner=None,
        socket_dir: Optional[str] = None,
    ) -> None:
        self._hosts = {host.name: host for host in hosts}
        self._local = local
        self._include_globs = tuple(include_globs)
        self._spawn = spawn
        self._runner = runner
        self._socket_dir = socket_dir
        self._backends: dict[str, RemoteBackend] = {}

    @property
    def backends(self) -> dict[str, RemoteBackend]:
        """The connections actually opened, for teardown and for the drawer."""

        return dict(self._backends)

    def for_ref(self, ref: SourceRef):
        if not isinstance(ref, RemoteRef):
            return self._local
        backend = self._backends.get(ref.node)
        if backend is None:
            host = self._hosts.get(ref.node)
            if host is None:
                # A ref naming a host that is no longer configured. The local
                # backend reports it as missing, which is the honest answer:
                # CLV has no way to reach it and inventing a connection would
                # be worse than saying so.
                return self._local
            backend = RemoteBackend(
                SSHConnection(
                    host,
                    spawn=self._spawn,
                    runner=self._runner,
                    socket_dir=self._socket_dir,
                ),
                include_globs=host.include_globs
                if host.include_globs is not None
                else self._include_globs,
            )
            self._backends[ref.node] = backend
        return backend

    def close(self) -> None:
        """Tear down every multiplex master this resolver opened."""

        for backend in self._backends.values():
            backend.connection.close()
        self._backends.clear()


def register() -> list:
    """Nothing. This module is a **backend**, not a source provider.

    The loader documents an empty return as the way a module declines to
    register itself, and this one declines permanently rather than
    conditionally. Without it the loader falls through to :data:`__all__` and
    tries to register :class:`RemoteBackend` as a plugin, which is rejected as
    "does not implement a CLV plugin interface" — a message about the wrong
    problem entirely.

    The distinction is the one this whole plan turns on. A
    :class:`~clv.plugins.LogSourceProvider` hands back a ``ProviderSource``,
    which is deliberately *not* a path and which starring, glob filtering and
    rotated-set grouping all skip by design. A remote log has to be none of
    that: it is an ordinary source under an ordinary root, read through an
    ordinary backend, and ``app.py`` wires the resolver in directly.

    What this module keeps from being a plugin is where it *lives*: a plugin
    may not spawn a subprocess without consent, and a network subprocess raises
    that bar rather than lowering it.
    """

    return []
