"""Running containers as log sources, as a CLV source provider.

A worked ``LogSourceProvider`` example. Drop this file in
``~/.config/clv/plugins/`` (CLV puts it in ``examples/`` for you) and add it to
``settings.conf``::

    [log_viewer]
    plugins = container_logs

    [plugin:container_logs]
    # Ships inert. Until this is true the provider offers nothing and runs
    # nothing -- see "Consent is the whole of it" below.
    enabled = true
    # podman or docker. Omitted, whichever is on PATH, podman first.
    runtime = podman

Each running container then appears under **Providers** in the source tree and
opens like any other source, tailing live.

Seven things are worth copying out of this file.

**Consent is the whole of it, and it is read fresh rather than cached.** This
plugin spawns a subprocess, and a plugin may not do that because it was
installed. ``enabled`` lives in this module's own section, arrives through
``configure()``, and is read on **every** ``discover()`` — so switching it on
takes effect without a restart, and switching it off stops the provider offering
anything from the next scan. The shipped ``journald`` provider is the same
shape; this is that pattern on a second subject, which is what makes it a
pattern rather than one plugin's habit.

**Consent first, capability second.** A machine with no ``podman`` and no
``docker`` should say so — but only to someone who asked for containers in the
first place. Probing before checking the opt-in would mean an uninstalled
runtime reporting itself to an operator who never wanted this plugin.

**A provider identifier is a ``Path``, and you cannot invent a scheme.**
``container/web-1`` here. A scheme of your own — ``container:web-1`` — is not
available to a plugin: ``refs.SOURCE_REF_TYPES`` is a closed union of types, and
``JournalRef`` lives in CLV's core rather than in the journald plugin precisely
because ``parse_ref`` decodes ``session.json`` before any plugin is imported. A
type registered at import time would decode the same file differently depending
on load order. A relative ``Path`` is what is left, and it survives
``format_ref``/``parse_ref`` unchanged, which is what a star has to do.

**Tailing means ``open_reader``, not ``open``.** An iterator cannot be asked to
stop, has nowhere to put cleanup, and blocks the poll it is drained from.
``open_reader`` returns an object with ``path``, ``prime()``, ``poll()``,
``close()`` and ``RELOAD_NOTICE``; ``prime`` and ``poll`` hand back a
:class:`~clv.api.TailRead`. ``open()`` is still implemented, because the
interface requires it and because it is two lines once the reader exists.

**``poll()`` runs on the event loop, so it may not block.** The subprocess's
stdout is set non-blocking and drained on the tick CLV already runs, rather than
read from a thread that would then have to be joined. This is the same rule the
rest of CLV lives under: a round trip on the timer is a frozen UI.

**Close what you opened, every time.** CLV calls ``close()`` on every source
switch and again at shutdown. Without it, switching between two containers ten
times leaves ten ``logs --follow`` processes running — and nothing in CLV would
report it, because from the outside the plugin is working.

**A subprocess must not inherit a frozen build's environment.** PyInstaller puts
its own ``_internal`` directory on ``LD_LIBRARY_PATH``; a *system* binary that
inherits it loads the bundle's libcrypto and libssl instead of the machine's and
dies whenever the build distribution differs from the running one, which for a
released binary is the normal case. CLV's own answer is
``journald.child_environment()`` — which ``clv/plugins/AGENTS.md`` tells you to
call and which **is not published in ``clv.api``**. Copying eight lines is
better than importing a drop-in module out of CLV's internals, so
:func:`child_environment` below is that function, and the gap is recorded rather
than papered over.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from clv.api import LogSourceProvider, ProviderSource, SourceRef, TailRead, setting_bool

#: The first path component every source this provider offers lives under, so
#: one ``container/`` folder appears in the tree rather than a flat list.
PREFIX = "container"

#: Runtimes tried in order when the operator named none. podman first because it
#: needs no daemon and no group membership, which matters for a viewer whose
#: whole posture is "no privilege escalation, anywhere".
RUNTIMES: tuple[str, ...] = ("podman", "docker")

#: Lines of history asked for when a source is opened. Bounded for the reason
#: every read in CLV is bounded: a container that has been up for a month must
#: not cost a month of log on the first render.
DEFAULT_LINES = 500

#: How long the container listing may take before it is abandoned. A runtime
#: whose daemon is wedged must cost one scan, not the source tree.
LIST_TIMEOUT = 5.0


def child_environment() -> dict[str, str]:
    """The environment a *system* binary should be run with.

    ``journald.child_environment()`` in eight lines, because that function is
    the documented answer and is not part of the published API. Whatever
    PyInstaller saved in ``*_ORIG`` is restored; failing that, the bundle's own
    directory is removed and everything the operator set is left alone — it is
    theirs, and only the injected entry is ours to take back out.
    """

    env = dict(os.environ)
    bundle = getattr(sys, "_MEIPASS", None)
    for variable in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(f"{variable}_ORIG", None)
        if original is not None:
            env[variable] = original
            continue
        if not bundle or variable not in env:
            continue
        kept = [part for part in env[variable].split(os.pathsep) if part != bundle]
        if kept:
            env[variable] = os.pathsep.join(kept)
        else:
            env.pop(variable, None)
    return env


def _run(argv: Sequence[str], *, timeout: float = LIST_TIMEOUT) -> str:
    """Run *argv* and return stdout, or ``""`` for anything that went wrong.

    Never raises. A provider that raised in ``discover()`` would cost the
    operator only its own sources — CLV guards the call — but it would also
    cost them the diagnosis, because "this plugin blew up" is not "podman is
    not installed".
    """

    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_environment(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


class ContainerReader:
    """Streams ``<runtime> logs --follow`` into CLV's reader contract."""

    #: Shown when the source restarts underneath the viewer. The ``{name}``
    #: placeholder is filled in by CLV.
    RELOAD_NOTICE = "{name} restarted."

    def __init__(
        self,
        path: SourceRef,
        *,
        runtime: str,
        max_lines: int = DEFAULT_LINES,
        spawn: Any = subprocess.Popen,
    ) -> None:
        self.path = path
        self._runtime = runtime
        self._max_lines = max_lines
        self._spawn = spawn
        self._process: Any = None
        self._remainder = ""
        self._offset = 0

    @property
    def offset(self) -> int:
        return self._offset

    def command(self) -> list[str]:
        """The argv this reader would run.

        A method rather than an inline list so a test can assert the argv
        without spawning anything — which is how the shipped journald provider
        is tested, and the only way to check a command on a machine that does
        not have the tool.
        """

        return [
            self._runtime,
            "logs",
            "--follow",
            f"--tail={self._max_lines}",
            container_name(self.path),
        ]

    def prime(self) -> TailRead:
        self.close()
        self._process = self._spawn(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=child_environment(),
        )
        self._remainder = ""
        self._offset = 0
        stdout = getattr(self._process, "stdout", None)
        if stdout is not None:
            try:
                os.set_blocking(stdout.fileno(), False)
            except (OSError, AttributeError, ValueError):
                # A fake process in a test, or a platform without it. The drain
                # below copes with a blocking read returning everything at once.
                pass
        return TailRead(lines=self._drain(), offset=self._offset)

    def poll(self) -> TailRead:
        if self._process is None:
            return TailRead(lines=[], offset=self._offset)
        if self._process.poll() is not None:
            # The container stopped, or the runtime exited. Draining what is
            # left and closing is the honest end of the stream; re-running the
            # command every tick would be a fork bomb with a nice name.
            lines = self._drain()
            self.close()
            return TailRead(lines=lines, offset=self._offset)
        return TailRead(lines=self._drain(), offset=self._offset)

    def _drain(self) -> list[str]:
        """Whatever is buffered, split into complete lines.

        A partial last line is held back rather than emitted, because half a
        line rendered now and the other half rendered next tick is two wrong
        rows where there should be one right one.
        """

        stdout = getattr(self._process, "stdout", None)
        if stdout is None:
            return []
        try:
            chunk = stdout.read()
        except (BlockingIOError, ValueError):
            return []
        if not chunk:
            return []
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")

        text = self._remainder + chunk
        parts = text.split("\n")
        self._remainder = parts.pop()
        lines = [line.rstrip("\r") for line in parts]
        self._offset += len(lines)
        return lines

    def close(self) -> None:
        """Stop the subprocess. Safe to call twice, and on a dead process."""

        process, self._process = self._process, None
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
        except Exception:  # noqa: BLE001 - it may already be gone
            pass
        finally:
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    stdout.close()
                except Exception:  # noqa: BLE001
                    pass


