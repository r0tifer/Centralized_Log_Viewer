from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from clv.plugins import (
    Exporter,
    ExportResult,
    FilterContext,
    FilterStage,
    LogSourceProvider,
    PluginRegistry,
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
    assert not satisfies("2.1.0", "garbage")


def test_incompatible_plugin_is_recorded_and_skipped() -> None:
    class FromTheFuture(FilterStage):
        name = "future"
        requires_clv = ">=9.0"

        def apply(self, entry, context):
            return entry

    registry = _registry(FromTheFuture())

    assert registry.total == 0
    assert "requires CLV" in registry.errors[0].message


def test_local_discovery_loads_drop_in_modules(tmp_path: Path, monkeypatch) -> None:
    """A module dropped into clv/plugins/filters/ is picked up by register()."""
    import clv.plugins as plugins_pkg

    plugin_dir = Path(plugins_pkg.__file__).resolve().parent / "filters"
    plugin_dir.mkdir(exist_ok=True)
    module_path = plugin_dir / "tmp_test_plugin.py"
    module_path.write_text(
        "from clv.plugins import FilterStage\n"
        "class Noop(FilterStage):\n"
        "    name = 'tmp-noop'\n"
        "    def apply(self, entry, context):\n"
        "        return entry\n"
        "def register():\n"
        "    return Noop()\n",
        encoding="utf-8",
    )
    try:
        registry = load_plugins(clv_version="2.1.0", include_entry_points=False)
        assert "tmp-noop" in [p.name for p in registry.filters]
    finally:
        module_path.unlink()


def test_local_discovery_loads_drop_in_exporters() -> None:
    """clv/plugins/exporters/ stays a drop-in directory.

    The three formats CLV ships are core (``clv.services.export``) so that a
    built-in cannot fail to load and the plugin count keeps meaning "installed
    plugins" — but the directory is still a live extension point, and this is
    the test that says so.
    """

    import clv.plugins as plugins_pkg

    plugin_dir = Path(plugins_pkg.__file__).resolve().parent / "exporters"
    plugin_dir.mkdir(exist_ok=True)
    module_path = plugin_dir / "tmp_test_exporter.py"
    module_path.write_text(
        "from clv.plugins import Exporter, ExportResult\n"
        "class Sink(Exporter):\n"
        "    name = 'tmp-sink'\n"
        "    def export(self, entries, context):\n"
        "        return ExportResult(ok=True, detail='ok')\n"
        "def register():\n"
        "    return Sink()\n",
        encoding="utf-8",
    )
    try:
        registry = load_plugins(clv_version="2.1.0", include_entry_points=False)
        assert "tmp-sink" in [p.name for p in registry.exporters]
    finally:
        module_path.unlink()


def test_broken_drop_in_module_is_reported_not_fatal() -> None:
    import clv.plugins as plugins_pkg

    plugin_dir = Path(plugins_pkg.__file__).resolve().parent / "filters"
    plugin_dir.mkdir(exist_ok=True)
    module_path = plugin_dir / "tmp_broken_plugin.py"
    module_path.write_text("raise RuntimeError('bad plugin')\n", encoding="utf-8")
    try:
        registry = load_plugins(clv_version="2.1.0", include_entry_points=False)
        assert any("import failed" in error.message for error in registry.errors)
    finally:
        module_path.unlink()


def test_load_plugins_never_raises_on_a_clean_tree() -> None:
    registry = load_plugins(clv_version="2.1.0")
    assert isinstance(registry, PluginRegistry)
