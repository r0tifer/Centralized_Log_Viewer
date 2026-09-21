"""CLV plugin interfaces and loader.

Twelve extension points, each matching a thing operators keep asking CLV to do
that core should not hard-code:

* :class:`LogSourceProvider` — where log lines come from.
* :class:`LogFormat` — how a line CLV does not recognise is parsed.
* :class:`QueryOperator` — a comparison token the query box does not have.
* :class:`ComputedField` — a queryable field derived rather than parsed.
* :class:`FilterStage` — what happens to a line on its way to the pane.
* :class:`ClusterRule` — one more volatile token the repeat clusterer folds out.
* :class:`ShapeContributor` — an extra component of the key two entries share.
* :class:`TimelineAnnotation` — marks on the timeline's axis.
* :class:`TimelineMetric` — what a timeline bucket measures, if not entries.
* :class:`WatchMatcher` — a watch rule kind that is not "this pattern matched".
* :class:`WatchSink` — where a watch hit is delivered.
* :class:`Exporter` — where the current view can be sent.

Plugins are loaded from two places: modules dropped into ``clv/plugins/``
(``sources/``, ``filters/``, ``exporters/`` or flat), and installed
distributions advertising a ``clv.plugins`` entry point.

Loading is defensive on purpose. A plugin that raises on import, fails its
version check, or does not implement an interface is recorded in
:attr:`PluginRegistry.errors` and skipped — a broken third-party plugin must
never stop CLV from starting.

**Import from :mod:`clv.api`, not from here.** Everything a plugin needs is
re-exported there — the same objects, not copies — under a written stability
promise and its own :data:`PLUGIN_API_VERSION`. This module keeps exporting
what it always has, so nothing existing breaks, but it is the loader's own
namespace: it holds internals that will move, and only ``clv.api`` is covered
by the deprecation policy in ``clv/plugins/AGENTS.md``.
"""

from __future__ import annotations

import collections.abc
import configparser
import importlib
import importlib.metadata
import inspect
import math
import os
import pkgutil
import re
import sys
import time
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from ..services.clustering import ClusterRuleSpec, ShapeSpec
from ..services.filtering import FilterSpec, TimeWindow
from ..services.formats import DEFAULT_PROFILE, FormatProfile
from ..services.parsing import FORMAT_NAMES, LogEntry, normalize_level
from ..services.query import (
    BUILTIN_OPERATORS,
    KEY_CHARS,
    ComputedSpec,
    OperatorSpec,
    is_query_key,
)
from ..services.refs import SourceRef
from ..services.timeline import AnnotationSpec, MetricSpec
from ..services.watch import (
    KIND_PATTERN,
    MatcherSpec,
    SinkSpec,
    WatchRule,
)

#: The version of the published plugin API — the surface re-exported by
#: :mod:`clv.api` — and **not** CLV's own version.
#:
#: The separation is the point. ``clv.__version__`` tracks the application and
#: moves whenever anything ships; this moves only when a published name changes
#: meaning, so a plugin declaring ``requires_api = ">=1.0,<2.0"`` stops caring
#: what release it is running on. The API is additive from here: a seam phase
#: adds names and this stays "1.0".
#:
#: Defined here rather than in :mod:`clv.api` because ``clv.api`` imports the
#: interfaces from this module, so the constant has to sit on the far side of
#: that edge for :meth:`PluginRegistry.add` to check against it without a cycle.
#: ``clv.api`` re-exports it as the name authors actually see.
PLUGIN_API_VERSION = "1.0"

#: Entry point group installed packages use to advertise CLV plugins.
ENTRY_POINT_GROUP = "clv.plugins"

#: Subdirectories of clv/plugins scanned for drop-in modules.
_LOCAL_SUBPACKAGES = ("sources", "filters", "exporters")

#: Extra plugin directories, ``os.pathsep``-separated, searched *before* the
#: user plugin directory.
#:
#: A development and test mechanism, not a way to install a plugin: it is how
#: this package's own tests get a plugin root without writing into the source
#: tree, and how an author runs a plugin they are editing in place. The
#: enable-list applies to it exactly as it does to the user directory.
PLUGIN_PATH_ENV = "CLV_PLUGIN_PATH"

#: The package name user plugins are imported *under*.
#:
#: A module in ``~/.config/clv/plugins/`` is not reachable by dotted import,
#: and putting the directory on ``sys.path`` is not the answer: a user file
#: called ``json.py`` would then shadow the standard library for the whole
#: process. Instead a synthetic namespace package is installed in
#: ``sys.modules`` whose ``__path__`` is the search roots, and user plugins are
#: imported as its submodules.
#:
#: That buys four things at once, each of which would otherwise be bespoke
#: code here: a bare ``foo.py`` and a package directory ``foo/`` load through
#: one code path; relative imports inside a package plugin work; every module
#: gets a stable ``__name__`` for :func:`_extract_plugins` to compare against
#: in its namespace scan; and the whole walk is the same shape
#: :func:`_load_local` already uses, so the per-module body is shared rather
#: than copied.
USER_PLUGIN_PACKAGE = "clv_user_plugins"


# --- interfaces -------------------------------------------------------------


class Plugin(ABC):
    """Common metadata for every plugin type."""

    #: Human-readable name shown in the UI.
    name: str = "unnamed plugin"

    #: Optional CLV version constraint, e.g. ``">=2.0,<3.0"``. When set and
    #: unsatisfied, the plugin is rejected with a recorded error.
    requires_clv: Optional[str] = None

    #: Optional constraint on the *published API*, e.g. ``">=1.0,<2.0"``, in
    #: the same grammar as :attr:`requires_clv`. This is the one to reach for:
    #: what a plugin actually depends on is the shape of :mod:`clv.api`, and
    #: pinning CLV's own version instead means re-releasing on every CLV
    #: release that changed nothing a plugin can see.
    requires_api: Optional[str] = None

    #: Where this plugin sits in every ordered registry CLV keeps. Lower runs
    #: first; ties are broken by name, so the order is a function of what is
    #: installed rather than of what the filesystem happened to list first.
    #:
    #: 100 leaves room on both sides deliberately. A stage that must see a line
    #: before anything has touched it -- an audit log, a metric counter -- takes
    #: a low number; one that must see the final text takes a high one. Two
    #: plugins that do not care both take the default and compose by name, which
    #: is at least a rule their authors can predict.
    priority: int = 100

    def describe(self) -> str:
        return self.name

    # --- lifecycle ----------------------------------------------------------
    #
    # All three are optional and all three default to doing nothing, so a plugin
    # that implements none of them behaves exactly as it did before they
    # existed. Each is guarded exactly as `apply` is: raising disables the
    # plugin for the session and records the reason once.

    def configure(self, settings: Mapping[str, str]) -> None:
        """Receive this plugin's ``[plugin:<name>]`` section, if it has one.

        Called once, after instantiation and before :meth:`setup`. *settings* is
        a **read-only view of a mapping CLV owns**, not a copy: when CLV re-reads
        the settings file, the values behind it change and this plugin sees the
        new ones without being called again. Keep the mapping rather than
        copying out of it if freshness matters -- that is what lets the journal
        provider honour the Advanced drawer's switch without a restart.

        Empty when the operator wrote no section, which is the common case and
        must not be treated as an error.

        Values are the raw strings ``configparser`` read. Use
        :func:`setting_bool` and :func:`setting_list` rather than reimplementing
        them: CLV and a plugin disagreeing about whether ``yes`` is true is a
        bug an operator has no way to see.
        """

    def setup(self) -> None:
        """Acquire whatever this plugin needs, once, before it is first used.

        Called after every plugin has been loaded and configured. Anything
        opened here should be released in :meth:`teardown`.

        Not called again when an operator re-enables a plugin from the ``P``
        dialog: this is session lifecycle, not the enable switch.
        """

    def teardown(self) -> None:
        """Release what :meth:`setup` acquired. Called once, at shutdown.

        Runs after CLV has closed its readers and before the session is
        persisted, so a plugin cannot resurrect a source on its way out.

        An exception here is recorded and ignored. A *hang* is not bounded --
        see ``clv/plugins/AGENTS.md``.
        """


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """One source a provider offers, and who offered it.

    A provider's sources are **not** filesystem paths, even though they look
    like one: nothing on disk answers to ``journal:unit/sshd.service``. That is
    still true and still matters — it is what keeps them out of include/exclude
    globs (which describe a directory walk) and out of rotated-set grouping
    (which is name arithmetic over files that rotate).

    **What that exclusion used to cover, and no longer does.** Starring and
    merging were on the list, and they were on it for a reason that did not
    survive examination: a provider source was not a ``SourceRef``, so
    ``refs.is_source_ref`` could not see it, so it could not be starred. That
    was cheap rather than right. A journal unit is the clearest case of a source
    someone wants starred, and comparing one unit across a fleet is the workflow
    remote sources exist for. :class:`~clv.services.refs.JournalRef` is now a
    ref, so both work.

    **This record is still not a ref, and that is what keeps the rest honest.**
    It is the *tree node's* payload: :attr:`path` carries the identity and this
    carries the label and the provider name that error attribution needs. A
    provider offering something that genuinely is not a source identity still
    cannot reach the starred set, because the union is over concrete ref types
    rather than over "anything source-shaped" — which is why it is a union of
    types and not a duck test.
    """

    #: The source's identity. A ``JournalRef`` for the journal; a provider that
    #: has no ref type of its own may still hand back a ``Path``, which is what
    #: ``LogSourceProvider.discover``'s bare-identifier contract allows.
    path: SourceRef
    label: str
    #: The provider's own name, for the tree group and for error attribution.
    provider: str = ""

    @property
    def name(self) -> str:
        return self.label or self.path.name


class LogSourceProvider(Plugin):
    """Supplies log sources that the filesystem walker would not find."""

    @abstractmethod
    def discover(self) -> Iterable[Path | ProviderSource]:
        """Return the sources this provider offers.

        Either bare identifiers or :class:`ProviderSource` records, when the
        provider has a better label than the identifier's last component.
        """

    @abstractmethod
    def open(self, path: Path) -> Iterator[str]:
        """Yield the lines of *path*.

        The simple contract, and still the whole of it for a provider that
        produces a finite list of lines. A provider that tails something should
        implement :meth:`open_reader` instead; this is then never called.
        """

    def open_reader(self, path: Path, *, max_lines: int) -> Optional[Any]:
        """Return a ``prime``/``poll`` reader for *path*, or None.

        Optional. Returning None — the default — means "use :meth:`open`", and
        core wraps that iterator in a reader itself, so a provider written
        against the original interface keeps working untouched.

        Implement this when the source is a live stream rather than a list of
        lines: an iterator cannot express tailing, cannot be asked to stop, and
        has nowhere to put the cleanup a subprocess needs. The returned object
        must expose ``path``, ``prime()``, ``poll()`` and ``RELOAD_NOTICE``,
        and should expose ``close()`` when it holds anything.
        """

        return None


@dataclass(frozen=True, slots=True)
class FilterContext:
    """Read-only view of viewer state handed to each filter stage."""

    spec: FilterSpec
    source: Optional[Path] = None


class FilterStage(Plugin):
    """Transforms or drops entries before they reach the pane.

    Return the entry (optionally modified via :func:`dataclasses.replace`) to
    keep it, or ``None`` to drop it. Redaction is the common case::

        def apply(self, entry, context):
            if "password" not in entry.raw:
                return entry
            return replace(entry, raw=entry.raw.replace("password", "******"))
    """

    @abstractmethod
    def apply(self, entry: LogEntry, context: FilterContext) -> Optional[LogEntry]:
        """Return the entry to keep, or None to drop it."""


class LogFormat(Plugin):
    """Teaches CLV to parse a line no built-in matcher recognises.

    **Built-ins first, plugins second, ``raw`` last.** :meth:`parse` is offered
    only the lines every built-in matcher already declined, so a syslog file
    costs an installed format nothing, and a format cannot take over a name CLV
    already answers to. *Replacing* a built-in is out of scope: when a format is
    worth CLV's own attention it is added to
    :mod:`clv.services.parsing`, which is what Phase 7b of ``PLUGIN_TODO.md``
    does for logfmt.

    **This is the hottest third-party code in CLV.** ``apply()`` runs per entry
    per render; ``parse()`` runs per *line read*, once, and on a source that
    nothing recognises it runs for every line of the file. Make the cheap
    rejection first — a length test, a character at a fixed offset — and compile
    the regex once at class level, never inside ``parse``. The read-path budget
    (``plugin_read_budget_ms``) disables a format that cannot hold to that.

    Four declarations, and three of them exist because a ``format_name`` is not
    a fact about the parser alone. Without them an entry renders as a downgrade:
    the right timestamp and level, the bare identifier where the format name
    should be, no source cell and no chips. Equal terms with a built-in is a
    *declaration* here, not an inference::

        class NginxError(LogFormat):
            name = "nginx-error"
            format_name = "nginx-error"
            label = "nginx error log"
            field_names = frozenset({"pid", "tid", "client", "request"})
            columns = FormatProfile(source_keys=("client",), pid_key="pid")

            def parse(self, line):
                ...
    """

    #: The ``format_name`` every entry :meth:`parse` returns must carry. Not
    #: optional in practice: it is how the detail pane, the column profile and
    #: the query vocabulary find what this format declared, all of which happen
    #: before any line has been read. Rejected at load when empty, when it is
    #: ``"raw"``, when it is a name in
    #: :data:`clv.services.parsing.FORMAT_NAMES`, or when another loaded format
    #: has already claimed it.
    format_name: str = ""

    #: Field names this format can produce, so field queries and the query
    #: box's completions know them before a matching line has been seen.
    field_names: frozenset[str] = frozenset()

    #: What an operator calls this format in the detail pane. Without one the
    #: pane falls back to the bare :attr:`format_name` identifier.
    label: str = ""

    #: Which of :attr:`field_names` earns the source cell, a pinned chip or a
    #: varying chip in a structured row. ``FormatProfile()`` is a legal answer
    #: and means "message only" — but it has to be the author's answer rather
    #: than a default they never saw. A profile naming a key outside
    #: :attr:`field_names` is rejected at load, because a profile pointing at a
    #: field the format never produces is a row that quietly loses its source
    #: cell.
    columns: FormatProfile = DEFAULT_PROFILE

    @abstractmethod
    def parse(self, line: str) -> Optional[LogEntry]:
        """Return an entry for *line*, or None to leave it to the next format.

        The entry must carry this format's :attr:`format_name`, and its
        ``fields`` must be a mapping of strings to strings — values are compared
        as the parser stored them and nothing downstream coerces. Returning
        anything else takes the format out of service with a message naming the
        rule it broke: the read path cannot afford to trust this one, because a
        stage that misbehaves costs a render and a format that misbehaves
        corrupts the buffer.

        ``raw`` should be the line as it arrived. Continuation carry-forward
        needs no cooperation: an entry with a ``format_name`` other than
        ``"raw"`` is structured, so the unparsed line after it inherits its
        timestamp and level exactly as it would after a built-in's.
        """


class QueryOperator(Plugin):
    r"""Teaches the query box a new comparison token.

    The grammar has always been a closed set --
    :data:`~clv.services.query.BUILTIN_OPERATORS` and an if-chain over it -- so a
    plugin could produce a *field* and say nothing new *about* one. This is the
    token::

        class RegexMatch(QueryOperator):
            name = "field-regex"
            token = "~"

            def test(self, stored, value):
                return _compiled(value).search(stored) is not None

    ``svc~web\d+`` then works in the query box, in a saved view and in a watch
    rule, because all three route through ``parse_query``.

    **Vocabulary, not structure.** This adds a word, never a sentence shape.
    There is still no ``OR``, no parentheses and no precedence; see
    :mod:`clv.services.query`'s docstring for how narrow the reversal is.

    **The token is checked at load, and most of the rules are about ambiguity.**
    It may not be one of CLV's own -- redefining ``:`` would change what every
    saved query already means -- and it may not contain a character a *key* may
    contain (:data:`~clv.services.query.KEY_CHARS`), because ``foo`` + ``a`` +
    ``bar`` is indistinguishable from the key ``fooabar`` and the tokeniser has
    no way to prefer one reading. Whitespace and quote characters are out for
    the same reason. Longest token wins, so registering ``~`` cannot break
    ``>=`` and registering ``~=`` cannot break ``~``.

    **This runs per entry per render**, beside :class:`FilterStage`. Compile
    once and cache; the render budget (``plugin_time_budget_ms``) takes out a
    plugin that cannot hold to that.
    """

    #: The token that introduces this comparison, e.g. ``"~"`` or ``"!~"``.
    #: Rejected at load when empty, when it is a built-in, when it contains a
    #: key character, whitespace or a quote, or when another loaded operator has
    #: already claimed it.
    token: str = ""

    @abstractmethod
    def test(self, stored: str, value: str) -> bool:
        """Whether the field's *stored* value satisfies the query's *value*.

        Both are strings exactly as they were stored and typed -- nothing
        downstream coerces, which is why ``>=`` has to decide numeric versus
        lexicographic per comparison rather than per field.

        Raising takes the operator out of service for the session and is
        recorded once; terms using it then report the plugin by name rather
        than quietly matching nothing.
        """


class ComputedField(Plugin):
    """A queryable field derived rather than parsed.

    What gives the grammar genuinely new power without adding a DSL: the log
    line never said ``age``, but every entry has one::

        class Age(ComputedField):
            name = "entry-age"
            field_name = "age"

            def value(self, entry):
                if entry.timestamp is None:
                    return None
                return str(int((datetime.now() - entry.timestamp).total_seconds()))

    ``age<60`` then works everywhere a parsed field does, and the name is
    offered in the query box's completions before a line has been read.

    **Parsed fields resolve first, always.** :func:`~clv.services.query.match_terms`
    consults this only when the entry has no field of that name, per entry. So a
    computed field *may* share a name with a parsed one -- it is not rejected at
    load -- and on any line that actually carries the key the line wins. A plugin
    can add to what CLV can be asked; it can never change what a line said.

    Returning ``None`` means "this entry has no such field", which is a
    different answer from "does not match": the entry is hidden and counted into
    ``FilterStats.hidden_missing_field``, and the UI explains it.

    **Per entry, like** :class:`QueryOperator`, and under the same budget.
    """

    #: The name this field answers to in a query. Rejected at load when empty,
    #: when it is not a legal query key, or when another loaded plugin has
    #: already claimed it.
    field_name: str = ""

    @abstractmethod
    def value(self, entry: LogEntry) -> Optional[str]:
        """This field's value for *entry*, or ``None`` when it has none.

        Must be a string: values are compared as the parser stores them and
        nothing downstream coerces. Returning anything else takes the plugin out
        of service with a message naming the rule it broke.
        """


