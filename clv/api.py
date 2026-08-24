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
    Exporter,
    ExportResult,
    FilterContext,
    FilterStage,
    LogSourceProvider,
    Plugin,
    ProviderSource,
    setting_bool,
    setting_list,
)
from .services.filtering import FilterSpec, TimeWindow
from .services.parsing import (
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
from .services.query import NORMALISED_FIELD_KEYS
from .services.refs import SourceRef

__all__ = [
    # The promise's own version. Constrain this, not clv.__version__.
    "PLUGIN_API_VERSION",
    # --- interfaces ---------------------------------------------------------
    "Plugin",
    "LogSourceProvider",
    "FilterStage",
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
    # --- crossing a process boundary ----------------------------------------
    # Published now, though nothing in-process needs it, precisely so the wire
    # form is part of the frozen contract rather than an artefact of whichever
    # phase first needed to move an entry between processes.
    "WIRE_VERSION",
    "entry_to_wire",
    "entry_from_wire",
]
