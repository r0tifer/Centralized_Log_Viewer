from __future__ import annotations

import importlib
import os
import sys
import time
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from clv.plugins import (
    MAX_PLUGIN_ERRORS,
    PLUGIN_PATH_ENV,
    USER_PLUGIN_PACKAGE,
    DiscoveredPlugin,
    Exporter,
    ExportResult,
    FilterContext,
    FilterStage,
    LogSourceProvider,
    PluginError,
    PluginErrors,
    PluginRegistry,
    ProviderSource,
    load_plugins,
    plugin_search_roots,
    satisfies,
)
from clv.services.filtering import FilterSpec
from clv.services.parsing import LogEntry, parse_lines


CONTEXT = FilterContext(spec=FilterSpec(), source=Path("/tmp/x.log"))


class Redactor(FilterStage):
    name = "redactor"

    def apply(self, entry, context):
        if "password" not in entry.raw:
            return entry
        return replace(entry, raw=entry.raw.replace("password", "******"))


class DropDebug(FilterStage):
    name = "drop-debug"

    def apply(self, entry, context):
        return None if entry.level == "DEBUG" else entry


class Exploding(FilterStage):
    name = "exploding"

    def apply(self, entry, context):
        raise RuntimeError("boom")


class DemoSource(LogSourceProvider):
    name = "demo-source"

    def discover(self):
        return [Path("/virtual/demo.log")]

    def open(self, path):
        yield "virtual line"


class DemoExporter(Exporter):
    name = "demo-exporter"

    def export(self, entries, context):
        return ExportResult(ok=True, detail=f"exported {len(entries)}")


def _registry(*plugins) -> PluginRegistry:
    registry = PluginRegistry()
    for plugin in plugins:
        registry.add(plugin, origin="test", clv_version="2.1.0")
    return registry


def test_plugins_are_classified_by_interface() -> None:
    registry = _registry(Redactor(), DemoSource(), DemoExporter())

    assert [p.name for p in registry.filters] == ["redactor"]
    assert [p.name for p in registry.sources] == ["demo-source"]
    assert [p.name for p in registry.exporters] == ["demo-exporter"]
    assert registry.total == 3
    assert registry.errors == []


def test_classes_are_instantiated_automatically() -> None:
    registry = _registry(Redactor)
    assert len(registry.filters) == 1


def test_non_plugin_objects_are_rejected_not_raised() -> None:
    registry = _registry(object())

    assert registry.total == 0
    assert "does not implement" in registry.errors[0].message


def test_filter_stages_transform_and_drop() -> None:
    entries = parse_lines(
        [
            "2026-08-07 09:25:01 - INFO - password=hunter2",
            "2026-08-07 09:25:02 - DEBUG - noisy",
            "2026-08-07 09:25:03 - INFO - fine",
        ]
    )
    registry = _registry(Redactor(), DropDebug())

    result = registry.apply_filters(entries, CONTEXT)

    assert len(result) == 2
    assert "hunter2" in result[0].raw
    assert "password" not in result[0].raw
    assert all(entry.level != "DEBUG" for entry in result)


def test_a_raising_stage_is_disabled_rather_than_crashing_the_render() -> None:
    entries = parse_lines(["2026-08-07 09:25:01 - INFO - still here"])
    registry = _registry(Exploding(), Redactor())

    result = registry.apply_filters(entries, CONTEXT)

    assert [entry.raw for entry in result] == [entries[0].raw]
    assert any("raised" in error.message for error in registry.errors)


def test_version_constraints_are_enforced() -> None:
    assert satisfies("2.1.0", None)
    assert satisfies("2.1.0", ">=2.0,<3.0")
    assert not satisfies("2.1.0", ">=3.0")
    assert satisfies("2.0", "==2.0.0")  # padded comparison

    # Deliberately changed: this used to assert a silent False. A constraint CLV
    # cannot read is not an unsatisfied constraint, and conflating the two meant
    # a typo and a genuine incompatibility both made the plugin vanish with
    # nothing to distinguish them. See the matrix further down.
    with pytest.raises(ValueError):
        satisfies("2.1.0", "garbage")


def test_incompatible_plugin_is_recorded_and_skipped() -> None:
    class FromTheFuture(FilterStage):
        name = "future"
        requires_clv = ">=9.0"

        def apply(self, entry, context):
            return entry

    registry = _registry(FromTheFuture())

    assert registry.total == 0
    assert "requires CLV" in registry.errors[0].message


@pytest.fixture
def drop_in(tmp_path, monkeypatch):
    """Install a module into a real plugin subpackage, from a temp directory.

    These tests used to write ``.py`` files straight into the live
    ``clv/plugins/filters/`` and ``exporters/`` directories and unlink them in a
    ``finally``. That mutates the source tree during a run, leaves ``.pyc``
    residue behind in ``__pycache__`` that the unlink does not remove, and
    strands a ``tmp_*.py`` in the package for good if a run is interrupted — a
    stray plugin that then loads in every later run and in the developer's own
    viewer.

    ``_load_local`` asks the import system where each subpackage lives rather
    than asking the filesystem, walking the package's own ``__path__``. So
    extending that ``__path__`` puts a temp directory inside the package for the
    duration of one test without touching the tree, and exercises exactly the
    code path a real drop-in takes. Bytecode lands under ``tmp_path`` and dies
    with it.
    """

    known = set(sys.modules)

    def install(subpackage: str, module: str, body: str) -> str:
        package = importlib.import_module(f"clv.plugins.{subpackage}")
        root = tmp_path / subpackage
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{module}.py").write_text(body, encoding="utf-8")
        if str(root) not in package.__path__:
            monkeypatch.setattr(package, "__path__", [*package.__path__, str(root)])
        importlib.invalidate_caches()
        return f"clv.plugins.{subpackage}.{module}"

    yield install

    # monkeypatch restores __path__; sys.modules is ours to clean, or the next
    # test importing the same name gets this one's module object.
    for name in [n for n in sys.modules if n not in known]:
        del sys.modules[name]


