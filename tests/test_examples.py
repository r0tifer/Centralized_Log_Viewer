"""The worked examples, held to the same bar as the documentation that cites them.

``clv/examples/`` is nine plugin modules that CLV writes into every operator's
plugin directory on first run. They are the first files a new author copies, and
:file:`clv/services/config.py` hands them out whether or not they still work —
:func:`_seed_plugin_examples` catches every exception, because seeding an example
is the least important thing that happens at startup.

That is the right call for startup and it is why these tests exist. An example
that stopped loading would be distributed to every new installation, silently,
and the first person to find out would be someone who copied it up a level and
got an error with a plugin they did not write.

Three properties are pinned here, each for its own reason.

**Every example loads through the real loader**, from a real plugin directory,
with ``CLV_PLUGIN_PATH`` set the way an operator's own directory is searched —
not by importing the module, which would prove only that it parses.

**Every example installs**, through ``clv plugin install``, against a manifest
built from the file's own digest. The manifest is generated here rather than
committed beside the example: a manifest pins a SHA-256 per file, so a committed
one would be stale the first time anybody improved a docstring, and a stale
digest in the repository is worse than none — ``verify`` would report the
operator's untouched file as tampered with.

**Every code block in the documentation comes from an example.** The plugin
chapter and the author's quick start both print Python at the reader, and a
snippet nobody executes is a snippet that rots. Extracting them and checking
them against the files is what makes "worked example" mean worked.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "clv" / "examples"
README = REPO_ROOT / "README.md"
PLUGIN_README = REPO_ROOT / "clv" / "plugins" / "README.md"

from clv.plugins import PLUGIN_PATH_ENV, Plugin, load_plugins  # noqa: E402
from clv.services.config import SEEDED_EXAMPLES, ensure_user_plugin_dir  # noqa: E402

#: Which registry list each example is expected to land in, and how many
#: plugins it should put there. Written out rather than derived, because a test
#: that asked the module what it exports and then asserted the module exports
#: that would pass for an example that had quietly stopped exporting anything.
EXPECTED: dict[str, dict[str, int]] = {
    "nginx_error": {"formats": 1},
    "field_regex": {"operators": 2, "computed": 1},
    "watch_alerts": {"matchers": 1, "sinks": 1},
    "cluster_rules": {"rules": 2, "contributors": 1},
    "timeline_marks": {"annotators": 1, "metrics": 1},
    "commands": {"commands": 2},
    "redact_secrets": {"filters": 1},
    "html_report": {"exporters": 1},
    "container_logs": {"sources": 1},
}

#: Every interface `clv.api` publishes, and the example that works it through.
#: The point of the mapping is the *completeness* assertion below it: Phase 16's
#: bar is one worked example per interface, and a seam added later with no
#: example is the thing this catches.
INTERFACE_EXAMPLE: dict[str, str] = {
    "LogSourceProvider": "container_logs",
    "LogFormat": "nginx_error",
    "QueryOperator": "field_regex",
    "ComputedField": "field_regex",
    "FilterStage": "redact_secrets",
    "ClusterRule": "cluster_rules",
    "ShapeContributor": "cluster_rules",
    "TimelineAnnotation": "timeline_marks",
    "TimelineMetric": "timeline_marks",
    "WatchMatcher": "watch_alerts",
    "WatchSink": "watch_alerts",
    "Exporter": "html_report",
    "Command": "commands",
}

MODULES = sorted(name[: -len(".py")] for name in SEEDED_EXAMPLES)


@pytest.fixture(autouse=True)
def _forget_user_plugins() -> Iterator[None]:
    """Drop the synthetic user package between tests.

    Without it a module loaded from one test's plugin directory is handed
    straight to the next test that asks for the same name — the same fixture
    ``tests/test_plugins.py`` carries, and for the same reason.
    """

    from clv.plugins import USER_PLUGIN_PACKAGE

    yield
    doomed = [
        name
        for name in sys.modules
        if name == USER_PLUGIN_PACKAGE
        or name.startswith(f"{USER_PLUGIN_PACKAGE}.")
    ]
    for name in doomed:
        del sys.modules[name]


def _installed(module: str, monkeypatch, settings=None):
    """Copy the seeded example up a level and load it, as an operator would."""

    root = ensure_user_plugin_dir()
    assert root is not None
    source = root / "examples" / f"{module}.py"
    (root / f"{module}.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv(PLUGIN_PATH_ENV, str(root))
    return load_plugins(
        enabled=[module],
        include_local=False,
        include_entry_points=False,
        settings=settings,
    )


# --- they load ---------------------------------------------------------------


@pytest.mark.parametrize("module", MODULES)
def test_every_example_loads_the_way_an_operator_installs_it(
    module: str, monkeypatch
) -> None:
    registry = _installed(module, monkeypatch)
    assert [str(error) for error in registry.errors] == []


@pytest.mark.parametrize("module", MODULES)
def test_every_example_registers_what_it_says_it_does(
    module: str, monkeypatch
) -> None:
    """Loading without error is not the same as supplying anything.

    A module whose ``register()`` came to return ``[]`` would pass the test
    above — CLV treats that as a plugin declining to register, which is a legal
    thing for one to do — and would supply nothing at all.
    """

    registry = _installed(module, monkeypatch)
    for attribute, count in EXPECTED[module].items():
        supplied = getattr(registry, attribute)
        assert len(supplied) == count, (
            f"{module} put {len(supplied)} plugin(s) in registry.{attribute}, "
            f"expected {count}"
        )


@pytest.mark.parametrize("module", MODULES)
def test_every_example_declares_the_api_it_was_written_against(module: str) -> None:
    """``requires_api``, on every class, because the checklist says to.

    ``clv/plugins/AGENTS.md`` opens its author checklist with it, and an example
    that does not do the first thing the checklist asks for is an example
    teaching the opposite.
    """

    import importlib

    imported = importlib.import_module(f"clv.examples.{module}")
    classes = [
        value
        for value in vars(imported).values()
        if isinstance(value, type)
        and issubclass(value, Plugin)
        and value.__module__ == imported.__name__
    ]
    assert classes, f"{module} defines no plugin class"
    undeclared = [cls.__name__ for cls in classes if not cls.requires_api]
    assert undeclared == [], f"{module}: no requires_api on {undeclared}"


@pytest.mark.parametrize("module", MODULES)
def test_every_example_imports_only_the_published_api(module: str) -> None:
    """``from clv.api import ...`` and nothing else out of ``clv``.

    The published surface is the only part of CLV under a stability promise, and
    an example reaching into ``clv.services`` would be handing every author who
    copied it a plugin that breaks on a refactor they will never hear about.
    """

    source = (EXAMPLES_DIR / f"{module}.py").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.match(r"^\s*(from|import)\s+clv\b", line)
        and not re.match(r"^\s*from clv\.api import\b", line)
    ]
    assert offenders == [], f"{module} imports CLV internals: {offenders}"


def test_there_is_an_example_for_every_published_interface() -> None:
    """The bar Phase 16 set, asserted against ``clv.api`` rather than a list.

    A fourteenth interface added later with no worked example fails here, which
    is the only place it would fail — the seam itself would work perfectly.
    """

    import clv.api as api

    published = {
        name
        for name in api.__all__
        if isinstance(getattr(api, name), type)
        and issubclass(getattr(api, name), Plugin)
        and name != "Plugin"
    }
    assert published == set(INTERFACE_EXAMPLE), (
        "published interfaces and worked examples disagree: "
        f"no example for {sorted(published - set(INTERFACE_EXAMPLE))}, "
        f"example for nothing published: {sorted(set(INTERFACE_EXAMPLE) - published)}"
    )
    for interface, module in INTERFACE_EXAMPLE.items():
        assert f"{module}.py" in SEEDED_EXAMPLES, (
            f"{interface}'s example {module} is not seeded, so an operator "
            "never receives it"
        )


# --- they install ------------------------------------------------------------


@pytest.mark.parametrize("module", MODULES)
def test_every_example_installs_against_a_manifest(
    module: str, tmp_path, monkeypatch
) -> None:
    """A generated manifest, verified by the same digest the installer re-hashes.

    The manifest is built here rather than committed: a committed one pins a
    SHA-256 per file and would go stale the first time anyone edited a
    docstring, at which point ``clv plugin verify`` would report an operator's
    untouched file as tampered with.
    """

    from clv.plugins.install import install
    from clv.plugins.manifest import MANIFEST_NAME, digest_file

    staged = tmp_path / f"src-{module}"
    staged.mkdir()
    payload = staged / f"{module}.py"
    payload.write_text(
        (EXAMPLES_DIR / f"{module}.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (staged / MANIFEST_NAME).write_text(
        "\n".join(
            [
                f'name = "{module}"',
                'version = "1.0.0"',
                'requires_api = ">=1.0,<2.0"',
                f'files = [{{ path = "{module}.py", '
                f'sha256 = "{digest_file(payload)}" }}]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    target = tmp_path / "plugins"
    result = install(str(staged), root=target)

    assert (target / f"{module}.py").is_file()
    assert result.name == module
    assert result.version == "1.0.0"
    # The manifest was read and its digest re-hashed rather than taken on
    # trust: `manifested` is False for an install that had nothing to check,
    # and an unmanifested pass here would make the whole test vacuous.
    assert result.manifested
    assert result.warnings == ()


@pytest.mark.parametrize("module", MODULES)
def test_installing_an_example_does_not_enable_it(
    module: str, tmp_path
) -> None:
    """Requirement 2, at the one door most likely to break it.

    An example is the file an operator is most likely to install without
    reading, which makes it the worst possible place for installing to imply
    running.
    """

    from clv.plugins.install import install

    staged = tmp_path / f"bare-{module}"
    staged.mkdir()
    (staged / f"{module}.py").write_text(
        (EXAMPLES_DIR / f"{module}.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    target = tmp_path / "plugins"
    install(str(staged), root=target)

    from clv.services.config import load_config

    assert (target / f"{module}.py").is_file()
    assert module not in load_config().plugins


# --- the documentation quotes them -------------------------------------------


_FENCE = re.compile(r"^```(\w*)\s*$")


def _python_blocks(text: str) -> list[str]:
    """Every ```python fence in *text*, dedented."""

    blocks: list[str] = []
    collecting: list[str] | None = None
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence is not None:
            if collecting is None:
                collecting = [] if fence.group(1) == "python" else None
            else:
                blocks.append(textwrap.dedent("\n".join(collecting)).strip())
                collecting = None
            continue
        if collecting is not None:
            collecting.append(line)
    return [block for block in blocks if block]


def _example_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(EXAMPLES_DIR.glob("*.py"))
    )


@pytest.mark.skipif(not README.exists(), reason="running from an installed package")
def test_every_python_block_in_the_plugin_chapter_is_a_real_example() -> None:
    """Documentation that cannot be executed is documentation that rots.

    Every line of every Python block in the plugin chapter has to appear in an
    example file. Line by line rather than block by block, so a block may quote
    a fragment of a file — which is what most of them do — without the test
    demanding the whole thing.
    """

    text = README.read_text(encoding="utf-8")
    chapter = text.split("\n## Plugins", 1)
    if len(chapter) == 1:  # pragma: no cover - guarded by test_readme_docs
        pytest.fail("README.md has no '## Plugins' chapter")
    body = chapter[1].split("\n## ", 1)[0]
    _assert_blocks_are_examples(_python_blocks(body), "README.md")


@pytest.mark.skipif(
    not PLUGIN_README.exists(), reason="clv/plugins/README.md not written yet"
)
def test_every_python_block_in_the_quick_start_is_a_real_example() -> None:
    _assert_blocks_are_examples(
        _python_blocks(PLUGIN_README.read_text(encoding="utf-8")),
        "clv/plugins/README.md",
    )


def _assert_blocks_are_examples(blocks: list[str], origin: str) -> None:
    corpus = _example_text()
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            # Blank lines, comments and the elisions a quoted fragment needs
            # carry no claim about the source and are not looked for in it.
            if not stripped or stripped.startswith("#") or stripped == "...":
                continue
            assert stripped in corpus, (
                f"{origin} prints a line no example contains:\n    {stripped}\n"
                "Every Python block in the plugin documentation has to be a "
                "file under clv/examples/ or a fragment of one."
            )
