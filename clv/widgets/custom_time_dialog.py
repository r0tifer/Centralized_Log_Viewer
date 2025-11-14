from __future__ import annotations

from datetime import date, datetime, time

from textual import events
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class CustomTimeRangeDialog(ModalScreen[tuple[str, str]]):
    """Modal dialog that collects a custom time range."""

    DEFAULT_CSS = """
    CustomTimeRangeDialog {
        align: center middle;
    }

    #custom-time-dialog {
        width: 60;
        min-height: 18;
        padding: 2;
        layout: vertical;
        border: round $surface 25%;
        background: $surface 10%;
    }

    .dialog-section {
        margin-top: 1;
    }

    .dialog-section-first {
        margin-top: 0;
    }

    #dialog-title {
        text-style: bold;
        padding-bottom: 1;
    }

    #range-columns {
        layout: horizontal;
    }

    #start-column {
        margin-right: 1;
    }

    #end-column {
        margin-left: 1;
    }

    .range-column {
        layout: vertical;
        width: 1fr;
    }

    .field-label {
        color: $text-muted;
        margin-top: 1;
    }

    .field-label-first {
        margin-top: 0;
    }

    .field-input {
        margin-top: 1;
    }

    .field-input-first {
        margin-top: 0;
    }

    #custom-time-dialog Input {
        border: tall $surface 25%;
        background: $surface 8%;
        height: 3;
    }

    #dialog-error {
        color: $error;
        min-height: 1;
    }

    #dialog-actions {
        layout: horizontal;
        align: right middle;
        padding-top: 1;
    }

    #apply-custom-range {
        margin-left: 1;
    }

    #dialog-actions Button {
        height: 3;
        padding: 0 2;
    }
    """

    _TIME_FORMATS: tuple[str, ...] = (
        "%H:%M",
        "%H:%M:%S",
        "%I %p",
        "%I:%M %p",
        "%I:%M:%S %p",
        "%I%p",
        "%I:%M%p",
        "%I:%M:%S%p",
    )

    def __init__(self, *, initial_start: str = "", initial_end: str = "") -> None:
        super().__init__()
        self._initial_start = initial_start
        self._initial_end = initial_end

    def compose(self) -> ComposeResult:
        start_date_value, start_time_value = self._split_datetime(self._initial_start)
        end_date_value, end_time_value = self._split_datetime(self._initial_end)

        with Container(id="custom-time-dialog"):
            yield Label(
                "Custom Time Range",
                id="dialog-title",
                classes="dialog-section dialog-section-first",
            )
            with Container(id="range-columns", classes="dialog-section"):
                with Container(id="start-column", classes="range-column"):
                    yield Label(
                        "Start Date",
                        classes="field-label field-label-first",
                    )
                    yield Input(
                        value=start_date_value,
                        placeholder="2024-01-15",
                        id="start-date-input",
                        classes="field-input field-input-first",
                    )
                    yield Label("Start Time", classes="field-label")
                    yield Input(
                        value=start_time_value,
                        placeholder="14:30 or 2:30 PM",
                        id="start-time-input",
                        classes="field-input",
                    )
                with Container(id="end-column", classes="range-column"):
                    yield Label(
                        "End Date",
                        classes="field-label field-label-first",
                    )
                    yield Input(
                        value=end_date_value,
                        placeholder="2024-01-15",
                        id="end-date-input",
                        classes="field-input field-input-first",
                    )
                    yield Label("End Time", classes="field-label")
                    yield Input(
                        value=end_time_value,
                        placeholder="18:30 or 6:30 PM",
                        id="end-time-input",
                        classes="field-input",
                    )
            yield Static("", id="dialog-error", classes="dialog-section")
            with Container(id="dialog-actions", classes="dialog-section"):
                yield Button("Cancel", id="cancel-custom-range")
                yield Button("Apply", id="apply-custom-range", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#start-date-input", Input).focus()
        self._show_error(None)

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
        if event.input.id in {
            "start-date-input",
            "start-time-input",
            "end-date-input",
            "end-time-input",
        }:
            self._finalize()

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[override]
        if event.button.id == "cancel-custom-range":
            self.dismiss(None)
        elif event.button.id == "apply-custom-range":
            self._finalize()

    def _finalize(self) -> None:
        start_date_input = self.query_one("#start-date-input", Input)
        start_time_input = self.query_one("#start-time-input", Input)
        end_date_input = self.query_one("#end-date-input", Input)
        end_time_input = self.query_one("#end-time-input", Input)

        start_date_str = start_date_input.value.strip()
        start_time_str = start_time_input.value.strip()
        end_date_str = end_date_input.value.strip()
        end_time_str = end_time_input.value.strip()

        if not start_date_str or not start_time_str or not end_date_str or not end_time_str:
            self._show_error("All fields are required.")
            return

        try:
            start_date_value = self._parse_date(start_date_str)
        except ValueError:
            self._show_error("Start date must follow YYYY-MM-DD.")
            start_date_input.focus()
            return

        try:
            start_time_value = self._parse_time(start_time_str)
        except ValueError:
            self._show_error("Start time must be a valid 12 or 24 hour time.")
            start_time_input.focus()
            return

        try:
            end_date_value = self._parse_date(end_date_str)
        except ValueError:
            self._show_error("End date must follow YYYY-MM-DD.")
            end_date_input.focus()
            return

        try:
            end_time_value = self._parse_time(end_time_str)
        except ValueError:
            self._show_error("End time must be a valid 12 or 24 hour time.")
            end_time_input.focus()
            return

        start_dt = datetime.combine(start_date_value, start_time_value)
        end_dt = datetime.combine(end_date_value, end_time_value)

        if end_dt <= start_dt:
            self._show_error("End must be after start.")
            return

        self._show_error(None)
        start_formatted = self._format_datetime(start_dt)
        end_formatted = self._format_datetime(end_dt)
        self.dismiss((start_formatted, end_formatted))

    @staticmethod
    def _parse_date(raw: str) -> date:
        return datetime.strptime(raw, "%Y-%m-%d").date()

    @classmethod
    def _parse_time(cls, raw: str) -> time:
        cleaned = raw.strip().upper().replace(".", "")
        cleaned = " ".join(cleaned.split())
        if cleaned.endswith(("AM", "PM")) and len(cleaned) > 2:
            prefix, suffix = cleaned[:-2], cleaned[-2:]
            if prefix and not prefix.endswith(" "):
                cleaned = f"{prefix} {suffix}"
        for fmt in cls._TIME_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).time()
            except ValueError:
                continue
        raise ValueError

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        if value.second:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _split_datetime(raw: str) -> tuple[str, str]:
        text = raw.strip()
        if not text:
            return "", ""
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return "", ""
        date_part = parsed.strftime("%Y-%m-%d")
        time_part = parsed.strftime("%H:%M:%S" if parsed.second else "%H:%M")
        return date_part, time_part

    def _show_error(self, message: str | None) -> None:
        error = self.query_one("#dialog-error", Static)
        if message:
            error.update(message)
            error.visible = True
        else:
            error.update("")
            error.visible = False
