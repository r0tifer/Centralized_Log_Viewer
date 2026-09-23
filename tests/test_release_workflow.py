"""The release workflow, pinned where review cannot see it.

Two things in `.github/workflows/release.yml` broke the first attempt at the
3.0.0 release, and neither was visible to anybody reading the diff. They are not
the kind of mistake that gets caught by being more careful; they are the kind
that gets caught by a test, which is why these exist.

Both failed *late* -- after the tag was pushed, on a workflow that runs only on
a tag, so nothing before that moment could have reported either one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists(), reason="running from an installed package"
)


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _docker_script() -> str:
    """The script passed to `bash -c` inside the AlmaLinux container.

    Sliced textually rather than through a YAML parser on purpose: what is being
    checked is the *shell quoting of the literal text*, and a parser that
    resolved the quoting would resolve away the thing under test.
    """

    text = _workflow()
    opener = "bash -euo pipefail -c '"
    start = text.index(opener) + len(opener)
    end = text.index("\n            '\n", start)
    return text[start:end]


def test_the_containerised_build_script_contains_no_apostrophe() -> None:
    """An apostrophe closes the quote and moves the build onto the runner.

    The script that builds the PyInstaller bundle is one single-quoted argument
    to `bash -c`. A `'` anywhere inside it -- **including inside a comment** --
    ends the quoted string early, and everything after it is executed by the
    shell on the runner instead of the shell in the AlmaLinux container.

    That is what happened to the first v3.0.0 tag. A comment saying
    "PyInstaller's analysis never saw them" split the argument, the build ran
    against the runner's hostedtoolcache interpreter, and the job died with
    `No module named PyInstaller` -- an error naming a Python that the step does
    not install and never intended to use. Both architectures failed
    identically; `publish` was skipped; the release produced nothing.

    Deliberately a test rather than a review note. The failure mode is a
    punctuation mark inside prose, in a file nobody runs locally, on a job that
    only executes once a tag exists.
    """

    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(_docker_script().splitlines(), start=1)
        if "'" in line
    ]
    assert not offenders, (
        "an apostrophe inside the single-quoted `bash -c` script in "
        "release.yml will end the quote and run the rest on the runner "
        "instead of in the container. Rewrite the wording to avoid it:\n  "
        + "\n  ".join(offenders)
    )


def test_the_build_script_still_builds_in_the_container() -> None:
    """The guard above is only meaningful while the slice is the real script.

    If the workflow stops using a single-quoted `bash -c`, the slice would
    silently select something else and the apostrophe test would pass over text
    that is not the build script.
    """

    script = _docker_script()
    for expected in ("dnf -y -q install python3.11", "-m PyInstaller -y"):
        assert expected in script, (
            f"release.yml no longer runs {expected!r} inside the container; "
            "the apostrophe guard may be inspecting the wrong text"
        )


def _normalise(name: str) -> str:
    """PEP 503 name normalisation."""

    return re.sub(r"[-_.]+", "-", name).lower()


def test_the_distribution_name_matches_the_package_name() -> None:
    """One product, one name, wherever it is installed from.

    The distribution was called `clv` while the .deb, the .rpm and the tarball
    were all called `centralized-log-viewer`. That is survivable right up until
    something authenticates against the name: PyPI's trusted publisher is bound
    to a project, so the upload came back
    `403 ... OIDC scoped token is not valid for project 'clv'` -- after the tag
    was pushed and the test matrix had gone green.

    `clv` is the import package, the console script and the binary. It is not
    the product, and this is the assertion that keeps the two from drifting
    apart again.
    """

    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "poetry"
    ]["name"]
    app_name = re.search(r"^\s*APP_NAME:\s*(\S+)\s*$", _workflow(), re.MULTILINE)
    assert app_name is not None, "release.yml declares no APP_NAME"

    assert _normalise(declared) == _normalise(app_name.group(1)), (
        f"pyproject declares the distribution {declared!r} but release.yml "
        f"packages it as {app_name.group(1)!r}. PyPI, the .deb and the .rpm "
        "would then be three different names for one product, and a trusted "
        "publisher bound to one of them rejects the others."
    )


def test_the_import_package_is_still_clv() -> None:
    """Renaming the distribution must not rename what anybody imports.

    `import clv`, `clv.api` and the `clv` command are the interface; the
    distribution name is packaging. A rename that moved the import package
    would break every installed plugin, since a plugin imports `clv.api` by
    name.
    """

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["poetry"]
    assert data["packages"] == [{"include": "clv"}]
    assert data["scripts"]["clv"] == "clv.cli:main"
