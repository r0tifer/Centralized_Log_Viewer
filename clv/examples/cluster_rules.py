r"""A normalisation rule and a shape component, as CLV clustering plugins.

A worked ``ClusterRule`` and ``ShapeContributor`` example. Drop this file in
``~/.config/clv/plugins/`` (CLV puts it in ``examples/`` for you) and add it to
``settings.conf``::

    [log_viewer]
    plugins = cluster_rules

    [plugin:cluster_rules]
    # Which field keeps two clusters apart. Unset, the contributor adds
    # nothing and only the rule below is doing any work.
    split_by = unit

It adds two things the repeat clusterer (`c`) could not do before.

An address stops splitting a cluster
    ``authentication failed for alice@corp.example`` is one event said a
    thousand times by a thousand people, and none of CLV's nine built-in rules
    touches an email address: it is not hex, not a UUID, not a path, not a
    number. So a thousand lines are a thousand rows, on exactly the log where
    folding repeats was worth having.

Two units stop sharing one
    ``Started`` from ``nginx.service`` and ``Started`` from ``postgres.service``
    read identically once normalised, and folding them together loses the one
    thing that distinguished them. A contributor puts the unit back into the
    key without changing what any other part of a shape means.

Six things are worth copying out of this file.

**A rule is data, not a method.** You declare a pattern and a placeholder and
CLV performs the substitution. There is no ``apply()`` to implement, which
means there is no third-party call on a path that runs per line — and it means
every mistake you can make is caught when the plugin loads, with a message,
rather than on a line in a running pane.

**The placeholder rules are real and they are checked.** No digit, because
plugin rules run *after* CLV's own and a later rule matching numbers would chew
up what an earlier one wrote — which is why every built-in placeholder is
digit-free. No backslash, because the placeholder is a substitution template
and ``\1`` would splice in a captured group. An **empty** placeholder is legal
and deletes the token, which is how the ANSI rule below works.

**Your pattern must not match the empty string.** ``[a-z]*`` matches at every
position, so a rule written that way would write its placeholder between every
character of every line. CLV refuses one at load; the reason it is worth
knowing is that the regex is the whole of what you are declaring, so this is
the one class of error there is nobody to catch later.

**Your pattern sees the line CLV has already normalised, not the raw one.**
Plugin rules run last, so every token CLV knows about is a placeholder by the
time you are handed the text. This is what makes appending safe — a rule of
yours cannot break the built-in order — and it is also the mistake to know
about. A Kubernetes pod name looks like the obvious rule to write::

    pattern = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b")   # never fires

By the time it runs, ``api-7d9f8b6c4-x2n9q`` is already ``api-<hex>-x2n9q``:
the built-in hex rule matched the middle hash, and the pattern above no longer
has anything to match. Check what ``c`` already does to your line before
writing a rule for it — and if your token genuinely begins its life inside one
CLV normalises, the rule you want is a narrower one, against what is left.

**A rule is memoised; a contributor is not.** ``normalise`` caches by line text,
so your pattern runs once per *distinct* line however often it is rendered. A
contributor is handed the whole entry, which the cache cannot key on, so it
runs per entry per render — every keystroke in the query box, over the whole
buffer. Read a field. Do not compute one.

**A contribution must be a pure function of the entry.** CLV clusters the
filtered set in one pass and folds tailed lines into that same stream one at a
time. A contribution that changed between those two calls would make the
incremental path and a full recompute disagree about what belongs with what,
and nothing would report it.
"""

from __future__ import annotations

import re

from clv.api import ClusterRule, LogEntry, ShapeContributor


class EmailAddress(ClusterRule):
    """``alice@corp.example`` → ``<email>``.

    Deliberately not a validator. A normalisation rule wants the shape of the
    thing, not its correctness: an address that is malformed is still the
    volatile part of the line, and a rule that refused to match it would leave
    exactly one cluster per typo.
    """

    name = "email-address"
    requires_api = ">=1.0,<2.0"
    pattern = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    placeholder = "<email>"


class AnsiColour(ClusterRule):
    r"""Strip SGR colour runs, so a coloured line clusters with a plain one.

    The example of an **empty** placeholder: the escape is not a volatile value
    standing in for something, it is decoration, and what should be left behind
    is nothing at all.

    It is also the lesson above, caught in this file's own example. The obvious
    pattern is ``\x1b\[[0-9;]*m``, and it never fires: CLV's integer rule has
    already turned ``\x1b[31m`` into ``\x1b[<int>m`` by the time a plugin rule
    is handed the line, and there are no digits left in it to match. Written as
    "everything up to the terminating ``m``" it matches either form, which is
    what a rule running last has to be written to do.

    ``priority`` orders plugin rules among *themselves* — this one runs before
    the address rule, so an escape sitting inside an address cannot stop that
    pattern matching. It cannot move a rule ahead of CLV's own nine; nothing
    can, and that is the guarantee that keeps the built-in order intact.
    """

    name = "ansi-colour"
    requires_api = ">=1.0,<2.0"
    priority = 50
    pattern = re.compile(r"\x1b\[[^m]*m")
    placeholder = ""


class SplitByField(ShapeContributor):
    """Keep entries apart by the value of one field.

    Configurable rather than hard-coded, because which field distinguishes two
    streams is a property of the operator's logs and not of this plugin —
    ``unit`` on a systemd box, ``container`` under Docker, ``tag`` on a plain
    syslog.

    **Inert until configured.** With no ``split_by`` set this returns ``""`` for
    every entry, which is the no-op contribution: the shape is exactly what it
    would have been without this plugin installed. That is the same shape of
    consent the journal provider and the alert sink ship with, applied to a
    seam where the cost of getting it wrong is a pane that clusters nothing.
    """

    name = "split-by-field"
    requires_api = ">=1.0,<2.0"

    def __init__(self) -> None:
        self._settings = {}

    def configure(self, settings) -> None:
        # Kept, not copied out of: CLV mutates the mapping behind this view when
        # it re-reads `settings.conf`, so reading through it is what lets an
        # edit take effect without a restart.
        self._settings = settings

    def contribute(self, entry: LogEntry) -> str:
        key = self._settings.get("split_by", "").strip()
        if not key:
            return ""
        # A plain dict read. Anything more expensive belongs somewhere that is
        # not called once per entry per render.
        return entry.fields.get(key, "")


__all__ = ["AnsiColour", "EmailAddress", "SplitByField"]