def _local(**kwargs) -> PluginRegistry:
    return load_plugins(clv_version="2.1.0", include_entry_points=False, **kwargs)


# --- drop-in discovery ------------------------------------------------------


def test_local_discovery_loads_drop_in_modules(drop_in) -> None:
    """A module dropped into clv/plugins/filters/ is picked up by register()."""

    drop_in(
        "filters",
        "tmp_test_plugin",
        "from clv.plugins import FilterStage\n"
        "class Noop(FilterStage):\n"
        "    name = 'tmp-noop'\n"
        "    def apply(self, entry, context):\n"
        "        return entry\n"
        "def register():\n"
        "    return Noop()\n",
    )

    assert "tmp-noop" in [p.name for p in _local().filters]


def test_local_discovery_loads_drop_in_exporters(drop_in) -> None:
    """clv/plugins/exporters/ stays a drop-in directory.

    The three formats CLV ships are core (``clv.services.export``) so that a
    built-in cannot fail to load and the plugin count keeps meaning "installed
    plugins" — but the directory is still a live extension point, and this is
    the test that says so.
    """

    drop_in(
        "exporters",
        "tmp_test_exporter",
        "from clv.plugins import Exporter, ExportResult\n"
        "class Sink(Exporter):\n"
        "    name = 'tmp-sink'\n"
        "    def export(self, entries, context):\n"
        "        return ExportResult(ok=True, detail='ok')\n"
        "def register():\n"
        "    return Sink()\n",
    )

    assert "tmp-sink" in [p.name for p in _local().exporters]


def test_broken_drop_in_module_is_reported_not_fatal(drop_in) -> None:
    drop_in("filters", "tmp_broken_plugin", "raise RuntimeError('bad plugin')\n")

    registry = _local()

    assert any("import failed" in error.message for error in registry.errors)


def test_load_plugins_never_raises_on_a_clean_tree() -> None:
    registry = load_plugins(clv_version="2.1.0")
    assert isinstance(registry, PluginRegistry)


# --- a module that exports nothing is diagnosed, not ignored ----------------


def test_a_plugin_subclass_is_found_without_register_or_dunder_all(drop_in) -> None:
    """The namespace scan: the most forgiving of the three strategies.

    Writing the class and forgetting the boilerplate is the single most likely
    first-run mistake, and it used to produce zero plugins, zero errors and no
    clue.
    """

    drop_in(
        "filters",
        "tmp_bare_class",
        "from clv.plugins import FilterStage\n"
        "class Bare(FilterStage):\n"
        "    name = 'tmp-bare'\n"
        "    def apply(self, entry, context):\n"
        "        return entry\n",
    )

    registry = _local()

    assert "tmp-bare" in [p.name for p in registry.filters]
    assert not registry.errors


def test_the_namespace_scan_ignores_imported_bases_and_abstract_subclasses(drop_in) -> None:
    """Only concrete classes the module itself defined.

    ``FilterStage`` is in the module namespace because it was imported; a
    subclass that forgot ``apply`` would be instantiated into a confusing
    ``TypeError`` rather than a message about what is missing.
    """

    drop_in(
        "filters",
        "tmp_partial",
        "from clv.plugins import FilterStage\n"
        "class Abstract(FilterStage):\n"
        "    name = 'tmp-abstract'\n"
        "class Concrete(FilterStage):\n"
        "    name = 'tmp-concrete'\n"
        "    def apply(self, entry, context):\n"
        "        return entry\n",
    )

    registry = _local()
    names = [p.name for p in registry.filters]

    assert "tmp-concrete" in names
    assert "tmp-abstract" not in names
    # The imported base must not be collected and instantiated either.
    assert not any(type(p) is FilterStage for p in registry.filters)


def test_a_module_defining_no_plugin_says_so(drop_in) -> None:
    drop_in("filters", "tmp_empty", "VALUE = 1\n")

    registry = _local()

    assert any(
        "defines no plugin" in error.message and error.origin.endswith("tmp_empty")
        for error in registry.errors
    ), list(registry.errors)


def test_register_returning_nothing_is_deliberate_and_not_diagnosed(drop_in) -> None:
    """Declining to register is how a plugin says "not on this machine"."""

    drop_in("filters", "tmp_declines", "def register():\n    return []\n")

    registry = _local()

    assert not any("defines no plugin" in error.message for error in registry.errors)


def test_register_may_return_a_generator(drop_in) -> None:
    drop_in(
        "filters",
        "tmp_generator",
        "from clv.plugins import FilterStage\n"
        "class Gen(FilterStage):\n"
        "    name = 'tmp-gen'\n"
        "    def apply(self, entry, context):\n"
        "        return entry\n"
        "def register():\n"
        "    yield Gen()\n",
    )

    assert "tmp-gen" in [p.name for p in _local().filters]


# --- two providers cannot shadow each other ---------------------------------


class _Offering(LogSourceProvider):
    """A provider that offers one fixed identifier and knows its own lines."""

    def __init__(self, name: str, line: str, path: str = "/virtual/shared.log") -> None:
        self.name = name
        self._line = line
        self._path = Path(path)

    def discover(self):
        return [ProviderSource(self._path, "shared", self.name)]

    def open(self, path):
        yield self._line


