"""Field-name completions for the query input.

Item 8 gives the query box a vocabulary — the field names the current source
actually reports — and a vocabulary nobody can see is a vocabulary nobody uses.
This is the dropdown that shows it.

It is an ``OptionList``, which already knows how to be a scrollable, keyboard
driven list, plus three behaviours a plain one does not have:

* it hides itself when it has nothing to offer, so it costs no rows in the
  common case (``height: auto`` over an empty list is still a border);
* ``escape`` dismisses instead of bubbling, because in this app ``escape``
  clears the query and closing a dropdown must never do that;
* it reports the chosen **name**, not an option index, so the query bar can
  rewrite the token under the caret without knowing how the list was built.

The widget never reads the query itself. :class:`~clv.widgets.query_bar.QueryBar`
decides when there is a partial key at the caret, hands over the candidates,
and performs the edit — which fires ``Input.Changed`` exactly as typing does,
so the app re-renders through the path it already has.
"""

from __future__ import annotations

from typing import Sequence

from textual import events
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

#: Most candidates offered at once. The list scrolls, but a dropdown taller
#: than this pushes the log pane down far enough to be its own annoyance.
MAX_CANDIDATES = 8


class FieldCompletions(OptionList):
    """Dropdown of field names matching what is being typed."""

    DEFAULT_CSS = """
    FieldCompletions {
        height: auto;
        max-height: 5;
        margin-bottom: 1;
        border: tall $surface 25%;
        background: $surface 8%;
    }

    FieldCompletions.-hidden { display: none; }

    FieldCompletions:focus {
        border: tall $accent 50%;
    }
    """

    class Accepted(Message):
        """A field name was chosen."""

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    class Dismissed(Message):
        """The list was closed without choosing anything."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._names: list[str] = []
        self.add_class("-hidden")
        self.can_focus = True

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._names)

    @property
    def open(self) -> bool:
        return not self.has_class("-hidden")

    def offer(self, names: Sequence[str]) -> None:
        """Show *names*, or hide when there are none."""

        candidates = list(names[:MAX_CANDIDATES])
        if candidates == self._names:
            self.set_class(not candidates, "-hidden")
            return
        self._names = candidates
        self.clear_options()
        if not candidates:
            self.add_class("-hidden")
            return
        self.add_options([Option(f"{name}:", id=name) for name in candidates])
        self.remove_class("-hidden")
        self.highlighted = 0

    def close(self) -> None:
        """Hide without emitting. The caller decides where focus goes."""

        self._names = []
        self.clear_options()
        self.add_class("-hidden")

    def accept_highlighted(self) -> bool:
        """Emit the highlighted name. False when there is nothing to emit."""

        index = self.highlighted
        if index is None or not (0 <= index < len(self._names)):
            return False
        self.post_message(self.Accepted(self._names[index]))
        return True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._names):
            self.post_message(self.Accepted(self._names[event.option_index]))

    async def on_key(self, event: events.Key) -> None:
        # Stopped here rather than left to bubble: `escape` is bound to
        # "clear the query", and closing a dropdown must not empty the box.
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(self.Dismissed())


__all__ = ["MAX_CANDIDATES", "FieldCompletions"]