class ClusterRule(Plugin):
    r"""One more volatile token for the repeat clusterer to normalise out.

    Clustering folds lines that read the same once their volatile tokens are
    replaced by placeholders. The built-in list — quoted strings, timestamps,
    UUIDs, addresses, hex, paths, numbers — is fixed and ordered, and a log
    whose noisy token is none of those gets one cluster per line, which is the
    feature not working on exactly the log that needed it::

        class EmailAddress(ClusterRule):
            name = "email-address"
            pattern = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
            placeholder = "<email>"

    **Declarative on purpose: you supply the pattern, CLV runs it.** There is no
    method to implement and therefore no third-party call on a path that runs
    per line. Everything that could go wrong is checked when the plugin loads,
    which is the one time an author is there to read the message.

    What is checked, and why each one is invisible at runtime:

    * :attr:`pattern` must be a compiled pattern or a string this accepts and
      compiles. An unreadable regex is reported with its own error.
    * It may not match the **empty string**. ``\d*`` matches at every position,
      so a rule written that way would splice its placeholder between every
      character of every line.
    * :attr:`placeholder` may contain **no digit**. Plugin rules run after the
      built-ins, so a later plugin rule matching numbers would chew up an
      earlier one's placeholder — the reason CLV's own nine are digit-free.
    * It may contain **no backslash**: it is a :func:`re.sub` replacement
      template, so a group reference in it would splice in whatever the pattern
      captured, or raise on a group that does not exist.

    An **empty** placeholder is legal and means "delete this token" — stripping
    an ANSI colour run is a real rule, and it is the reason this is not refused
    as an oversight.

    **Ordering is append-only, and you are handed the line CLV has already
    normalised.** Plugin rules run after every built-in, in
    :func:`plugin_sort_key` order — the only position that cannot break the
    built-in order, in which each rule runs on what the previous left. It is
    also the trap: the obvious Kubernetes rule,
    ``re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b")``, never fires, because by
    the time it runs ``api-7d9f8b6c4-x2n9q`` is already ``api-<hex>-x2n9q``.
    Press ``c`` and look at what a line collapses to before writing a rule for
    it; ``clv/examples/cluster_rules.py`` works the case through.

    **Cost: one substitution per *distinct* line.** ``normalise`` is memoised,
    so a repeated line is shaped once however often it is rendered. The cluster
    budget (``plugin_time_budget_ms``) measures what that costs, and because
    the cache absorbs the repeats what it sees is the work that is actually new.
    """

    #: The regular expression this rule replaces. A compiled pattern, or a
    #: string CLV compiles at load. Rejected when absent, unreadable, or able to
    #: match the empty string.
    pattern: Any = ""

    #: What to write in its place. No digit, no backslash; empty means delete.
    placeholder: str = ""


class ShapeContributor(Plugin):
    """An extra component of the key two entries must share to cluster.

    A cluster's shape is the source, the level and the normalised message.
    Field *values* are deliberately not in it — a request ID differing between
    two lines is exactly what must not split a cluster — but sometimes a value
    is precisely what should::

        class ByUnit(ShapeContributor):
            name = "cluster-by-unit"

            def contribute(self, entry):
                return entry.fields.get("unit", "")

    Two units logging the same sentence then stay two clusters, and nothing
    about what a shape already means has changed.

    **A contributor that returns the same string for everything is a no-op.**
    That is what makes adding one safe: it can only ever split clusters further,
    never merge two that were apart.

    **Your answer must be a pure function of the entry.** CLV clusters the whole
    filtered set on a render and folds tailed lines into that same stream one at
    a time; a contribution that changes between those two calls makes the
    incremental path and a full recompute disagree about what belongs with what.
    Returning anything but a string takes the plugin out of service, because the
    value is composed into the key and a repr would give every entry a shape of
    its own.

    **Cost: per entry, per render, and memoised by nothing.** The shape cache is
    keyed on the line's text, which is not enough to remember an answer that
    depends on the whole entry — so this is the expensive half of the clustering
    seam and the half the budget is really there for. Read a field; do not
    compute one.
    """

    @abstractmethod
    def contribute(self, entry: LogEntry) -> str:
        """This entry's extra shape component, or ``""`` to add nothing.

        ``""`` adds nothing *at all* rather than an empty component, which is
        what lets a contributor answer for the entries it knows about and stay
        out of the way for the rest — and what makes a contributor taken out of
        service leave the shape byte-identical to a build without it.

        Raising takes the contributor out of service for the session and is
        recorded once; clustering then continues on the shape it had before the
        plugin was installed.
        """


class TimelineAnnotation(Plugin):
    """Marks on the timeline's axis: not how much happened, but what else did.

    The histogram (``b``) answers *when did this start*. An annotation answers
    *what was happening then* — a deploy, an incident, a maintenance window —
    which is the difference between a spike and a spike with a cause::

        class Deploys(TimelineAnnotation):
            name = "deploy-marks"

            def annotations(self, window):
                for moment, version in self._deploys:      # read at setup()
                    yield (moment, f"deploy {version}", "notice")

    The bucket a mark lands in is drawn underlined and in the mark's own
    severity colour, its label goes in the caption when that bucket is selected,
    and ``shift+←`` / ``shift+→`` step between marked buckets.

    **You are called on the event loop, and you may not do I/O.** This runs
    inside the render that draws the bar, so a provider that opens a socket
    blocks the pane — and it is charged against ``plugin_time_budget_ms`` like
    every other render-path plugin, so one that does it anyway strikes out and
    is switched off. Fetch in :meth:`~Plugin.setup`, or on a thread of your own,
    and answer this call from memory.

    **You are asked once per window, not once per render.** The answer is cached
    against the window the bar is showing and reused until the grid moves, so
    typing in the query box does not re-ask you. The cache is cleared whenever
    the plugin registry changes, which is how switching a provider off takes its
    marks off the bar.

    **Return what you have; CLV drops what does not fit.** A moment outside the
    window is not drawn — filtering by ``window`` yourself is an optimisation,
    not a requirement. At most
    :data:`~clv.services.timeline.MAX_ANNOTATIONS` marks are kept for one grid.

    The third element is a severity, normalised the way a parsed line's is, and
    ``None`` is the ordinary answer for something that is not a severity at all.
    """

    @abstractmethod
    def annotations(
        self, window: TimeWindow
    ) -> Iterable[tuple[datetime, str, Optional[str]]]:
        """``(moment, label, level)`` for the visible *window*.

        Anything that is not a three-element tuple of a :class:`~datetime.datetime`,
        a string and a severity-or-``None`` takes the plugin out of service with
        a message naming what it returned — the marks are drawn and captioned,
        and a provider that half-answers would put a mark of unknown meaning on
        the axis.

        Raising does the same and is recorded once; the bar then renders exactly
        as it did before the plugin was installed.
        """


class TimelineMetric(Plugin):
    """What a timeline bucket measures, when counting entries is the wrong unit.

    A hundred lines is not a hundred kilobytes, and a log where the interesting
    quantity is bytes, duration or retries gets a histogram that answers a
    question nobody asked::

        class BytesRead(TimelineMetric):
            name = "bytes-metric"
            metric_name = "bytes read"
            unit = "B"

            def value(self, entry):
                return float(entry.fields.get("bytes", 0) or 0)

    The bar is then scaled by the metric, and the caption leads with it and
    names this plugin — because a bar whose heights mean bytes looks exactly
    like a bar whose heights mean lines.

    **Your metric must be foldable, and that is why this interface is so thin.**
    :meth:`~clv.services.timeline.Timeline.extend` folds a newly tailed line
    into its bucket by arithmetic, which is what makes tailing cost what arrived
    rather than what is buffered. A sum survives that; a median, a percentile or
    a distinct count does not. So you declare a per-entry number and **CLV does
    the summing** — there is no ``aggregate()`` to implement, which makes a
    non-foldable metric unexpressible rather than quietly wrong on every line
    after the first render.

    ``None`` means "not measured by me" and contributes nothing, so a metric can
    answer for the entries it understands and stay out of the way for the rest.
    An entry with **no timestamp** is never passed here at all: it has no bucket,
    it is reported in ``undated``, and a metric does not get to change that.

    **One metric runs at a time.** Two enabled metrics is a conflict CLV
    resolves by :attr:`~Plugin.priority` and reports in the ``P`` dialog, naming
    the winner; the loser stays loaded and contributes nothing, and takes over
    if the winner is switched off. Losing a tie-break is not a fault and does
    not mark anything failed.

    **Cost: per entry, per rebuild, memoised by nothing.** The same budget and
    the same shape as a :class:`ShapeContributor` — read a field, do not compute
    one.
    """

    #: What this measures, in the words the caption will use — ``"bytes read"``,
    #: not ``"bytes_read_total"``. Required: a plugin that cannot name its
    #: metric is refused at load, because the caption would have no way to say
    #: what the bar is showing.
    metric_name: str = ""

    #: Printed after the scaled figure: ``unit = "B"`` reads as ``1.4 MB``.
    #: Optional, and empty prints a bare number. Prefixes are thousands-based,
    #: so a metric wanting binary ones does its own division and says so here.
    unit: str = ""

    @abstractmethod
    def value(self, entry: LogEntry) -> Optional[float]:
        """This entry's contribution, or ``None`` to measure nothing.

        Must be a finite real number. A bool is refused with a message rather
        than summed as 1 — ``1.0 if condition else 0.0`` is how you count a
        subset, and it says so where a ``True`` would not. A non-number, an
        infinity or a NaN takes the plugin out of service, because each one
        makes the bar's scale meaningless rather than merely wrong.

        Raising does the same and is recorded once; the bar then goes back to
        counting entries.
        """


class WatchMatcher(Plugin):
    """Teaches CLV a kind of watch rule that is not "this pattern matched".

    A rule declares a :attr:`~clv.services.watch.WatchRule.kind`; a matcher
    claims one. When they meet, CLV stops treating the rule's ``pattern`` as a
    query and hands it to this plugin untouched — it is **your** parameter
    string, in whatever spelling you document, and CLV neither parses nor
    validates it beyond asking :meth:`validate`.

    ``kind`` is matched casefolded, must not contain whitespace, and may not be
    ``"pattern"``: that one is CLV's and every rule ever saved already means
    something under it.

    **A matcher is offered lines.** :meth:`matches` is called once per newly
    arrived entry per enabled rule of this kind, so a rule kind that must fire
    because *nothing* arrived — a silence or absence rule — cannot be written
    against this interface. There is no tick and no clock here; holding state
    between calls is how a threshold or a burst kind is written, and that state
    is yours to bound.

    **Per entry, and charged for it.** The calls are measured against the read
    budget and three consecutive passes over it take the plugin out of service,
    exactly as a slow :class:`LogFormat` is. Make the cheap rejection first.
    """

    #: The rule kind this matcher claims, e.g. ``"burst"``. A matcher declaring
    #: nothing here is rejected at load, because no rule could ever reach it.
    kind: str = ""

    @abstractmethod
    def matches(self, entry: LogEntry, rule: WatchRule) -> bool:
        """Whether *entry* should fire *rule*.

        *rule* is the whole record, so ``rule.pattern`` is this matcher's
        parameter string and ``rule.name`` is what the operator called it —
        which is the key to use if this matcher keeps per-rule state.

        Never raise for something the operator typed. A malformed parameter is
        :meth:`validate`'s business; raising here disables the plugin for the
        session over a rule that can be fixed in the dialog.
        """

    def validate(self, pattern: str) -> Optional[str]:
        """Why *pattern* is not a usable parameter for this kind, or ``None``.

        Optional, and worth implementing: it is what puts the complaint where
        the operator typed it rather than leaving them a rule that silently
        never fires. Returning ``None`` — the default — means "anything goes".
        """

        return None


class WatchSink(Plugin):
    """Somewhere a watch hit is delivered, besides the toast.

    **You are fed the result of rate limiting, never the raw hits.** A rule
    matching five hundred lines inside one window reaches you once, with
    ``count=500``. That is not a convenience: a rule matching every line is the
    behaviour that makes people switch a feature like this off, and a sink that
    could bypass the coalescing would be able to do it to somebody else's
    inbox.

    **You do not run on the event loop.** Every sink gets a thread of its own,
    so blocking on a socket is allowed here in a way it is allowed nowhere else
    in CLV. There is a deadline: a sink that has not returned from one
    :meth:`deliver` within ``plugin_sink_timeout_ms`` is taken out of service
    and stops being fed. Its thread keeps running — CLV cannot kill a thread —
    which is why a sink that talks to the network should set its own timeouts
    rather than relying on this one.

    **You get a name and a count, and nothing else, unless you say otherwise.**
    Set :attr:`wants_entries` to receive a bounded sample of the matching lines.
    That declaration is shown to the operator in the ``P`` dialog, because a
    sink that reads log content and sends it somewhere is a thing they are
    entitled to know about before they enable it.

    **Egress is the operator's decision, not yours.** The convention for a sink
    that leaves the machine is the one the journal provider follows for reading
    it: ship inert, read your destination from your own ``[plugin:<name>]``
    section, and deliver nothing until it is set.
    """

    #: Whether :meth:`deliver` should receive the matching lines. Default False,
    #: and the default is the one to keep unless the sink genuinely cannot do
    #: its job without them.
    wants_entries: bool = False

    @abstractmethod
    def deliver(
        self,
        name: str,
        count: int,
        context: FilterContext,
        entries: Sequence[LogEntry] = (),
    ) -> None:
        """Deliver one rule's window.

        *name* is the rule's name and *count* is how many lines matched inside
        the window — the true count, whatever ``entries`` holds. *entries* is
        empty unless :attr:`wants_entries` is set, and is capped at
        ``SINK_SAMPLE_LIMIT`` when it is.
        """


@dataclass(frozen=True, slots=True)
class ExportResult:
    """What an exporter did, so the UI can report it."""

    ok: bool
    detail: str = ""
    destination: Optional[Path] = None


class Exporter(Plugin):
    """Sends the currently visible entries somewhere.

    **Two kinds, and the default is the self-routing one.** An exporter that
    knows where its output goes — a syslog forwarder, an HTTP endpoint, a fixed
    report path — implements ``export(entries, context)`` and reports what it
    did as :class:`ExportResult`. Nothing about that changed.

    An exporter that writes a *file* wants the destination the operator just
    typed, and until now could not have it: the export dialog hardcoded every
    plugin choice as supplying no path, disabled its path input, and said so.
    Setting :attr:`wants_path` re-enables the input and hands the chosen path to
    :meth:`export` as ``destination``.
    """

    #: Whether the export dialog should ask the operator for a destination and
    #: pass it to :meth:`export`. False — the default, and what every exporter
    #: written before this attribute existed gets — means the dialog's path
    #: input stays disabled and ``destination`` is never passed, so an
    #: ``export(self, entries, context)`` written against the original
    #: interface keeps working untouched.
    wants_path: bool = False

    #: Suffix the dialog's suggested filename gets when :attr:`wants_path` is
    #: set, without a leading dot (``"ndjson"``). Empty falls back to ``log``,
    #: which is what the built-in formats do.
    suggested_extension: str = ""

    @abstractmethod
    def export(
        self,
        entries: Sequence[LogEntry],
        context: FilterContext,
        *,
        destination: Optional[Path] = None,
    ) -> ExportResult:
        """Write or transmit *entries*.

        *destination* is the path the operator chose, and is passed **only**
        when the exporter set :attr:`wants_path`. Keyword-only so that adding it
        could not change what an existing positional call means.
        """


# --- adapting the simple contract -------------------------------------------


class IteratorReader:
    """Turns a provider's ``open()`` iterator into a reader.

    So that the older, simpler half of :class:`LogSourceProvider` keeps working
    now that core expects ``prime``/``poll``. The iterator is drained up to the
    line budget on prime and then drained further on each poll, which is enough
    for a provider that yields a finite list and honest for one that does not:
    a generator that blocks would block the poll, which is why a provider that
    tails should implement ``open_reader`` instead.
    """

    RELOAD_NOTICE = "{name} was reloaded."

    def __init__(self, path: Path, lines: Iterator[str], *, max_lines: int) -> None:
        self.path = path
        self._lines = lines
        self._max_lines = max_lines
        self._offset = 0
        self._exhausted = False

    @property
    def offset(self) -> int:
        return self._offset

    def _drain(self, limit: int) -> list[str]:
        collected: list[str] = []
        if self._exhausted:
            return collected
        for line in self._lines:
            collected.append(str(line).rstrip("\n"))
            if len(collected) >= limit:
                return collected
        self._exhausted = True
        return collected

    def prime(self):
        from ..services.reader import TailRead

        lines = self._drain(self._max_lines)
        self._offset = len(lines)
        return TailRead(lines=lines, offset=self._offset)

    def poll(self):
        from ..services.reader import TailRead

        lines = self._drain(self._max_lines)
        self._offset += len(lines)
        return TailRead(lines=lines, offset=self._offset)

    def close(self) -> None:
        closer = getattr(self._lines, "close", None)
        if closer is not None:
            closer()


# --- version constraints ----------------------------------------------------
#
# A PEP 440 subset, hand-rolled. ``packaging`` is not a dependency and will not
# become one — the minimal-dependency policy is not relaxed for the plugin work.
#
# The comparator this replaces stripped non-digits per segment, so "2.6.0rc1"
# became (2, 6, 1) and a release candidate compared as *newer* than its own
# release. It also rejected `~=` and `^` outright, which is not "unsatisfied" but
# "unparseable" — and it returned False for both, silently disabling a plugin
# whose author wrote the most idiomatic constraint in the ecosystem. Both of
# those are failures a plugin author cannot diagnose from the outside, which is
# why the replacement is a real grammar rather than a wider regex.
#
# **One deliberate divergence from PEP 440: prereleases are always considered.**
# Strict PEP 440 excludes a prerelease from a range unless the range itself names
# one, so `requires_clv=">=2.6"` would be *unsatisfied* on a running 2.7.0rc1 and
# every plugin would vanish on any release-candidate build. CLV compares versions
# in plain order instead. Documented in clv/plugins/AGENTS.md.

