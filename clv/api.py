"""The published plugin API — what a plugin imports, and all it should import.

Everything here is a **re-export of the real thing**, never a wrapper. A plugin
receives the same :class:`~clv.services.parsing.LogEntry` objects CLV's own
render path holds, and hands back the same :class:`~clv.plugins.ExportResult`
CLV's own dialog reads. There is deliberately no DTO layer: a conversion per
entry per render is the one cost CLV cannot pay, and a plugin that had to
convert would be a second-class author writing against a first-class core.

Crossing a *process* boundary is the exception, and it has an explicit encoding
rather than an implicit one — see :func:`entry_to_wire` below.

Why this module exists at all
-----------------------------

Before it, a plugin author imported ``clv.plugins`` for the interfaces,
``clv.services.parsing`` for ``LogEntry``, ``clv.services.filtering`` for
``FilterSpec`` and ``clv.services.query`` for the field vocabulary: four
internal modules, none of them under any promise. That was survivable while the
only plugins were ones CLV shipped. It stops being survivable the moment there
is a public install path, because whatever third parties are importing on that
day is what CLV is stuck with.

So the surface is decided deliberately, published in one place, and frozen by
``tests/test_api_surface.py``, which holds :data:`__all__` and the signature of
every published callable as literal data. Changing the API means changing that
file, which means a reviewer sees it in the diff.

Two versions, and they are not the same version
-----------------------------------------------

:data:`PLUGIN_API_VERSION` tracks *the promise*; ``clv.__version__`` tracks the
application. They move independently, and a plugin should constrain the first::

    class Redact(FilterStage):
        name = "redact-secrets"
        requires_api = ">=1.0,<2.0"

The stability policy — what may change, what may not, and what a
``DeprecationWarning`` obliges CLV to do — is written out in
``clv/plugins/AGENTS.md`` under *API surface and stability*. The short form:
a published name is removed only on an API major, and **anything not published
here is internal and may move without notice**, including
``clv.services.parsing.LogEntry`` under its own name.

Importing this module is cheap and side-effect free: it pulls in no Textual
widget, no Rich renderable and not ``clv.app``, so a plugin's own unit tests can
use it without a running screen. That is asserted, not intended.
"""

from __future__ import annotations

from .plugins import (
    PLUGIN_API_VERSION,
    ClusterRule,
    ComputedField,
    Exporter,
    ExportResult,
    FilterContext,
    FilterStage,
    LogFormat,
    LogSourceProvider,
    Plugin,
    ProviderSource,
    QueryOperator,
    ShapeContributor,
    TimelineAnnotation,
    TimelineMetric,
    WatchMatcher,
    WatchSink,
    setting_bool,
    setting_list,
)
from .services.filtering import FilterSpec, TimeWindow
from .services.formats import DEFAULT_PROFILE, FormatProfile
from .services.parsing import (
    FORMAT_NAMES,
    LEVEL_CRITICAL,
    LEVEL_DEBUG,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_ORDER,
    LEVEL_TRACE,
    LEVEL_WARN,
    SEVERITY_BUCKETS,
    WIRE_VERSION,
    LogEntry,
    entry_from_wire,
    entry_to_wire,
    highest_level,
    level_matches,
    level_rank,
    normalize_level,
)
from .services.query import BUILTIN_OPERATORS, NORMALISED_FIELD_KEYS
from .services.refs import SourceRef
from .services.watch import KIND_PATTERN, SINK_SAMPLE_LIMIT, WatchRule

