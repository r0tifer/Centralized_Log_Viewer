"""nginx ``error_log``, as a CLV log format. A worked ``LogFormat`` example.

Drop this file in ``~/.config/clv/plugins/`` (CLV puts it there for you) and add
it to ``settings.conf``::

    [log_viewer]
    plugins = nginx_error

nginx writes its error log in a shape no built-in matcher recognises — the date
is slash-separated, so every line of it is ``raw`` today::

    2026/08/07 09:25:01 [error] 1234#0: *42 connect() failed (111: Connection
    refused) while connecting to upstream, client: 10.0.0.5, server: example.com,
    request: "GET /pay HTTP/1.1", upstream: "http://10.0.0.9:8080/pay"

Three things are worth copying out of this file.

**Normalise onto CLV's vocabulary rather than nginx's.** The client address is
filed under ``host``, which is the key BSD syslog and the Common Log Format
already use for it, so ``host:10.0.0.5`` answers across all three. Inventing
``client`` would have been the literal translation and the wrong one: it makes a
field query have to know which format produced the line, which is the thing
:mod:`clv.services.parsing`'s normalisation exists to prevent. A genuinely new
concept — nginx's ``upstream`` — gets a key of its own.

**Declare the row, or the row is a downgrade.** ``label`` and ``columns`` are
optional in the type and not optional in practice. Without them an entry renders
with the right timestamp and level, the bare ``nginx-error`` identifier where the
format name should be, no source cell and no chips — no error anywhere, just a
worse row than a built-in gets for the same line.

**Reject cheaply, and reject first.** ``parse()`` is offered every line no
built-in recognised, which on a large unrecognised file is every line of it. The
two character tests below decide almost every non-match without touching the
regex, and the regex is compiled once at import rather than per call.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from clv.api import FormatProfile, LogEntry, LogFormat, normalize_level

#: ``2026/08/07 09:25:01 [error] 1234#0: *42 message``
#:
#: The connection id (``*42``) is optional: nginx omits it for messages that are
#: not about a connection, and a pattern that required it would silently decline
#: every startup and configuration-reload line in the file.
_LINE = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<level>[a-z]+)\] "
    r"(?P<pid>\d+)#(?P<tid>\d+): "
    r"(?:\*(?P<cid>\d+) )?"
    r"(?P<msg>.*)$"
)

#: The ``key: value`` tail nginx appends to a request-scoped message. Values may
#: be quoted (``request: "GET / HTTP/1.1"``) or bare (``client: 10.0.0.5``), and
#: a bare one runs to the next ``, key:`` rather than to the next comma — a
#: message body is full of commas and splitting on them loses half of it.
_DETAIL = re.compile(
    r'(?P<key>client|server|request|upstream|host|referrer|subrequest): '
    r'(?:"(?P<quoted>[^"]*)"|(?P<bare>[^,]*))'
)

#: nginx's own detail names, mapped onto the keys CLV normalises across formats.
#: ``client`` becomes ``host`` for the reason in the module docstring; ``host``
#: (the HTTP Host header) becomes ``vhost``, because it is a different fact from
#: the one every other format files under ``host`` and quietly overloading the
#: key would make one query mean two things.
_KEYS = {
    "client": "host",
    "server": "server",
    "request": "request",
    "upstream": "upstream",
    "host": "vhost",
    "referrer": "referrer",
    "subrequest": "subrequest",
}


class NginxErrorFormat(LogFormat):
    """nginx's error log, with its request details recovered as fields."""

    name = "nginx-error"
    requires_api = ">=1.0,<2.0"

    format_name = "nginx-error"
    label = "nginx error log"

    field_names = frozenset(
        {"pid", "tid", "cid", "host", "server", "request", "upstream", "vhost",
         "referrer", "subrequest"}
    )

    #: `server` before `host` in the source cell: which vhost logged this is the
    #: scanning axis for an error log, and the client is a per-row detail. It
    #: falls through to the client for the connection-level messages that name
    #: no server. `host` is also a chip, so a merge across several clients still
    #: shows it — the `varying` rule turns that on with no special case here.
    columns = FormatProfile(
        source_keys=("server", "host"),
        pid_key="pid",
        chips=("host", "upstream", "request"),
    )

    def parse(self, line: str) -> Optional[LogEntry]:
        # Two character tests before the regex. Every line of a file nothing
        # recognises reaches this method, and almost none of them are nginx.
        if len(line) < 20 or line[4] != "/" or line[7] != "/":
            return None
        match = _LINE.match(line)
        if match is None:
            return None

        try:
            timestamp = datetime.strptime(match.group("ts"), "%Y/%m/%d %H:%M:%S")
        except ValueError:  # pragma: no cover - the pattern already bounds this
            timestamp = None

        message = match.group("msg")
        fields: dict[str, str] = {}
        for key in ("pid", "tid", "cid"):
            value = match.group(key)
            if value:
                fields[key] = value

        first = len(message)
        for detail in _DETAIL.finditer(message):
            value = detail.group("quoted")
            if value is None:
                value = (detail.group("bare") or "").strip()
            if value:
                fields[_KEYS[detail.group("key")]] = value
            first = min(first, detail.start())

        # The details are cells and chips now, so the message cell stops
        # repeating them. `raw` is untouched and is still what the detail pane
        # shows, what `y` copies and what an export writes.
        message = message[:first].rstrip().rstrip(",")

        return LogEntry(
            raw=line,
            timestamp=timestamp,
            level=normalize_level(match.group("level")),
            message=message,
            format_name=self.format_name,
            # Only when there is something to carry: an entry with no fields
            # takes the parser's shared read-only empty mapping, which is what
            # keeps the common line allocating nothing. A plain dict is a legal
            # `fields` — CLV treats it as read-only and never writes to it — so
            # there is no wrapping to do here.
            **({"fields": fields} if fields else {}),
        )


def register() -> list[LogFormat]:
    return [NginxErrorFormat()]
