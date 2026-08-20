"""Pretty-printing a log payload that is itself a document.

A structured row answers *when, how bad, from what, and what happened*. This
answers a different question: some log lines carry a whole document in their
message — a JSON event, an XML request body, an HTML error page a proxy echoed
back — and reading one as a single wrapped line is hopeless.

Five formats, and the order they are tried in matters
-----------------------------------------------------

``preview`` is the only entry point precisely so the order and its reasons live
in one place, rather than being an implicit property of a chained ``or``.

1. **JSON** and 2. **XML** first, because each ends in a real parse. A parse is
   a proof, and proofs outrank heuristics. A well-formed XHTML fragment is
   labelled XML rather than HTML; that is a naming quibble about a document we
   render correctly either way.
3. **HTML**, after XML, because it is the *unproven* markup path. ``minidom``
   does not parse real HTML — ``<div class="a"><br>text</div>``,
   ``<ul><li>one<li>two</ul>`` and ``<head><meta charset="utf-8"></head>`` all
   raise ``ExpatError`` over void and optional-end elements — so HTML cannot
   borrow XML's proof and needs guards of its own.
4. **CSS**, and it must come before CSV. ``.btn{font-family:Helvetica,Arial,
   sans-serif}`` has two commas, one row, three fields, no quotes and no space
   in any cell: it clears *every* guard in :func:`_csv` and draws a two-row
   table over a stylesheet. Ordering is the fix, not another CSV rule.
5. **CSV** last. It is the loosest detector and the one with a regression
   history — see :func:`_csv`.

The bias, everywhere
--------------------

Deliberately toward refusing. A missed preview costs a reader one glance at a
line that is already legible; a false one buries three real entries under a
table of nothing. Every detector demands a *structural* signal before it
commits, and where a format has no opening character to look for — CSV and CSS
both — that signal is spelled out as explicit guards.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
from xml.dom import minidom

from rich.console import RenderableType
from rich.syntax import Syntax
from rich.table import Table

#: Longest payload worth previewing. Beyond this the pretty-print is itself a
#: wall of text, and the cost of producing it is paid on every render.
PAYLOAD_MAX_CHARS = 8_192

#: A field of this many words reads as prose, not as a cell. `"New York"` and
#: `"John Smith"` stay well under it; a sentence fragment does not. Shared with
#: the CSS selector test rather than re-invented there.
CSV_MAX_FIELD_WORDS = 4

#: Element names that make markup credible. Not exhaustive by design: this is a
#: test for "does this name real elements", and a payload built entirely from
#: exotic tags is a document we can afford to miss.
HTML_ELEMENTS = frozenset(
    """html head body div span p a table tr td th ul ol li h1 h2 h3 br img
    form input script style link meta title pre code section nav header footer
    button label tbody thead""".split()
)

#: ``<div``, ``</div``. No space is permitted after the bracket or the slash:
#: ``a < b`` must never read as a tag, and no real markup writes ``< div>``.
_HTML_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)")

#: One ``selector { declarations }`` block. The body excludes braces, so a
#: nested payload cannot match and ``@media`` is refused rather than mangled.
_CSS_BLOCK = re.compile(r"([^{}();]{1,120})\{([^{}]{0,2000})\}")
_CSS_PROPERTY = re.compile(r"^-{0,2}[a-zA-Z][a-zA-Z0-9-]*$")

#: What makes a token look like a selector rather than a field name: a leading
#: sigil, or a combinator. **An interior dot deliberately does not count** --
#: `com.acme.User{name:bob}` is a fully-qualified class name in a struct dump,
#: and a rule that read it as a descendant selector would draw a stylesheet over
#: every Java log in the world.
_CSS_SIGNAL_PREFIX = (".", "#", "@", "*", ":")
_CSS_SIGNAL_ANYWHERE = (">", "+", "~", "[")


def preview(
    payload: str,
    *,
    theme: str,
    csv_max_rows: int,
    csv_max_cols: int,
) -> tuple[RenderableType, str] | None:
    """Render *payload* as the document it is, or return None.

    The detectors are tried in the order the module docstring sets out, and the
    first one to commit wins. *theme* is a Pygments theme name, passed in rather
    than hardcoded so the preview follows the terminal's light or dark palette.
    """

    text = payload.strip()
    if not text or len(text) > PAYLOAD_MAX_CHARS:
        return None
    return (
        _json(text, theme)
        or _xml(text, theme)
        or _html(text, theme)
        or _css(text, theme)
        or _csv(text, csv_max_rows, csv_max_cols)
    )


def _json(payload: str, theme: str) -> tuple[RenderableType, str] | None:
    if not payload.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return Syntax(pretty, "json", theme=theme, word_wrap=True), "JSON"


def _xml(payload: str, theme: str) -> tuple[RenderableType, str] | None:
    if not payload.startswith("<"):
        return None
    try:
        dom = minidom.parseString(payload)
    except Exception:  # noqa: BLE001 - minidom raises broadly
        return None
    pretty = "\n".join(
        line for line in dom.toprettyxml(indent="  ").splitlines() if line.strip()
    )
    return Syntax(pretty, "xml", theme=theme, word_wrap=True), "XML"


def _html(payload: str, theme: str) -> tuple[RenderableType, str] | None:
    """Markup that ``minidom`` refused, when it still looks like a document.

    Reached only after :func:`_xml` has declined, so everything here is markup
    no XML parser would accept. The guards stand in for the proof that buys:

    * **A leading ``<``**, the same structural opener its siblings demand.
    * **A closing tag somewhere.** A lone ``<foo>`` is an angle bracket, not a
      document — which is what disqualifies ``<stdin>: line 3`` and the RFC 5424
      priority prefix ``<134>1 …``.
    * **Three tags, and two distinct recognised element names.** One tag is a
      typo; ``</dev/null> failed`` has exactly one and names nothing. A
      ``<!doctype html`` or ``<html`` opener is a document declaring itself and
      satisfies the distinct-name rule on its own.

    Never re-indented. Deciding where to break HTML means deciding whether the
    whitespace between two tags is significant, and a log preview is not the
    place to make that call: the value here is highlighting, not reflow.
    """

    if not payload.startswith("<") or "</" not in payload:
        return None
    tags = _HTML_TAG.findall(payload)
    if len(tags) < 3:
        return None
    lowered = payload[:16].lower()
    declared = lowered.startswith("<!doctype html") or lowered.startswith("<html")
    if not declared and len({name.lower() for _, name in tags} & HTML_ELEMENTS) < 2:
        return None
    return Syntax(payload, "html", theme=theme, word_wrap=True), "HTML"


def _split_declarations(body: str) -> list[str]:
    """Split a block body on `;`, ignoring semicolons inside a quoted value.

    `content:"a;b"` is one declaration, not two. A blind `body.split(";")` reads
    it as two, decides the second half has no `property: value` shape, and
    refuses a perfectly good stylesheet.
    """

    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for character in body:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
            current.append(character)
        elif character in "\"'":
            quote = character
            current.append(character)
        elif character == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return [part for part in parts if part.strip()]


def _css(payload: str, theme: str) -> tuple[RenderableType, str] | None:
    """A stylesheet, which like CSV has no opening character to look for.

    So it is all structural guards, and each one names the thing it refuses:

    * **The first character is not ``{``.** A stylesheet opens with a selector
      or an at-rule, never with a block. A payload that opens with a brace is
      either JSON that :func:`_json` already refused, meaning it is malformed,
      or a struct dump. This is the rule that stops CSS becoming JSON's
      consolation prize.
    * **Balanced braces, and nothing but ``selector { … }`` between them.**
      Removing every block must leave only whitespace, so a stylesheet quoted
      inside a sentence is not one. Nesting is refused rather than flattened.
    * **Every declaration is shaped ``property: value``**, and there is at
      least one.
    * **A selector that looks like one** — a leading ``.#@*:`` sigil, or a
      combinator. This guard carries the weight, and it is the *only* thing
      separating ``.btn{font-family:…}`` from ``Java{name:bob}`` and
      ``com.acme.User{name:bob}``.
    * **No prose-shaped selector**, reusing :data:`CSV_MAX_FIELD_WORDS` rather
      than inventing a second answer to the same question.

    Semicolons inside a quoted value are respected rather than split on — see
    :func:`_split_declarations`. Unlike :func:`_csv`, which backs off entirely
    when a payload quotes anything, this can afford to parse the quoting, so it
    does.

    **A semicolon is deliberately not required, and one declaration is enough.**
    Demanding either looks harmless and breaks the case this detector exists
    for: ``.btn{font-family:Helvetica,Arial,sans-serif}`` has no semicolon and a
    single declaration, and it is precisely the payload that clears every guard
    in :func:`_csv` and gets a two-row table drawn over it. Tightening here
    hands the stylesheet straight back to CSV.

    ``body{margin:0}`` is refused for want of a sigil, and that miss is
    accepted: a stylesheet large enough for anyone to paste into a log has class
    selectors in it.
    """

    if "{" not in payload or "}" not in payload:
        return None
    if payload.startswith("{"):
        return None
    if payload.count("{") != payload.count("}"):
        return None
    blocks = _CSS_BLOCK.findall(payload)
    if not blocks or _CSS_BLOCK.sub("", payload).strip():
        return None

    declarations = 0
    signalled = False
    for selector, body in blocks:
        name = selector.strip()
        if not name or len(name.split()) > CSV_MAX_FIELD_WORDS:
            return None
        if name.startswith(_CSS_SIGNAL_PREFIX) or any(
            token in name for token in _CSS_SIGNAL_ANYWHERE
        ):
            signalled = True
        for declaration in _split_declarations(body):
            prop, separator, _ = declaration.partition(":")
            if not separator or not _CSS_PROPERTY.match(prop.strip()):
                return None
            declarations += 1
    if not signalled or not declarations:
        return None

    # Minified CSS is re-broken, which is most of the value here: a stylesheet
    # arrives in a log as one long line or not at all.
    return Syntax(_css_reflow(blocks), "css", theme=theme, word_wrap=True), "CSS"


def _css_reflow(blocks: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for selector, body in blocks:
        lines.append(f"{' '.join(selector.split())} {{")
        lines.extend(
            f"  {declaration.strip()};" for declaration in _split_declarations(body)
        )
        lines.append("}")
    return "\n".join(lines)


def _csv(payload: str, max_rows: int, max_cols: int) -> tuple[RenderableType, str] | None:
    """A table, but only when the payload is actually tabular.

    **The guard used to be `"," not in payload`, and prose has commas.**
    `sshd[...]: AuthorizedKeysCommand ... failed, status 22` split on the one
    comma in `failed, status 22` and drew a two-column table over an ordinary
    syslog line — several per screen, each eating five rows of the pane.

    The siblings show what was missing: `_json` demands a leading `{` or `[`
    and `_xml` a leading `<`. Both require a *structural* signal before they
    commit. These rules are that signal for CSV, which has no opening character
    to look for:

    * **A consistent field count** across every row, and at least two.
      Ragged rows are text that happens to contain commas.
    * **Two delimiters at least**, so one comma can never make a table.
    * **Three fields when there is only one row.** A single line split in
      two is the overwhelmingly common false positive and almost never real
      CSV; a genuine one-line record has more columns than that.
    * **No prose-shaped field**, and stricter for a single row than for
      many, because one row is the least evidence there is: alone, any
      space in a cell is disqualifying; among several rows of consistent
      width, only a sentence-length one is. Skipped entirely when the
      payload quotes anything, since quoting is a writer declaring its own
      field boundaries and that declaration outranks this heuristic.

    Deliberately biased toward refusing: a missed preview costs a reader one
    glance at a line that is already legible, and a false one buries three
    real entries behind a table of nothing.
    """

    if payload.count(",") < 2:
        return None
    try:
        rows = list(itertools.islice(csv.reader(io.StringIO(payload)), max_rows))
    except csv.Error:
        return None
    if not rows:
        return None
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        return None
    (width,) = widths
    if width < 2 or (len(rows) == 1 and width < 3):
        return None
    if '"' not in payload:
        # Quoting is a writer declaring its own field boundaries, and that
        # declaration outranks everything below.
        fields = [field.strip() for row in rows for field in row]
        # **One row is judged hardest, because one row is the least
        # evidence there is.** With several rows the consistent width is
        # itself the structure, so a spacey cell is credible; with one, the
        # only signal is the commas — and `Started sshd, nginx, and dbus`
        # has those too. A lone record with a space in a cell is prose.
        if len(rows) == 1:
            if any(" " in field for field in fields):
                return None
        elif any(len(field.split()) >= CSV_MAX_FIELD_WORDS for field in fields):
            return None
    column_count = min(width, max_cols)
    if column_count == 0:
        return None

    table = Table(box=None, show_header=True, show_edge=False, pad_edge=False)
    for index in range(column_count):
        table.add_column(f"Col {index + 1}", overflow="fold")
    truncated = False
    for row in rows:
        if len(row) > column_count:
            truncated = True
        padded = list(row[:column_count])
        padded.extend([""] * (column_count - len(padded)))
        table.add_row(*padded)
    if truncated:
        table.add_row(*(["…"] * column_count))
    return table, "CSV preview"


__all__ = ["CSV_MAX_FIELD_WORDS", "HTML_ELEMENTS", "PAYLOAD_MAX_CHARS", "preview"]
