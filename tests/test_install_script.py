"""Contract tests for install.sh.

The script had no coverage and shipped broken twice: once expecting an asset
name and archive shape the release workflow never produced, and once with an
EXIT trap referencing a function-local variable, which under `set -u` aborted
the trap, skipped cleanup and made a successful install exit non-zero.

These tests exercise only the paths that need no network: argument handling,
architecture detection, the cleanup contract, and a --from-local install.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available"),
    pytest.mark.skipif(not INSTALL_SH.exists(), reason="running from an installed package"),
]


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, **kwargs.pop("env", {})}
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        **kwargs,
    )


def _fake_build(tmp_path: Path) -> Path:
    """A stand-in for PyInstaller's dist/clv onedir tree."""
    tree = tmp_path / "dist" / "clv"
    (tree / "_internal").mkdir(parents=True)
    binary = tree / "clv"
    binary.write_text("#!/usr/bin/env bash\necho fake-clv \"$@\"\n", encoding="utf-8")
    binary.chmod(0o755)
    (tree / "_internal" / "settings.conf").write_text(
        "[log_viewer]\nlog_dirs = /var/log\n", encoding="utf-8"
    )
    return tree


def test_script_is_syntactically_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_help_exits_cleanly() -> None:
    result = _run("--help")

    assert result.returncode == 0
    assert "--from-local" in result.stdout
    assert "--gpg-fpr" in result.stdout


def test_unknown_argument_is_rejected() -> None:
    result = _run("--nonsense")

    assert result.returncode != 0
    assert "Unknown arg" in result.stderr


def test_from_local_install_lays_out_the_tree_and_launcher(tmp_path: Path) -> None:
    tree = _fake_build(tmp_path)
    bindir = tmp_path / "bin"
    libdir = tmp_path / "lib" / "clv"
    bindir.mkdir()

    result = _run(
        "--from-local", str(tree), "--prefix", str(bindir), "--libdir", str(libdir)
    )

    assert result.returncode == 0, result.stderr
    # The whole onedir tree is installed, not just the executable.
    assert (libdir / "clv").is_file()
    assert (libdir / "_internal" / "settings.conf").is_file()
    assert os.access(libdir / "clv", os.X_OK)

    launcher = bindir / "clv"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert str(libdir / "clv") in launcher.read_text(encoding="utf-8")

    # And the launcher actually runs the installed binary.
    run = subprocess.run([str(launcher), "--flag"], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0
    assert "fake-clv --flag" in run.stdout


def test_install_leaves_no_temporary_directory_behind(tmp_path: Path) -> None:
    """The EXIT trap must not reference an out-of-scope local under `set -u`.

    That regression printed "tmpdir: unbound variable", skipped cleanup and
    returned 1 from an install that had actually succeeded.
    """
    tree = _fake_build(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scratch = tmp_path / "tmpspace"
    scratch.mkdir()

    result = _run(
        "--from-local",
        str(tree),
        "--prefix",
        str(bindir),
        "--libdir",
        str(tmp_path / "lib" / "clv"),
        env={"TMPDIR": str(scratch)},
    )

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert list(scratch.iterdir()) == [], "install.sh left a temp directory behind"


def test_download_path_completes_and_cleans_up(tmp_path: Path) -> None:
    """Covers download_and_install itself, with curl and tag lookup stubbed.

    This is the path that shipped broken: its EXIT trap referenced a
    function-local, so `set -u` aborted the trap, the scratch tree survived and
    a successful install returned 1. The --from-local tests never reach this
    function, so without stubbing the network nothing exercised it.
    """
    tree = _fake_build(tmp_path)
    asset = "centralized-log-viewer-linux-x86_64.tar.gz"

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    subprocess.run(
        ["tar", "-C", str(tree.parent), "-czf", str(fixtures / asset), "clv"],
        check=True,
        timeout=60,
    )
    sums = subprocess.run(
        ["sha256sum", asset], cwd=fixtures, capture_output=True, text=True, timeout=60
    )
    (fixtures / "SHA256SUMS").write_text(sums.stdout, encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    libdir = tmp_path / "lib" / "clv"
    scratch = tmp_path / "tmpspace"
    scratch.mkdir()

    harness = tmp_path / "download.sh"
    harness.write_text(
        f"""set -euo pipefail
source {_sourceable(tmp_path)}
PREFIX={bindir}
LIBDIR={libdir}
detect_arch() {{ echo x86_64; }}
resolve_tag() {{ printf 'v0.0.0-test'; }}
curl() {{
  local out="" url=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o) out="$2"; shift 2;;
      --retry) shift 2;;
      -*) shift;;
      *) url="$1"; shift;;
    esac
  done
  case "$url" in
    *SHA256SUMS.asc) return 1;;                      # no signature published
    *SHA256SUMS) cp {fixtures}/SHA256SUMS "$out";;
    *.tar.gz) cp {fixtures}/{asset} "$out";;
    *) return 1;;
  esac
}}
download_and_install
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "TMPDIR": str(scratch)},
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "unbound variable" not in result.stderr
    assert "Checksum OK" in result.stdout
    assert (libdir / "clv").is_file()
    assert (libdir / "_internal" / "settings.conf").is_file()
    assert (bindir / "clv").is_file()
    assert list(scratch.iterdir()) == [], "download path left a temp directory behind"


def test_download_path_rejects_a_bad_checksum(tmp_path: Path) -> None:
    tree = _fake_build(tmp_path)
    asset = "centralized-log-viewer-linux-x86_64.tar.gz"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    subprocess.run(
        ["tar", "-C", str(tree.parent), "-czf", str(fixtures / asset), "clv"],
        check=True,
        timeout=60,
    )
    (fixtures / "SHA256SUMS").write_text(f"{'0' * 64}  {asset}\n", encoding="utf-8")

    harness = tmp_path / "badsum.sh"
    harness.write_text(
        f"""set -euo pipefail
source {_sourceable(tmp_path)}
PREFIX={tmp_path}/bin
LIBDIR={tmp_path}/lib/clv
mkdir -p "$PREFIX"
detect_arch() {{ echo x86_64; }}
resolve_tag() {{ printf 'v0.0.0-test'; }}
curl() {{
  local out="" url=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o) out="$2"; shift 2;;
      --retry) shift 2;;
      -*) shift;;
      *) url="$1"; shift;;
    esac
  done
  case "$url" in
    *SHA256SUMS.asc) return 1;;
    *SHA256SUMS) cp {fixtures}/SHA256SUMS "$out";;
    *.tar.gz) cp {fixtures}/{asset} "$out";;
    *) return 1;;
  esac
}}
download_and_install
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, timeout=120
    )

    assert result.returncode != 0
    assert "Checksum mismatch" in result.stderr
    assert not (tmp_path / "lib" / "clv" / "clv").exists(), "installed despite a bad checksum"