def test_two_providers_offering_one_identifier_each_open_their_own_lines() -> None:
    """The defect: ``_owners`` keyed on the path alone, so the second won.

    Selecting provider A's row in the tree yielded provider B's lines — silently,
    with nothing anywhere to suggest that had happened.
    """

    alpha, beta = _Offering("alpha", "from-alpha"), _Offering("beta", "from-beta")
    registry = _registry(alpha, beta)

    sources = registry.discover_sources()

    assert len(sources) == 2
    for source in sources:
        reader = registry.open_source(source, max_lines=10)
        assert reader is not None
        assert reader.prime().lines == [f"from-{source.provider}"]


def test_a_shared_identifier_is_reported_once_naming_both_providers() -> None:
    registry = _registry(_Offering("alpha", "a"), _Offering("beta", "b"))

    registry.discover_sources()
    registry.discover_sources()  # a rescan must not double the report

    collisions = [e for e in registry.errors if "each opens its own source" in e.message]
    assert len(collisions) == 1
    assert "alpha" in collisions[0].origin and "beta" in collisions[0].origin


def test_a_source_naming_no_provider_resolves_when_it_is_unambiguous() -> None:
    """Older state, or a hand-built record, still opens when only one owner fits."""

    registry = _registry(_Offering("alpha", "from-alpha"))
    registry.discover_sources()

    anonymous = ProviderSource(Path("/virtual/shared.log"), "shared", "")
    reader = registry.open_source(anonymous, max_lines=10)

    assert reader is not None
    assert reader.prime().lines == ["from-alpha"]


def test_an_ambiguous_source_naming_no_provider_refuses_to_guess() -> None:
    registry = _registry(_Offering("alpha", "a"), _Offering("beta", "b"))
    registry.discover_sources()

    anonymous = ProviderSource(Path("/virtual/shared.log"), "shared", "")

    assert registry.open_source(anonymous, max_lines=10) is None
    assert any("refusing to guess" in error.message for error in registry.errors)


# --- disable() --------------------------------------------------------------


def test_a_raising_stage_is_disabled_for_the_session_not_the_pass() -> None:
    """The defect: 200 render passes produced 200 identical errors."""

    entries = parse_lines(["one", "two", "three"])
    registry = _registry(Exploding(), DropDebug())

    for _ in range(200):
        kept = registry.apply_filters(entries, CONTEXT)

    assert len(registry.errors) == 1
    assert registry.errors[0].count == 1, "the stage must not be retried, not merely deduped"
    assert registry.is_disabled(registry.filters[0])
    # The remaining stages keep running and the entries still render.
    assert len(kept) == len(entries)


def test_disable_is_idempotent_and_records_one_error() -> None:
    stage = Redactor()
    registry = _registry(stage)

    for _ in range(10):
        registry.disable(stage, "over budget")

    assert len(registry.errors) == 1
    assert registry.disabled_reason(stage) == "over budget"


def test_enable_puts_a_disabled_plugin_back() -> None:
    stage = Redactor()
    registry = _registry(stage)
    registry.disable(stage, "raised: boom")

    assert registry.enable(stage) is True
    assert not registry.is_disabled(stage)
    assert registry.enable(stage) is False


def test_a_disabled_plugin_is_skipped_but_never_removed() -> None:
    """Removal would re-target an export: app.py addresses exporters by index."""

    exporter = DemoExporter()
    registry = _registry(exporter)
    registry.disable(exporter, "raised: boom")

    assert registry.exporters == [exporter]
    assert registry.total == 1


def test_a_disabled_provider_neither_discovers_nor_opens() -> None:
    provider = _Offering("alpha", "from-alpha")
    registry = _registry(provider)
    sources = registry.discover_sources()
    registry.disable(provider, "raised: boom")

    assert registry.discover_sources() == []
    assert registry.open_source(sources[0], max_lines=10) is None


# --- errors are bounded and deduplicated ------------------------------------


def test_identical_failures_collapse_into_one_entry_with_a_count() -> None:
    errors = PluginErrors()

    for _ in range(500):
        errors.append(PluginError("stage", "raised: boom"))

    assert len(errors) == 1
    assert errors[0].count == 500
    assert "×500" in str(errors[0])


def test_the_error_cap_holds_and_says_how_many_were_dropped() -> None:
    errors = PluginErrors()

    for index in range(MAX_PLUGIN_ERRORS + 30):
        errors.append(PluginError(f"plugin-{index}", "broken"))

    assert len(errors) == MAX_PLUGIN_ERRORS
    assert errors.dropped == 30
    assert errors.overflow_note == "and 30 more"


def test_the_error_collection_still_behaves_like_the_list_it_replaced() -> None:
    """app.py indexes, slices, len()s and appends to this from outside."""

    errors = PluginErrors()
    assert not errors and errors == [] and len(errors) == 0
    assert errors.overflow_note == ""

    errors.append(PluginError("a", "one"))
    errors.append(PluginError("b", "two"))

    assert errors[0].origin == "a"
    assert [e.origin for e in errors[:2]] == ["a", "b"]
    assert len(errors) == 2
    assert bool(errors) is True


# --- version constraints ----------------------------------------------------


