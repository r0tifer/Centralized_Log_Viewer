"""Edit ``settings.conf`` in place, without losing the operator's comments.

``configparser`` reads this file; nothing writes it back through
``configparser``, because :meth:`ConfigParser.write` discards every comment and
every blank line in the file. The settings file is the operator's — the shipped
one is two thirds prose explaining what each option does — so it is *edited*
rather than regenerated, one line at a time, leaving every byte this module was
not asked to touch exactly where it was.

**Sections are the point.** ``sources.persist_setting`` and
``sources.persist_log_sources`` used to scan the whole file with no notion of
which ``[section]`` a line belonged to, which was harmless while ``[log_viewer]``
was the only section that ever existed. Once one real ``[ssh:<name>]`` section is
present it stops being harmless in two directions:

* an option the file does not already carry was appended at **end of file**,
  which is inside the last host section. ``enable_ssh`` written there is read by
  nothing — ``config._parse_host`` ignores it and it is not a refused key, so
  there is not even a warning. The switch flips, reports success, and is off
  again on the next launch.
* ``log_dirs`` is a key in *both* schemas. A ``[log_viewer]`` that relies on the
  default plus any ``[ssh:web01]`` meant ``Ctrl+S`` rewrote **the remote host's**
  directories with paths from this machine.

Every operation here is therefore scoped to a named section, and an append lands
before the next header rather than at the end of the file.

**Editing is key-level and never regenerates a section.** Only the keys the
caller names are replaced; every other line in the body — comments, blank lines,
options this version of CLV does not know about, and a refused ``password =``
that :mod:`clv.services.config` reports on every load — survives byte-identical.
That single rule is what keeps three separate promises at once: an operator's
comments are preserved, a refused key keeps producing its warning instead of
being silently eaten, and a section the parser *skipped* (a bad port means the
host never reaches ``LogConfig.hosts``) is not deleted by a UI that cannot see it.

**Removal never touches a line above the header.** The tempting rule — "a comment
block directly above a header with no blank line between them is a note about
that section, so it goes too" — is rejected. The shipped ``settings.conf`` ends
inside a commented-out ``# [ssh:db02]`` example with no blank line after it, so
appending one real section is enough to make that example look like a note
belonging to the new host, and removing the host would delete documentation the
operator never wrote and cannot get back. Leaving an orphaned comment behind is
untidy; deleting the wrong thing is not recoverable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

#: ``configparser``'s own defaults, taken from the parser CLV actually
#: constructs, so what this module skips as a comment and what the parser skips
#: as a comment cannot drift apart.
COMMENT_PREFIXES = ("#", ";")

#: The section every global option lives in. Named here rather than imported
#: from :mod:`clv.services.config` so this module stays a text editor with no
#: opinion about the schema.
DEFAULT_SECTION = "log_viewer"


@dataclass(frozen=True, slots=True)
class SectionSpan:
    """Where one ``[name]`` section sits in a list of lines.

    ``end`` is one past the last body line that is neither blank nor a comment,
    so a trailing run of blank lines and comments before the next header belongs
    to whatever comes after rather than to this section. That is what makes an
    insert land under the section's real content and a removal stop short of the
    next section's banner.
    """

    name: str
    header: int
    start: int
    end: int


def _header_name(line: str) -> Optional[str]:
    """``"[ssh:web01]"`` -> ``"ssh:web01"``, or ``None`` for anything else."""

    stripped = line.strip()
    if len(stripped) < 2 or not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    return stripped[1:-1].strip()


def _is_comment(line: str) -> bool:
    return line.strip().startswith(COMMENT_PREFIXES)


def _option_name(line: str) -> Optional[str]:
    """The key a body line assigns, or ``None`` if it does not assign one.

    ``=`` only, matching the two functions this module replaces. ``configparser``
    also accepts ``:``, but supporting it here would change what an existing file
    means rather than only where new text lands, and this module's whole job is
    to move nothing it was not asked to move.
    """

    stripped = line.strip()
    if not stripped or _is_comment(stripped) or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _leading(line: str) -> str:
    """The line's indentation, so a rewritten option keeps it."""

    return line[: len(line) - len(line.lstrip())]


def spans_of(lines: Sequence[str]) -> list[SectionSpan]:
    """Every section in *lines*, in file order, duplicates included."""

    headers = [
        (index, name)
        for index, line in enumerate(lines)
        if (name := _header_name(line)) is not None
    ]
    found: list[SectionSpan] = []
    for position, (index, name) in enumerate(headers):
        limit = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        end = limit
        while end > index + 1 and (
            not lines[end - 1].strip() or _is_comment(lines[end - 1])
        ):
            end -= 1
        found.append(SectionSpan(name=name, header=index, start=index + 1, end=end))
    return found