def container_name(path: SourceRef) -> str:
    """The container a source identifier names.

    ``container/web-1`` -> ``web-1``. Read off ``name`` rather than by
    stripping a prefix from the string, so a ref type that spells itself
    differently still answers correctly.
    """

    return str(getattr(path, "name", "") or str(path).rsplit("/", 1)[-1])


class ContainerLogs(LogSourceProvider):
    """Offers every running container as a tailable source."""

    name = "container_logs"
    requires_api = ">=1.0,<2.0"

    def __init__(self, *, runner=None, spawn=subprocess.Popen) -> None:
        #: Injected so the container listing can be tested against captured
        #: output rather than against whatever the machine running the suite
        #: happens to have running.
        self._runner = runner or _run
        self._spawn = spawn
        self._settings: Optional[Mapping[str, str]] = None
        #: Why the last scan offered nothing, in the operator's terms. Read by
        #: nothing in CLV -- it is here because a provider that knows why it is
        #: empty and keeps it to itself is a provider nobody can debug.
        self.status = "disabled"

    # --- consent -------------------------------------------------------------

    def configure(self, settings: Mapping[str, str]) -> None:
        """Adopt ``[plugin:container_logs]``.

        The mapping is a **live view**, not a copy: CLV refreshes it behind this
        reference when the settings file is re-read, so holding it is what lets
        ``_enabled()`` below see a change with no restart. Copying the values
        out here would be the obvious thing and would pin this plugin to
        whatever the file said at startup.
        """

        self._settings = settings

    def _enabled(self) -> bool:
        if self._settings is None:
            # Constructed directly rather than loaded -- by a test, or by
            # anything else in process. No section means no consent.
            return False
        return setting_bool(self._settings, "enabled", False)

    def _runtime(self) -> str:
        """The named runtime, or the first one on PATH. ``""`` for none.

        ``shutil.which`` rather than trusting PATH at spawn time, because a
        PyInstaller bundle's PATH is not the shell's and a missing tool should
        be a sentence in the tree rather than an exception at open.
        """

        if self._settings is not None:
            named = str(self._settings.get("runtime", "")).strip()
            if named:
                return named if shutil.which(named) else ""
        for candidate in RUNTIMES:
            if shutil.which(candidate):
                return candidate
        return ""

    # --- the interface -------------------------------------------------------

    def discover(self) -> Iterable[ProviderSource]:
        """Every running container, or nothing at all when not opted in."""

        if not self._enabled():
            self.status = "disabled (set enabled = true in [plugin:container_logs])"
            return []

        runtime = self._runtime()
        if not runtime:
            self.status = f"no container runtime found (tried {', '.join(RUNTIMES)})"
            return []

        listing = self._runner(
            [runtime, "ps", "--format", "{{.Names}}"], timeout=LIST_TIMEOUT
        )
        names = [line.strip() for line in listing.splitlines() if line.strip()]
        if not names:
            self.status = f"{runtime}: no running containers"
            return []

        self.status = f"{runtime}: {len(names)} running"
        return [
            ProviderSource(
                path=Path(PREFIX) / name,
                label=name,
                provider=self.name,
            )
            for name in sorted(names)
        ]

    def open_reader(
        self, path: SourceRef, *, max_lines: int = DEFAULT_LINES, **kwargs: Any
    ) -> Optional[ContainerReader]:
        runtime = self._runtime()
        if not runtime:
            raise OSError(
                "No container runtime is available. Install podman or docker, "
                "or set runtime = <name> in [plugin:container_logs]."
            )
        return ContainerReader(
            path,
            runtime=runtime,
            max_lines=min(max_lines, DEFAULT_LINES),
            spawn=self._spawn,
        )

    def open(self, path: SourceRef) -> Iterator[str]:
        """The simple contract, for completeness.

        Never reached while :meth:`open_reader` returns a reader, which it
        always does. Implemented because the interface requires it, and because
        it is three lines once the reader exists.
        """

        reader = self.open_reader(path, max_lines=DEFAULT_LINES)
        try:
            yield from reader.prime().lines
        finally:
            reader.close()


def register() -> list[LogSourceProvider]:
    return [ContainerLogs()]
