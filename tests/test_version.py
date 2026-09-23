"""The package version and the packaging metadata must agree.

clv.__version__ is a literal because a PyInstaller bundle carries no
distribution metadata for importlib.metadata to read. That makes it a second
source of truth, so it needs a guard: plugin `requires_clv` constraints are
evaluated against clv.__version__, and the release workflow checks the git tag
against pyproject.toml. If the two drift, a release reports a version it is not.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import clv

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["poetry"]["version"]


@pytest.mark.skipif(not PYPROJECT.exists(), reason="running from an installed package")
def test_package_version_matches_pyproject() -> None:
    assert clv.__version__ == _pyproject_version(), (
        f"clv/__init__.py says {clv.__version__} but pyproject.toml says "
        f"{_pyproject_version()}. Update both before tagging a release."
    )


def test_version_is_a_usable_release_string() -> None:
    parts = clv.__version__.split(".")
    assert len(parts) >= 2, "version needs at least major.minor"
    assert all(part.isdigit() for part in parts[:2]), "major and minor must be numeric"


def test_plugin_constraints_evaluate_against_the_real_version() -> None:
    """The guard exists because this is what consumes __version__."""
    from clv.plugins import satisfies

    major = clv.__version__.split(".")[0]
    assert satisfies(clv.__version__, f">={major}.0")
    assert not satisfies(clv.__version__, f">={int(major) + 1}.0")


def test_the_plugin_api_version_is_not_the_application_version() -> None:
    """Two numbers, moving independently, asserted apart on purpose.

    CLV is 3.0.0 and the plugin API is 1.0, and that gap **is** the Phase 2
    separation doing its job on its first outing: the doctrine reversals and the
    new argv layer are major-version news for the application, while every
    addition to the published surface since Phase 2 was additive and nothing
    published was removed.

    Asserted here rather than only in `tests/test_api_surface.py` so that a
    future edit bumping the two together has to come through this file and
    justify itself. A plugin declaring `requires_api = ">=1.0,<2.0"` is making a
    promise about the surface, not about the release, and silently moving the
    API version to match the app would break every such plugin for no reason.
    """

    from clv.plugins import PLUGIN_API_VERSION

    assert PLUGIN_API_VERSION == "1.0"
    assert PLUGIN_API_VERSION != clv.__version__


def test_a_plugin_pinned_to_the_published_api_still_loads() -> None:
    """The constraint every example declares, evaluated against this build.

    `requires_api = ">=1.0,<2.0"` is what the author checklist tells people to
    write. A release that made it unsatisfiable would disable every correctly
    written plugin in existence, and would do it quietly -- each one reported in
    the `P` dialog as `incompatible`, which reads like the plugin's fault.
    """

    from clv.plugins import PLUGIN_API_VERSION, satisfies

    assert satisfies(PLUGIN_API_VERSION, ">=1.0,<2.0")