__all__ = [
    # The promise's own version. Constrain this, not clv.__version__.
    "PLUGIN_API_VERSION",
    # --- interfaces ---------------------------------------------------------
    "Plugin",
    "LogSourceProvider",
    "LogFormat",
    "QueryOperator",
    "ComputedField",
    "FilterStage",
    "ClusterRule",
    "ShapeContributor",
    "TimelineAnnotation",
    "TimelineMetric",
    "WatchMatcher",
    "WatchSink",
    "Exporter",
    # --- data handed to a plugin --------------------------------------------
    "LogEntry",
    "FilterContext",
    "FilterSpec",
    "TimeWindow",
    "ProviderSource",
    # What a source *is*. `ProviderSource.path` is one of these and
    # `LogSourceProvider.discover` may return one, so a provider author needs
    # the type even though CLV accepts a bare `Path` as shorthand.
    "SourceRef",
    # --- declaring a format --------------------------------------------------
    # A `format_name` is four registrations and only one of them is the parser.
    # `FormatProfile` says which of a format's fields earn the source cell and
    # the chips, so a plugin format's rows are structured on the same terms a
    # built-in's are rather than falling back to a bare message line.
    "FormatProfile",
    "DEFAULT_PROFILE",
    # The names CLV already answers to, and therefore the ones a plugin format
    # may not claim. Published so an author can check rather than discover it
    # from a load error.
    "FORMAT_NAMES",
    # --- extending the clustering --------------------------------------------
    # No constant joins these two, and the absence is the point. `FORMAT_NAMES`,
    # `BUILTIN_OPERATORS` and `KIND_PATTERN` are published because each names a
    # namespace a plugin can collide in and would otherwise discover from a load
    # error. Clustering has no such namespace: two rules may write the same
    # placeholder harmlessly, and a contributor claims nothing at all.
    # --- extending the timeline ----------------------------------------------
    # `TimeWindow` is already published above, and it is the whole of what a
    # `TimelineAnnotation` is handed -- no constant joins these two either. A
    # metric claims no namespace (there is one metric and it is settled by
    # priority, not by a name nobody else may take), and a mark claims nothing
    # at all.
    # --- extending the watch rules -------------------------------------------
    # A matcher is handed the whole rule, because its `pattern` is the matcher's
    # own parameter string and its `name` is the key to hold per-rule state
    # under.
    "WatchRule",
    # The rule kind CLV owns, and therefore the one a `WatchMatcher` may not
    # claim. Published on the same argument as `FORMAT_NAMES` and
    # `BUILTIN_OPERATORS`: an author should be able to check rather than
    # discover it from a load error.
    "KIND_PATTERN",
    # How many lines a `wants_entries` sink can actually be handed, so a sink
    # sizes its payload against the real ceiling rather than against the count.
    "SINK_SAMPLE_LIMIT",
    # --- data a plugin hands back -------------------------------------------
    "ExportResult",
    # --- the severity vocabulary --------------------------------------------
    # Published because the alternative is every plugin reimplementing it, and
    # reimplementing it badly: "WARNING" and "WARN" and syslog's `4` are the
    # same severity, and a plugin that decides otherwise makes CLV disagree
    # with itself about what an operator filtered for.
    "normalize_level",
    "level_rank",
    "level_matches",
    "highest_level",
    "LEVEL_TRACE",
    "LEVEL_DEBUG",
    "LEVEL_INFO",
    "LEVEL_NOTICE",
    "LEVEL_WARN",
    "LEVEL_ERROR",
    "LEVEL_CRITICAL",
    "LEVEL_ORDER",
    # Read-only by convention rather than by type: it is a plain dict and
    # mutating it would change what every severity filter in the process means.
    "SEVERITY_BUCKETS",
    # --- reading a plugin's own settings -------------------------------------
    # `configure()` hands over the raw strings configparser read, and the two
    # coercions every plugin then needs are published for the same reason
    # `normalize_level` is: the alternative is each plugin writing
    # `value.lower() == "true"` and CLV disagreeing with the operator's own file
    # about what `yes`, `on` and `1` mean.
    "setting_bool",
    "setting_list",
    # --- the field vocabulary -----------------------------------------------
    # The keys the parser normalises across formats, recognised as field terms
    # even before a line carrying one has been read.
    "NORMALISED_FIELD_KEYS",
    # --- extending the query -------------------------------------------------
    # The comparison tokens CLV owns, and therefore the ones a `QueryOperator`
    # may not claim. Published on the same argument as `FORMAT_NAMES`: an author
    # should be able to check rather than discover it from a load error.
    "BUILTIN_OPERATORS",
    # --- crossing a process boundary ----------------------------------------
    # Published now, though nothing in-process needs it, precisely so the wire
    # form is part of the frozen contract rather than an artefact of whichever
    # phase first needed to move an entry between processes.
    "WIRE_VERSION",
    "entry_to_wire",
    "entry_from_wire",
]