@pytest.mark.parametrize(
    ("version", "constraint", "expected"),
    [
        # the ordinary cases, which must keep working
        ("2.6.0", ">=2.0,<3.0", True),
        ("3.0.0", ">=2.0,<3.0", False),
        ("2.6.0", None, True),
        ("2.6.0", "", True),
        ("2.6.0", "2.6.0", True),
        ("2.0", "==2.0.0", True),
        ("2.6.0", "  >= 2.0 , < 3.0  ", True),
        # >= must not be a string comparison
        ("2.6.0", ">=2.10", False),
        ("2.10.0", ">=2.10", True),
        # the prerelease defect: "2.6.0rc1" became (2, 6, 1), so a release
        # candidate compared as *newer* than its own release
        ("2.6.0rc1", ">=2.6.0", False),
        ("2.6.0", ">=2.6.0rc1", True),
        ("2.6.0rc1", "<2.6.0", True),
        ("1.0b2", ">1.0b1", True),
        ("1.0a1", ">1.0.dev1", True),
        ("1.0", ">1.0rc1", True),
        ("1.0.post1", ">1.0", True),
        ("1.0alpha1", "==1.0a1", True),  # spelling is normalised
        # ~= was rejected outright, disabling the plugin that used it
        ("2.6.0", "~=2.6", True),
        ("2.9.9", "~=2.6", True),
        ("3.0.0", "~=2.6", False),
        ("2.6.5", "~=2.6.1", True),
        ("2.6.0", "~=2.6.1", False),
        ("2.7.0", "~=2.6.1", False),
        # ^ likewise, accepted as a documented Poetry alias
        ("2.6.0", "^2.0", True),
        ("3.0.0", "^2.0", False),
        ("0.2.9", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        # wildcards and negation
        ("2.6.0", "==2.6.*", True),
        ("2.6.9", "==2.6.*", True),
        ("2.7.0", "==2.6.*", False),
        ("2.6.0rc1", "==2.6.*", True),
        ("2.6.0", "!=2.6.0", False),
        ("2.6.1", "!=2.6.0", True),
        ("2.7.0", "!=2.6.*", True),
    ],
)
def test_version_constraint_matrix(version, constraint, expected) -> None:
    assert satisfies(version, constraint) is expected


def test_a_prerelease_of_clv_does_not_disable_every_plugin() -> None:
    """CLV's one deliberate divergence from PEP 440, pinned.

    Strict PEP 440 excludes prereleases from a range that does not name one, so
    ``>=2.6`` would be unsatisfied on a running 2.7.0rc1 and every plugin would
    vanish on any release-candidate build. CLV compares in plain order instead.
    """

    assert satisfies("2.7.0rc1", ">=2.6") is True
    assert satisfies("2.7.0rc1", ">=2.0,<3.0") is True


@pytest.mark.parametrize(
    "constraint",
    ["~~2.6", ">=abc", "2.6.0..", "=>2.6", ">=", "~=2", "<=2.*", ">= <2.0"],
)
def test_an_unparseable_constraint_raises_naming_itself(constraint) -> None:
    """Never a silent False.

    The previous comparator returned False for anything it could not read, so a
    typo and a genuine incompatibility were indistinguishable from the outside:
    both simply made the plugin disappear.
    """

    with pytest.raises(ValueError) as excinfo:
        satisfies("2.6.0", constraint)

    assert constraint in str(excinfo.value) or "2" in str(excinfo.value)


def test_a_plugin_with_an_unreadable_constraint_is_reported_by_name() -> None:
    class Typo(FilterStage):
        name = "typo"
        requires_clv = "~~2.6"

        def apply(self, entry, context):
            return entry

    registry = _registry(Typo())

    assert registry.total == 0
    assert "bad requires_clv" in registry.errors[0].message
    assert "~~2.6" in registry.errors[0].message


# --- entry point target shapes ----------------------------------------------


class _FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


def _load_entry_point(monkeypatch, target) -> PluginRegistry:
    import clv.plugins as plugins_pkg

    monkeypatch.setattr(
        plugins_pkg.importlib.metadata,
        "entry_points",
        lambda: SimpleNamespace(select=lambda group: [_FakeEntryPoint("demo", target)]),
    )
    return load_plugins(clv_version="2.1.0", include_local=False)


def test_an_entry_point_may_name_a_plugin_class(monkeypatch) -> None:
    registry = _load_entry_point(monkeypatch, Redactor)

    assert [p.name for p in registry.filters] == ["redactor"]
    assert not registry.errors


def test_an_entry_point_may_name_a_module(monkeypatch) -> None:
    module = types.ModuleType("demo_module")
    module.register = lambda: Redactor()
    registry = _load_entry_point(monkeypatch, module)

    assert [p.name for p in registry.filters] == ["redactor"]
    assert not registry.errors


def test_an_entry_point_may_name_a_zero_argument_factory(monkeypatch) -> None:
    """The defect: a factory was rejected as "does not implement an interface".

    ``entry_points = {"clv.plugins": ["x = mypkg:make_plugin"]}`` is the most
    natural thing to write and the message it produced was about the wrong
    problem entirely.
    """

    registry = _load_entry_point(monkeypatch, lambda: Redactor())

    assert [p.name for p in registry.filters] == ["redactor"]
    assert not registry.errors


def test_an_entry_point_may_name_an_instance(monkeypatch) -> None:
    registry = _load_entry_point(monkeypatch, Redactor())

    assert [p.name for p in registry.filters] == ["redactor"]


def test_a_factory_needing_arguments_is_reported_as_such(monkeypatch) -> None:
    registry = _load_entry_point(monkeypatch, lambda config: Redactor())

    assert registry.total == 0
    assert "zero-argument factory" in registry.errors[0].message


def test_an_entry_point_whose_register_raises_does_not_escape(monkeypatch) -> None:
    """``load_plugins`` documents that it never raises; this used to be a lie."""

    module = types.ModuleType("demo_broken")

    def register():
        raise RuntimeError("boom")

    module.register = register
    registry = _load_entry_point(monkeypatch, module)

    assert any("register() failed" in error.message for error in registry.errors)


# --- the published API contract ---------------------------------------------


def test_requires_api_is_checked_and_accepted() -> None:
    """The constraint the documentation tells authors to use, honoured."""

    class Modern(FilterStage):
        name = "modern"
        requires_api = ">=1.0,<2.0"

        def apply(self, entry, context):
            return entry

    registry = _registry(Modern())

    assert [p.name for p in registry.filters] == ["modern"]
    assert not registry.errors


def test_an_unsatisfied_requires_api_names_both_versions() -> None:
    """"Which API do you want, and which am I" — a report a stranger can act on."""

    class FromTheFuture(FilterStage):
        name = "future-api"
        requires_api = ">=2.0"

        def apply(self, entry, context):
            return entry

    registry = PluginRegistry()
    registry.add(FromTheFuture(), origin="test", clv_version="2.1.0", api_version="1.0")

    assert registry.total == 0
    message = registry.errors[0].message
    assert "requires plugin API >=2.0" in message
    assert "running 1.0" in message


def test_an_unreadable_requires_api_is_an_error_not_a_silent_false() -> None:
    """Same rule as ``requires_clv``: a typo must not look like incompatibility."""

    class Typo(FilterStage):
        name = "api-typo"
        requires_api = "~~1.0"

        def apply(self, entry, context):
            return entry

    registry = _registry(Typo())

    assert registry.total == 0
    assert "bad requires_api" in registry.errors[0].message
    assert "~~1.0" in registry.errors[0].message


def test_both_constraints_are_checked_independently() -> None:
    """A plugin may declare either or both, and each fails on its own account.

    The pairing matters: `requires_clv` satisfied is not a reason to skip
    `requires_api`, and a loop over the two that short-circuited on the first
    pass would do exactly that.
    """

    class Both(FilterStage):
        name = "both"
        requires_clv = ">=2.0,<3.0"
        requires_api = ">=9.0"

        def apply(self, entry, context):
            return entry

    registry = PluginRegistry()
    registry.add(Both(), origin="test", clv_version="2.1.0", api_version="1.0")

    assert registry.total == 0
    assert "requires plugin API" in registry.errors[0].message

    class OtherWay(Both):
        name = "other-way"
        requires_clv = ">=9.0"
        requires_api = ">=1.0"

    registry = PluginRegistry()
    registry.add(OtherWay(), origin="test", clv_version="2.1.0", api_version="1.0")

    assert registry.total == 0
    assert "requires CLV" in registry.errors[0].message


def test_a_plugin_declaring_neither_constraint_still_loads() -> None:
    """`requires_api` is optional, and adding it changed nothing for anyone."""

    assert _registry(Redactor()).total == 1


def test_an_exporter_written_before_wants_path_is_never_handed_a_destination() -> None:
    """The compatibility claim, tested against code that would break if it were false.

    ``Exporter`` is an ABC and does not check signatures, so an exporter written
    against the original three-argument ``export`` subclasses cleanly. What
    keeps it *working* is that ``wants_path`` defaults to False and the app
    calls the two shapes differently — this asserts the old shape is still
    called the old way, by using one that would raise ``TypeError`` otherwise.
    """

    class Legacy(Exporter):
        name = "legacy"

        def __init__(self) -> None:
            self.seen = 0

        def export(self, entries, context):  # no `destination`, deliberately
            self.seen = len(entries)
            return ExportResult(ok=True, detail="legacy")

    legacy = Legacy()
    registry = _registry(legacy)

    assert registry.exporters == [legacy]
    assert legacy.wants_path is False
    assert legacy.suggested_extension == ""
    assert legacy.export([], CONTEXT).ok


def test_an_exporter_can_ask_for_the_operator_s_destination() -> None:
    class Writer(Exporter):
        name = "writer"
        wants_path = True
        suggested_extension = "ndjson"

        def __init__(self) -> None:
            self.destination = None

        def export(self, entries, context, *, destination=None):
            self.destination = destination
            return ExportResult(ok=True, detail="written", destination=destination)

    writer = Writer()
    _registry(writer)

    outcome = writer.export([], CONTEXT, destination=Path("/tmp/out.ndjson"))

    assert writer.destination == Path("/tmp/out.ndjson")
    assert outcome.destination == Path("/tmp/out.ndjson")


def test_a_plugin_importing_only_clv_api_loads_and_runs_every_interface(drop_in) -> None:
    """Phase 2's gate, as a test rather than as a paragraph.

    The published API is only a promise if a plugin can be written against it
    *alone*. This drops a module whose sole CLV import is ``clv.api`` into a
    real plugin subpackage, loads it through the real loader, and then exercises
    all three interfaces — including the two things the API added, a
    ``requires_api`` constraint and an exporter that asks for a destination.

    If ``clv.api`` ever stops re-exporting something an author needs, this fails
    at import with a ``ImportError`` recorded against the module, which is
    exactly how a stranger would experience the same gap.
    """

    module = drop_in(
        "filters",
        "tmp_api_only_plugin",
        "from dataclasses import replace\n"
        "\n"
        "from clv.api import (\n"
        "    Exporter,\n"
        "    ExportResult,\n"
        "    FilterStage,\n"
        "    LEVEL_ERROR,\n"
        "    LogSourceProvider,\n"
        "    NORMALISED_FIELD_KEYS,\n"
        "    ProviderSource,\n"
        "    entry_from_wire,\n"
        "    entry_to_wire,\n"
        "    level_rank,\n"
        "    normalize_level,\n"
        ")\n"
        "from pathlib import Path\n"
        "\n"
        "class ApiSource(LogSourceProvider):\n"
        "    name = 'api-source'\n"
        "    requires_api = '>=1.0,<2.0'\n"
        "    def discover(self):\n"
        "        return [ProviderSource(Path('/virtual/api.log'), 'API log')]\n"
        "    def open(self, path):\n"
        "        yield 'a line from the api-only provider'\n"
        "\n"
        "class ApiStage(FilterStage):\n"
        "    name = 'api-stage'\n"
        "    requires_api = '>=1.0,<2.0'\n"
        "    def apply(self, entry, context):\n"
        "        assert 'host' in NORMALISED_FIELD_KEYS\n"
        "        if level_rank(normalize_level('err')) != level_rank(LEVEL_ERROR):\n"
        "            return None\n"
        "        return replace(entry, message=entry.message.upper())\n"
        "\n"
        "class ApiExporter(Exporter):\n"
        "    name = 'api-exporter'\n"
        "    requires_api = '>=1.0,<2.0'\n"
        "    wants_path = True\n"
        "    suggested_extension = 'ndjson'\n"
        "    def export(self, entries, context, *, destination=None):\n"
        "        # Round-tripping through the wire form is what a plugin does\n"
        "        # when it hands entries to something outside this process.\n"
        "        moved = [entry_from_wire(entry_to_wire(e)) for e in entries]\n"
        "        return ExportResult(ok=True, detail=str(len(moved)), destination=destination)\n"
        "\n"
        "def register():\n"
        "    return [ApiSource(), ApiStage(), ApiExporter()]\n",
    )

    registry = _local()

    assert not [error for error in registry.errors if module in error.origin]
    assert [p.name for p in registry.sources if p.name == "api-source"] == ["api-source"]
    assert [p.name for p in registry.filters if p.name == "api-stage"] == ["api-stage"]
    assert [p.name for p in registry.exporters if p.name == "api-exporter"] == [
        "api-exporter"
    ]

    # The provider offers its source and opens it.
    offered = [s for s in registry.discover_sources() if s.provider == "api-source"]
    assert [s.label for s in offered] == ["API log"]
    reader = registry.open_source(offered[0], max_lines=10)
    assert reader.prime().lines == ["a line from the api-only provider"]

    # The stage runs over real entries and the exporter takes a destination.
    entries = parse_lines(["2026-08-21 09:25:01 ERROR boom"])
    staged = registry.apply_filters(entries, CONTEXT)
    assert [e.message for e in staged] == ["BOOM"]

    exporter = next(p for p in registry.exporters if p.name == "api-exporter")
    outcome = exporter.export(staged, CONTEXT, destination=Path("/tmp/api.ndjson"))
    assert outcome.ok and outcome.detail == "1"


# --- the user plugin directory ----------------------------------------------
#
# Phase 3. A plugin an operator installed lives outside the package, is found
# without being imported, and runs only once they have named it. The tests
# below are ordered the way the rule is: what loads, then what must *not*.


@pytest.fixture(autouse=True)
def _forget_user_plugins():
    """Drop the synthetic user package between tests.

    ``load_plugins`` installs it in ``sys.modules`` whenever it has roots to
    search, so without this a test that loaded a plugin from a temp directory
    would leave the package — and everything imported under it — for the next
    test that asked for the same name.
    """

    yield
    for name in [
        n
        for n in sys.modules
        if n == USER_PLUGIN_PACKAGE or n.startswith(f"{USER_PLUGIN_PACKAGE}.")
    ]:
        del sys.modules[name]


@pytest.fixture
def user_root(tmp_path, monkeypatch):
    """Plugin roots on ``CLV_PLUGIN_PATH`` — which is what that variable is for.

    Simpler than ``drop_in``'s ``__path__`` surgery, and deliberately so: this
    is the documented development mechanism, so the tests and a plugin author
    reach the loader by the same door. ``conftest.py`` already points
    ``XDG_CONFIG_HOME`` at a temp tree, so the real user plugin directory is
    isolated for free and stays empty.

    ``sys.modules`` is cleaned up for the same reason ``drop_in`` cleans it: the
    synthetic user package and everything imported under it would otherwise be
    handed to the next test that imports the same name.
    """

    known = set(sys.modules)
    roots: list[Path] = []

    def make(name: str = "root") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        roots.append(root)
        monkeypatch.setenv(PLUGIN_PATH_ENV, os.pathsep.join(str(r) for r in roots))
        importlib.invalidate_caches()
        return root

    yield make

    for name in [n for n in sys.modules if n not in known]:
        del sys.modules[name]


#: A filter stage whose name says which file it came from, so a shadowing test
#: can tell the winner from the loser rather than merely counting.
_STAGE = """\
from clv.api import FilterStage

class Stage(FilterStage):
    name = "{name}"

    def apply(self, entry, context):
        return entry
"""


def _plugin(root: Path, module: str, name: str | None = None) -> Path:
    path = root / f"{module}.py"
    path.write_text(_STAGE.format(name=name or module), encoding="utf-8")
    return path


def _user(**kwargs) -> PluginRegistry:
    """Load from user roots only, so a real bundled plugin cannot blur a result."""

    return load_plugins(
        clv_version="2.1.0", include_local=False, include_entry_points=False, **kwargs
    )


def test_a_named_module_in_a_user_root_loads(user_root) -> None:
    root = user_root()
    _plugin(root, "redact_secrets")

    registry = _user(enabled=["redact_secrets"])

    assert [stage.name for stage in registry.filters] == ["redact_secrets"]
    assert not registry.errors


def test_an_unnamed_module_is_listed_but_not_loaded(user_root) -> None:
    root = user_root()
    _plugin(root, "redact_secrets")

    registry = _user(enabled=[])

    assert registry.total == 0
    assert [entry.name for entry in registry.available()] == ["redact_secrets"]
    # Being installed and not enabled is the designed resting state, not a
    # problem, so it must not reach the channel the log panel paints amber.
    assert not registry.errors


def test_an_unnamed_module_is_never_imported(user_root) -> None:
    """Requirement 2's teeth, and the one assertion here that cannot be softened.

    Deliberately not an assertion about the registry: a module could be
    imported for its side effects and then have its plugins discarded, and
    every registry-level assertion in this file would still pass while the
    operator's machine had run a stranger's code. The only proof is that the
    code did not run, so the module writes a file and the test is that the file
    is not there.
    """

    root = user_root()
    sentinel = root / "SENTINEL"
    (root / "loud.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported')\n" + _STAGE.format(name="loud"),
        encoding="utf-8",
    )

    registry = _user(enabled=[])

    assert not sentinel.exists(), "an unlisted plugin executed code"
    assert registry.total == 0

    # And the same module, named, does run — otherwise the assertion above
    # would pass just as well against a loader that had stopped working.
    registry = _user(enabled=["loud"])
    assert sentinel.exists()
    assert [stage.name for stage in registry.filters] == ["loud"]


def test_a_package_directory_loads_like_a_single_file(user_root) -> None:
    """Including a relative import, which is why the synthetic package exists."""

    root = user_root()
    package = root / "nginx_format"
    package.mkdir()
    (package / "patterns.py").write_text("LABEL = 'nginx'\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from . import patterns\n" + _STAGE.format(name="{name}").format(name="nginx"),
        encoding="utf-8",
    )
    # The sibling module is reachable, which a bare sys.path insertion or a
    # hand-rolled spec loader would not give for free.
    (package / "__init__.py").write_text(
        "from . import patterns\n"
        "from clv.api import FilterStage\n"
        "\n"
        "class Stage(FilterStage):\n"
        "    name = patterns.LABEL\n"
        "\n"
        "    def apply(self, entry, context):\n"
        "        return entry\n",
        encoding="utf-8",
    )

    registry = _user(enabled=["nginx_format"])

    assert [stage.name for stage in registry.filters] == ["nginx"]
    assert [entry.is_package for entry in registry.discovered] == [True]


def test_the_enable_list_is_matched_without_regard_to_case(user_root) -> None:
    root = user_root()
    _plugin(root, "redact_secrets")

    registry = _user(enabled=["Redact_Secrets"])

    assert [stage.name for stage in registry.filters] == ["redact_secrets"]


def test_a_module_whose_name_starts_with_underscore_is_skipped(user_root) -> None:
    root = user_root()
    _plugin(root, "_private")

    registry = _user(enabled=["_private"])

    assert registry.total == 0
    assert registry.discovered == []
    # It is not "installed but not enabled" either — CLV never considered it.
    assert "not found" in registry.errors[0].message


def test_the_earlier_root_wins_and_the_shadow_is_reported(user_root) -> None:
    first, second = user_root("first"), user_root("second")
    _plugin(first, "dup", name="from-first")
    _plugin(second, "dup", name="from-second")

    registry = _user(enabled=["dup"])

    assert [stage.name for stage in registry.filters] == ["from-first"]
    assert len(registry.errors) == 1
    assert "shadowed by" in registry.errors[0].message
    assert str(first) in registry.errors[0].message
    shadowed = [e for e in registry.discovered if e.shadowed_by]
    assert [entry.root for entry in shadowed] == [second]


def test_an_enabled_user_module_shadows_a_bundled_one(user_root, drop_in) -> None:
    """Replacing a shipped plugin is possible, and it is reported when it happens."""

    drop_in("filters", "shadowme", _STAGE.format(name="bundled"))
    root = user_root()
    _plugin(root, "shadowme", name="from-the-user")

    registry = load_plugins(
        clv_version="2.1.0", include_entry_points=False, enabled=["shadowme"]
    )

    names = [stage.name for stage in registry.filters]
    assert "from-the-user" in names
    assert "bundled" not in names
    assert any(
        "shadowed by" in error.message and "shadowme" in error.origin
        for error in registry.errors
    )


def test_an_unlisted_user_module_shadows_nothing(user_root, drop_in) -> None:
    """The guard that keeps a dropped file from changing CLV without being named.

    Without it, copying a file called `journald.py` into the plugin directory
    would take the shipped journal provider out of service — an install-time
    side effect from a file the operator never enabled, which is precisely what
    the enable-list exists to prevent.
    """

    drop_in("filters", "shadowme", _STAGE.format(name="bundled"))
    root = user_root()
    _plugin(root, "shadowme", name="from-the-user")

    registry = load_plugins(clv_version="2.1.0", include_entry_points=False, enabled=[])

    names = [stage.name for stage in registry.filters]
    assert "bundled" in names
    assert "from-the-user" not in names
    assert not [e for e in registry.errors if "shadowed" in e.message]


def test_two_bundled_subpackages_may_share_a_module_name(drop_in) -> None:
    """Unchanged behaviour, pinned because the shadow rule could have broken it.

    `sources/x.py` and `filters/x.py` are two different modules and always have
    been. Only a *user* root may claim a name away from a bundled drop-in.
    """

    drop_in("filters", "samename", _STAGE.format(name="the-filter"))
    drop_in(
        "exporters",
        "samename",
        "from clv.api import Exporter, ExportResult\n"
        "\n"
        "class E(Exporter):\n"
        "    name = 'the-exporter'\n"
        "\n"
        "    def export(self, entries, context, *, destination=None):\n"
        "        return ExportResult(ok=True, detail='')\n",
    )

    registry = load_plugins(clv_version="2.1.0", include_entry_points=False)

    assert "the-filter" in [stage.name for stage in registry.filters]
    assert "the-exporter" in [exp.name for exp in registry.exporters]
    assert not [e for e in registry.errors if "shadowed" in e.message]


def test_a_named_but_absent_plugin_is_reported_by_name(user_root) -> None:
    root = user_root()
    _plugin(root, "present")

    registry = _user(enabled=["present", "typo_here"])

    assert [stage.name for stage in registry.filters] == ["present"]
    assert len(registry.errors) == 1
    assert "typo_here" in registry.errors[0].message
    assert str(root) in registry.errors[0].message


def test_a_missing_or_empty_root_is_a_silent_non_event(tmp_path, monkeypatch) -> None:
    absent = tmp_path / "not-there"
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(PLUGIN_PATH_ENV, os.pathsep.join([str(absent), str(empty)]))

    registry = _user(enabled=[])

    assert registry.total == 0
    assert registry.discovered == []
    assert not registry.errors


def test_an_unreadable_root_is_reported_or_ignored_never_raised(
    tmp_path, monkeypatch
) -> None:
    """An operator who chmod'd their own plugin directory does not need a crash."""

    root = tmp_path / "locked"
    root.mkdir()
    _plugin(root, "hidden")
    root.chmod(0o000)
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(root))
    try:
        # Running as root, mode 000 stops nothing, and the assertion below would
        # be about the container rather than about the loader.
        if os.access(root, os.R_OK):
            pytest.skip("this user can read a mode-000 directory")
        registry = _user(enabled=["hidden"])
    finally:
        root.chmod(0o755)

    # Either silently skipped or reported; raising is what must not happen.
    assert registry.total == 0