_VERSION_RE = re.compile(
    r"""^\s*
    v?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_letter>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_num>\d+)?)?
    (?:[-_.]?post[-_.]?(?P<post_num>\d+)?|-(?P<post_bare>\d+))?
    (?:[-_.]?dev[-_.]?(?P<dev_num>\d+)?)?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

#: Prerelease spellings that mean the same thing, and their order.
_PRE_STAGES = {
    "a": 0, "alpha": 0,
    "b": 1, "beta": 1,
    "c": 2, "rc": 2, "pre": 2, "preview": 2,
}

#: Sort-key components. A dev release with no prerelease sorts *before* every
#: prerelease of the same version; a version with no prerelease at all sorts
#: after all of them; "no dev segment" sorts after any dev segment.
_NO_PRE_BUT_DEV = (-1, 0)
_FINAL = (99, 0)
_NO_DEV = 1 << 62

#: Longest first, so ">=" is never read as ">" with a stray "=".
_OPERATORS = (">=", "<=", "==", "!=", "~=", "^", ">", "<")


@dataclass(frozen=True, slots=True)
class _Version:
    """A parsed version, in the shape the sort key needs."""

    release: tuple[int, ...]
    pre: Optional[tuple[int, int]]
    post: Optional[int]
    dev: Optional[int]

    def key(self, width: int) -> tuple:
        """Order-preserving key, padded so 2.0 and 2.0.0 compare equal.

        Orders 1.0.dev1 < 1.0a1.dev1 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 < 1.0.post1.
        """

        release = self.release + (0,) * (width - len(self.release))
        if self.pre is not None:
            pre = self.pre
        elif self.dev is not None and self.post is None:
            pre = _NO_PRE_BUT_DEV
        else:
            pre = _FINAL
        return (
            release,
            pre,
            -1 if self.post is None else self.post,
            _NO_DEV if self.dev is None else self.dev,
        )


def _parse_version(text: str) -> Optional[_Version]:
    """Parse a version, or None if it is not one. Never raises."""

    match = _VERSION_RE.match(text)
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))

    letter = match.group("pre_letter")
    pre = (
        (_PRE_STAGES[letter.lower()], int(match.group("pre_num") or 0))
        if letter
        else None
    )

    if match.group("post_num") is not None:
        post: Optional[int] = int(match.group("post_num"))
    elif match.group("post_bare") is not None:
        post = int(match.group("post_bare"))
    elif re.search(r"post", text, re.IGNORECASE):
        post = 0  # a bare ".post" means post 0
    else:
        post = None

    if match.group("dev_num") is not None:
        dev: Optional[int] = int(match.group("dev_num"))
    elif re.search(r"dev", text, re.IGNORECASE):
        dev = 0  # a bare ".dev" means dev 0
    else:
        dev = None

    return _Version(release, pre, post, dev)


def _compare(left: _Version, right: _Version) -> int:
    """-1, 0 or 1, comparing on equal release width."""

    width = max(len(left.release), len(right.release))
    a, b = left.key(width), right.key(width)
    return (a > b) - (a < b)


def _release_prefix_match(current: _Version, target: tuple[int, ...]) -> bool:
    """Whether *current*'s release starts with *target* — the ``==2.6.*`` test."""

    padded = current.release + (0,) * (len(target) - len(current.release))
    return padded[: len(target)] == target


def _split_operator(piece: str) -> tuple[str, str]:
    """Split a constraint piece into its operator and operand.

    A bare version means ``==``, which is what the previous comparator did and
    what an author writing ``requires_clv = "2.6.0"`` means.
    """

    stripped = piece.strip()
    for operator in _OPERATORS:
        if stripped.startswith(operator):
            return operator, stripped[len(operator):].strip()
    return "==", stripped


def _expand(operator: str, operand: str, constraint: str) -> list[tuple[str, str]]:
    """Rewrite ``~=`` and ``^`` into the plain comparisons they stand for.

    ``~=X.Y`` is ``>=X.Y, ==X.*``; ``~=X.Y.Z`` is ``>=X.Y.Z, ==X.Y.*``. Poetry's
    ``^`` is accepted as a documented alias and expands to the next significant
    release: ``^2.0.0`` is ``>=2.0.0,<3.0.0``, ``^0.2.3`` is ``>=0.2.3,<0.3.0``,
    and ``^0.0.3`` is ``>=0.0.3,<0.0.4``.
    """

    if operator == "~=":
        if operand.endswith(".*"):
            raise ValueError(
                f"unparseable constraint {constraint!r}: ~= cannot take a wildcard"
            )
        parsed = _parse_version(operand)
        if parsed is None or len(parsed.release) < 2:
            raise ValueError(
                f"unparseable constraint {constraint!r}: ~= needs at least two "
                f"release segments, got {operand!r}"
            )
        prefix = ".".join(str(part) for part in parsed.release[:-1])
        return [(">=", operand), ("==", f"{prefix}.*")]

    if operator == "^":
        parsed = _parse_version(operand)
        if parsed is None:
            raise ValueError(f"unparseable constraint {constraint!r}")
        release = parsed.release + (0,) * (3 - len(parsed.release))
        upper = [1]
        for index, part in enumerate(release):
            if part != 0:
                upper = list(release[: index + 1])
                upper[index] += 1
                break
        return [(">=", operand), ("<", ".".join(str(part) for part in upper))]

    return [(operator, operand)]


def satisfies(version: str, constraint: Optional[str]) -> bool:
    """Check *version* against a comma-separated constraint like ``>=2.0,<3.0``.

    Supported: ``>=``, ``<=``, ``>``, ``<``, ``==``, ``!=``, the ``==X.Y.*``
    wildcard, ``~=`` compatible-release, and ``^`` as a documented Poetry alias.
    Pieces are ANDed. An empty or absent constraint means "any version".

    Raises :class:`ValueError` naming the constraint when it cannot be parsed.
    That is deliberate and is the point of the rewrite: the previous comparator
    returned a silent ``False`` for anything it did not recognise, so a typo and
    a genuinely incompatible plugin were indistinguishable — both simply vanished.
    Callers record the error against the plugin; see :meth:`PluginRegistry.add`.
    """

    if not constraint or not constraint.strip():
        return True

    current = _parse_version(version)
    if current is None:
        raise ValueError(f"unparseable version {version!r}")

    for piece in constraint.split(","):
        if not piece.strip():
            continue
        operator, operand = _split_operator(piece)
        if not operand:
            raise ValueError(f"unparseable constraint {constraint!r}")

        for op, value in _expand(operator, operand, constraint):
            if value.endswith(".*"):
                if op not in ("==", "!="):
                    raise ValueError(
                        f"unparseable constraint {constraint!r}: "
                        f"{op} cannot take a wildcard"
                    )
                body = value[:-2]
                if not re.fullmatch(r"\d+(?:\.\d+)*", body):
                    raise ValueError(f"unparseable constraint {constraint!r}")
                matched = _release_prefix_match(
                    current, tuple(int(part) for part in body.split("."))
                )
                if (op == "==" and not matched) or (op == "!=" and matched):
                    return False
                continue

            target = _parse_version(value)
            if target is None:
                raise ValueError(f"unparseable constraint {constraint!r}")
            order = _compare(current, target)
            if not {
                ">=": order >= 0,
                "<=": order <= 0,
                ">": order > 0,
                "<": order < 0,
                "==": order == 0,
                "!=": order != 0,
            }[op]:
                return False

    return True


# --- registry ---------------------------------------------------------------


#: How many *distinct* problems the registry keeps. Anything past this is
#: counted, not stored: the collection exists to tell an operator what is wrong,
#: and a list long enough to scroll has stopped doing that.
MAX_PLUGIN_ERRORS = 50


#: What kind of problem a :class:`PluginError` records. A string rather than an
#: enum, to match ``ConfigIssue.severity`` next door and to stay printable.
#:
#: The distinction is not cosmetic: "this plugin is broken" and "this plugin
#: wants a CLV you are not running" call for different actions from the
#: operator, and the management UI must not have to *parse the message* to tell
#: them apart. ``"load"`` is the unremarkable case and stays the default.
#: ``conflict`` is the odd one and is deliberately not a fault: it is two
#: plugins competing for something only one of them can have — today, the one
#: metric a timeline bucket measures. CLV picks by ``priority`` and says so, the
#: loser stays loaded and healthy, and :meth:`PluginRegistry.status` leaves the
#: row's state alone and shows the note as its detail.
ERROR_CATEGORIES = (
    "load",
    "incompatible",
    "shadowed",
    "missing",
    "runtime",
    "conflict",
)


@dataclass
class PluginError:
    origin: str
    message: str
    #: How many times this exact problem happened. Filled by
    #: :meth:`PluginErrors.append`; a plugin that fails per render used to
    #: append a fresh identical error every pass.
    count: int = 1
    #: One of :data:`ERROR_CATEGORIES`. Deliberately **not** part of the
    #: identity used to collapse repeats: the same origin reporting the same
    #: message is the same problem however it is classified, and a category
    #: that split the dedup key would let one fault grow two entries.
    category: str = "load"

    def __str__(self) -> str:  # pragma: no cover - trivial
        repeats = f" (×{self.count})" if self.count > 1 else ""
        return f"{self.origin}: {self.message}{repeats}"


class PluginErrors(Sequence[PluginError]):
    """Everything that went wrong, deduplicated and bounded.

    A plain list was wrong in both directions. It grew without limit — and
    :meth:`PluginRegistry.apply_filters` runs per render, so a single raising
    stage produced one error per pass and 200 passes produced 200 identical
    entries — and every one of them was printed into the log panel, where a wall
    of repeats buries the discovery summary the operator actually opened CLV to
    read.

    Identical ``(origin, message)`` pairs collapse into one entry with a count.
    Past :data:`MAX_PLUGIN_ERRORS` distinct problems the rest are counted in
    :attr:`dropped` and reported by :attr:`overflow_note`, so the collection
    never lies about how much it is not showing.

    Deliberately list-like: it is appended to from ``app.py`` as well as from
    here, indexed, sliced and truth-tested, and none of those call sites should
    have to care that it is no longer a list.
    """

    __slots__ = ("_errors", "_index", "_dropped")

    def __init__(self, errors: Optional[Iterable[PluginError]] = None) -> None:
        self._errors: list[PluginError] = []
        self._index: dict[tuple[str, str], PluginError] = {}
        self._dropped = 0
        for error in errors or ():
            self.append(error)

    def append(self, error: PluginError) -> None:
        """Record *error*, collapsing a repeat into the entry already held."""

        key = (error.origin, error.message)
        existing = self._index.get(key)
        if existing is not None:
            existing.count += error.count
            return
        if len(self._errors) >= MAX_PLUGIN_ERRORS:
            self._dropped += 1
            return
        self._index[key] = error
        self._errors.append(error)

    @property
    def dropped(self) -> int:
        """Distinct problems the cap refused to store."""

        return self._dropped

    @property
    def overflow_note(self) -> str:
        """``"and N more"`` when the cap dropped something, else ``""``."""

        if not self._dropped:
            return ""
        return f"and {self._dropped} more"

    def discard(self, origin: str, *, category: Optional[str] = None) -> int:
        """Forget everything recorded against *origin*. Returns how many went.

        The counterpart to :meth:`PluginRegistry.enable`. A plugin taken out of
        service by a fault keeps the fault on the record, which is right up
        until the operator puts it back — after that the entry describes a state
        that no longer holds, and, worse, it is still in :attr:`_index`, so the
        *next* genuine failure would collapse into it and be reported as a
        repeat of something already dealt with rather than as news.

        *category* narrows it, so re-enabling clears the runtime fault without
        also erasing the load-time diagnosis that is still true.
        """

        doomed = [
            error
            for error in self._errors
            if error.origin == origin
            and (category is None or error.category == category)
        ]
        for error in doomed:
            self._errors.remove(error)
            self._index.pop((error.origin, error.message), None)
        return len(doomed)

    def clear(self) -> None:
        self._errors.clear()
        self._index.clear()
        self._dropped = 0

    def __getitem__(self, index):  # type: ignore[override]
        return self._errors[index]

    def __len__(self) -> int:
        return len(self._errors)

    def __iter__(self) -> Iterator[PluginError]:
        return iter(self._errors)

    def __eq__(self, other: Any) -> Any:
        """Compare equal to a plain list, so ``errors == []`` still reads right."""

        if isinstance(other, PluginErrors):
            return self._errors == other._errors
        if isinstance(other, (list, tuple)):
            return self._errors == list(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # mutable, like the list it replaces

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PluginErrors({self._errors!r}, dropped={self._dropped})"


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    """A plugin CLV found in a user root, whether or not it was loaded.

    New state, and deliberately not a :class:`PluginError`. Being installed but
    not enabled is the *designed* resting state of a user plugin -- it is what
    "installing a plugin is not consent to run it" looks like from the
    registry's side -- and ``app.py`` prints every error into the log panel in
    amber as a problem. A plugin waiting to be named is not a problem.

    A shadowed entry keeps :attr:`shadowed_by` so the report can say which
    origin won the name rather than only that something did.
    """

    #: The module name as found on disk, without ``.py``.
    name: str
    #: The search root it was found in.
    root: Path
    #: Whether it was named in the enable-list and therefore imported.
    enabled: bool
    #: A directory with an ``__init__.py`` rather than a single file.
    is_package: bool = False
    #: The origin that claimed this name first, if this one lost it.
    shadowed_by: Optional[str] = None


#: The reason recorded when an operator turns a plugin off from the management
#: UI, as opposed to a fault taking it out of service. Compared against rather
#: than merely displayed: a fault always leaves a :class:`PluginError` behind
#: and an operator's decision never does, so this is what tells
#: :meth:`PluginRegistry.status` whether a disabled plugin is *broken* or simply
#: *switched off* — two rows that must not read the same.
OPERATOR_DISABLE_REASON = "turned off by the operator"

#: Every state a plugin can be in, as the management UI names them.
#:
#: ``"isolated"`` is here and is never produced. Phase 13 of ``PLUGIN_TODO.md``
#: fills it in; naming it now is what stops the row layout being redesigned then.
PLUGIN_STATES = ("loaded", "not enabled", "failed", "incompatible", "isolated")


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """A live plugin, and the module it came out of.

    :meth:`PluginRegistry.add` files plugins into :attr:`~PluginRegistry.sources`,
    :attr:`~PluginRegistry.filters` and :attr:`~PluginRegistry.exporters` by
    kind, and nothing there records *where each one came from*. Enable and
    disable need that: the enable-list names a module, one module may export
    several plugins, and turning it off has to reach all of them.
    """

    #: Excluded from equality: a plugin is third-party code and may define
    #: ``__eq__`` however it likes, which is not something a registry record
    #: should inherit.
    plugin: Any = field(compare=False)
    #: The plugin's own name, as :func:`_plugin_name` reads it.
    name: str
    #: The origin string its loader used — a path, a dotted module, or
    #: ``clv.plugins:<entry point>``.
    origin: str
    #: Which interfaces it was filed under, in :data:`_KINDS` order.
    kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginStatus:
    """One row of the management UI: an installable unit and how it is doing.

    The unit is the **origin**, not the plugin object, because that is what an
    operator installs, names in ``settings.conf`` and deletes. One module
    exporting three stages is one row that says ``filter``, not three rows.

    Built by :meth:`PluginRegistry.status` and handed to the dialog, which flips
    :attr:`enabled` and :attr:`reinstate` on a copy and hands the set back. The
    dialog decides nothing: the app diffs the two and does the work.
    """

    #: What the operator would write in ``settings.conf`` — the module name for
    #: a user plugin, the last dotted segment for a bundled one.
    name: str
    #: The full origin, shown as the row's detail line.
    origin: str
    #: ``"user"``, ``"bundled"`` or ``"entry point"``. Decides whether turning
    #: this row off is written to ``settings.conf`` or lasts for the session.
    source: str
    #: The interfaces this origin supplies, aggregated over its plugins.
    kinds: tuple[str, ...] = ()
    #: One of :data:`PLUGIN_STATES`.
    state: str = "loaded"
    #: The recorded message, the unsatisfied constraint, or what shadowed it —
    #: in full, because being truncated into a shared line is the problem this
    #: whole surface exists to fix.
    detail: str = ""
    #: The category of the error driving :attr:`state`, so the dialog knows
    #: whether Re-enable applies without reading :attr:`detail`.
    category: str = ""
    #: Whether this origin supplies a :class:`WatchSink` that asked to be handed
    #: log lines. Shown in the row, because "this plugin reads your log content
    #: and sends it where it is configured to" is the single fact an operator
    #: most needs before enabling something, and it is knowable from the
    #: declaration without running anything.
    reads_content: bool = False
    #: Working copy. True when this plugin should be running.
    enabled: bool = True
    #: Working copy. Set when the operator asks for a fault-disabled plugin to
    #: be put back into service.
    reinstate: bool = False


#: Interface name to the registry list it is filed in, in the order a row lists
#: them. One definition, so ``add`` and ``status`` cannot disagree about what
#: kinds exist.
_KINDS: tuple[tuple[str, type], ...] = (
    ("source", LogSourceProvider),
    ("format", LogFormat),
    ("operator", QueryOperator),
    ("computed field", ComputedField),
    ("filter", FilterStage),
    ("cluster rule", ClusterRule),
    ("shape", ShapeContributor),
    ("timeline", TimelineAnnotation),
    ("metric", TimelineMetric),
    ("matcher", WatchMatcher),
    ("sink", WatchSink),
    ("exporter", Exporter),
)


def _origin_source(origin: str) -> str:
    """Which of the three search roots *origin* came from.

    Read off the string rather than recorded at load time, because ``add()``
    takes an origin and nothing else — and every loader already spells its
    origins distinctly: ``clv.plugins:<name>`` with a colon for an entry point,
    ``clv.plugins.<name>`` with a dot for a bundled drop-in, and a filesystem
    path for anything in a user root.
    """

    if origin == ENTRY_POINT_GROUP or origin.startswith(f"{ENTRY_POINT_GROUP}:"):
        return "entry point"
    if origin.startswith(f"{ENTRY_POINT_GROUP}."):
        return "bundled"
    return "user"


def _origin_label(origin: str, source: str) -> str:
    """The short name for *origin* — what an operator would type or look for."""

    if source == "entry point":
        _, _, name = origin.partition(":")
        return name or origin
    if source == "bundled":
        return origin.rpartition(".")[2] or origin
    # A path, or the bare name of a plugin that settings.conf asked for and that
    # was never found on disk.
    return Path(origin).name if os.sep in origin or "/" in origin else origin


#: Passes in a row a plugin must run over its budget before it is taken out of
#: service.
#:
#: Consecutive, not cumulative, and not one. The first render after a source
#: opens pays every cold cost a plugin has — a ``re.compile``, a file read, an
#: import a lazy author deferred — and a large paste or a loaded CI box can put
#: an otherwise healthy stage over the line for a pass or two. Disabling on the
#: first breach would take those plugins out of service and leave Phase 4's
#: Re-enable as the only way back. Three in a row is a plugin that is slow, not
#: a plugin that was unlucky.
_BUDGET_STRIKES = 3


class PluginBudget:
    """Cumulative wall time per plugin per pass, judged against a ceiling.

    One instance per *path*, not per plugin. The render path — every
    :class:`FilterStage` over every buffered entry, once per keystroke in the
    query box — gets one, built by the app from ``plugin_time_budget_ms``.
    There are five: the render path, the read path (``LogFormat.parse`` over one
    batch of lines), the query plugins, the watch matchers, and the clustering
    plugins. Two ceilings and one policy between them — the same class with a
    different label and a different key, so no path restates the rule and they
    cannot drift apart.

    A pass is ``start()``, some number of ``charge()`` calls, ``settle()``.
    Time is attributed to the plugin rather than to the call, because "which
    plugin is costing me this" is the question an operator has and "which of
    five thousand calls" is not.

    Exceeding the budget is not itself a fault: :attr:`_over` counts *consecutive*
    passes and any pass under the line clears it. When the count reaches
    :data:`_BUDGET_STRIKES` the plugin goes through :meth:`PluginRegistry.disable`
    — the same mechanism a raising stage has used since Phase 1, recording once,
    surfacing in the plugins modal as ``failed`` with a ``runtime`` category, and
    reachable by Re-enable. A time budget is a reason to disable a plugin, not a
    reason to invent a second way of disabling one.

    A ``limit_ms`` of zero or less turns the guard off entirely. That is the
    documented escape hatch for an operator who would rather have a slow plugin
    than a disabled one, and callers check :attr:`active` first, so switching it
    off also costs no measurement.
    """

    __slots__ = ("_registry", "limit_ms", "label", "_strikes", "_pass", "_seen", "_over")

    def __init__(
        self,
        registry: "PluginRegistry",
        *,
        limit_ms: float,
        label: str,
        strikes: int = _BUDGET_STRIKES,
    ) -> None:
        self._registry = registry
        self.limit_ms = float(limit_ms)
        self.label = label
        self._strikes = max(1, int(strikes))
        #: Seconds charged to each plugin in the pass currently open.
        self._pass: dict[int, float] = {}
        #: ``id(plugin) -> plugin`` for the pass currently open, so
        #: :meth:`settle` can name what it is disabling. Keyed on identity for
        #: the reason :attr:`PluginRegistry._disabled` is: a plugin is
        #: third-party code and may define ``__eq__`` without ``__hash__``.
        self._seen: dict[int, Any] = {}
        #: Consecutive over-budget passes per plugin. Survives a pass; cleared
        #: for a plugin the moment one of its passes comes in under the line.
        self._over: dict[int, int] = {}

    @property
    def active(self) -> bool:
        """Whether this budget measures anything at all."""

        return self.limit_ms > 0

    def start(self) -> None:
        """Open a pass."""

        self._pass.clear()
        self._seen.clear()

    def charge(self, plugin: Any, seconds: float) -> None:
        """Attribute *seconds* of the open pass to *plugin*."""

        key = id(plugin)
        self._pass[key] = self._pass.get(key, 0.0) + seconds
        self._seen[key] = plugin

    def settle(self) -> None:
        """Close the pass, and disable anything that has now struck out."""

        if not self.active:
            # Callers check `active` before they measure, so this is normally
            # unreachable -- but a budget that is switched off has to be off
            # however it is driven, not only when the caller remembers.
            self._pass.clear()
            self._seen.clear()
            return

        limit = self.limit_ms / 1000.0
        for key, elapsed in self._pass.items():
            plugin = self._seen[key]
            if elapsed <= limit:
                self._over.pop(key, None)
                continue
            strikes = self._over.get(key, 0) + 1
            if strikes < self._strikes:
                self._over[key] = strikes
                continue
            # Cleared rather than left at the limit: if the operator re-enables
            # this plugin it starts again with a clean count, which is the only
            # reading of Re-enable that is not a lie.
            self._over.pop(key, None)
            self._registry.disable(
                plugin,
                f"over the {self.label} budget "
                f"({elapsed * 1000:.0f} ms against {self.limit_ms:.0f} ms) "
                f"on {self._strikes} consecutive passes",
                origin=_plugin_name(plugin),
            )
        self._pass.clear()
        self._seen.clear()

    def forget(self, plugin: Any) -> None:
        """Drop *plugin*'s strike count. Called when it is put back in service."""

        self._over.pop(id(plugin), None)


@dataclass
class PluginRegistry:
    """Everything successfully loaded, plus everything that failed to load."""

    sources: list[LogSourceProvider] = field(default_factory=list)
    #: Plugin-supplied parsers, in :func:`plugin_sort_key` order. Consulted only
    #: for a line every built-in matcher declined — see :class:`LogFormat`.
    formats: list[LogFormat] = field(default_factory=list)
    #: Plugin-supplied comparison tokens and derived fields, in
    #: :func:`plugin_sort_key` order. Consulted per entry, and only through the
    #: specs :meth:`query_stack` hands to ``clv.services.query``.
    operators: list[QueryOperator] = field(default_factory=list)
    computed: list[ComputedField] = field(default_factory=list)
    filters: list[FilterStage] = field(default_factory=list)
    #: Plugin-supplied normalisation rules and shape components, in
    #: :func:`plugin_sort_key` order. A rule is consulted per *distinct* line
    #: behind the shape cache; a contributor per entry, behind nothing. Both are
    #: reached only through the specs :meth:`cluster_stack` hands to
    #: ``clv.services.clustering``.
    rules: list[ClusterRule] = field(default_factory=list)
    contributors: list[ShapeContributor] = field(default_factory=list)
    #: Plugin-supplied marks on the time axis and the one metric a bucket
    #: measures, in :func:`plugin_sort_key` order. A provider is asked once per
    #: window; a metric is consulted per entry per rebuild. Both are reached
    #: only through the specs :meth:`timeline_stack` hands to
    #: ``clv.services.timeline``.
    annotators: list[TimelineAnnotation] = field(default_factory=list)
    metrics: list[TimelineMetric] = field(default_factory=list)
    #: Plugin-supplied rule kinds and delivery destinations, in
    #: :func:`plugin_sort_key` order. Reached only through the specs
    #: :meth:`watch_stack` hands to ``clv.services.watch``.
    matchers: list[WatchMatcher] = field(default_factory=list)
    sinks: list[WatchSink] = field(default_factory=list)
    exporters: list[Exporter] = field(default_factory=list)
    errors: PluginErrors = field(default_factory=PluginErrors)
    #: Every plugin found in a user root, loaded or not, in search order.
    #: Empty on a build with no user plugin directory, which is the common case
    #: and stays free: nothing is imported to fill this in.
    discovered: list[DiscoveredPlugin] = field(default_factory=list)
    #: Every plugin that loaded, paired with the module it came from, in load
    #: order. The three lists above are keyed by *kind* and a plugin appears in
    #: as many of them as it implements; this is the one place a live plugin can
    #: be traced back to the thing an operator installed.
    loaded: list[LoadedPlugin] = field(default_factory=list)
    #: Which provider offered which source, filled by `discover_sources`. Keyed
    #: on **(provider name, path)**, because a source identifier means nothing
    #: without the provider that coined it: keyed on the path alone, the second
    #: provider to offer an identifier silently replaced the first and selecting
    #: provider A's row in the tree opened provider B's lines.
    _owners: dict[tuple[str, Path], LogSourceProvider] = field(
        default_factory=dict, repr=False
    )
    #: Plugins taken out of service, ``id(plugin) -> reason``. Keyed on identity
    #: because a plugin is third-party code that may define ``__eq__`` without
    #: ``__hash__``; the registry's own lists hold every plugin alive for the
    #: session, so an id cannot be recycled underneath this map.
    _disabled: dict[int, str] = field(default_factory=dict, repr=False)
    #: Each plugin's ``[plugin:<name>]`` section, keyed on the casefolded name
    #: the operator writes in ``settings.conf``.
    #:
    #: These dicts are **handed out as read-only views and then mutated in
    #: place**, which is the whole mechanism: a plugin keeps the view it was
    #: given at ``configure()`` time, CLV updates the dict behind it whenever it
    #: re-reads the settings file, and the plugin sees the new value without a
    #: second hook and without reading a file itself. Replacing a dict here
    #: instead of clearing it would strand every view already handed out.
    _settings: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    #: Section names claimed by a plugin that actually loaded. What is left in
    #: :attr:`_settings` after a load is a section configuring nothing, which is
    #: reported -- an operator who tuned a plugin that is not running should be
    #: told, not left wondering why the setting does nothing.
    _configured: set[str] = field(default_factory=set, repr=False)
    #: Whether :meth:`start` has run. ``setup()`` is once per session.
    _started: bool = field(default=False, repr=False)
    #: Whether :meth:`shutdown` has run. ``teardown()`` is once per session, and
    #: ``on_unmount`` is not guaranteed to fire exactly once.
    _stopped: bool = field(default=False, repr=False)
    #: Bumped whenever what this registry would *do* to an entry changes.
    #: See :attr:`generation`.
    _generation: int = field(default=0, repr=False)

    @property
    def total(self) -> int:
        return (
            len(self.sources)
            + len(self.formats)
            + len(self.operators)
            + len(self.computed)
            + len(self.filters)
            + len(self.rules)
            + len(self.contributors)
            + len(self.annotators)
            + len(self.metrics)
            + len(self.matchers)
            + len(self.sinks)
            + len(self.exporters)
        )

    @property
    def generation(self) -> int:
        """A counter that moves whenever the plugins' output could change.

        Every cache downstream of a plugin keys on this. The staged view in
        ``app.py`` is one; the clustering shape cache
        (:func:`clv.services.clustering.normalise`) is the one that makes it
        necessary, because it is an ``lru_cache`` on a module-level function and
        ``PLUGIN_TODO.md`` Phase 10 feeds it plugin-supplied rules — without a
        generation it would go on serving pre-plugin shapes after a rule was
        switched on, which is a wrong answer rather than a stale one.

        Moves on: a plugin being disabled or re-enabled, the end of a load, and
        a settings refresh. That last one is easy to leave out and would be a
        bug: ``[plugin:<name>]`` sections are handed over as live mappings a
        plugin reads *through* (see :attr:`_settings`), so editing a redaction
        pattern changes what a stage returns without touching the stage.

        It does not move for a :meth:`disable` that hit the idempotence guard —
        nothing changed, so nothing downstream needs rebuilding.
        """

        return self._generation

    # --- ordering ------------------------------------------------------------

    def order(self) -> None:
        """Sort every ordered registry by :func:`plugin_sort_key`.

        Called once, at the end of :func:`load_plugins`, and deliberately **not**
        from :meth:`add`. Two reasons, and the second is the load-bearing one:
        a caller that adds a plugin after load reads it back as ``filters[-1]``
        and sorting under it would be a surprise; and ``app.py`` builds the
        export dialog's choices as ``plugin:<index>`` over
        :attr:`exporters`, so the order these lists are in has to be settled
        before anything can address them positionally. Sorting once, before any
        dialog exists, is what keeps that safe.
        """

        self.sources.sort(key=plugin_sort_key)
        self.formats.sort(key=plugin_sort_key)
        self.operators.sort(key=plugin_sort_key)
        self.computed.sort(key=plugin_sort_key)
        self.filters.sort(key=plugin_sort_key)
        self.rules.sort(key=plugin_sort_key)
        self.contributors.sort(key=plugin_sort_key)
        # Priority order is load-bearing here rather than merely tidy: it is how
        # two metrics competing for one bucket are settled, so `metrics[0]`
        # being the winner is a property of this sort having run.
        self.annotators.sort(key=plugin_sort_key)
        self.metrics.sort(key=plugin_sort_key)
        # Both were missed when the watch seam landed, and each docstring said
        # otherwise. It cost nothing for a matcher, which is looked up by kind —
        # but a sink was delivered to in `pkgutil.iter_modules` order while
        # claiming to run in priority order, which is the accident `priority`
        # exists to remove.
        self.matchers.sort(key=plugin_sort_key)
        self.sinks.sort(key=plugin_sort_key)
        self.exporters.sort(key=plugin_sort_key)
        # The end of a load: whatever a downstream cache holds was computed
        # before these plugins existed.
        self._generation += 1

    # --- configuration -------------------------------------------------------

    def settings_for(self, name: str) -> Mapping[str, str]:
        """The read-only view of *name*'s section, created if it is new.

        Always a view onto a dict this registry owns, even when the operator
        wrote no section: a plugin handed an empty mapping at load time and a
        section added by a later ``refresh_settings`` should see it appear,
        rather than holding a view onto something that got replaced.
        """

        return types.MappingProxyType(self._settings.setdefault(name.casefold(), {}))

    def refresh_settings(self, settings: Mapping[str, Mapping[str, str]]) -> None:
        """Adopt a freshly parsed set of ``[plugin:<name>]`` sections.

        In place, per section, so every view already handed to a plugin stays
        live. A section the new mapping does not carry is emptied rather than
        forgotten -- an operator who deleted a section means the plugin should
        fall back to its defaults, and a view onto a stale dict would go on
        reporting the deleted values forever.
        """

        for name, values in settings.items():
            current = self._settings.setdefault(name.casefold(), {})
            current.clear()
            current.update(values)
        for name, current in self._settings.items():
            if name not in settings:
                current.clear()
        # A plugin reads through the view it was handed, so a changed section
        # changes what it returns without any call reaching this module.
        self._generation += 1

    # --- lifecycle -----------------------------------------------------------

    def _run_hook(self, plugin: Any, hook: str, *args: Any) -> bool:
        """Call one optional hook, guarded exactly as ``apply`` is.

        Returns whether it succeeded. A raise disables the plugin for the
        session through :meth:`disable`, which records the reason once however
        many times it is reached -- the same mechanism a raising filter stage
        has used since Phase 1, rather than a second one with its own semantics.
        """

        method = getattr(plugin, hook, None)
        if method is None:
            return True
        try:
            method(*args)
        except Exception as exc:  # noqa: BLE001 - third-party code
            self.disable(
                plugin, f"{hook}() raised: {exc}", origin=_plugin_name(plugin)
            )
            return False
        return True

    def start(self) -> None:
        """Run ``setup()`` on every plugin still in service. Once per session.

        Called by the app after loading and after any post-load wiring, not by
        the loader: :func:`load_plugins` is imported by plugin authors' own unit
        tests and by half this project's suite, and it should not start
        acquiring resources on their behalf.
        """

        if self._started:
            return
        self._started = True
        for record in self.loaded:
            if self.is_disabled(record.plugin):
                continue
            self._run_hook(record.plugin, "setup")

    def shutdown(self) -> None:
        """Run ``teardown()`` on every plugin that was set up. Once per session.

        A plugin disabled by a failed ``configure()`` or ``setup()`` is skipped:
        it never acquired anything, and calling ``teardown()`` on a half-built
        object is how a shutdown path acquires its own bugs.

        An exception is recorded and shutdown continues. A plugin that *hangs*
        here still hangs exit -- bounding that needs the time budget, and
        building a second one here is what ``PLUGIN_TODO.md`` Phase 6 exists to
        prevent. Said out loud in ``clv/plugins/AGENTS.md`` rather than left for
        an operator to discover.
        """

        if self._stopped:
            return
        self._stopped = True
        for record in self.loaded:
            if self.is_disabled(record.plugin):
                continue
            self._run_hook(record.plugin, "teardown")

    def available(self) -> list[DiscoveredPlugin]:
        """Plugins installed in a user root but not enabled.

        What an operator who copied a file in and has not yet named it in
        ``settings.conf`` needs to be told. A shadowed plugin is not here: it
        was not merely left unenabled, it lost its name to something else and
        is reported on its own account.
        """

        return [
            entry
            for entry in self.discovered
            if not entry.enabled and entry.shadowed_by is None
        ]

    def status(self) -> list[PluginStatus]:
        """One row per installable unit: what it is, and how it is doing.

        The whole of what the management UI knows, built here rather than in
        ``app.py`` so it can be asserted without a screen. It merges the three
        things the loader records separately and that an operator experiences as
        one fact -- :attr:`loaded`, :attr:`discovered` and :attr:`errors` -- and
        it is keyed on origin, because the origin is the file somebody copied in.

        Empty for a registry that loaded nothing and found nothing, which is
        Requirement 10: a build with no plugins has nothing to say.
        """

        by_origin: dict[str, list[LoadedPlugin]] = {}
        for record in self.loaded:
            by_origin.setdefault(record.origin, []).append(record)

        # Indexed twice on purpose. A load-time error is recorded against the
        # origin, but a *runtime* one is recorded against the plugin's own name
        # -- `disable()` and `app.py` both name the plugin, which is the right
        # thing for the message and the wrong key for this join.
        errors_by_origin: dict[str, list[PluginError]] = {}
        for error in self.errors:
            errors_by_origin.setdefault(error.origin, []).append(error)
        origins_by_name: dict[str, str] = {}
        for record in self.loaded:
            origins_by_name.setdefault(record.name, record.origin)

        claimed_errors: set[int] = set()

        def errors_for(origin: str, names: Sequence[str]) -> list[PluginError]:
            found = list(errors_by_origin.get(origin, ()))
            for name in names:
                if origins_by_name.get(name) != origin:
                    continue
                found.extend(
                    error
                    for error in errors_by_origin.get(name, ())
                    if error not in found
                )
            claimed_errors.update(id(error) for error in found)
            return found

        rows: list[PluginStatus] = []
        seen: set[str] = set()

        def build(origin: str, discovered: Optional[DiscoveredPlugin]) -> None:
            if origin in seen:
                return
            seen.add(origin)
            records = by_origin.get(origin, [])
            source = _origin_source(origin)
            kinds = tuple(
                label
                for label, _ in _KINDS
                if any(label in record.kinds for record in records)
            )
            reads_content = any(
                getattr(record.plugin, "wants_entries", False)
                for record in records
                if isinstance(record.plugin, WatchSink)
            )
            errors = errors_for(origin, [record.name for record in records])
            categories = {error.category for error in errors}
            # Not a fault and not a reason to call anything "not enabled": a
            # plugin that lost a tie-break is loaded, healthy and one switch away
            # from being the one that runs. It contributes its note to `detail`
            # below and nothing to the state, which is why it is taken out of
            # the set the state is decided from -- a module shipping a losing
            # metric *and* a working annotation must not be reported as broken.
            conflicts = [error for error in errors if error.category == "conflict"]
            categories.discard("conflict")
            detail = "; ".join(
                error.message + (f" (x{error.count})" if error.count > 1 else "")
                for error in errors
            )
            operator_off = bool(records) and all(
                self.disabled_reason(record.plugin) == OPERATOR_DISABLE_REASON
                for record in records
            )
            faulted = [
                reason
                for reason in (
                    self.disabled_reason(record.plugin) for record in records
                )
                if reason is not None and reason != OPERATOR_DISABLE_REASON
            ]

            if "incompatible" in categories:
                state, category = "incompatible", "incompatible"
            elif categories - {"shadowed"}:
                state = "failed"
                category = next(
                    error.category
                    for error in errors
                    if error.category not in ("shadowed", "conflict")
                )
            elif categories:
                # Shadowed, and nothing else. Losing a name to something found
                # first is not a fault -- the plugin is intact and the operator
                # has a choice to make about which one they meant.
                state, category = "not enabled", "shadowed"
            elif faulted:
                state, category, detail = "failed", "runtime", faulted[0]
            elif operator_off:
                state, category = "not enabled", ""
                detail = (
                    "off for this session"
                    if source != "user"
                    else "off"
                )
                # Kept beside it rather than replaced by it: "off" and "it would
                # not have been the metric anyway" are two different things for
                # an operator deciding whether switching it on would change
                # anything.
                if conflicts:
                    detail = "; ".join(
                        [detail] + [error.message for error in conflicts]
                    )
            elif discovered is not None and not discovered.enabled:
                state, category = "not enabled", ""
            else:
                state, category = "loaded", ""

            named = True if discovered is None else discovered.enabled
            rows.append(
                PluginStatus(
                    name=(
                        discovered.name
                        if discovered is not None
                        else _origin_label(origin, source)
                    ),
                    origin=origin,
                    source=source,
                    kinds=kinds,
                    state=state,
                    detail=detail,
                    category=category,
                    reads_content=reads_content,
                    enabled=named and not operator_off,
                )
            )

        # User roots first, and from `discovered` rather than from `loaded`:
        # a plugin sitting there unimported has no live object to be found by.
        for entry in self.discovered:
            build(str(entry.root / entry.name), entry)
        for origin in by_origin:
            build(origin, None)
        # Whatever is left is something that never produced a plugin at all --
        # an import that raised, a version constraint that refused, a name in
        # settings.conf matching nothing on disk. Those are the rows an operator
        # most needs, so they are the ones that must not be dropped for having
        # no object behind them.
        for error in self.errors:
            if id(error) not in claimed_errors:
                build(error.origin, None)

        group = {"user": 0, "bundled": 1, "entry point": 2}
        rows.sort(key=lambda row: (group[row.source], row.name.casefold()))
        return rows

    # --- taking a plugin out of service -------------------------------------

    def disable(
        self,
        plugin: Any,
        reason: str,
        *,
        origin: Optional[str] = None,
        record: bool = True,
    ) -> None:
        """Take *plugin* out of service for the rest of the session.

        Idempotent: the second and later calls record nothing, which is the
        whole point. A raising filter stage used to be disabled *for the current
        pass only*, so it was retried on the next render and appended a fresh
        identical error every time — 200 render passes, 200 errors.

        General rather than filter-specific on purpose. Later work needs to
        disable a plugin for its own reasons — a time budget, a failed
        lifecycle hook, a killed isolation host — and none of it should invent a
        second mechanism with second semantics.

        Nothing is removed from :attr:`sources`, :attr:`filters` or
        :attr:`exporters`. Those lists are addressed positionally elsewhere
        (``app.py`` builds export choices keyed ``plugin:<index>``), so removal
        would silently re-target an in-flight export. Disabling is a marking.

        *record* is False when the operator switched the plugin off themselves.
        :attr:`errors` is the amber problem channel, and a plugin doing exactly
        what it was told is not a problem — the same distinction Phase 3 drew
        for a plugin that is installed and waiting to be named.
        """

        key = id(plugin)
        if key in self._disabled:
            return
        self._disabled[key] = reason
        self._generation += 1
        if record:
            self.errors.append(
                PluginError(
                    origin or _plugin_name(plugin), reason, category="runtime"
                )
            )

    def enable(self, plugin: Any) -> bool:
        """Put a disabled plugin back into service. True if it was disabled."""

        if self._disabled.pop(id(plugin), None) is None:
            return False
        self._generation += 1
        return True

    def is_disabled(self, plugin: Any) -> bool:
        return id(plugin) in self._disabled

    def disabled_reason(self, plugin: Any) -> Optional[str]:
        return self._disabled.get(id(plugin))

    # --- loading -------------------------------------------------------------

    def add(
        self,
        plugin: Any,
        *,
        origin: str,
        clv_version: str,
        api_version: str = PLUGIN_API_VERSION,
    ) -> bool:
        """Classify and store *plugin*, recording why it was rejected if so.

        *api_version* defaults to the running :data:`PLUGIN_API_VERSION` and is
        a parameter only so a test can pin it; no loader passes it.
        """

        if isinstance(plugin, type):
            try:
                plugin = plugin()
            except Exception as exc:  # noqa: BLE001 - third-party code
                self.errors.append(PluginError(origin, f"could not be instantiated: {exc}"))
                return False

        if not isinstance(
            plugin,
            (
                LogSourceProvider,
                LogFormat,
                QueryOperator,
                ComputedField,
                FilterStage,
                ClusterRule,
                ShapeContributor,
                TimelineAnnotation,
                TimelineMetric,
                WatchMatcher,
                WatchSink,
                Exporter,
            ),
        ):
            self.errors.append(
                PluginError(origin, "does not implement a CLV plugin interface")
            )
            return False

        # Both constraints, in the same grammar, with the same two failure
        # shapes: an unreadable constraint is an error naming it, and an
        # unsatisfied one names both versions. A plugin may declare either or
        # both, and each is checked on its own account -- `requires_api` is the
        # one the documentation tells authors to use.
        for label, requirement, running in (
            ("requires_clv", getattr(plugin, "requires_clv", None), clv_version),
            ("requires_api", getattr(plugin, "requires_api", None), api_version),
        ):
            try:
                compatible = satisfies(running, requirement)
            except ValueError as exc:
                # A constraint CLV cannot read is an error naming the
                # constraint, never a silent False: a typo and a genuine
                # incompatibility used to look identical from the outside, and
                # both simply vanished.
                self.errors.append(PluginError(origin, f"bad {label}: {exc}"))
                return False
            if not compatible:
                subject = "CLV" if label == "requires_clv" else "plugin API"
                self.errors.append(
                    PluginError(
                        origin,
                        f"requires {subject} {requirement}, running {running}",
                        category="incompatible",
                    )
                )
                return False

        if isinstance(plugin, LogFormat):
            problem = self._format_fault(plugin)
            if problem is not None:
                self.errors.append(PluginError(origin, problem))
                return False

        if isinstance(plugin, (QueryOperator, ComputedField)):
            problem = self._query_fault(plugin)
            if problem is not None:
                self.errors.append(PluginError(origin, problem))
                return False

        if isinstance(plugin, ClusterRule):
            problem = self._cluster_fault(plugin)
            if problem is not None:
                self.errors.append(PluginError(origin, problem))
                return False

        if isinstance(plugin, TimelineMetric):
            problem = self._metric_fault(plugin)
            if problem is not None:
                self.errors.append(PluginError(origin, problem))
                return False

        if isinstance(plugin, WatchMatcher):
            problem = self._watch_fault(plugin)
            if problem is not None:
                self.errors.append(PluginError(origin, problem))
                return False

        # Every interface it implements, not the first one that matched. The
        # previous `if/elif/else` filed a plugin that was both a provider and a
        # stage as a provider alone, and its `apply()` was never called -- a
        # plugin silently doing half of what it says it does, with no diagnosis
        # anywhere, because from the outside it *had* loaded.
        kinds: list[str] = []
        for label, interface in _KINDS:
            if isinstance(plugin, interface):
                kinds.append(label)
                self._list_for(label).append(plugin)
        self.loaded.append(
            LoadedPlugin(
                plugin=plugin,
                name=_plugin_name(plugin),
                origin=origin,
                kinds=tuple(kinds),
            )
        )
        # Configured *after* filing, because a hook that raises is disabled
        # through `disable()`, and `disable()` marks a plugin the registry is
        # already holding. A plugin that fails here stays in the lists and stays
        # skipped by every use site, exactly as a raising filter stage does.
        #
        # Keyed on the origin's short name -- `_origin_label` yields `journald`
        # for `clv.plugins.sources.journald` and `redact_secrets` for a user
        # path -- so the word in `[plugin:<name>]` is the same word the operator
        # already wrote in `plugins =`.
        section = _origin_label(origin, _origin_source(origin))
        self._configured.add(section.casefold())
        self._run_hook(plugin, "configure", self.settings_for(section))
        return True

    def _format_fault(self, plugin: LogFormat) -> Optional[str]:
        """Why *plugin* may not be registered as a format, or None if it may.

        **At load, never at the first line.** Everything here is knowable
        without parsing anything, and every one of these mistakes is silent at
        runtime: a format claiming ``json`` would take over a built-in's row
        shape, and a profile naming a field the format never produces renders a
        row with no source cell and no diagnosis anywhere. A read path that
        discovered these per line would be paying for them forever and reporting
        them once the buffer was already wrong.
        """

        name = getattr(plugin, "format_name", "")
        if not isinstance(name, str) or not name.strip():
            return "declares no format_name, so nothing can find what it registered"
        if name != name.strip():
            return f"format_name {name!r} has leading or trailing whitespace"
        if name in FORMAT_NAMES:
            # `raw` is in FORMAT_NAMES, so this covers it and says something
            # truer than "raw is reserved" would.
            return (
                f"format_name {name!r} is a built-in format. A plugin adds a "
                "format, it does not replace one"
            )
        for other in self.formats:
            if getattr(other, "format_name", "") == name:
                return (
                    f"format_name {name!r} is already registered by "
                    f"{_plugin_name(other)}"
                )

        names = getattr(plugin, "field_names", frozenset())
        if isinstance(names, (str, bytes)) or not isinstance(names, Iterable):
            return "field_names must be a set of strings"
        try:
            declared = frozenset(names)
        except TypeError:
            return "field_names must be a set of strings"
        if any(not isinstance(key, str) or not key for key in declared):
            return "field_names must be a set of non-empty strings"

        profile = getattr(plugin, "columns", DEFAULT_PROFILE)
        if not isinstance(profile, FormatProfile):
            return "columns must be a FormatProfile"
        unknown = sorted(profile.keys() - declared)
        if unknown:
            return (
                "columns names "
                + ", ".join(unknown)
                + ", which is not in field_names — the row would lose the cell "
                "it points at"
            )
        return None

    def _query_fault(self, plugin: Any) -> Optional[str]:
        """Why *plugin* may not join the query grammar, or None if it may.

        **At load, never at the first term.** Everything here is knowable
        without parsing anything, and the consequence of missing it is not an
        exception but an *ambiguity*: a token sharing a character with a field
        key makes ``key~value`` and the key ``keyXvalue`` two readings of the
        same string, and the tokeniser would settle it by accident.
        """

        if isinstance(plugin, QueryOperator):
            token = getattr(plugin, "token", "")
            if not isinstance(token, str) or not token.strip():
                return "declares no token, so no query could ever reach it"
            if token != token.strip() or any(char.isspace() for char in token):
                return f"token {token!r} contains whitespace"
            if token in BUILTIN_OPERATORS:
                return (
                    f"token {token!r} is one of CLV's own comparisons. A plugin "
                    "adds an operator, it does not redefine one — every saved "
                    "query already means something under this token"
                )
            clashing = "".join(sorted(KEY_CHARS.intersection(token)))
            if clashing:
                return (
                    f"token {token!r} uses {clashing!r}, which a field key may "
                    "also contain, so a term using it could not be told from a "
                    "longer key"
                )
            if any(char in "\"'" for char in token):
                return f"token {token!r} contains a quote, which groups a value"
            for other in self.operators:
                if getattr(other, "token", "") == token:
                    return (
                        f"token {token!r} is already registered by "
                        f"{_plugin_name(other)}"
                    )
            return None

        name = getattr(plugin, "field_name", "")
        if not isinstance(name, str) or not name.strip():
            return "declares no field_name, so no query could ever ask for it"
        if name != name.strip():
            return f"field_name {name!r} has leading or trailing whitespace"
        if not is_query_key(name):
            return (
                f"field_name {name!r} is not a legal query key: it must start "
                "with a letter or underscore and use only letters, digits, "
                "underscore, dot and hyphen"
            )
        # Casefolded, because `query._lookup` matches a field key
        # case-insensitively: `Age` and `age` are one field to every query that
        # could ask for either, so two plugins claiming them are colliding even
        # though the strings differ. Compared exactly, the second would load and
        # then silently replace the first in the installed registry.
        folded = name.casefold()
        for other in self.computed:
            if getattr(other, "field_name", "").casefold() == folded:
                return (
                    f"field_name {name!r} is already registered by "
                    f"{_plugin_name(other)}"
                )
        # A name already in NORMALISED_FIELD_KEYS is deliberately *not* refused:
        # a computed field is consulted only for an entry that has no such
        # parsed field, so it cannot shadow one. See `ComputedField`.
        return None

    def _cluster_fault(self, plugin: ClusterRule) -> Optional[str]:
        """Why *plugin* may not join the normalisation rules, or None if it may.

        **At load, never at the first line**, and here that is not merely the
        cheaper place — it is the only place. A :class:`ClusterRule` declares a
        pattern and a placeholder and CLV performs the substitution, so there is
        no call that could fail later and nothing to disable when it does. Every
        one of these is silent at runtime: a placeholder with a digit in it gets
        chewed up by the next plugin rule that matches numbers, a backslash
        splices in a capture group, and a pattern matching the empty string
        rewrites every position of every line. All three read, from the outside,
        as clustering behaving oddly on this log.
        """

        try:
            pattern = _compiled_rule(plugin)
        except re.error as exc:
            return f"pattern is not a usable regular expression: {exc}"
        except TypeError:
            declared = getattr(plugin, "pattern", None)
            if declared is None or declared == "":
                return "declares no pattern, so it would normalise nothing"
            return (
                f"pattern must be a compiled regular expression or a string, "
                f"not {type(declared).__name__}"
            )
        if pattern.search("") is not None:
            return (
                f"pattern {pattern.pattern!r} matches the empty string, so it "
                "would write its placeholder at every position of every line"
            )

        placeholder = getattr(plugin, "placeholder", "")
        if not isinstance(placeholder, str):
            return (
                "placeholder must be a string, not "
                f"{type(placeholder).__name__}"
            )
        if "\\" in placeholder:
            return (
                f"placeholder {placeholder!r} contains a backslash. It is a "
                "substitution template, so a group reference in it would be "
                "replaced by whatever the pattern captured rather than written "
                "out"
            )
        if any(char.isdigit() for char in placeholder):
            return (
                f"placeholder {placeholder!r} contains a digit. Plugin rules "
                "run after CLV's own, so a later rule matching numbers would "
                "rewrite what this one produced — which is why every built-in "
                "placeholder is digit-free"
            )
        return None

    def _watch_fault(self, plugin: "WatchMatcher") -> Optional[str]:
        """Why *plugin* may not claim its rule kind, or None if it may.

        **At load, never at the first rule**, on the same argument as
        :meth:`_query_fault`: everything here is knowable without evaluating
        anything, and the consequence of missing it is a matcher that loaded
        cleanly and then never runs, because another one has the kind.
        """

        kind = getattr(plugin, "kind", "")
        if not isinstance(kind, str) or not kind.strip():
            return "declares no kind, so no watch rule could ever reach it"
        if kind != kind.strip() or any(char.isspace() for char in kind):
            return f"kind {kind!r} contains whitespace"
        if kind.casefold() == KIND_PATTERN:
            return (
                f"kind {kind!r} is CLV\'s own. A plugin adds a rule kind, it "
                "does not redefine one — every watch rule ever saved already "
                "means something under this kind"
            )
        # Casefolded, because a stored `kind` is operator-facing text and
        # `matcher_for` folds when it looks one up: compared exactly, the second
        # plugin to claim `Burst` against `burst` would load and then never be
        # reached, which from the outside is a plugin that is simply not working.
        folded = kind.casefold()
        for other in self.matchers:
            if getattr(other, "kind", "").casefold() == folded:
                return (
                    f"kind {kind!r} is already registered by "
                    f"{_plugin_name(other)}"
                )
        return None

    def _metric_fault(self, plugin: "TimelineMetric") -> Optional[str]:
        """Why *plugin* may not be a metric, or None if it may.

        One check, and it is about the caption rather than about the number. A
        bar drawn from a metric and a bar drawn from counts are the same glyphs;
        the caption is the only thing that distinguishes them, and a metric with
        no name leaves it saying nothing. At load, like the other three faults —
        an unnamed metric would otherwise be discovered as a caption that reads
        oddly, which nobody reports as a bug.
        """

        name = getattr(plugin, "metric_name", "")
        if not isinstance(name, str) or not name.strip():
            return (
                "declares no metric_name; a bar scaled by a metric has to be "
                "able to say what it is showing"
            )
        return None

    def timeline_stack(
        self, *, budget: Optional[PluginBudget] = None
    ) -> "TimelineStack":
        """The enabled timeline plugins as ``clv.services.timeline`` takes them.

        The fifth of the same shape, for the fifth time for the same reason:
        ``timeline.py`` may not import this module, so the guard travels with
        the plugin rather than being fetched alongside it. Unlike the four
        before it this one also *decides* something — which metric runs — because
        picking by ``priority`` needs the registry, and the service must not be
        handed a choice it has no way to make.
        """

        return TimelineStack(self, budget=budget)

    def cluster_stack(
        self, *, budget: Optional[PluginBudget] = None
    ) -> "ClusterStack":
        """The enabled clustering plugins as ``clv.services.clustering`` takes them.

        The fourth of the same shape, for the fourth time for the same reason:
        ``clustering.py`` may not import this module, so the guard travels with
        the plugin rather than being fetched alongside it.
        """

        return ClusterStack(self, budget=budget)

    def watch_stack(self, *, budget: Optional[PluginBudget] = None) -> "WatchStack":
        """The loaded watch plugins as specs ``clv.services.watch`` can install.

        The third of the same shape, for the third time for the same reason:
        ``watch.py`` may not import this module, so the guard travels with the
        plugin rather than being fetched alongside it.
        """

        return WatchStack(self, budget=budget)

    def query_stack(self, *, budget: Optional[PluginBudget] = None) -> "QueryStack":
        """The loaded query plugins as specs ``clv.services.query`` can install.

        The same shape :meth:`format_stack` has, for the same reason: the guard
        travels *with* the plugins because ``query.py`` may not import this
        module, and handing it bare plugins would mean handing it a fault
        callback and a budget as well.
        """

        return QueryStack(self, budget=budget)

    def format_stack(self, *, budget: Optional[PluginBudget] = None) -> "FormatStack":
        """The enabled formats, guarded and budgeted, for the read path.

        Handed to :class:`clv.services.parsing.LogParser` as its ``formats``.
        The guard travels *with* the formats rather than being fetched
        separately, because ``parsing.py`` may not import this module and a
        format that raises still has to be disabled and reported by name.
        """

        return FormatStack(self, budget=budget)

    def _list_for(self, kind: str) -> list[Any]:
        return {
            "source": self.sources,
            "format": self.formats,
            "operator": self.operators,
            "computed field": self.computed,
            "filter": self.filters,
            "cluster rule": self.rules,
            "shape": self.contributors,
            "timeline": self.annotators,
            "metric": self.metrics,
            "matcher": self.matchers,
            "sink": self.sinks,
            "exporter": self.exporters,
        }[kind]

    def discover_sources(self) -> list[ProviderSource]:
        """Ask every provider what it offers, skipping the ones that raise.

        Same contract as a ``FilterStage`` that throws: recorded, surfaced in
        the drawer, and survivable. A broken provider must not be able to stop
        discovery, which is the one thing standing between the operator and
        every source they have.
        """

        found: list[ProviderSource] = []
        self._owners = {}
        offered_by: dict[Path, list[str]] = {}
        for provider in self.sources:
            if self.is_disabled(provider):
                continue
            name = _plugin_name(provider)
            try:
                offered = list(provider.discover())
            except Exception as exc:  # noqa: BLE001 - third-party code
                self.errors.append(PluginError(name, f"discover() raised: {exc}"))
                continue
            for item in offered:
                source = (
                    item
                    if isinstance(item, ProviderSource)
                    else ProviderSource(Path(item), Path(item).name, name)
                )
                if not source.provider:
                    source = ProviderSource(source.path, source.label, name)
                key = (source.provider, source.path)
                if key in self._owners:
                    # The same provider offering the same identifier twice: a
                    # genuine shadow, and only one of them can ever be opened.
                    self.errors.append(
                        PluginError(
                            source.provider,
                            f"offers {source.path} more than once; keeping the first",
                        )
                    )
                    continue
                self._owners[key] = provider
                offered_by.setdefault(source.path, []).append(source.provider)
                found.append(source)

        # Two *different* providers offering one identifier is no longer a bug —
        # each row now opens its own provider's lines — but it is worth saying,
        # because the operator sees two rows that may well be labelled the same.
        for path, providers in offered_by.items():
            if len(providers) > 1:
                self.errors.append(
                    PluginError(
                        ", ".join(sorted(providers)),
                        f"all offer {path}; each opens its own source",
                    )
                )
        return found

    def open_source(self, source: ProviderSource, *, max_lines: int) -> Optional[Any]:
        """Build a reader for a provider source, or None if it failed.

        Prefers the provider's own ``open_reader``; falls back to wrapping
        ``open()``, so both halves of the interface reach the same pane.
        """

        provider = self._resolve_owner(source)
        if provider is None:
            return None
        if self.is_disabled(provider):
            self.errors.append(
                PluginError(
                    _plugin_name(provider),
                    f"is disabled: {self.disabled_reason(provider)}",
                )
            )
            return None
        name = _plugin_name(provider)
        try:
            reader = provider.open_reader(source.path, max_lines=max_lines)
            if reader is not None:
                return reader
            return IteratorReader(
                source.path, iter(provider.open(source.path)), max_lines=max_lines
            )
        except Exception as exc:  # noqa: BLE001 - third-party code
            self.errors.append(PluginError(name, f"open() raised: {exc}"))
            return None

    def reader_for_ref(self, ref: SourceRef, *, max_lines: int) -> Optional[Any]:
        """Build a reader for a **ref** a provider offers, or ``None``.

        :meth:`open_source` takes the tree node's record, which is what the
        operator clicked. This takes the identity alone, which is what a
        *restored* source is: ``session.json`` stores ``journal:unit/sshd.service``
        and nothing else, so opening a starred unit at launch, or merging one
        with a file, has no ``ProviderSource`` to hand.

        Resolution is by ref rather than by ``(provider, path)`` because a
        stored ref names no provider — the same fallback ``_resolve_owner``
        already makes for a record built by hand, and it refuses an ambiguous
        answer for the same reason: resolving by luck is the defect that key was
        widened to fix.

        ``None`` when nothing offers it, which is the ordinary case rather than
        an error: the journal is off, or the unit no longer exists.
        ``sources.check_access`` is where that becomes a message.
        """

        candidates = {
            owner: holder
            for (owner, path), holder in self._owners.items()
            if path == ref
        }
        if len(candidates) != 1:
            return None
        provider = next(iter(candidates.values()))
        if self.is_disabled(provider):
            return None
        name = _plugin_name(provider)
        try:
            reader = provider.open_reader(ref, max_lines=max_lines)
            if reader is not None:
                return reader
            return IteratorReader(ref, iter(provider.open(ref)), max_lines=max_lines)
        except Exception as exc:  # noqa: BLE001 - third-party code
            self.errors.append(PluginError(name, f"open() raised: {exc}"))
            return None

    def offers(self, ref: SourceRef) -> bool:
        """Whether any loaded provider currently offers *ref*.

        Cheap and side-effect free, so the tree and ``check_access`` can ask on
        every render without opening anything.
        """

        return any(path == ref for _owner, path in self._owners)

    def _resolve_owner(self, source: ProviderSource) -> Optional[LogSourceProvider]:
        """Find the provider that offered *source*.

        The record carries the provider's own name, so the usual case is an
        exact ``(provider, path)`` hit. A ``ProviderSource`` built by hand or
        restored from older state may have no provider name; that falls back to
        matching on the path alone, and only when exactly one provider offers it
        — resolving an ambiguous one by luck is the defect this key was widened
        to fix.
        """

        provider = self._owners.get((source.provider, source.path))
        if provider is not None:
            return provider

        candidates = {
            owner: holder
            for (owner, path), holder in self._owners.items()
            if path == source.path
        }
        if source.provider or not candidates:
            self.errors.append(
                PluginError(source.provider or "provider", "no longer offers this source")
            )
            return None
        if len(candidates) > 1:
            self.errors.append(
                PluginError(
                    ", ".join(sorted(candidates)),
                    f"all offer {source.path} and the source names no provider; "
                    "refusing to guess",
                )
            )
            return None
        return next(iter(candidates.values()))

    def apply_filters(
        self,
        entries: Sequence[LogEntry],
        context: FilterContext,
        *,
        budget: Optional[PluginBudget] = None,
    ) -> list[LogEntry]:
        """Run every filter stage over *entries*, skipping stages that raise.

        A stage that raises is disabled for the **session**, not for the pass.
        Disabling it per pass meant retrying it on the next render and recording
        the same failure again, so a broken stage cost one error per render
        rather than one error.

        *budget*, when given and :attr:`~PluginBudget.active`, charges each
        stage the wall time of its own calls and judges the totals when the pass
        closes. It is optional and defaults to ``None`` so that every existing
        caller — and every plugin author's unit test — gets exactly the function
        that was here before. Zero installed stages returns above without
        touching any of it, which is Requirement 10: a build with no plugins
        enters no measurement path at all.

        The loop is **stage-outer**: each stage sees the whole surviving set in
        buffer order, then hands what it kept to the next one. It used to be
        entry-outer, and that was changed here rather than earlier because
        measurement is what made the difference visible. Entry-outer needs two
        clock reads *per call* — entries times stages — and a no-op stage over
        five thousand entries then costs four times more to time than to run,
        which at ``max_buffer_lines = 500_000`` is a fifth of a second of pure
        measurement that the budget would then charge to the plugin. Stage-outer
        needs two reads per stage per pass, and the cost of the guard stops
        depending on how big the buffer is.

        What it costs in exchange is one intermediate list per stage — pointers,
        not entries, and only two are ever live at once. The composition itself
        is unchanged: a dropped entry is still invisible to later stages, a
        raising stage still lets the entry it choked on through untouched and is
        still skipped by everything after it, and the order within any one stage
        is still the buffer's.
        """

        if not self.filters:
            return list(entries)

        # Hoisted out of what used to be an inner loop: this was an identity
        # lookup per entry per stage.
        active = [stage for stage in self.filters if not self.is_disabled(stage)]
        if not active:
            return list(entries)

        timing = budget is not None and budget.active
        if timing:
            budget.start()

        clock = time.perf_counter
        current: list[LogEntry] = list(entries)
        for stage in active:
            if not current:
                # Everything has been dropped. Nothing left to hand on, and a
                # stage called with nothing has nothing to say.
                break
            started = clock() if timing else 0.0
            kept: list[LogEntry] = []
            for index, entry in enumerate(current):
                try:
                    survivor = stage.apply(entry, context)
                except Exception as exc:  # noqa: BLE001 - third-party code
                    # The pane keeps working with the remaining stages. This
                    # entry and every one after it passes through untouched,
                    # which is what the per-entry check used to arrive at by
                    # skipping a stage that had already been disabled.
                    self.disable(stage, f"raised: {exc}", origin=_plugin_name(stage))
                    kept.extend(current[index:])
                    break
                if survivor is not None:
                    kept.append(survivor)
            if timing:
                budget.charge(stage, clock() - started)
            current = kept

        if timing:
            budget.settle()
        return current


class QueryStack:
    """The loaded query plugins, wrapped in everything CLV owes them.

    Produces the :class:`~clv.services.query.OperatorSpec` and
    :class:`~clv.services.query.ComputedSpec` records that
    ``query.install_query_plugins`` stores, with every third-party callable
    already carrying its guard: an exception disables the plugin through
    :meth:`PluginRegistry.disable`, a non-string return does the same with the
    rule it broke, and wall time is charged to the budget.

    **Loaded, not enabled.** Every loaded plugin gets a spec; one that is out of
    service gets a spec whose callable is ``None``. That is what keeps its token
    reserved while it is switched off, so ``svc~web`` reports the plugin by name
    instead of silently becoming a regex — see
    :class:`~clv.services.query.OperatorSpec`.

    The wrappers re-check :meth:`PluginRegistry.is_disabled` per call as well,
    because a plugin that raises mid-render is disabled immediately and the
    specs are only rebuilt on the next generation change.
    """

    __slots__ = ("_registry", "operators", "computed", "_budget")

    def __init__(
        self, registry: "PluginRegistry", *, budget: Optional[PluginBudget] = None
    ) -> None:
        self._registry = registry
        self._budget = budget
        self.operators: tuple[OperatorSpec, ...] = tuple(
            OperatorSpec(
                token=plugin.token,
                plugin=_plugin_name(plugin),
                test=None if registry.is_disabled(plugin) else self._guard_test(plugin),
            )
            for plugin in registry.operators
        )
        self.computed: tuple[ComputedSpec, ...] = tuple(
            ComputedSpec(
                field_name=plugin.field_name,
                plugin=_plugin_name(plugin),
                value=None if registry.is_disabled(plugin) else self._guard_value(plugin),
            )
            for plugin in registry.computed
        )

    def start(self) -> None:
        """Open a budget pass. One filter of the buffer is one pass."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.start()

    def settle(self) -> None:
        """Close the pass, disabling anything that has struck out."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.settle()

    def _guard_test(self, plugin: QueryOperator):
        registry = self._registry
        budget = self._budget
        clock = time.perf_counter

        def test(stored: str, value: str) -> bool:
            if registry.is_disabled(plugin):
                return False
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                result = plugin.test(stored, value)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return False
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)
            return bool(result)

        return test

    def _guard_value(self, plugin: ComputedField):
        registry = self._registry
        budget = self._budget
        clock = time.perf_counter

        def value(entry: LogEntry) -> Optional[str]:
            if registry.is_disabled(plugin):
                return None
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                result = plugin.value(entry)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return None
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)
            if result is None or isinstance(result, str):
                return result
            # Values are compared as the parser stored them and nothing
            # downstream coerces, so a non-string here would be compared against
            # a string and quietly never match.
            registry.disable(
                plugin,
                f"value() returned {type(result).__name__}; a computed field "
                "must return a string or None",
                origin=_plugin_name(plugin),
            )
            return None

        return value


