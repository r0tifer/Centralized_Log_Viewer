"""Read OpenSSH's ``~/.ssh/config`` so its machines can be offered for import.

**A reader, never a writer.** That file belongs to OpenSSH and to the operator;
CLV parses it once, when asked, to answer "which machines do you already have
names for?" and never writes a byte back. It lives in ``services/`` rather than
in :mod:`clv.services.config` because that module owns *CLV's* schema, and this
one is a guest in somebody else's.

**Only the alias is imported.** ``HostName``, ``User``, ``Port`` and
``IdentityFile`` are read for the picker to show and are deliberately not
persisted. CLV connects by running the system ``ssh`` binary, which reads this
same file itself — so writing ``host = 10.0.0.9`` into ``settings.conf`` would
stop ``ssh`` matching the ``Host`` block and silently lose ``ProxyJump``,
per-host keys, ``known_hosts`` and everything else CLV does not parse. Keeping
the alias as the destination is what makes "inherits ``~/.ssh/config``
wholesale" true rather than aspirational.

That also settles :data:`clv.services.config._REFUSED_HOST_KEYS` structurally
instead of by a blocklist: :class:`SSHConfigHost` has no field a password, a
passphrase or a sudo flag could land in, however the file spells them.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional

from .config import (
    RemoteHost,
    validate_host_name,
    validate_identity_file,
    validate_port,
    validate_remote_dirs,
)

#: Where OpenSSH keeps the per-user file. Expanded at call time, never at
#: import time — see :func:`ssh_config_path`.
DEFAULT_SSH_CONFIG = "~/.ssh/config"

#: How deep ``Include`` may nest before the scan gives up and says so. A
#: self-including file is a typo, not a reason to raise RecursionError.
MAX_INCLUDE_DEPTH = 8

#: What an imported host gets for ``log_dirs``, matching the shipped settings
#: template. The picker shows it and lets it be edited before anything is
#: written, because this is the one field OpenSSH cannot answer for us.
DEFAULT_LOG_DIRS = "/var/log"

#: ``Key value``, ``Key=value`` and ``Key = value`` are all one directive.
_DIRECTIVE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*(?:=\s*|\s+)(.*)$")

#: The four keys worth showing. Lower-cased, because ssh_config(5) keywords are
#: case-insensitive.
_INTERESTING = ("hostname", "user", "port", "identityfile")


@dataclass(frozen=True, slots=True)
class SSHConfigHost:
    """One machine an operator has already named, as the file describes it."""

    name: str
    hostname: Optional[str] = None
    user: Optional[str] = None
    port: Optional[int] = None
    identity_file: Optional[Path] = None
    identity_warning: Optional[str] = None
    #: The other literal patterns sharing this ``Host`` line. Shown, not
    #: imported — see :func:`_finish_block`.
    also: tuple[str, ...] = ()
    source: Optional[Path] = None

    def summary(self) -> str:
        """A one-line description for the picker: where this alias points."""

        where = self.hostname or self.name
        if self.port is not None and self.port != 22:
            where = f"{where}:{self.port}"
        parts = [where]
        if self.user:
            parts.append(self.user)
        if self.also:
            parts.append("also " + ", ".join(self.also))
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class SSHConfigScan:
    """Everything one scan found, plus what it could not make sense of.

    ``notes`` are finished sentences rather than codes: they are shown verbatim
    in the picker's hint, which is the only place they are ever read.
    """

    path: Path
    hosts: tuple[SSHConfigHost, ...] = ()
    notes: tuple[str, ...] = ()

    def without(self, names: Iterable[str]) -> "SSHConfigScan":
        """The same scan minus aliases already spoken for, keeping the notes."""

        taken = set(names)
        return replace(
            self, hosts=tuple(host for host in self.hosts if host.name not in taken)
        )


def ssh_config_path() -> Path:
    """Where to look, resolved **now**.

    Called rather than stored as a module constant so that a test which moves
    ``HOME`` moves this too. The autouse ``isolated_environment`` fixture does
    exactly that, and a path expanded at import time would sail straight past it
    into the developer's real configuration.
    """

    return Path(DEFAULT_SSH_CONFIG).expanduser()


def read_ssh_config(path: Optional[Path] = None) -> SSHConfigScan:
    """Scan *path* (default ``~/.ssh/config``) for machines worth importing.

    Never raises. A missing file, an unreadable include and a cycle are each a
    note on the result, because this runs behind a button an operator pressed
    out of curiosity and a traceback is not an answer to "what have I got?".
    """

    target = ssh_config_path() if path is None else Path(path)
    state = _ScanState()
    _parse_file(target, state, depth=0)
    _finish_block(state)
    if state.matches:
        state.notes.append(
            f"{state.matches} Match block(s) skipped: a Match is conditional and "
            "has no alias to name a host after."
        )
    return SSHConfigScan(target, tuple(state.hosts), tuple(state.notes))


def as_remote_host(
    entry: SSHConfigHost, log_dirs: str, existing: Iterable[str] = ()
) -> tuple[Optional[RemoteHost], Optional[str]]:
    """``(host, complaint)`` for importing *entry* with *log_dirs*.

    Both halves of the validation are the ones ``settings.conf`` itself uses, so
    an import cannot produce a section the config parser would then refuse. In
    particular the empty-``log_dirs`` refusal here is the *same* rule
    ``config._parse_host`` applies after the write — applied before it instead,
    which is the difference between a complaint in the dialog and a host that
    silently never appears.
    """

    problem = validate_host_name(entry.name, existing)
    if problem is not None:
        return None, problem

    dirs, complaints = validate_remote_dirs(log_dirs)
    if complaints:
        return None, complaints[0]
    if not dirs:
        return None, (
            f"{entry.name} needs at least one log directory on the remote host, "
            "such as /var/log."
        )

    # `host` is the alias on purpose, and nothing else is carried over. See the
    # module docstring: the alias is what lets `ssh` apply the operator's own
    # block. `host_options` omits `host` when it equals `name`, so the written
    # section is two lines.
    return RemoteHost(name=entry.name, host=entry.name, log_dirs=dirs), None


# --- parsing ----------------------------------------------------------------


class _ScanState:
    """Mutable working area for one scan. Private; never leaves this module."""

    def __init__(self) -> None:
        self.hosts: list[SSHConfigHost] = []
        self.notes: list[str] = []
        self.visited: set[Path] = set()
        self.matches = 0
        # The block being read: its literal patterns, and the keys seen so far.
        self.patterns: list[str] = []
        self.values: dict[str, str] = {}
        self.origin: Optional[Path] = None
        # True inside a Match block, whose keys belong to nothing importable.
        self.skipping = False


def _tokenize(rest: str) -> list[str]:
    """Split a directive's argument, honouring double quotes.

    Hand-rolled rather than :mod:`shlex`, which treats a backslash as an escape
    and would eat the separators in a Windows ``IdentityFile`` path.
    """

    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    for character in rest.strip():
        if character == '"':
            quoted = not quoted
            continue
        if character.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(character)
    if current:
        tokens.append("".join(current))
    return tokens


def _is_pattern(token: str) -> bool:
    """True for a token that matches hosts rather than naming one.

    ``Host *`` and ``Host web?`` describe a set, and ``!bastion`` describes an
    exclusion. None of the three is a machine, and none gives a name to put in
    ``[ssh:<name>]``.
    """

    return token.startswith("!") or "*" in token or "?" in token


def _split_keyword(line: str) -> Optional[tuple[str, str]]:
    """``(lowercased keyword, verbatim argument)``, or None for a non-directive.

    A ``#`` only starts a comment at the beginning of a line (after whitespace).
    Mid-line it stays part of the argument, because the ``ssh`` binary that
    actually connects treats it literally — and disagreeing with the program
    doing the work would be a worse bug than an odd-looking identity path.
    """

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _DIRECTIVE.match(line)
    if match is None:
        return None
    return match.group(1).lower(), match.group(2).strip()


def _finish_block(state: _ScanState) -> None:
    """Turn the block just ended into a candidate, if it named a machine."""

    patterns, values, origin = state.patterns, state.values, state.origin
    state.patterns, state.values = [], {}
    if not patterns:
        return

    port, complaint = validate_port(values.get("port", ""))
    if complaint is not None:
        state.notes.append(f"{patterns[0]}: {complaint}")
        port = None

    identity: Optional[Path] = None
    identity_warning: Optional[str] = None
    if values.get("identityfile"):
        identity, identity_warning = validate_identity_file(values["identityfile"])

    state.hosts.append(
        SSHConfigHost(
            name=patterns[0],
            hostname=values.get("hostname"),
            user=values.get("user"),
            port=port,
            identity_file=identity,
            identity_warning=identity_warning,
            also=tuple(patterns[1:]),
            source=origin,
        )
    )


def _expand_include(argument: str, base_dir: Path) -> list[Path]:
    """The files an ``Include`` names, sorted, globs expanded.

    A relative argument resolves against the *including file's* directory, which
    is what makes the near-universal ``Include conf.d/*`` work from ``~/.ssh``.
    """

    found: list[Path] = []
    for token in _tokenize(argument):
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        matches = sorted(glob.glob(str(candidate)))
        found.extend(Path(match) for match in matches)
    return found


def _parse_file(path: Path, state: _ScanState, depth: int) -> None:
    """Read one file into *state*, following ``Include`` inline.

    Inline, so an ``Include`` inside a ``Host`` block continues that block —
    which is what OpenSSH does, and the reason this is not a pre-pass.
    """

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in state.visited:
        state.notes.append(f"Skipped {path}: it is included more than once.")
        return
    state.visited.add(resolved)

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        if depth == 0:
            state.notes.append(f"No SSH configuration at {path}.")
        else:
            state.notes.append(f"Could not read {path}: it does not exist.")
        return
    except OSError as exc:
        # One bad include may not cost the rest of the scan.
        state.notes.append(f"Could not read {path}: {exc.strerror or exc}.")
        return

    for line in lines:
        directive = _split_keyword(line)
        if directive is None:
            continue
        keyword, argument = directive

        if keyword == "host":
            _finish_block(state)
            state.skipping = False
            state.origin = path
            state.patterns = [
                token for token in _tokenize(argument) if not _is_pattern(token)
            ]
            state.values = {}
            continue

        if keyword == "match":
            _finish_block(state)
            state.skipping = True
            state.matches += 1
            continue

        if keyword == "include":
            if depth >= MAX_INCLUDE_DEPTH:
                state.notes.append(
                    f"Stopped at {path}: Include is nested more than "
                    f"{MAX_INCLUDE_DEPTH} deep."
                )
                continue
            targets = _expand_include(argument, path.expanduser().parent)
            if not targets:
                state.notes.append(f"Include {argument!r} in {path} matched no files.")
            for target in targets:
                _parse_file(target, state, depth + 1)
            continue

        if state.skipping or not state.patterns:
            # Keys before the first Host, or inside a Match, belong to no
            # importable machine. Keys in a *wildcard* block land here too,
            # because `state.patterns` is empty for one — CLV does not emulate
            # OpenSSH's "first obtained value wins" across blocks, and does not
            # need to: the import keeps the alias, so `ssh` applies `Host *`
            # itself at connect time. A second, drifting opinion about that
            # would be worse than none.
            continue

        # First occurrence wins, which is OpenSSH's rule for every keyword.
        if keyword in _INTERESTING and keyword not in state.values:
            # Through the tokenizer, not stored raw: these keys take a single
            # argument that may be quoted, and `HostName "q.example"` must reach
            # the picker as a hostname rather than as one with quotes in it.
            tokens = _tokenize(argument)
            if tokens:
                state.values[keyword] = tokens[0]


__all__ = [
    "DEFAULT_LOG_DIRS",
    "DEFAULT_SSH_CONFIG",
    "MAX_INCLUDE_DEPTH",
    "SSHConfigHost",
    "SSHConfigScan",
    "as_remote_host",
    "read_ssh_config",
    "ssh_config_path",
]
