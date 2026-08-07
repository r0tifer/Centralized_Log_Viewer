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
