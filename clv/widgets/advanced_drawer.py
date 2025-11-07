from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Button, Input, Label, Static, Switch


class _LabelInputField(Static):
    """A field composed of a label and an input widget."""

    def __init__(self, label: str, placeholder: str) -> None:
        super().__init__(classes="field")
        self._label_text = label
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Label(self._label_text, classes="field-label")
        yield Input(placeholder=self._placeholder, classes="field-control")


class _LabelSwitchField(Static):
    """A field composed of a label and a switch widget."""

    def __init__(self, label: str, value: bool) -> None:
        super().__init__(classes="field")
        self._label_text = label
        self._value = value

    def compose(self) -> ComposeResult:
        yield Label(self._label_text, classes="field-label")
        yield Switch(value=self._value, classes="field-control")


class AdvancedFiltersDrawer(Static):
    DEFAULT_CSS = """
    AdvancedFiltersDrawer {
        border-top: solid $surface 15%;
        padding: 1 2;
        background: $surface 3%;
    }

    AdvancedFiltersDrawer.-hidden {
        display: none;
    }

    AdvancedFiltersDrawer .drawer-grid {
        layout: horizontal;
    }

    AdvancedFiltersDrawer .drawer-grid > * {  
        margin: 0 2 1 0;
    }

    AdvancedFiltersDrawer .drawer-grid > *:last-child {
        margin-right: 0;
    }

    AdvancedFiltersDrawer Input {
        border: tall $surface 20%;
        background: $surface 2%;
        width: 28;
        height: 3;
    }

    AdvancedFiltersDrawer .field {
        layout: vertical;
    }

    AdvancedFiltersDrawer .field > .field-control {
        margin-top: 1;
    }

    AdvancedFiltersDrawer .field > .field-label {
        height: 1;
    }

    AdvancedFiltersDrawer Switch {
        height: 3;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="advanced-drawer")
        self._visible = False
        self.add_class("-hidden")

    def compose(self) -> ComposeResult:
        with Container(classes="drawer-grid"):
            yield self._field("Exclude paths", "tmp/,node_modules")
            yield self._field("Source filters", "app/*.log")
            yield self._field("Max lines", "5000")
            yield self._toggle_field("Follow symlinks", True)
        with Container(classes="drawer-actions"):
            yield Button("Close", id="close-advanced")

    def _field(self, label: str, placeholder: str) -> Static:
        return _LabelInputField(label, placeholder)

    def _toggle_field(self, label: str, value: bool) -> Static:
        return _LabelSwitchField(label, value)

    def show(self) -> None:
        self.remove_class("-hidden")
        self._visible = True

    def hide(self) -> None:
        self.add_class("-hidden")
        self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible
