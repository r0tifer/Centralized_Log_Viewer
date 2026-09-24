"""What the help overlay says, and the vocabulary it says it in.

Textual-free on purpose, the way `severity.py` is: this is data a widget
renders, not a widget. Keeping it here rather than in `services/` follows the
rule that a widget may not import `clv.app` — the app owns `BINDINGS`, hands
the grouped sections in, and this module turns them into the Keys page beside
the four written ones.

**The content is written out rather than read from `README.md`.** Only
`settings.conf` ships beside the package (`pyproject.toml`'s `include`), so an
installed CLV has no README to read — `tests/test_readme_docs.py` skips itself
for exactly that reason. Help that exists only in a file the wheel does not
carry is help that is missing everywhere it is most needed.

The block vocabulary is five kinds and is deliberately not a markup language.
Adding a sixth is cheaper than the day someone needs nested lists and reaches
for a Markdown parser, which would put a document renderer, its CSS and its
version drift into a modal that has fourteen rows to work with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Union

#: Textual key names that do not read as themselves. Anything not listed is
#: either a single character (shown as-is) or a modifier combination.
_KEY_DISPLAY: dict[str, str] = {
    "escape": "Esc",
    "asterisk": "*",
    "question_mark": "?",
    "slash": "/",
    "plus": "+",
    "minus": "-",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "up": "Up",
    "down": "Down",
    "enter": "Enter",
    "space": "Space",
    "tab": "Tab",
}


def format_key(key: str) -> str:
    """Render a Textual key name the way an operator would type it."""

    if len(key) == 1:  # "/", "[", "+" are bound as themselves
        return key
    known = _KEY_DISPLAY.get(key)
    if known is not None:
        return known
    parts = key.split("+")
    return "+".join(
        _KEY_DISPLAY.get(part, part.upper() if len(part) == 1 else part.capitalize())
        for part in parts
    )


@dataclass(frozen=True)
class HelpSection:
    """One titled group of bindings, in the order the app declared them."""

    title: str
    #: ``(key, description)`` pairs, keys still in Textual's naming.
    rows: tuple[tuple[str, str], ...]


# --- the block vocabulary ----------------------------------------------------


@dataclass(frozen=True)
class Heading:
    """A subhead inside a page."""

    text: str


@dataclass(frozen=True)
class Paragraph:
    """Wrapped prose."""

    text: str


@dataclass(frozen=True)
class Bullets:
    """A list. Each item wraps with a hanging indent under its bullet."""

    items: tuple[str, ...]


@dataclass(frozen=True)
class Example:
    """Something to type, shown verbatim.

    Never wrapped — a query broken across two lines is a query that does not
    work — so every line here has to fit the body at 80 columns. The budget is
    asserted by ``test_every_example_line_fits_at_eighty_columns``.
    """

    lines: tuple[str, ...]
    caption: str = ""


@dataclass(frozen=True)
class KeyRows:
    """Left/right pairs, in the Keys page's two-column layout.

    The left cell is rendered **verbatim**. `format_key` is applied by
    `keys_page`, where the left cell is a Textual key name that needs
    translating (`pageup` becomes `PgUp`); everywhere else it holds literal
    syntax such as `key:value`, which capitalising would silently corrupt.
    """

    rows: tuple[tuple[str, str], ...]


HelpBlock = Union[Heading, Paragraph, Bullets, Example, KeyRows]


@dataclass(frozen=True)
class HelpPage:
    """One tab, and everything behind it."""

    #: The tab's value, and the suffix of its container id.
    key: str
    #: What the tab strip shows. Short: the strip has 72 cells at 80 columns.
    label: str
    #: What the dialog title shows beside "Help".
    title: str
    blocks: tuple[HelpBlock, ...]


# --- the written pages -------------------------------------------------------

_OVERVIEW = HelpPage(
    key="overview",
    label="Overview",
    title="Getting started",
    blocks=(
        Paragraph(
            "CLV reads log files and shows them one line at a time, live. "
            "Point it at folders or individual files; folders are searched "
            "all the way down."
        ),
        Paragraph(
            "It is not restricted to *.log. Any readable text file counts, "
            "including UTF-16 exports from Windows and PowerShell, and .ods "
            "spreadsheets. Binary files are detected by their contents and "
            "skipped — never by their name."
        ),
        Heading("The two panes"),
        Bullets(
            (
                "Left: the source tree, one branch per configured root.",
                "Right: the log itself, with a line cursor you move with the "
                "arrow keys.",
                "Ctrl+B swaps between them when the terminal is too narrow to "
                "show both.",
            )
        ),
        Heading("Adding a source"),
        Paragraph(
            "Press a and type a path. Ctrl+S writes what you have added into "
            "settings.conf so it comes back next launch; without it the "
            "source lasts for this session only. Ctrl+R re-reads the "
            "configuration and rescans."
        ),
        Example(
            (
                "a          then  /var/log",
                "a          then  /srv/app/current/app.log",
            ),
            caption="A folder is searched recursively; a file is taken as-is.",
        ),
        Heading("Groups in the tree"),
        Paragraph(
            "Four groups sit above the configured roots, and all of them "
            "arrive collapsed so they never push your logs off screen:"
        ),
        KeyRows(
            (
                ("Views", "Saved filter sets — see the Reading page"),
                ("Providers", "Sources a plugin found, such as the journal"),
                ("Starred", "Logs you marked with *"),
                ("Merged", "Logs you combined with x"),
            )
        ),
        Heading("Scenario: your first five minutes"),
        Bullets(
            (
                "Press a and add /var/log. CLV scans it off the UI thread and "
                "reports what it found and what it skipped.",
                "Open the one you actually read. Press * to star it.",
                "Star exactly one log and CLV opens straight into it next "
                "launch. Star several and they are simply grouped at the top.",
                "Press Ctrl+S to keep the source across restarts.",
            )
        ),
        Heading("Where settings live"),
        Paragraph(
            "~/.config/clv/settings.conf, which ships two-thirds comments and "
            "is worth reading. clv --print-default-config prints a fresh copy; "
            "clv --upgrade-config rewrites yours from the template, keeping "
            "your values and saving the previous file alongside."
        ),
        Paragraph(
            "Nothing here is reported anywhere. CLV has no telemetry, reads "
            "only what you configured, and never runs sudo."
        ),
    ),
)

_SEARCH = HelpPage(
    key="search",
    label="Search",
    title="Search & filtering",
    blocks=(
        Paragraph(
            "Two rules explain everything the pane does. First, the query "
            "never drops what it cannot parse — it matches the whole raw "
            "line, so an unstructured line is searchable like any other. "
            "Second, severity and time filters only hide lines that "
            "demonstrably lack what you asked for, and when they do the empty "
            "pane says so and counts them."
        ),
        Heading("The query box"),
        Paragraph(
            "Press / to focus it, Enter to apply, Esc to clear. What you type "
            "is a regular expression unless you turn Regex off in the "
            "Advanced drawer (f), in which case it is a plain substring."
        ),
        Paragraph(
            "Smart case: a lowercase query is case-insensitive, and a single "
            "uppercase character opts back into matching case. Case "
            "sensitive and Invert match are switches in the same drawer."
        ),
        Example(
            (
                "timeout|refused",
                "oom-killer",
                "Failed password",
            ),
            caption="Plain regexes, matched against the whole line.",
        ),
        Heading("Field terms"),
        Paragraph(
            "The parser recovers more than a timestamp and a level — a "
            "hostname, a program tag, an HTTP status, every key of a JSON "
            "payload — and the same box can ask about any of it. Terms "
            "combine with and; whatever is left over is the regex."
        ),
        Example(
            ("tag:sshd host:web01 status>=500 timeout|refused",),
            caption="Four field terms, then the regex.",
        ),
        KeyRows(
            (
                ("key:value", "contains, smart-case"),
                ("key:", "the field is present at all"),
                ("key=value", "exactly equal, case-sensitive"),
                ("key!=value", "not equal"),
                ("key>=value", "numeric when both sides are numbers, else A-Z"),
                ("key<=value", "the same, the other way. Also key> and key<"),
            )
        ),
        Paragraph(
            'Quote a value to keep spaces or colons inside it: msg:"disk '
            'full". Quotes group; they are not part of the value.'
        ),
        Paragraph(
            "Always-known names are host, tag, pid, msgid, ident, user, "
            "request, status, size and node. Every key a JSON or logfmt line "
            "carries is added as soon as one is read. Start typing and the "
            "list drops down: Tab takes the first, Down steps in, Esc "
            "dismisses."
        ),
        Paragraph(
            "node is the machine CLV read the line from; host is what the "
            "line says about itself. They differ, and the difference is the "
            "point of a merged view."
        ),
        Heading("Why sshd: still searches for text"),
        Paragraph(
            "A word:word token becomes a field term only when the key is one "
            "CLV knows. Everything else stays part of the regex, byte for "
            "byte — so sshd:, kernel: oom-killer and 10:30:00 mean exactly "
            "what they always did. The cost is that a typo like hsot:web01 "
            "is searched for as text rather than reported, which is why the "
            "completions exist."
        ),
        Heading("Time"),
        Paragraph(
            "t cycles All, 15m, 1h, 6h and 24h. It deliberately steps over "
            "Custom, which needs a dialog rather than a keypress — click it, "
            "or click it again to adjust a range you already set. Selecting a "
            "bucket on the severity timeline (b) sets an ordinary custom "
            "range you can dismiss like any other filter."
        ),
        Paragraph(
            "g jumps the cursor to a moment rather than filtering to it. It "
            "takes an offset or an absolute time; a bare offset means the "
            "past, because in a log it always does."
        ),
        Example(
            (
                "-15m        -6h        -2d        +2h",
                "15m                     (the same as -15m)",
                "2026-08-07 09:25:01     2026-08-07",
            ),
            caption="Units are s, m, h, d, w. Whole numbers only.",
        ),
        Heading("Severity"),
        Paragraph(
            "s cycles All, Debug, Info, Warn, Error. Debug includes TRACE, "
            "Info includes NOTICE, and Error includes CRITICAL. A line whose "
            "format declares no level is never guessed at."
        ),
        Heading("When the pane is empty it tells you why"),
        Paragraph(
            "Hidden lines are counted by reason and never merged into one "
            "total, because the three reasons need different answers:"
        ),
        Bullets(
            (
                "no detected severity — nothing in this source declares a "
                "level, so a severity filter cannot include it.",
                "no detected timestamp — this source's format carries no "
                "date, so a time window cannot include it.",
                "carries no 'status' field — this source's format does not "
                "report it. Filtering a syslog with status>=500 is the "
                "usual cause.",
            )
        ),
        Heading("Scenario: which machine started throwing 500s"),
        Bullets(
            (
                "Merge the access logs from every host with x, open the set "
                "with u.",
                "Type status>=500 and press Enter.",
                "Press b for the timeline and find the spike.",
                "Enter on that bucket narrows the window to it.",
                "Add node: to see which machine the surviving lines are from.",
            )
        ),
        Example(
            ("node:web01 status>=500",),
            caption="The fleet query, once you know which host it was.",
        ),
    ),
)

_SOURCES = HelpPage(
    key="sources",
    label="Sources",
    title="Sources & discovery",
    blocks=(
        Paragraph(
            "A source is a file CLV can read. Where it lives — this disk, an "
            "archive, another machine, the journal — changes how it is "
            "opened and nothing else. All of them are starrable, mergeable "
            "and filterable in the same way."
        ),
        Heading("What discovery skips, and why"),
        Paragraph(
            "Every skipped file is attributed to exactly one reason, because "
            "only one of them is yours to change:"
        ),
        Bullets(
            (
                "unsupported — CLV cannot display it, because its contents "
                "are binary.",
                "filtered out — your own include or exclude globs hid it.",
                "unreadable — the read itself failed, usually permissions.",
            )
        ),
        Paragraph(
            "A file you named yourself is always listed by path when it is "
            "skipped, never folded into a tally. Globs, symlink following "
            "and the binary check are all in the Advanced drawer (f)."
        ),
        Paragraph(
            "An unreadable file is reported with the group that would fix "
            "it. CLV will not read it by becoming someone else: there is no "
            "sudo, anywhere, not behind a setting."
        ),
        Heading("Compressed and rotated logs"),
        Paragraph(
            ".gz, .bz2 and .xz are read directly. app.log, app.log.1 and "
            "app.log.2.gz are presented as one source spanning all three, "
            "oldest lines first, and only the live member is tailed."
        ),
        Paragraph(
            "This is the one place the bounded-read promise bends: deflate "
            "has no cheap tail, so a compressed member is read forward. "
            "Memory stays capped; the work is proportional to the member. A "
            "rotated set spends one shared budget newest-first, so the older "
            "members are often never opened."
        ),
        Heading("Starring"),
        Paragraph(
            "* stars the log under the cursor, and starred logs are repeated "
            "in a group at the top of the tree — so a favourite buried "
            "several folders deep is one keystroke away. Star exactly one "
            "and CLV opens it on launch. A star whose log has since rotated "
            "away is still listed, dimmed, carrying the mark that removes it."
        ),
        Heading("Merging"),
        KeyRows(
            (
                ("x", "add or remove the log under the cursor"),
                ("u", "open the set as one timestamp-ordered stream"),
                ("X", "empty the set"),
                ("Ctrl+X", "merge this path across every host that has it"),
            )
        ),
        Paragraph(
            "A merged pane adds a source column, and the origin is queryable "
            "as source: and node:. Filters, navigation, marks, the detail "
            "pane and export all work there exactly as on a single file."
        ),
        Paragraph(
            "A line with no timestamp is anchored after the last timestamped "
            "line from its own source and counted — never dropped, and never "
            "silently interleaved somewhere it did not belong."
        ),
        Paragraph(
            "An ordering across machines is only as trustworthy as their "
            "clocks. When the members disagree about the time zone or the "
            "clock, CLV says so beside the anchored count rather than "
            "letting you read causation out of a wrong interleaving."
        ),
        Heading("Remote sources over SSH"),
        Paragraph(
            "Name a host in settings.conf — or press R and add it — and its "
            "folders appear in the same tree as the ones on this disk, "
            "discovered recursively, tailed, filtered, starred and merged."
        ),
        Example(
            (
                "[ssh:web01]",
                "log_dirs = /var/log, /srv/app/logs",
            ),
            caption="A host section. The name is the ssh destination.",
        ),
        Paragraph(
            "Nothing connects until enable_ssh is on — the switch is Remote "
            "(SSH) in the Advanced drawer. CLV runs the ssh binary, so it "
            "inherits the setup you already have: your agent, your keys, "
            "your ~/.ssh/config with its ProxyJump and known_hosts. Scan SSH "
            "config in the drawer offers those aliases for import."
        ),
        Paragraph(
            "There is no password option and no sudo option — not in the "
            "config schema, not in any dialog, not anywhere. A connection "
            "that needs typed input fails as unreachable. Host key "
            "verification is never disabled. Nothing is installed on the "
            "remote and nothing is left running there."
        ),
        Paragraph(
            "An unreachable source is reported, never rendered as an empty "
            "one: a pane that went quiet because a link dropped is not the "
            "same fact as a log that stopped."
        ),
        Heading("The systemd journal"),
        Paragraph(
            "Turn on Journal (systemd) in the Advanced drawer and journal "
            "units appear under Providers. It is a plugin because reading it "
            "runs journalctl, and nothing spawns a subprocess until you ask."
        ),
        Example(
            ("unit:sshd.service Failed password",),
            caption="Journal fields are ordinary field terms.",
        ),
    ),
)

_READING = HelpPage(
    key="reading",
    label="Reading",
    title="Reading the log",
    blocks=(
        Heading("The cursor"),
        Paragraph(
            "Arrow keys move a cursor through the log. Moving it pauses "
            "following, so the lines under you stay put while you read; End "
            "jumps to the last line and resumes. w toggles following on its "
            "own, and the status bar always says which state you are in."
        ),
        KeyRows(
            (
                ("Up / Down", "move one line"),
                ("PgUp / PgDn", "move a screen"),
                ("Home / End", "first line / last line and resume following"),
                ("n / N", "next / previous match"),
                ("+ / -", "show more / fewer lines"),
            )
        ),
        Paragraph(
            "With no query, n and N step between warnings and worse — which "
            "is usually what you wanted when you pressed it."
        ),
        Heading("Seeing one event whole"),
        Paragraph(
            "Enter (or d) opens the detail pane on the line under the "
            "cursor: the raw text beside its parsed timestamp, canonical "
            "severity, detected format and every field the parser recovered. "
            "Four formats carry no fields at all, and the pane says which "
            "rather than showing an empty list."
        ),
        Paragraph(
            "A line no format recognises inherits the timestamp and severity "
            "of the entry above it, so a stack trace survives a show-me-only-"
            "errors filter along with the ERROR that produced it."
        ),
        Heading("Structured columns"),
        Paragraph(
            "o turns a dense log into aligned time, level and source cells "
            "with the message starting at the same column on every row, and "
            "pretty-prints JSON, XML, HTML, CSS and CSV payloads in place."
        ),
        Heading("The severity timeline"),
        Paragraph(
            "b draws a one-row histogram of the filtered set above the log, "
            "coloured by the worst severity in each bucket. It is a control "
            "rather than a picture: Left and Right move between buckets and "
            "Enter narrows the time window to the one you are on. "
            "Shift+Left and Shift+Right step between plugin annotations."
        ),
        Heading("Marks"),
        Paragraph(
            "m bookmarks the line under the cursor and M steps between "
            "marks. They are keyed by the line's content rather than its "
            "position, so they survive filtering and tailing. They are "
            "session-only and never written to disk — a mark is derived from "
            "log content, and log content does not go in a state file."
        ),
        Heading("Collapsing repeats"),
        Paragraph(
            "c folds repeated lines into one counted row by normalising the "
            "volatile tokens — ids, addresses, durations, paths — out of "
            "them. A WARN and an ERROR that read alike stay apart, and a "
            "merged view never folds two logs together."
        ),
        Paragraph(
            "Nothing is hidden: it is a display transform, never a filter. "
            "Enter expands a cluster in place and every line inside is still "
            "selectable, markable and exportable."
        ),
        Heading("Saved views"),
        Paragraph(
            "V saves the filters that are active under a name; v lists them, "
            "where Enter applies, r renames and d twice deletes. A view "
            "captures the query, severity, time window, search options, "
            "globs, the open log and the merged set."
        ),
        Paragraph(
            "A view whose query needs a plugin records which one. If it is "
            "not installed the view is kept exactly as you wrote it, marked, "
            "and refused rather than applied — a query missing its operator "
            "is not a narrower search, it is a regex that happens to parse."
        ),
        Heading("Watch rules"),
        Paragraph(
            "W manages patterns that highlight arriving lines, raise a toast, "
            "or both: a adds, Enter edits, space enables or disables, d "
            "deletes. A pattern is the same grammar as the query box."
        ),
        Paragraph(
            "Matches are coalesced — the first immediately, the rest of the "
            "window counted and reported together — because a rule that "
            "matches every line is what makes people switch the feature off."
        ),
        Heading("Getting it out"),
        KeyRows(
            (
                ("Ctrl+E", "export the filtered set to a file"),
                ("y", "copy the line, or the view, to the clipboard"),
                ("Ctrl+L", "copy mode — hides all chrome for a mouse drag"),
            )
        ),
        Paragraph(
            "Export writes JSON Lines, CSV or plain text, and writes the "
            "whole filtered set rather than the lines on screen. It can be "
            "narrowed to marked lines only, or to the collapsed clusters. "
            "Overwriting an existing file takes a second press."
        ),
        Paragraph(
            "y goes through the terminal itself (OSC 52), so it works over "
            "SSH and inside tmux, where a mouse selection copies the wrong "
            "thing. An oversized payload is truncated at a line boundary and "
            "reported, never silently cut."
        ),
        Heading("Plugins"),
        Paragraph(
            "P lists what is installed — space enables or disables, r "
            "re-enables one that failed. C runs a plugin command by name. A "
            "plugin that breaks is reported and skipped, never fatal."
        ),
        Paragraph(
            "A file in ~/.config/clv/plugins/ is listed but not run until "
            "you name it in the plugins setting, so installing one and "
            "running one stay two decisions. A plugin is trusted code: it "
            "runs with your privileges, in CLV's process, and can read every "
            "log CLV can open. Install one the way you would install any "
            "other program."
        ),
        Example(
            (
                "[log_viewer]",
                "plugins = redact_secrets, nginx_format",
            ),
            caption="Listed is not enough; naming it here is what runs it.",
        ),
    ),
)

#: The written pages, in tab order. The Keys page is appended by `help_pages`.
STATIC_PAGES: tuple[HelpPage, ...] = (_OVERVIEW, _SEARCH, _SOURCES, _READING)

#: What `?` lands on the first time it is pressed in a session.
DEFAULT_PAGE = STATIC_PAGES[0].key


def keys_page(sections: Sequence[HelpSection]) -> HelpPage:
    """The cheatsheet, as a page.

    A pure function of what the app passed in, which is a pure function of
    ``BINDINGS`` — so the guarantee that a key cannot go missing from help
    survives the overlay growing four pages that are written by hand.
    """

    # No preamble. This page is the one people arrive at knowing what they
    # want, and the four rows an introduction costs are four bindings pushed
    # under the fold at 24 rows. What it would have said — that the list is
    # generated and therefore complete — is in the README and on Overview.
    blocks: list[HelpBlock] = []
    for section in sections:
        blocks.append(Heading(section.title))
        blocks.append(
            KeyRows(
                tuple(
                    (format_key(key), description)
                    for key, description in section.rows
                )
            )
        )
    return HelpPage(
        key="keys",
        label="Keys",
        title="Keyboard shortcuts",
        blocks=tuple(blocks),
    )


def help_pages(sections: Sequence[HelpSection]) -> tuple[HelpPage, ...]:
    """The four written pages, plus a Keys page built from *sections*."""

    return (*STATIC_PAGES, keys_page(sections))


__all__ = [
    "Bullets",
    "DEFAULT_PAGE",
    "Example",
    "Heading",
    "HelpBlock",
    "HelpPage",
    "HelpSection",
    "KeyRows",
    "Paragraph",
    "STATIC_PAGES",
    "format_key",
    "help_pages",
    "keys_page",
]