class ClusterStack:
    """The enabled clustering plugins, wrapped in everything CLV owes them.

    Produces the :class:`~clv.services.clustering.ClusterRuleSpec` and
    :class:`~clv.services.clustering.ShapeSpec` records
    ``clustering.install_cluster_plugins`` stores, with the substitution and
    the contribution already carrying their guard and their budget.

    **Enabled only, and that is the one place this seam departs from the two
    before it.** A ``QueryOperator``'s token and a ``WatchMatcher``'s kind stay
    registered while their plugin is switched off, because a saved query or rule
    means something under them and dropping one would silently reinterpret it. A
    cluster rule reserves nothing: no saved view, watch rule or session names
    one, and there is no text whose meaning could change. So an out-of-service
    rule is simply not installed, and the shapes go back to being what they were
    — which is also the only behaviour an operator switching a rule off in the
    ``P`` dialog could reasonably expect.

    The wrappers re-check :meth:`PluginRegistry.is_disabled` per call anyway,
    because a plugin disabled mid-render is disabled immediately and the specs
    are only rebuilt on the next generation change.
    """

    __slots__ = ("_registry", "rules", "contributors", "_budget")

    def __init__(
        self, registry: "PluginRegistry", *, budget: Optional[PluginBudget] = None
    ) -> None:
        self._registry = registry
        self._budget = budget
        self.rules: tuple[ClusterRuleSpec, ...] = tuple(
            ClusterRuleSpec(
                plugin=_plugin_name(plugin),
                apply=self._guard_apply(plugin),
            )
            for plugin in registry.rules
            if not registry.is_disabled(plugin)
        )
        self.contributors: tuple[ShapeSpec, ...] = tuple(
            ShapeSpec(
                plugin=_plugin_name(plugin),
                contribute=self._guard_contribute(plugin),
            )
            for plugin in registry.contributors
            if not registry.is_disabled(plugin)
        )

    def start(self) -> None:
        """Open a budget pass. One clustering of the filtered set is one pass."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.start()

    def settle(self) -> None:
        """Close the pass, disabling anything that has struck out."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.settle()

    def _guard_apply(self, plugin: ClusterRule):
        """One rule's substitution, timed and contained.

        The pattern and the placeholder are read **once**, here, rather than per
        line: a plugin that recomputed either between calls would otherwise make
        the memoised shape of a line depend on when it was first seen.

        The ``except`` is belt and braces and is expected never to fire.
        :meth:`PluginRegistry._cluster_fault` has already proved the pattern
        compiles, does not match the empty string, and has a placeholder that is
        a literal — after which ``sub`` has nothing left to raise about short of
        the interpreter running out of something. It is here because the cost of
        an ``except`` that does not fire is zero and the cost of one missing is
        a render that dies on a line.
        """

        registry = self._registry
        budget = self._budget
        clock = time.perf_counter
        pattern = _compiled_rule(plugin)
        placeholder = plugin.placeholder

        def apply(text: str) -> str:
            if registry.is_disabled(plugin):
                return text
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                return pattern.sub(placeholder, text)
            except Exception as exc:  # noqa: BLE001 - third-party pattern
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return text
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)

        return apply

    def _guard_contribute(self, plugin: ShapeContributor):
        registry = self._registry
        budget = self._budget
        clock = time.perf_counter

        def contribute(entry: LogEntry) -> str:
            if registry.is_disabled(plugin):
                # The no-op contribution, not a marker: a disabled contributor
                # has to leave the shape it would have widened exactly as it
                # was before the plugin was installed.
                return ""
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                result = plugin.contribute(entry)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return ""
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)
            if isinstance(result, str):
                return result
            # The contribution is composed into the shape. A non-string would be
            # formatted by its repr, and an object whose repr carries its
            # address gives every entry a shape of its own -- clustering
            # switched off, with nothing on screen to say so.
            registry.disable(
                plugin,
                f"contribute() returned {type(result).__name__}; a shape "
                "contributor must return a string",
                origin=_plugin_name(plugin),
            )
            return ""

        return contribute


