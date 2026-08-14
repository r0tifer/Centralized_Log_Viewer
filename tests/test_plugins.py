from __future__ import annotations

import importlib
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from clv.plugins import (
    MAX_PLUGIN_ERRORS,
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
