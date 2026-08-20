"""What the structured switch will and will not pretty-print.

Every detector here is a heuristic standing in for a parse, and each one is a
liability: a false positive buries three real log entries under a table or a
panel of nothing. So most of this file is the *refusals*, and the corpora are
kept as regression records rather than as illustrations — the CSV lines below
are verbatim from a remote `auth.log` that this code once mangled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from clv.app import LogViewerApp
from clv.widgets import payloads


def _preview(payload: str, *, rows: int = 20, cols: int = 10):
    return payloads.preview(
        payload, theme="ansi_dark", csv_max_rows=rows, csv_max_cols=cols
    )


def _label(payload: str) -> str | None:
    result = _preview(payload)
    return None if result is None else result[1]


# --- the CSV preview refuses prose ------------------------------------------

_NOT_CSV = [
    # The three from a real remote `auth.log`, which drew a two-column table
    # each: the only delimiter is the comma in "failed, status 22".
    "ip-172-31-25-196 sshd[1129735]: AuthorizedKeysCommand "
    "/usr/share/ec2-instance-connect/eic_run_authorized_keys ubuntu "
    "SHA256:+eMeAed1cROUGW1ZzeQReeBNfgJ/FNNRpSfA5ptx3Zk failed, status 22",
    "Connection closed by 34.52.153.82 port 8476, preauth",
    "pam_unix(cron:session): session opened for user root(uid=0), by root(uid=0)",
    # Prose with enough commas to clear the delimiter count.
    "Started sshd, nginx, cron, and dbus units",
    # Ragged rows are text that happens to contain commas.
    "a,b,c\nd,e",
]

_IS_CSV = [
    "col1,col2\n1,2\n3,4",                        # the ordinary case
    "alice,30,nyc",                               # one row, but clean cells
    "name,city\nJohn Smith,New York\nJane Roe,Boston",  # spaces, structure carries it
    '"John Smith","New York, NY",42',             # quoted: the writer said so
]


@pytest.mark.parametrize("payload", _NOT_CSV)
def test_prose_with_a_comma_is_not_a_csv_preview(payload: str) -> None:
    """A log line is not a spreadsheet.

    The guard was `"," not in payload`, so any sentence with a comma became a
    table — three of them at once in a remote `auth.log`, each burying real
    entries under five rows of nothing. Its siblings both demand a structural
    opener (`{`, `[`, `<`); these are CSV's, which has none to demand.
    """

    assert _preview(payload) is None


@pytest.mark.parametrize("payload", _IS_CSV)
def test_real_csv_still_previews(payload: str) -> None:
    """The other half, so the fix is a narrowing and not a removal."""

    assert _label(payload) == "CSV preview"


def test_csv_formatter_respects_limits() -> None:
    result = _preview("col1,col2\n1,2\n3,4\n5,6", rows=2, cols=2)

    assert result is not None
    table, label = result
    assert isinstance(table, Table)
    assert label == "CSV preview"
    assert len(table.columns) == 2
    assert len(table.rows) == 2


# --- the detectors are ordered, and the order is load-bearing ---------------

def test_json_is_tried_before_css() -> None:
    """Both open with a brace. A parse is a proof, so the parse goes first."""

    assert _label('{"a": 1, "b": 2}') == "JSON"


def test_a_stylesheet_is_not_a_csv_preview() -> None:
    """The reason CSS is tried before CSV, in one line.

    `.btn{font-family:Helvetica,Arial,sans-serif}` has two commas, one row,
    three fields, no quotes and no space in any cell — it clears *every* guard
    in `_csv`. Ordering is what stops a table being drawn over a stylesheet;
    no additional CSV rule would have caught it.
    """

    assert _label(".btn{font-family:Helvetica,Arial,sans-serif}") == "CSS"


def test_minidom_cannot_parse_real_html() -> None:
    """Why HTML needs its own path instead of borrowing XML's proof.

    Void elements, optional end tags and bare attributes are all legal HTML and
    all fatal to an XML parser, so `_xml` declines and `_html` has to decide on
    structure alone.
    """

    from xml.dom import minidom

    for markup in (
        '<div class="a"><br>text</div>',
        "<ul><li>one<li>two</ul>",
        '<head><meta charset="utf-8"></head>',
    ):
        with pytest.raises(Exception):
            minidom.parseString(markup)
        assert _label(markup) == "HTML"


# --- CSS: the guards, and what they refuse ----------------------------------

_NOT_CSS = [
    "Java{name:bob}",                       # a struct dump: no selector shape
    "Java{name:bob;age:3}",                 # the hardened struct dump
    "Config{host:localhost;port:5432}",     # ditto, with a colon in the value
    "com.acme.User{name:bob}",              # an interior dot is not a selector
    "com.acme.Order{id:91;total:4200}",
    "{color:red;padding:0}",                # opens with a block: JSON's leftovers
    "@media screen{.a{color:red}}",         # nesting is refused, not flattened
    "body{margin:0;padding:0}",             # a real miss, and an accepted one
]

_IS_CSS = [
    ".btn{color:red;padding:0}",
    "#main .row > td{border:0;margin:0}",
    "div > span{color:red}",                # a combinator, with no leading sigil
    "@keyframes spin{from:0;to:1}",
]


@pytest.mark.parametrize("payload", _NOT_CSS)
def test_a_struct_dump_is_not_a_stylesheet(payload: str) -> None:
    assert _preview(payload) is None


@pytest.mark.parametrize("payload", _IS_CSS)
def test_real_css_previews(payload: str) -> None:
    assert _label(payload) == "CSS"


def test_a_quoted_semicolon_does_not_split_a_declaration() -> None:
    """`content:"a;b"` is one declaration, not two."""

    result = _preview('.q{content:"a;b";color:red}')

    assert result is not None
    assert result[1] == "CSS"
    # Re-broken one declaration per line, with the quoted value intact.
    assert 'content:"a;b";' in result[0].code


def test_minified_css_is_re_broken() -> None:
    """A stylesheet arrives in a log as one long line or not at all."""

    result = _preview(".btn{color:red;padding:0}")

    assert result is not None
    assert result[0].code.splitlines() == [".btn {", "  color:red;", "  padding:0;", "}"]


# --- HTML: the guards, and what they refuse ---------------------------------

_NOT_HTML = [
    "<stdin>: line 3",                          # one bracket, no document
    "</dev/null> failed",                       # a closing bracket, one tag
    "<134>1 2026-08-07T09:25:01Z host app 1 - - msg",   # RFC 5424 priority
    "a < b and b > c",                          # arithmetic
]

_IS_HTML = [
    '<div class="a"><br>text</div>',
    "<ul><li>one<li>two</ul>",
    "<!DOCTYPE html><html><head><title>502 Bad Gateway</title><br></html>",
]


@pytest.mark.parametrize("payload", _NOT_HTML)
def test_angle_brackets_are_not_html(payload: str) -> None:
    assert _preview(payload) is None


@pytest.mark.parametrize("payload", _IS_HTML)
def test_real_html_previews(payload: str) -> None:
    assert _label(payload) == "HTML"


def test_html_is_never_re_indented() -> None:
    """Reflowing markup means ruling on significant whitespace. Not here."""

    markup = '<div class="a"><br>text</div>'
    result = _preview(markup)

    assert result is not None
    assert result[0].code == markup


# --- bounds and themes -------------------------------------------------------

def test_an_enormous_payload_is_not_previewed() -> None:
    """Past the cap the pretty-print is itself a wall of text."""

    payload = "[" + ",".join(str(n) for n in range(10_000)) + "]"
    assert len(payload) > payloads.PAYLOAD_MAX_CHARS
    assert _preview(payload) is None


def test_an_empty_payload_is_not_previewed() -> None:
    assert _preview("   ") is None


def test_the_syntax_theme_follows_the_app_theme() -> None:
    """A `Syntax` bakes its theme in, and `LogView` re-renders from the stored
    renderable — so without `watch_theme` a light terminal keeps a dark panel.

    Sibling of `test_the_pane_background_follows_the_active_theme` in
    `tests/test_log_rendering.py`, which exists for the same class of bug.
    """

    async def scenario() -> None:
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.theme = "textual-dark"
            await pilot.pause()
            dark = app._syntax_theme()

            app.theme = "textual-light"
            await pilot.pause()
            light = app._syntax_theme()

            assert dark != light, "the payload panel ignored the theme"

    asyncio.run(scenario())


def _token_styles(syntax: Syntax) -> set[str]:
    """The styles a `Syntax` actually paints, as `render_line` would see them."""

    return {
        str(segment.style)
        for segment in Console(width=60).render(syntax)
        if segment.text.strip()
    }


def test_a_panel_on_screen_is_repainted_when_the_theme_flips(tmp_path: Path) -> None:
    """End to end, and the reason `watch_theme` exists.

    The two ANSI themes are not cosmetic variants of each other: a number is
    `bright_blue` under `ansi_dark` and `blue` under `ansi_light`, and bright
    blue on a light terminal is the washed-out case. Because a `Syntax` bakes
    its theme in at construction and `LogView` re-renders a row from the stored
    renderable, only a redraw can change this.
    """

    async def scenario() -> None:
        path = tmp_path / "json.log"
        path.write_text(
            '2026-08-07 09:25:01 - INFO - {"status": "ok", "n": 42}\n', encoding="utf-8"
        )
        app = LogViewerApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_source(path, announce=False)
            await pilot.pause()
            app._set_structured(True)
            app.theme = "textual-dark"
            await pilot.pause()

            panel = app.log_panel.rows[0].renderable.renderables[1]
            assert isinstance(panel.renderable, Syntax)
            dark = _token_styles(panel.renderable)
            assert app._syntax_theme() == "ansi_dark"

            app.theme = "textual-light"
            await pilot.pause()

            panel = app.log_panel.rows[0].renderable.renderables[1]
            light = _token_styles(panel.renderable)
            assert app._syntax_theme() == "ansi_light"

            # Only numbers and keywords move; a string token stays bright_blue
            # under both. So the claim is that the palettes differ at all --
            # which they cannot unless the panel was rebuilt.
            assert dark != light, "the panel kept the palette it was built with"
            assert "blue" in light and "blue" not in dark

    asyncio.run(scenario())