class TimelineStack:
    """The enabled timeline plugins, wrapped in everything CLV owes them.

    Produces the :class:`~clv.services.timeline.AnnotationSpec` records and the
    single :class:`~clv.services.timeline.MetricSpec` that
    ``timeline.install_timeline_plugins`` stores, with every third-party
    callable already carrying its guard and its budget.

    **Enabled only**, like :class:`ClusterStack` and for the same reason: a mark
    on an axis and a bucket's unit are named by nothing that is saved, so a
    plugin switched off is simply not installed and the bar goes back to being
    what it was. Nothing could be silently reinterpreted in the meantime, which
    is what makes Phase 8's reservation rule unnecessary here.

    **This one also decides something.** The other four stacks install whatever
    they are given; a bucket can only measure one thing, so this picks the
    highest-priority enabled metric, records a note against every loser naming
    the winner, and hands the service a single spec. The service is not given a
    choice it would have no basis to make, and the decision is re-run on every
    rebuild of the stack — so switching the winner off promotes the runner-up
    rather than leaving the bar counting entries.
    """

    __slots__ = ("_registry", "annotators", "metric", "_budget")

    def __init__(
        self, registry: "PluginRegistry", *, budget: Optional[PluginBudget] = None
    ) -> None:
        self._registry = registry
        self._budget = budget
        self.annotators: tuple[AnnotationSpec, ...] = tuple(
            AnnotationSpec(
                plugin=_plugin_name(plugin),
                fetch=self._guard_fetch(plugin),
            )
            for plugin in registry.annotators
            if not registry.is_disabled(plugin)
        )
        self.metric: Optional[MetricSpec] = self._elect_metric()

    def start(self) -> None:
        """Open a budget pass. One rebuild of the bar is one pass."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.start()

    def settle(self) -> None:
        """Close the pass, disabling anything that has struck out."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.settle()

    def _elect_metric(self) -> Optional[MetricSpec]:
        """The metric that runs, and a note for each one that does not.

        ``registry.metrics`` is in :func:`plugin_sort_key` order, so the first
        enabled plugin in it is the highest-priority one and the election is a
        walk rather than a sort.

        Every metric's previous note is discarded first, because this contest is
        re-run whenever the stack is rebuilt and last time's loser may be this
        time's winner. Leaving the old note behind would leave the ``P`` dialog
        saying a plugin is not in use while the bar is drawn from its numbers.
        """

        registry = self._registry
        winner: Optional[TimelineMetric] = None
        for plugin in registry.metrics:
            registry.errors.discard(_plugin_name(plugin), category="conflict")
            if registry.is_disabled(plugin):
                continue
            if winner is None:
                winner = plugin
                continue
            registry.errors.append(
                PluginError(
                    _plugin_name(plugin),
                    f"metric not in use: {_plugin_name(winner)} has priority. "
                    "A bucket measures one thing; switch that plugin off to "
                    "use this one",
                    category="conflict",
                )
            )
        if winner is None:
            return None
        return MetricSpec(
            plugin=_plugin_name(winner),
            metric_name=winner.metric_name,
            # Read once, here, rather than per caption: a plugin that changed
            # its unit between renders would relabel numbers that had already
            # been summed under the old one.
            unit=str(getattr(winner, "unit", "") or ""),
            value=self._guard_value(winner),
        )

    def _guard_fetch(self, plugin: TimelineAnnotation):
        registry = self._registry
        budget = self._budget
        clock = time.perf_counter

        def fetch(window: TimeWindow) -> tuple[tuple[datetime, str, Optional[str]], ...]:
            if registry.is_disabled(plugin):
                return ()
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                # `list` inside the `try`, not outside: the documented shape is
                # a generator, and a generator that raises does so while it is
                # being walked rather than when it is created.
                produced = list(plugin.annotations(window))
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return ()
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)
            return self._marks(plugin, produced)

        return fetch

    def _marks(
        self, plugin: TimelineAnnotation, produced: Sequence[Any]
    ) -> tuple[tuple[datetime, str, Optional[str]], ...]:
        """Check what a provider answered, or take it out of service.

        All or nothing per call, rather than dropping the bad item: a provider
        that cannot say what shape its marks are is a provider whose *good* marks
        cannot be trusted to mean what their labels say, and a mark of uncertain
        meaning on a time axis is worse than no mark at all.
        """

        marks: list[tuple[datetime, str, Optional[str]]] = []
        for item in produced:
            if not isinstance(item, tuple) or len(item) != 3:
                self._registry.disable(
                    plugin,
                    f"annotations() yielded {type(item).__name__}; each mark "
                    "must be a (moment, label, level) tuple",
                    origin=_plugin_name(plugin),
                )
                return ()
            moment, label, level = item
            if not isinstance(moment, datetime) or not isinstance(label, str):
                self._registry.disable(
                    plugin,
                    "annotations() yielded a mark that is not "
                    "(datetime, str, level); got "
                    f"({type(moment).__name__}, {type(label).__name__})",
                    origin=_plugin_name(plugin),
                )
                return ()
            # Normalised exactly as a parsed line's level is, so a mark saying
            # `"warning"` is coloured the same as a line saying `"WARN"` and an
            # unrecognised word becomes None rather than an invented severity.
            marks.append((moment, label, normalize_level(level)))
        return tuple(marks)

    def _guard_value(self, plugin: TimelineMetric):
        registry = self._registry
        budget = self._budget
        clock = time.perf_counter

        def value(entry: LogEntry) -> Optional[float]:
            if registry.is_disabled(plugin):
                return None
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                result = plugin.value(entry)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return None
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)
            if result is None:
                return None
            if isinstance(result, bool) or not isinstance(result, (int, float)):
                # A bool is refused rather than summed as 1: counting a subset
                # of entries is a legitimate metric and `1.0 if ... else 0.0`
                # says so, where a `True` reads as an answer to a different
                # question.
                registry.disable(
                    plugin,
                    f"value() returned {type(result).__name__}; a timeline "
                    "metric must return a number or None",
                    origin=_plugin_name(plugin),
                )
                return None
            number = float(result)
            if not math.isfinite(number):
                # An infinity or a NaN does not make one bucket wrong, it makes
                # the whole bar meaningless: `peak_value` is what every height
                # is scaled against, and neither has a scale.
                registry.disable(
                    plugin,
                    f"value() returned {result!r}; a timeline metric must "
                    "return a finite number",
                    origin=_plugin_name(plugin),
                )
                return None
            return number

        return value


