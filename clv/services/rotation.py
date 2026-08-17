"""Grouping a rotated log into one source.

``app.log``, ``app.log.1`` and ``app.log.2.gz`` are three files and one log.
Presenting them as three unrelated sources makes the operator do the join by
hand — open each in turn, remember where the last one ended — which is exactly
the work a viewer should be doing. Grouped, a rotated set spans weeks in one
pane, which is something Event Viewer cannot do at all.

Two rules decide what this module does and does not promise:

* **Only the live head tails.** A member that has rotated out is finished;
  nothing will ever append to ``syslog.2.gz``. Older members are read once, at
  open, and never polled again.
* **The budget is shared, and spent newest-first.** ``max_buffer_lines`` caps
  the *set*, not each member. Members are read back from the head until the
  budget is met and then no further, so a set whose head already fills the
  buffer opens as fast as a single file and a twelve-member set does not cost
  twelve decompressions to show the last hour.

Recognised shapes, after any compression suffix is stripped: ``app.log.1``,
``app.log-20260811``, ``app.log.2026-08-11``. A gap in the numbering is fine —
the set is whatever is on disk, not whatever a sequence implies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .backend import LOCAL, SourceBackend
from .compressed import is_compressed, read_compressed_tail, strip_compression_suffix
from .reader import AnyReader, TailRead, open_reader, read_last_lines
from .refs import is_source_ref

#: ``app.log.1`` — the classic logrotate name. Higher is older.
_NUMERIC_RE = re.compile(r"^(?P<base>.+)\.(?P<index>\d+)$")

#: ``app.log-20260811`` and ``app.log.2026-08-11``, with either separator.
#: Later dates are newer, which is the opposite of the numeric ordering.
_DATE_RE = re.compile(r"^(?P<base>.+?)[.\-](?P<date>\d{8}|\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True, slots=True)
class RotatedMember:
    """One file in a rotated set."""

    path: Path
    #: Sorts newest-first within the set. The live head is ``(0, 0)``.
    rank: tuple[int, int]

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def live(self) -> bool:
        """Whether this is the member still being written to."""

        return self.rank == (0, 0)


@dataclass(frozen=True, slots=True)
class RotatedSet:
    """Several files presented as one logical source."""

    #: The name the set is known by — the head's name, with no rotation suffix.
    base: Path
    #: Newest first, so a budget spent from the front reads the newest history.
    members: tuple[RotatedMember, ...]

    @property
    def head(self) -> Path:
        """The member a tail would follow. The newest, live or not."""

        return self.members[0].path

    @property
    def name(self) -> str:
        return self.base.name

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(member.path for member in self.members)

    def __len__(self) -> int:
        return len(self.members)

    def __contains__(self, path: object) -> bool:
        return is_source_ref(path) and path in self.paths


def _rank(path: Path) -> tuple[Path, tuple[int, int]] | None:
    """Split *path* into the log it belongs to and its age within that log.

    Returns None when the name says nothing about rotation, which is the
    common case and has to stay cheap.
    """

    stem = strip_compression_suffix(path)
    name = stem.name

    match = _NUMERIC_RE.match(name)
    if match:
        try:
            index = int(match.group("index"))
        except ValueError:  # pragma: no cover - the group is \d+
            return None
        return stem.with_name(match.group("base")), (1, index)

    match = _DATE_RE.match(name)
    if match:
        digits = match.group("date").replace("-", "")
        # Negated so that a later date sorts *earlier* in the members tuple:
        # dates run the opposite way from logrotate's numbering.
        return stem.with_name(match.group("base")), (2, -int(digits))

    if is_compressed(path):
        # `app.log.gz` with no index at all: still a rotated-out copy of
        # `app.log`, and the only member of its generation.
        return stem, (1, 0)

    return None


def group_rotated(paths: Iterable[Path]) -> tuple[list[RotatedSet], list[Path]]:
    """Split *paths* into rotated sets and everything left over.

    A set needs at least two members: one file called ``app.log.1`` with no
    siblings is a file, and wrapping it in a set would only add a node to
    expand before reading it.
    """

    groups: dict[Path, list[RotatedMember]] = {}
    for path in paths:
        ranked = _rank(path)
        if ranked is None:
            # The live head of whatever it may turn out to be the head of.
            groups.setdefault(path, []).append(RotatedMember(path, (0, 0)))
            continue
        base, rank = ranked
        groups.setdefault(base, []).append(RotatedMember(path, rank))

    sets: list[RotatedSet] = []
    singles: list[Path] = []
    for base, members in groups.items():
        if len(members) < 2:
            singles.append(members[0].path)
            continue
        ordered = tuple(sorted(members, key=lambda member: (member.rank, str(member.path))))
        sets.append(RotatedSet(base=base, members=ordered))

    sets.sort(key=lambda item: str(item.base).lower())
    singles.sort(key=lambda item: str(item).lower())
    return sets, singles


class RotatedSetReader:
    """Reads a rotated set as one oldest-to-newest stream.

    Interface-compatible with ``SourceReader``: same ``path`` / ``prime`` /
    ``poll`` / ``RELOAD_NOTICE``, so nothing above this cares how many files
    are behind one source. What it adds is :attr:`TailRead.origins`, because
    with several files behind one source "which file is this line from" stops
    having a constant answer — and the status line has to be able to say.
    """

    RELOAD_NOTICE = "{name} was rotated; reloaded."

    def __init__(
        self,
        rotated_set: RotatedSet,
        *,
        max_lines: int,
        backend: SourceBackend = LOCAL,
        **kwargs,
    ) -> None:
        self.rotated_set = rotated_set
        #: The head, so everything keyed by path (starring, marks on the live
        #: member) keeps naming a real file.
        self.path = rotated_set.head
        self._max_lines = max_lines
        #: Every member of a set lives wherever the set does, so one backend
        #: answers for all of them -- unlike a merged view, which is the case
        #: that needs a resolver.
        self._backend = backend
        self._live: AnyReader = open_reader(
            rotated_set.head, max_lines=max_lines, backend=backend, **kwargs
        )
        #: How many members the last prime actually opened, and how many it
        #: could have. Opening a set is the one path that is not instant, so it
        #: has to be able to say what it did.
        self.members_read = 0

    @property
    def members_available(self) -> int:
        return len(self.rotated_set)

    @property
    def offset(self) -> int:
        return self._live.offset

    def prime(self) -> TailRead:
        """Read back from the head until the shared budget is met."""

        budget = self._max_lines
        collected: list[tuple[Path, list[str]]] = []
        truncated = False
        self.members_read = 0

        for index, member in enumerate(self.rotated_set.members):
            if budget <= 0:
                # Older than anything that would fit. Not read, not opened,
                # and — for a compressed member — not decompressed.
                truncated = True
                break
            if index == 0:
                # Only the head gets a persistent reader: it is the one that
                # will still be growing after this.
                result = self._live.prime()
                member_lines, member_truncated = result.lines, result.truncated
            else:
                member_lines, member_truncated = self._read_member(member.path, budget)
            self.members_read += 1
            truncated = truncated or member_truncated
            if not member_lines:
                continue
            member_lines = member_lines[-budget:]
            collected.append((member.path, member_lines))
            budget -= len(member_lines)

        lines: list[str] = []
        origins: list[Path] = []
        # Reversed: the members were walked newest-first to spend the budget,
        # but a log reads oldest-first.
        for path, member_lines in reversed(collected):
            lines.extend(member_lines)
            origins.extend([path] * len(member_lines))

        return TailRead(
            lines=lines,
            offset=self._live.offset,
            truncated=truncated,
            origins=tuple(origins),
        )

    def _read_member(self, path: Path, budget: int) -> tuple[list[str], bool]:
        """Read one rotated-out member's newest *budget* lines."""

        try:
            if is_compressed(path):
                text = read_compressed_tail(path, budget, backend=self._backend)
                return text.lines, text.truncated
            result = read_last_lines(path, budget, backend=self._backend)
            return result.lines, result.truncated
        except OSError:
            # One damaged member must not cost the rest of the history. The
            # file is still listed in the tree, where opening it directly
            # reports why it will not read.
            return [], True

    def poll(self) -> TailRead:
        """Tail the live head. Rotated-out members are finished."""

        result = self._live.poll()
        if not result.lines:
            return result
        return TailRead(
            lines=result.lines,
            offset=result.offset,
            truncated=result.truncated,
            rotated=result.rotated,
            origins=(self.path,) * len(result.lines),
        )

    def close(self) -> None:
        closer = getattr(self._live, "close", None)
        if closer is not None:
            closer()


def describe_set(rotated_set: RotatedSet, members_read: int) -> str:
    """What opening a set just did, for the notification."""

    total = len(rotated_set)
    if members_read >= total:
        return f"{rotated_set.name}: read {_plural(total, 'member', 'members')}."
    return (
        f"{rotated_set.name}: read {members_read} of {total} members — "
        "the buffer filled before the older ones were needed."
    )


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def sets_by_path(sets: Sequence[RotatedSet]) -> dict[Path, RotatedSet]:
    """Index every member path to the set it belongs to."""

    return {path: item for item in sets for path in item.paths}


__all__ = [
    "RotatedMember",
    "RotatedSet",
    "RotatedSetReader",
    "describe_set",
    "group_rotated",
    "sets_by_path",
]