def test_cleanup_is_safe_before_a_scratch_directory_exists() -> None:
    """Exiting early (bad args, unsupported arch) must not trip the trap."""
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", f"source {INSTALL_SH} --help"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    assert "unbound variable" not in result.stderr


def test_missing_local_source_fails_with_a_clear_message(tmp_path: Path) -> None:
    result = _run("--from-local", str(tmp_path / "nope"))

    assert result.returncode != 0
    assert "not found" in result.stderr.lower()


def test_local_source_without_the_executable_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = _run("--from-local", str(empty))

    assert result.returncode != 0
    assert "clv" in result.stderr


def _sourceable(tmp_path: Path) -> Path:
    """install.sh with its `main "$@"` call removed, so functions can be sourced."""
    body = INSTALL_SH.read_text(encoding="utf-8").replace('\nmain "$@"\n', "\n")
    library = tmp_path / "install_lib.sh"
    library.write_text(body, encoding="utf-8")
    return library


def test_unsupported_architecture_points_at_a_source_install(tmp_path: Path) -> None:
    """Rather than 404ing on an asset that was never built for it."""
    harness = tmp_path / "arch.sh"
    harness.write_text(
        f"set -euo pipefail\n"
        f"source {_sourceable(tmp_path)}\n"
        f"uname() {{ echo ppc64le; }}\n"
        f"detect_arch\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, timeout=60
    )

    assert result.returncode != 0
    assert "Unsupported architecture" in result.stderr
    assert "pip install" in result.stderr


@pytest.mark.parametrize(
    "machine, expected",
    [("x86_64", "x86_64"), ("amd64", "x86_64"), ("aarch64", "aarch64"), ("arm64", "aarch64")],
)
def test_architecture_aliases_map_to_release_asset_names(
    tmp_path: Path, machine: str, expected: str
) -> None:
    harness = tmp_path / f"arch_{machine}.sh"
    harness.write_text(
        f"set -euo pipefail\n"
        f"source {_sourceable(tmp_path)}\n"
        f"uname() {{ echo {machine}; }}\n"
        f"detect_arch\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_archive_validation_rejects_unsafe_and_unexpected_layouts(tmp_path: Path) -> None:
    """Guards against a tarball writing outside the extraction directory."""
    import tarfile

    good = tmp_path / "good.tar.gz"
    tree = tmp_path / "stage" / "clv"
    tree.mkdir(parents=True)
    (tree / "clv").write_text("x", encoding="utf-8")
    with tarfile.open(good, "w:gz") as archive:
        archive.add(tree, arcname="clv")

    bad = tmp_path / "bad.tar.gz"
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "payload").write_text("x", encoding="utf-8")
    with tarfile.open(bad, "w:gz") as archive:
        archive.add(other, arcname="elsewhere")

    def validate(path: Path) -> subprocess.CompletedProcess:
        harness = tmp_path / f"validate_{path.stem}.sh"
        harness.write_text(
            f"set -euo pipefail\n"
            f"source {_sourceable(tmp_path)}\n"
            f"validate_archive {path}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            ["bash", str(harness)], capture_output=True, text=True, timeout=60
        )

    assert validate(good).returncode == 0
    rejected = validate(bad)
    assert rejected.returncode != 0
    assert "Unexpected entry" in rejected.stderr