class WatchStack:
    """The loaded watch plugins, wrapped in everything CLV owes them.

    Produces the :class:`~clv.services.watch.MatcherSpec` and
    :class:`~clv.services.watch.SinkSpec` records
    ``watch.install_watch_plugins`` stores, with every third-party callable
    already carrying its guard.

    **Matchers: loaded, not enabled.** Every loaded matcher gets a spec; one out
    of service gets ``matches=None``. That is what keeps its *kind* claimed
    while it is switched off, so a rule declaring that kind reports the plugin
    by name instead of falling back to the pattern path and matching its
    parameter string as a query — the same reservation, and the same reason,
    as :class:`~clv.services.query.OperatorSpec`.

    **Sinks: only the enabled ones.** The asymmetry is deliberate. A kind has to
    stay reserved because a saved rule *means* something under it; a
    destination reserves nothing and means nothing, so a sink that is out of
    service is simply not in the list and nothing is delivered to it.
    """

    __slots__ = ("_registry", "matchers", "sinks", "_budget")

    def __init__(
        self, registry: "PluginRegistry", *, budget: Optional[PluginBudget] = None
    ) -> None:
        self._registry = registry
        self._budget = budget
        self.matchers: tuple[MatcherSpec, ...] = tuple(
            MatcherSpec(
                kind=plugin.kind,
                plugin=_plugin_name(plugin),
                matches=(
                    None
                    if registry.is_disabled(plugin)
                    else self._guard_matches(plugin)
                ),
                validate=(
                    None
                    if registry.is_disabled(plugin)
                    else self._guard_validate(plugin)
                ),
            )
            for plugin in registry.matchers
        )
        self.sinks: tuple[SinkSpec, ...] = tuple(
            SinkSpec(
                plugin=_plugin_name(plugin),
                deliver=self._guard_deliver(plugin),
                wants_entries=bool(getattr(plugin, "wants_entries", False)),
                disable=self._disabler(plugin),
            )
            for plugin in registry.sinks
            if not registry.is_disabled(plugin)
        )

    def start(self) -> None:
        """Open a budget pass. One poll's batch of new lines is one pass."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.start()

    def settle(self) -> None:
        """Close the pass, disabling anything that has struck out."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.settle()

    def _disabler(self, plugin: Any):
        """A one-argument kill switch for *plugin*, closed over the registry.

        Handed to a :class:`~clv.services.watch.SinkSpec` so the dispatcher can
        retire a sink that *hangs* — which no wrapper around ``deliver`` can
        detect, because a call that never returns never reaches its ``except``.
        """

        registry = self._registry

        def disable(reason: str) -> None:
            registry.disable(plugin, reason, origin=_plugin_name(plugin))

        return disable

    def _guard_matches(self, plugin: "WatchMatcher"):
        registry = self._registry
        budget = self._budget
        clock = time.perf_counter

        def matches(entry: LogEntry, rule: Any) -> bool:
            if registry.is_disabled(plugin):
                return False
            timing = budget is not None and budget.active
            mark = clock() if timing else 0.0
            try:
                result = plugin.matches(entry, rule)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                return False
            finally:
                if timing:
                    budget.charge(plugin, clock() - mark)
            # Python's truth protocol rather than a type check, unlike
            # `ComputedField.value`. A wrong *type* there is a silent
            # never-matches, because the value is compared against a string; a
            # truthy object here is what an author writing
            # `return self._pattern.search(entry.raw)` plainly meant, and
            # refusing it would be pedantry with a plugin taken out of service
            # at the end of it.
            return bool(result)

        return matches

    def _guard_validate(self, plugin: "WatchMatcher"):
        registry = self._registry

        def validate(pattern: str) -> Optional[str]:
            if registry.is_disabled(plugin):
                return None
            try:
                result = plugin.validate(pattern)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"validate() raised: {exc}", origin=_plugin_name(plugin)
                )
                return None
            # Off the render path entirely -- this runs when the operator
            # presses Save in the rules dialog -- so it is unbudgeted, and a
            # non-string is read as "no complaint" rather than shown as one.
            return result if isinstance(result, str) and result else None

        return validate

    def _guard_deliver(self, plugin: "WatchSink"):
        """Wrap ``deliver`` so a raise disables the sink instead of the thread.

        Deliberately **not** budgeted. A sink is one call per rule per window on
        a thread of its own, so "slow" is not a symptom here and a
        :class:`PluginBudget`'s three-strikes-per-pass policy has no pass to
        count. What can actually go wrong is a call that never comes back, and
        that is the dispatcher's deadline, not a stopwatch around a call that
        has already returned.
        """

        registry = self._registry

        def deliver(
            name: str,
            count: int,
            context: Any,
            entries: Sequence[LogEntry] = (),
        ) -> None:
            if registry.is_disabled(plugin):
                return
            try:
                plugin.deliver(name, count, context, entries)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )

        return deliver