def test_no_roots_at_all_stays_silent(monkeypatch) -> None:
    """Requirement 10: a build with no user plugins costs nothing and says nothing."""

    monkeypatch.delenv(PLUGIN_PATH_ENV, raising=False)

    registry = _user(roots=[], enabled=[])

    assert registry.total == 0
    assert registry.discovered == []
    assert not registry.errors
    # Nothing was imported, so no synthetic package was installed.
    assert USER_PLUGIN_PACKAGE not in sys.modules


def test_a_named_plugin_with_no_plugin_directory_still_says_so() -> None:
    """The silence guard: no directory is the likeliest reason a name is absent.

    Worth its own test because the natural implementation returns early when
    there is nothing to search, which would swallow exactly the report the
    operator in this situation needs.
    """

    registry = _user(roots=[], enabled=["redact_secrets"])

    assert len(registry.errors) == 1
    assert "redact_secrets" in registry.errors[0].message
    assert "no plugin directory" in registry.errors[0].message


def test_clv_plugin_path_is_searched_before_the_user_directory(
    tmp_path, monkeypatch
) -> None:
    from clv.services.config import user_plugin_dir

    override = tmp_path / "dev"
    override.mkdir()
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(override))

    roots = plugin_search_roots()

    assert roots[0] == override
    assert roots[-1] == user_plugin_dir()


