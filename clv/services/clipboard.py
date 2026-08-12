"""Assembling what `y` puts on the clipboard.

`Ctrl+L` copy mode strips the chrome so the terminal's own mouse selection grabs
log text only — which is exactly the mechanism that is unavailable over tmux,
screen and a plain SSH session, the headless case CLV exists for. OSC 52 asks
the terminal to set the *local* clipboard from the keyboard, so it works down a
remote connection with no dependency and no helper binary. The two are
complementary and both are kept.

Emitting the escape sequence is Textual's job (``App.copy_to_clipboard``); this
module owns the part that has to be right: how much text goes into it.

Why a size cap and not chunking
-------------------------------

OSC 52 has no continuation form. A second sequence *replaces* the clipboard
rather than appending to it, so a payload cannot be streamed in parts — and
terminals (and tmux, which caps a passthrough sequence at roughly 74 kB) drop or
truncate an oversized one, usually without saying anything. A configurable byte
cap is therefore the honest mechanism: truncate deliberately, at a line
boundary, and report exactly what was copied. A silent partial copy is the
failure this exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class CopyPayload:
    """The text to hand the terminal, plus what it cost to fit the cap."""

    text: str
    copied_lines: int
    dropped_lines: int

    @property
    def empty(self) -> bool:
        return self.copied_lines == 0

    @property
    def truncated(self) -> bool:
        return self.dropped_lines > 0

    @property
    def summary(self) -> str:
        """The notification text. Here rather than in the app so it is testable."""

        if self.empty:
            return "Nothing to copy."
        total = self.copied_lines + self.dropped_lines
        if self.truncated:
            return (
                f"Copied {self.copied_lines} of {total} lines — "
                f"clipboard cap reached, {self.dropped_lines} dropped."
            )
        if self.copied_lines == 1:
            return "Copied 1 line."
        return f"Copied {self.copied_lines} lines."


EMPTY_PAYLOAD = CopyPayload("", 0, 0)


def prepare_payload(lines: Sequence[str], *, max_bytes: int) -> CopyPayload:
    """Join *lines* into a clipboard payload no larger than *max_bytes*.

    Truncation keeps the **newest** lines — the pane is tail-oriented, and the
    end of a log is what an operator is copying when they hit the cap — and
    always cuts on a line boundary, never mid-line. The number of lines left
    behind is reported so the caller can say so out loud.
    """

    if not lines:
        return EMPTY_PAYLOAD

    kept: list[str] = []
    size = 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        # +1 for the newline that will join this line to the one after it.
        cost = len(line.encode("utf-8")) + (1 if kept else 0)
        if kept and size + cost > max_bytes:
            break
        if not kept and cost > max_bytes:
            # A single line over the cap: copying half a line is worse than
            # copying nothing, and the caller reports the miss.
            return CopyPayload("", 0, len(lines))
        kept.append(line)
        size += cost

    kept.reverse()
    return CopyPayload("\n".join(kept), len(kept), len(lines) - len(kept))


__all__ = ["EMPTY_PAYLOAD", "CopyPayload", "prepare_payload"]