class SettingsDocument:
    """A settings file held as lines, edited in place, written once.

    The dialog makes several edits per confirm — add a host, rename another,
    remove a third — and a function that re-read and re-wrote the file per edit
    would multiply the window in which a concurrent write is lost, for no gain.
    One :meth:`load`, any number of mutations, one :meth:`save`.
    """

    def __init__(self, lines: Optional[Sequence[str]] = None) -> None:
        self._lines: list[str] = list(lines) if lines is not None else []

    # --- construction --------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "SettingsDocument":
        """Read *path*, or start the file the two persist helpers always did.

        A missing file becomes ``[log_viewer]`` and a blank line, which is what
        ``persist_setting`` seeded before this module existed. ``OSError``
        propagates: every caller here reports it to whoever pressed the button.
        """

        if not path.exists():
            return cls([f"[{DEFAULT_SECTION}]", ""])
        return cls(path.read_text(encoding="utf-8").splitlines())

    # --- reading -------------------------------------------------------------

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def sections(self) -> list[str]:
        """Every section name, in file order."""

        return [span.name for span in spans_of(self._lines)]

    def _span(self, section: str) -> Optional[SectionSpan]:
        """The *first* span named *section*.

        First rather than last because a duplicate section is already reported by
        ``config._read_parser``, and editing the copy the operator can see at the
        top of their file is the less surprising of two bad answers.
        """

        for span in spans_of(self._lines):
            if span.name == section:
                return span
        return None

    def has_section(self, section: str) -> bool:
        return self._span(section) is not None

    def get(self, section: str, option: str) -> Optional[str]:
        """The raw value of *option* within *section*, unstripped of nothing."""

        span = self._span(section)
        if span is None:
            return None
        for index in range(span.start, span.end):
            if _option_name(self._lines[index]) == option:
                return self._lines[index].partition("=")[2].strip()
        return None

    # --- writing -------------------------------------------------------------

    def set(self, section: str, option: str, value: str) -> None:
        """Set ``option = value`` inside *section*, creating either if absent.

        An existing option is rewritten where it stands, keeping its indentation
        and its position among the operator's comments. A new one is inserted
        after the section's last real content line rather than at end of file,
        which is the whole reason this module exists.
        """

        span = self._span(section)
        if span is None:
            self.add_section(section, [(option, value)])
            return

        for index in range(span.start, span.end):
            if _option_name(self._lines[index]) == option:
                self._lines[index] = f"{_leading(self._lines[index])}{option} = {value}"
                return

        self._lines.insert(span.end, f"{option} = {value}")

    def remove_option(self, section: str, option: str) -> bool:
        """Delete *option* from *section*. Returns whether there was one."""

        span = self._span(section)
        if span is None:
            return False
        for index in range(span.start, span.end):
            if _option_name(self._lines[index]) == option:
                del self._lines[index]
                return True
        return False

    def add_section(self, section: str, options: Iterable[tuple[str, str]] = ()) -> None:
        """Append ``[section]`` with *options* at end of file.

        Separated from whatever precedes it by exactly one blank line, so an
        appended host never opens on the last line of a comment block.
        """

        if self._lines and self._lines[-1].strip():
            self._lines.append("")
        self._lines.append(f"[{section}]")
        self._lines.extend(f"{key} = {value}" for key, value in options)

    def remove_section(self, section: str) -> bool:
        """Delete every span named *section*, header included.

        Every span, not the first: a file with two ``[ssh:web01]`` headers means
        one host to ``configparser``, so removing that host has to remove both or
        the one left behind resurrects it on the next load.

        Bottom up, so an earlier span's indices are still valid when it is
        reached. Nothing above a header is ever touched — see the module
        docstring for what that rule is protecting.
        """

        matches = [span for span in spans_of(self._lines) if span.name == section]
        if not matches:
            return False
        for span in reversed(matches):
            del self._lines[span.header : span.end]
        return True

    # --- output --------------------------------------------------------------

    def text(self) -> str:
        """The file's contents, always newline-terminated."""

        payload = "\n".join(self._lines)
        if not payload.endswith("\n"):
            payload += "\n"
        return payload

    def save(self, path: Path) -> None:
        """Write through a sibling temp file, then ``os.replace`` it.

        The same technique as ``StateStore.save`` and ``export.write_atomically``,
        and load-bearing here for a reason neither of those has: a truncated
        write to this file costs the operator every host they have configured and
        every comment they have written, and the window is exactly the moment
        they pressed Save in a dialog.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.clv-tmp")
        try:
            temp.write_text(self.text(), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