def test_the_search_roots_are_de_duplicated(tmp_path, monkeypatch) -> None:
    """Naming the user directory in CLV_PLUGIN_PATH must not make it shadow itself."""

    from clv.services.config import user_plugin_dir

    monkeypatch.setenv(PLUGIN_PATH_ENV, str(user_plugin_dir()))

    assert plugin_search_roots().count(user_plugin_dir()) == 1


def test_the_user_root_does_not_depend_on_where_the_package_lives(
    tmp_path, monkeypatch
) -> None:
    """A frozen build is an equal citizen, and this is the reason it can be.

    The bundled walk asks the *import system* where `clv/plugins/sources/`
    lives, because under PyInstaller it is inside an archive and not a
    directory at all — the lesson recorded on `_load_local`. The user root is
    the opposite case and must not inherit that problem: it is derived from
    `$XDG_CONFIG_HOME`, never from `__file__`, so it is a real writable
    directory in every build.

    Simulating `sys._MEIPASS` is a proxy for a real frozen build, not a
    substitute for one. It is here because the property it pins — that nothing
    about finding a user plugin is a function of where the package was
    unpacked — is the one that would silently make the binary user's install
    path vanish, exactly as `Path.is_dir()` once silently removed the journal.
    """

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    root = tmp_path / "userplugins"
    root.mkdir()
    _plugin(root, "frozen_ok")
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(root))

    registry = _user(enabled=["frozen_ok"])

    assert [stage.name for stage in registry.filters] == ["frozen_ok"]
    assert not registry.errors