class FormatStack(Sequence):
    """The enabled :class:`LogFormat` plugins, wrapped in everything CLV owes them.

    A ``Sequence`` so that the read path's ``formats=()`` default and a live
    stack are the same kind of thing: :class:`~clv.services.parsing.LogParser`
    tests it for truth and calls :meth:`parse` only when there is something to
    call, so a build with no format plugins enters none of this.

    **Why the guard travels with the formats.** ``parsing.py`` may not import
    this module — the dependency runs the other way and every seam in
    ``PLUGIN_TODO.md`` is injected rather than imported. But a format that
    raises has to be disabled through :meth:`PluginRegistry.disable`, and one
    that returns a malformed entry has to be reported by the rule it broke.
    Handing the parser bare plugins would mean handing it a fault callback and a
    budget as well, and spreading one plugin's guard across two modules. So the
    guard is one object and the parser holds one reference.

    **The clock is read once per call, not twice.** The read before the loop is
    reused as the previous call's end stamp, which halves the measurement on the
    path where it matters most: this runs per line, and Phase 6 already found
    that a guard costing more than the work it guards is a guard that disables
    healthy plugins.
    """

    __slots__ = ("_registry", "_formats", "_budget")

    def __init__(
        self, registry: "PluginRegistry", *, budget: Optional[PluginBudget] = None
    ) -> None:
        self._registry = registry
        #: Snapshotted at construction: the set of *loaded* formats is settled
        #: at the end of `load_plugins` and never grows afterwards. Whether one
        #: is in service is asked per line, because that does change.
        self._formats = tuple(registry.formats)
        self._budget = budget

    def __len__(self) -> int:
        return len(self._formats)

    def __getitem__(self, index):  # type: ignore[override]
        return self._formats[index]

    def start(self) -> None:
        """Open a budget pass. One read's batch of lines is one pass."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.start()

    def settle(self) -> None:
        """Close the pass, disabling anything that has struck out."""

        budget = self._budget
        if budget is not None and budget.active:
            budget.settle()

    def parse(self, line: str) -> Optional[LogEntry]:
        """The first valid entry any enabled format returns, or None.

        Never raises. A format that throws, or that hands back something the
        :class:`LogEntry` contract does not allow, is taken out of service for
        the session through the same :meth:`PluginRegistry.disable` a raising
        filter stage has used since Phase 1 — recorded once, shown in the ``P``
        dialog as ``failed``, and reachable by Re-enable.
        """

        registry = self._registry
        budget = self._budget
        timing = budget is not None and budget.active
        clock = time.perf_counter
        mark = clock() if timing else 0.0

        for plugin in self._formats:
            if registry.is_disabled(plugin):
                continue
            try:
                entry = plugin.parse(line)
            except Exception as exc:  # noqa: BLE001 - third-party code
                registry.disable(
                    plugin, f"raised: {exc}", origin=_plugin_name(plugin)
                )
                continue
            finally:
                if timing:
                    now = clock()
                    budget.charge(plugin, now - mark)
                    mark = now
            if entry is None:
                continue
            problem = _entry_fault(entry, plugin.format_name)
            if problem is not None:
                registry.disable(
                    plugin,
                    f"parse() {problem}",
                    origin=_plugin_name(plugin),
                )
                continue
            return entry
        return None


def _entry_fault(entry: Any, format_name: str) -> Optional[str]:
    """Why *entry* is not a usable :class:`LogEntry`, or None if it is.

    Checked on every line a format claims, which is the one place in this file
    that validates third-party output per item rather than per pass. It is worth
    it here and nowhere else: a filter stage that misbehaves costs a render, and
    a format that misbehaves writes a wrong entry into the buffer that every
    query, bucket and cluster downstream then believes.

    Cheap by construction — an ``isinstance``, a string compare, and a walk of a
    mapping that ``_MAX_FIELDS`` already bounds at 64 and that is empty for most
    formats.
    """

    if not isinstance(entry, LogEntry):
        return f"returned {type(entry).__name__}, not a LogEntry"
    if entry.format_name != format_name:
        return (
            f"returned an entry with format_name {entry.format_name!r}, "
            f"not the {format_name!r} it declared"
        )
    for key, value in entry.fields.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return (
                f"returned a non-string field ({key!r}: {type(value).__name__}). "
                "Values are compared as the parser stored them and nothing "
                "downstream coerces"
            )
    return None


def _compiled_rule(plugin: ClusterRule) -> re.Pattern[str]:
    """*plugin*'s pattern, compiled. Raises what ``re.compile`` raises.

    A :class:`ClusterRule` may declare either a compiled pattern or the string
    for one, because writing ``re.compile`` around a literal is the kind of
    ceremony an author forgets and then cannot diagnose. Called from
    :meth:`PluginRegistry._cluster_fault`, which is where the failure is
    reported, and again from :class:`ClusterStack`, where it cannot fail — the
    ``re`` module memoises compilation, so the second call is a dict lookup.
    """

    pattern = getattr(plugin, "pattern", "")
    if isinstance(pattern, re.Pattern):
        return pattern
    if isinstance(pattern, str) and pattern:
        return re.compile(pattern)
    raise TypeError(f"pattern must be a pattern or a string, not {type(pattern)!r}")


def _plugin_name(plugin: Any) -> str:
    """The name to attribute a problem to. Never raises, never empty."""

    try:
        name = getattr(plugin, "name", "")
    except Exception:  # noqa: BLE001 - a property on third-party code
        name = ""
    return name or type(plugin).__name__


def plugin_sort_key(plugin: Any) -> tuple[int, str]:
    """The order every ordered plugin registry runs in. Ascending.

    **One rule, defined once.** Filter stages are the only ordered registry
    today; ``PLUGIN_TODO.md`` adds five more -- formats, query operators, cluster
    rules, watch matchers, sinks -- and each of them calls this rather than
    restating it, so two plugins cannot be ordered one way in one registry and
    the other way in another.

    ``priority`` first, then the plugin's own name casefolded. The tie-break is
    what makes the order a function of *what is installed* rather than of what
    ``pkgutil.iter_modules`` happened to list first: two stages that both take
    the default compose the same way on every machine, which is at least a rule
    their authors can predict and design around.

    :func:`_plugin_name` never raises and never returns empty, which is what
    makes this safe to call on third-party code. A ``priority`` that is not an
    int is read as the default rather than raising mid-sort -- a plugin with a
    typo in one attribute should be mis-ordered, not fatal.
    """

    priority = getattr(plugin, "priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        priority = 100
    return (priority, _plugin_name(plugin).casefold())


#: What ``configparser`` accepts for a boolean, so a plugin and CLV cannot
#: disagree about whether ``yes`` is true. Taken from the class rather than
#: retyped, because retyping it is exactly the divergence this exists to stop.
_BOOLEAN_STATES = configparser.ConfigParser.BOOLEAN_STATES


def setting_bool(
    settings: Mapping[str, str], key: str, default: bool = False
) -> bool:
    """Read *key* from a plugin's settings as a boolean.

    Published for the same reason :func:`~clv.services.parsing.normalize_level`
    is: the alternative is every plugin writing ``value.lower() == "true"`` and
    CLV disagreeing with the operator's file about what ``yes``, ``on`` and
    ``1`` mean. An unreadable value is *default*, not an error -- a plugin's
    settings are operator prose and a typo should not take the plugin out of
    service.
    """

    raw = settings.get(key)
    if raw is None:
        return default
    return _BOOLEAN_STATES.get(str(raw).strip().casefold(), default)


def setting_list(settings: Mapping[str, str], key: str) -> list[str]:
    """Read *key* from a plugin's settings as a comma-separated list.

    The same shape ``log_dirs`` uses, quotes stripped and empties dropped, so a
    plugin's list and CLV's own parse identically. Absent or empty is ``[]``.
    """

    raw = settings.get(key)
    if raw is None:
        return []
    from ..services.config import _split_list

    return _split_list(str(raw))


def _as_list(produced: Any) -> list[Any]:
    """Normalise whatever ``register()`` handed back into a list."""

    if produced is None:
        return []
    if isinstance(produced, (list, tuple, set, frozenset)):
        return list(produced)
    # A generator is an obvious way to write ``register()`` and used to be
    # collected as one unusable object. Plugins are not iterable, so this
    # cannot swallow a plugin that happens to be returned on its own.
    if isinstance(produced, collections.abc.Iterator):
        return list(produced)
    return [produced]


def _extract_plugins(module: Any) -> tuple[list[Any], Optional[str]]:
    """Pull plugin objects out of a loaded module, and say so when there are none.

    Three strategies, in order, plus a diagnosis — because the failure this
    returns a message for is the single most likely first-run experience for a
    new plugin author, and it used to be completely silent: a module with no
    ``register()`` and no ``__all__`` loaded as zero plugins and zero errors.

    1. ``register()``, returning one plugin or any iterable of them. Returning
       ``None`` or an empty list is **deliberate** — it is how a plugin declines
       to register itself on this machine — and is never diagnosed.
    2. ``__all__``, listing plugin classes or instances.
    3. A scan of the module's own namespace for concrete :class:`Plugin`
       subclasses **defined in that module**. The ``__module__`` test keeps an
       imported base class out, and the abstractness test keeps out a subclass
       that forgot to implement its interface method — which would otherwise be
       instantiated into a confusing ``TypeError`` at ``add()``.

    Returns ``(candidates, diagnosis)``; the diagnosis is None unless the module
    genuinely says nothing about what it exports.
    """

    register = getattr(module, "register", None)
    if callable(register):
        return _as_list(register()), None

    exported = getattr(module, "__all__", None)
    if exported:
        return [getattr(module, name) for name in exported if hasattr(module, name)], None

    module_name = getattr(module, "__name__", None)
    found = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, Plugin)
        and value.__module__ == module_name
        and not inspect.isabstract(value)
    ]
    if found:
        return found, None

    return [], "defines no plugin — add register() or __all__"


def _load_module(
    registry: PluginRegistry,
    module_name: str,
    *,
    origin: str,
    clv_version: str,
) -> None:
    """Import one module and file whatever it exports into *registry*.

    Shared by the bundled walk and the user-root walk. The two differ in where
    they find a name and in whether they are allowed to import it at all; once
    a name has been cleared for import the handling is identical, and it should
    stay identical -- a plugin must not be diagnosed differently for having been
    installed in a different directory.
    """

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - third-party code
        registry.errors.append(PluginError(origin, f"import failed: {exc}"))
        return
    try:
        candidates, diagnosis = _extract_plugins(module)
    except Exception as exc:  # noqa: BLE001 - third-party code
        registry.errors.append(PluginError(origin, f"register() failed: {exc}"))
        return
    if diagnosis:
        registry.errors.append(PluginError(origin, diagnosis))
    for candidate in candidates:
        registry.add(candidate, origin=origin, clv_version=clv_version)


def plugin_search_roots() -> list[Path]:
    """The user plugin roots, in search order, first-wins.

    ``CLV_PLUGIN_PATH`` first, then ``~/.config/clv/plugins/``. The env var is
    ahead so an author can run a plugin they are editing without moving it, and
    so the tests get a root without writing into the source tree.

    De-duplicated by resolved path with order preserved: naming the user
    directory in ``CLV_PLUGIN_PATH`` should not make every plugin in it report
    itself as shadowing itself.
    """

    roots: list[Path] = []
    raw = os.environ.get(PLUGIN_PATH_ENV, "")
    roots.extend(
        Path(piece).expanduser()
        for piece in raw.split(os.pathsep)
        if piece.strip()
    )
    # Local import for the same reason `load_plugins` imports `clv.__version__`
    # locally: `clv.api` re-exports this module, and the loader has no business
    # dragging the settings parser into a plugin author's unit tests.
    from ..services.config import user_plugin_dir

    try:
        roots.append(user_plugin_dir())
    except Exception:  # noqa: BLE001 - a $HOME-less environment is not fatal
        pass

    ordered: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:  # pragma: no cover - unresolvable path
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(root)
    return ordered


def _install_user_package(roots: Sequence[Path]) -> None:
    """Put the synthetic user-plugin package into ``sys.modules``.

    Rebuilt on every load rather than cached, because the roots can differ
    between one call and the next -- which in practice means between one test
    and the next.
    """

    package = types.ModuleType(USER_PLUGIN_PACKAGE)
    package.__path__ = [str(root) for root in roots]  # type: ignore[attr-defined]
    package.__doc__ = "Synthetic package: CLV user plugins, imported by path."
    sys.modules[USER_PLUGIN_PACKAGE] = package


def _load_user_roots(
    registry: PluginRegistry,
    clv_version: str,
    roots: Sequence[Path],
    enabled: Sequence[str],
    claimed: dict[str, str],
) -> None:
    """Walk the user roots, importing only what the enable-list names.

    The order of the two checks in the loop is the phase's whole point:
    ``pkgutil.iter_modules`` yields a name **without importing it**, and
    ``importlib`` is reached only for a name the operator wrote in
    ``settings.conf``. An unlisted module is recorded and left alone, so
    dropping a file into the plugin directory cannot execute anything.
    """

    # Each root paired with what is in it, resolved before anything is
    # installed: on the overwhelmingly common machine with no user plugins at
    # all there is nothing to search, and nothing should be put into
    # `sys.modules` on its behalf.
    listings: list[tuple[Path, list]] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            listings.append((root, list(pkgutil.iter_modules([str(root)]))))
        except OSError:
            # An unreadable root is a non-event, not a failure. The operator
            # who chmod'd their own plugin directory does not need CLV to stop.
            continue

    wanted = set(enabled)
    found: set[str] = set()

    # Not an early return even when there is nothing to search: a plugin named
    # in settings.conf that is nowhere on disk still has to be reported, and
    # "the directory does not exist" is the most likely reason for it.
    if listings:
        _install_user_package([root for root, _ in listings])

    for root, entries in listings:
        for info in entries:
            if info.name.startswith("_"):
                continue
            key = info.name.casefold()
            found.add(key)
            origin = f"{root / info.name}"

            if key in claimed:
                registry.discovered.append(
                    DiscoveredPlugin(
                        name=info.name,
                        root=root,
                        enabled=False,
                        is_package=info.ispkg,
                        shadowed_by=claimed[key],
                    )
                )
                registry.errors.append(
                    PluginError(
                        origin,
                        f"shadowed by {claimed[key]}, which was found first",
                        category="shadowed",
                    )
                )
                continue

            if key not in wanted:
                registry.discovered.append(
                    DiscoveredPlugin(
                        name=info.name,
                        root=root,
                        enabled=False,
                        is_package=info.ispkg,
                    )
                )
                continue

            claimed[key] = origin
            registry.discovered.append(
                DiscoveredPlugin(
                    name=info.name,
                    root=root,
                    enabled=True,
                    is_package=info.ispkg,
                )
            )
            _load_module(
                registry,
                f"{USER_PLUGIN_PACKAGE}.{info.name}",
                origin=origin,
                clv_version=clv_version,
            )

    # A typo in settings.conf says so. Naming a plugin that is not there used
    # to be indistinguishable from naming nothing at all.
    where = ", ".join(str(root) for root in roots)
    for name in enabled:
        if name not in found:
            # Reported against the *name*, not against a generic "plugins"
            # origin: this is a row in the management UI as much as it is a line
            # in the log panel, and a row has to be able to say which plugin it
            # is about.
            registry.errors.append(
                PluginError(
                    name,
                    "named in settings.conf but was not found"
                    + (f" in {where}" if where else " -- no plugin directory exists"),
                    category="missing",
                )
            )


def _load_local(
    registry: PluginRegistry,
    clv_version: str,
    claimed: Optional[dict[str, str]] = None,
    claimed_by_user: Optional[dict[str, str]] = None,
) -> None:
    """Import drop-in modules under clv/plugins/ (flat and in subpackages).

    Where each subpackage *lives* is asked of the import system rather than of
    the filesystem. In a PyInstaller bundle the modules are inside the archive
    and ``clv/plugins/sources/`` is not a directory on disk, so testing
    ``is_dir()`` skipped every drop-in — silently, since finding no plugins is
    not an error. That is the whole of why the shipped binary offered no
    journal: not packaging, not the opt-in, just a filesystem check standing in
    for a question only the loader can answer.
    """

    claimed = {} if claimed is None else claimed
    claimed_by_user = {} if claimed_by_user is None else claimed_by_user

    package_dir = Path(__file__).resolve().parent
    search: list[tuple[str, str]] = [(str(package_dir), __name__)]
    for sub in _LOCAL_SUBPACKAGES:
        package_name = f"{__name__}.{sub}"
        try:
            subpackage = importlib.import_module(package_name)
        except ImportError:
            # A build that dropped the subpackage entirely. Not an error worth
            # reporting: an absent drop-in folder is a valid state.
            continue
        except Exception as exc:  # noqa: BLE001 - a broken __init__ is on them
            registry.errors.append(PluginError(package_name, f"import failed: {exc}"))
            continue
        search.extend((str(entry), package_name) for entry in getattr(subpackage, "__path__", ()))

    for directory, package_name in search:
        for info in pkgutil.iter_modules([directory]):
            if info.name.startswith("_") or info.name in _LOCAL_SUBPACKAGES:
                continue
            module_name = f"{package_name}.{info.name}"
            key = info.name.casefold()
            # Only a *user* root can shadow a bundled drop-in, and only one the
            # operator enabled -- an unlisted file is never imported, so it
            # cannot displace anything. Two bundled subpackages sharing a
            # basename are untouched by this: `claimed_by_user` holds user
            # names only, and nothing here writes into it.
            if key in claimed_by_user:
                registry.errors.append(
                    PluginError(
                        module_name,
                        f"shadowed by {claimed_by_user[key]}, which was found first",
                        category="shadowed",
                    )
                )
                continue
            claimed.setdefault(key, module_name)
            _load_module(
                registry, module_name, origin=module_name, clv_version=clv_version
            )


def _entry_point_candidates(loaded: Any) -> tuple[list[Any], Optional[str]]:
    """Resolve whatever an entry point pointed at into plugin candidates.

    Four legal target shapes, each handled and each documented, because the
    previous test — ``hasattr(loaded, "__name__") and not isinstance(loaded, type)``
    — is true for a **function** as well as a module. A perfectly correct
    ``clv.plugins = ["x = mypkg:make_plugin"]`` was therefore sent through
    :func:`_extract_plugins`, which found no ``register`` and no ``__all__`` on a
    function object, and the fallthrough handed the function itself to ``add()``
    to be rejected as "does not implement a CLV plugin interface" — a message
    about the wrong problem entirely.

    A ``Plugin`` instance is tested for before ``callable``: an instance is
    callable too if its class defines ``__call__``.
    """

    if isinstance(loaded, types.ModuleType):
        return _extract_plugins(loaded)
    if isinstance(loaded, Plugin):
        return [loaded], None
    if isinstance(loaded, type):
        return [loaded], None
    if callable(loaded):
        try:
            signature = inspect.signature(loaded)
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            signature = None
        if signature is not None and any(
            parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            for parameter in signature.parameters.values()
        ):
            return [], (
                "points at a callable that requires arguments; an entry point "
                "must name a module, a plugin class, or a zero-argument factory"
            )
        return _as_list(loaded()), None
    return [loaded], None


def _load_entry_points(
    registry: PluginRegistry,
    clv_version: str,
    claimed: Optional[dict[str, str]] = None,
) -> None:
    """Load plugins advertised by installed distributions.

    Last in the search order, so an entry point whose name a user root or a
    bundled drop-in already took loses it -- and is told so, rather than being
    resolved by whichever happened to run first.
    """

    claimed = {} if claimed is None else claimed

    try:
        entry_points = importlib.metadata.entry_points()
        selected = entry_points.select(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 - environment dependent
        registry.errors.append(PluginError(ENTRY_POINT_GROUP, f"lookup failed: {exc}"))
        return

    for entry_point in selected:
        origin = f"{ENTRY_POINT_GROUP}:{entry_point.name}"
        key = entry_point.name.casefold()
        if key in claimed:
            registry.errors.append(
                PluginError(
                    origin,
                    f"shadowed by {claimed[key]}, which was found first",
                    category="shadowed",
                )
            )
            continue
        claimed.setdefault(key, origin)
        try:
            loaded = entry_point.load()
        except Exception as exc:  # noqa: BLE001 - third-party code
            registry.errors.append(PluginError(origin, f"load failed: {exc}"))
            continue
        try:
            candidates, diagnosis = _entry_point_candidates(loaded)
        except Exception as exc:  # noqa: BLE001 - third-party code
            # Previously unguarded, so a module entry point whose register()
            # raised propagated straight out of load_plugins() — contradicting
            # its own "Never raises" contract.
            registry.errors.append(PluginError(origin, f"register() failed: {exc}"))
            continue
        if diagnosis:
            registry.errors.append(PluginError(origin, diagnosis))
        for candidate in candidates:
            registry.add(candidate, origin=origin, clv_version=clv_version)


def load_plugins(
    *,
    clv_version: Optional[str] = None,
    include_local: bool = True,
    include_entry_points: bool = True,
    include_user: bool = True,
    roots: Optional[Sequence[Path]] = None,
    enabled: Iterable[str] = (),
    settings: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> PluginRegistry:
    """Discover and load all available plugins.

    Search order, first name wins, and a loser is **reported** rather than
    silently dropped: ``CLV_PLUGIN_PATH``, then ``~/.config/clv/plugins/``,
    then the bundled ``clv/plugins/`` drop-ins, then ``clv.plugins`` entry
    points.

    *enabled* names the user-root modules the operator consented to run. It has
    to be passed in rather than applied afterwards, because it decides whether
    a module is **imported at all** -- filtering an already-loaded registry
    would be a consent check that runs after the code it was guarding.

    Bundled drop-ins ignore *enabled* entirely. They shipped with CLV, and the
    operator's trust in them is the trust they placed in CLV.

    *settings* is the parsed ``[plugin:<name>]`` sections. Seeded **before**
    anything is imported, so a plugin is configured on the same pass it is
    constructed rather than being handed its settings some time after it has
    started deciding things without them.

    Never raises: any failure is captured in :attr:`PluginRegistry.errors`.
    """

    if clv_version is None:
        from .. import __version__ as clv_version  # local import avoids a cycle

    registry = PluginRegistry()
    if settings:
        registry.refresh_settings(settings)
    #: Names taken by an enabled *user* module. Only these may displace a
    #: bundled drop-in; two bundled subpackages sharing a basename are two
    #: different modules, as they have always been.
    claimed_by_user: dict[str, str] = {}
    #: The above plus bundled module names -- what an entry point competes with.
    claimed: dict[str, str] = {}

    if include_user:
        search_roots = list(roots) if roots is not None else plugin_search_roots()
        _load_user_roots(
            registry,
            clv_version,
            search_roots,
            [name.casefold() for name in enabled],
            claimed,
        )
        claimed_by_user.update(claimed)
    if include_local:
        _load_local(registry, clv_version, claimed, claimed_by_user)
    if include_entry_points:
        _load_entry_points(registry, clv_version, claimed)

    _report_unclaimed_settings(registry)
    # Last, so it sorts everything every loader contributed. See
    # `PluginRegistry.order` for why this is not done incrementally in `add`.
    registry.order()
    return registry


def _report_unclaimed_settings(registry: PluginRegistry) -> None:
    """Name every ``[plugin:<name>]`` section that configures nothing.

    Two messages rather than one, because they have two different answers. A
    section for a plugin sitting in the plugin directory unnamed means "you
    tuned it but never turned it on"; a section for a name that is nowhere means
    "this is a typo, or the plugin is gone". Telling an operator the first when
    the second is true sends them looking in the wrong file.

    Reported against ``plugin:<name>`` -- the section header they would search
    for -- and categorised ``missing``, matching how a name in the enable-list
    that resolves to nothing is already reported.
    """

    discovered = {entry.name.casefold() for entry in registry.discovered}
    for name in registry._settings:
        if name in registry._configured or not registry._settings[name]:
            continue
        registry.errors.append(
            PluginError(
                f"plugin:{name}",
                "configured in settings.conf but not enabled; add it to the "
                "plugins list to run it"
                if name in discovered
                else "configured in settings.conf but no such plugin is loaded",
                category="missing",
            )
        )


__all__ = [
    "ENTRY_POINT_GROUP",
    "ERROR_CATEGORIES",
    "MAX_PLUGIN_ERRORS",
    "OPERATOR_DISABLE_REASON",
    "PLUGIN_API_VERSION",
    "PLUGIN_PATH_ENV",
    "PLUGIN_STATES",
    "USER_PLUGIN_PACKAGE",
    "DiscoveredPlugin",
    "Exporter",
    "ExportResult",
    "FilterContext",
    "FilterStage",
    "FormatStack",
    "IteratorReader",
    "LoadedPlugin",
    "LogFormat",
    "QueryOperator",
    "ComputedField",
    "QueryStack",
    "ClusterRule",
    "ShapeContributor",
    "ClusterStack",
    "TimelineAnnotation",
    "TimelineMetric",
    "TimelineStack",
    "WatchMatcher",
    "WatchSink",
    "WatchStack",
    "LogSourceProvider",
    "ProviderSource",
    "Plugin",
    "PluginError",
    "PluginErrors",
    "PluginRegistry",
    "PluginStatus",
    "load_plugins",
    "plugin_search_roots",
    "plugin_sort_key",
    "satisfies",
    "setting_bool",
    "setting_list",
]