def test_discovering_many_unlisted_files_costs_nothing_measurable(
    tmp_path, monkeypatch
) -> None:
    """A directory of files nobody enabled must not become a startup cost.

    A deliberately loose ceiling, in the shape `tests/test_clustering.py` uses:
    the point is to catch an order-of-magnitude regression — an implementation
    that imports first and filters afterwards — not to measure this machine. A
    tight budget on a shared CI box is a flaky test, and a flaky test gets
    deleted. The measured number goes in the commit message.
    """

    empty = tmp_path / "empty"
    empty.mkdir()
    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for index in range(200):
        _plugin(crowded, f"plugin_{index}")

    monkeypatch.setenv(PLUGIN_PATH_ENV, str(empty))
    start = time.perf_counter()
    _user(enabled=[])
    baseline = time.perf_counter() - start

    monkeypatch.setenv(PLUGIN_PATH_ENV, str(crowded))
    start = time.perf_counter()
    registry = _user(enabled=[])
    crowded_cost = time.perf_counter() - start

    assert len(registry.available()) == 200
    assert registry.total == 0, "not one of them was imported"
    assert crowded_cost < 0.5, (
        f"200 unlisted files took {crowded_cost:.3f}s against {baseline:.3f}s empty"
    )


def test_the_registry_reports_where_each_plugin_was_found(user_root) -> None:
    root = user_root()
    _plugin(root, "enabled_one")
    _plugin(root, "disabled_one")

    registry = _user(enabled=["enabled_one"])

    found = {entry.name: entry for entry in registry.discovered}
    assert found["enabled_one"] == DiscoveredPlugin(
        name="enabled_one", root=root, enabled=True, is_package=False
    )
    assert found["disabled_one"].enabled is False
    assert all(entry.root == root for entry in registry.discovered)
