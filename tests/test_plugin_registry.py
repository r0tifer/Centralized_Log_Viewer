"""Manifests, installing, verifying, and refusing a hostile archive.

Three groups, and the middle one is the phase's real security surface.

*Manifests* are parsed from bytes an author wrote, so every malformed shape has
to come back as a sentence rather than a traceback — the person reading it is
usually not the person who wrote the file.

*Archives* arrive from somewhere else entirely. The tests below build tarballs
that no packaging tool would produce — a member called ``../../etc/x``, a
symlink pointing at ``/etc/passwd``, a device node, a member that expands to
more than the cap — and assert two things of each: that it is refused, and that
**nothing was written outside the staging directory**. The second is the one
that matters. A refusal that has already written the file is not a refusal.

*Commands* are asserted to import nothing, across every subcommand, by a plugin
whose import writes a sentinel file. That property is what makes it safe to run
``clv plugin info`` on something you have not decided to trust.

Nothing here touches a network. The URL path is exercised through ``file://``
and through a stub opener; the signature path uses a throwaway key generated
into ``tmp_path`` and is skipped where ``ssh-keygen`` is absent.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest

from clv import cli
from clv.plugins import install as install_mod
from clv.plugins import manifest as manifest_mod
from clv.plugins.install import InstallError, install, unpack
from clv.plugins.manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    ManifestError,
    digest_file,
    parse_manifest,
    read_record,
    read_trusted_signers,
    trust_store_path,
)
from clv.services.config import user_config_path, user_plugin_dir

# --- helpers -----------------------------------------------------------------

_PLUGIN_SOURCE = textwrap.dedent(
    """\
    from clv.api import FilterStage

    class Stage(FilterStage):
        name = "{name}"

        def apply(self, entry, context):
            return entry
    """
)


def _manifest_text(name: str, files: dict[str, str], **extra: str) -> str:
    """A well-formed manifest for *files*, a mapping of path to digest."""

    lines = [f'name = "{name}"', 'version = "1.0.0"']
    for key, value in extra.items():
        lines.append(f'{key} = "{value}"')
    entries = ", ".join(
        f'{{ path = "{path}", sha256 = "{digest}" }}'
        for path, digest in files.items()
    )
    lines.append(f"files = [{entries}]")
    return "\n".join(lines) + "\n"


@pytest.fixture
def source_dir(tmp_path):
    """A directory holding one plugin module and a manifest describing it."""

    def make(name: str = "demo_plugin", *, manifest: bool = True, **extra):
        root = tmp_path / f"src-{name}"
        root.mkdir(parents=True, exist_ok=True)
        module = root / f"{name}.py"
        module.write_text(_PLUGIN_SOURCE.format(name=name), encoding="utf-8")
        if manifest:
            (root / MANIFEST_NAME).write_text(
                _manifest_text(name, {f"{name}.py": digest_file(module)}, **extra),
                encoding="utf-8",
            )
        return root

    return make


@pytest.fixture
def tarball(tmp_path):
    """Pack a directory into a .tar.gz under an optional wrapping directory."""

    def make(root: Path, name: str = "bundle.tar.gz", *, prefix: str = "") -> Path:
        archive = tmp_path / name
        with tarfile.open(archive, "w:gz") as bundle:
            for entry in sorted(root.rglob("*")):
                arcname = str(entry.relative_to(root))
                bundle.add(entry, arcname=f"{prefix}{arcname}" if prefix else arcname)
        return archive

    return make


def _crafted(path: Path, build) -> Path:
    """A tar built by hand, so it can contain what no packer would emit."""

    with tarfile.open(path, "w") as bundle:
        build(bundle)
    return path


def _add_bytes(bundle: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    bundle.addfile(info, io.BytesIO(payload))


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _enable_key(value: str = "") -> None:
    """Put a `plugins` key in `[log_viewer]`, through the editor CLV uses.

    Not an append. Appending lands at end of file, which — once any
    `[plugin:<name>]` section exists — is *inside* that section;
    `settings_file.py`'s module docstring is about exactly that hazard, and
    several tests here add such a section. Going through `SettingsDocument`
    puts the key where it belongs whatever else is in the file.
    """

    from clv.services.settings_file import DEFAULT_SECTION, SettingsDocument

    path = user_config_path()
    document = SettingsDocument.load(path)
    document.set(DEFAULT_SECTION, "plugins", value)
    document.save(path)


# --- manifest parsing --------------------------------------------------------


def test_a_complete_manifest_parses() -> None:
    manifest = parse_manifest(
        textwrap.dedent(
            """\
            name = "nginx_format"
            version = "1.2.0"
            requires_api = ">=1.0,<2.0"
            requires_clv = ">=3.0"
            kinds = ["LogFormat"]
            author = "Alice"
            homepage = "https://example.org"
            description = "nginx error logs"
            files = [{ path = "nginx_format.py", sha256 = "%s" }]
            """
            % ("a" * 64)
        ).encode()
    )
    assert manifest.name == "nginx_format"
    assert manifest.version == "1.2.0"
    assert manifest.requires_api == ">=1.0,<2.0"
    assert manifest.kinds == ("LogFormat",)
    assert manifest.files[0].path == "nginx_format.py"


def test_a_minimal_manifest_parses() -> None:
    manifest = parse_manifest(
        _manifest_text("tiny", {"tiny.py": "b" * 64}).encode()
    )
    assert manifest.name == "tiny"
    assert manifest.requires_api is None
    assert manifest.kinds == ()


def test_an_unknown_key_is_kept_rather_than_refused() -> None:
    """A newer manifest has to install on an older CLV.

    Refusing a key this version does not know would make every future addition
    to the format a breaking change, which is the opposite of what a versioned
    manifest is for.
    """

    manifest = parse_manifest(
        (_manifest_text("fwd", {"fwd.py": "c" * 64}) + 'future = "yes"\n').encode()
    )
    assert manifest.extra["future"] == "yes"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"name = \x00", "not valid TOML"),
        (b'version = "1.0"\nfiles = []\n', "has no `name`"),
        (b'name = "x"\nfiles = []\n', "has no `version`"),
        (b'name = "x"\nversion = "1"\n', "has no `files`"),
        (b'name = "9bad"\nversion = "1"\nfiles = [{path="a",sha256="' + b"d" * 64 + b'"}]\n', "not a usable plugin name"),
        (b'name = "x"\nversion = "1"\nfiles = [{path="a.py",sha256="short"}]\n', "no usable `sha256`"),
    ],
)
def test_a_malformed_manifest_is_a_sentence_not_a_traceback(body, expected) -> None:
    with pytest.raises(ManifestError) as caught:
        parse_manifest(body, origin="clv-plugin.toml")
    assert expected in str(caught.value)
    # Every message names the file, so an error from inside a downloaded
    # tarball says which file is wrong rather than only what is wrong with it.
    assert "clv-plugin.toml" in str(caught.value)


def test_a_manifest_cannot_name_a_path_outside_itself() -> None:
    """`files` is what the install re-hashes, so a traversal there would read."""

    with pytest.raises(ManifestError) as caught:
        parse_manifest(
            _manifest_text("x", {"../../etc/passwd": "e" * 64}).encode()
        )
    assert "climbs out" in str(caught.value)


def test_a_file_listed_twice_is_refused() -> None:
    body = (
        'name = "x"\nversion = "1"\n'
        'files = [{path="a.py",sha256="%s"}, {path="a.py",sha256="%s"}]\n'
        % ("f" * 64, "f" * 64)
    )
    with pytest.raises(ManifestError) as caught:
        parse_manifest(body.encode())
    assert "listed twice" in str(caught.value)


# --- installing --------------------------------------------------------------


def test_install_from_a_directory(source_dir) -> None:
    result = install(str(source_dir("demo_plugin")))
    assert result.destination == user_plugin_dir() / "demo_plugin.py"
    assert result.destination.is_file()
    assert result.record.version == "1.0.0"


def test_install_from_a_tarball(source_dir, tarball) -> None:
    archive = tarball(source_dir("tarred"), prefix="tarred-1.0.0/")
    result = install(str(archive))
    assert result.destination == user_plugin_dir() / "tarred.py"
    assert result.destination.is_file()


def test_install_from_a_file_url(source_dir, tarball) -> None:
    archive = tarball(source_dir("urled"))
    result = install(archive.as_uri())
    assert result.destination.is_file()
    assert result.record.installed_from == archive.as_uri()


def test_installing_does_not_enable(source_dir, capsys) -> None:
    """Requirement 2, at the CLI and in the output.

    The command says so in as many words, because this is the moment an
    operator would most reasonably assume otherwise.
    """

    code, out, _ = _run(capsys, "plugin", "install", str(source_dir("inert")))
    assert code == cli.EXIT_OK
    assert "NOT enabled" in out
    assert "plugins = inert" in out

    from clv.services.config import load_config

    assert "inert" not in load_config().plugins


def test_an_installed_plugin_is_listed(source_dir, capsys) -> None:
    install(str(source_dir("listed")))
    code, out, _ = _run(capsys, "plugin", "list")
    assert code == cli.EXIT_OK
    assert "listed 1.0.0 — not enabled (module)" in out


def test_a_package_directory_installs(tmp_path) -> None:
    root = tmp_path / "pkgsrc"
    package = root / "pkg_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        _PLUGIN_SOURCE.format(name="pkg_plugin"), encoding="utf-8"
    )
    (package / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / MANIFEST_NAME).write_text(
        _manifest_text(
            "pkg_plugin",
            {
                "pkg_plugin/__init__.py": digest_file(package / "__init__.py"),
                "pkg_plugin/extra.py": digest_file(package / "extra.py"),
            },
        ),
        encoding="utf-8",
    )
    result = install(str(root))
    assert result.destination == user_plugin_dir() / "pkg_plugin"
    assert (result.destination / "extra.py").is_file()
    assert result.record.is_package


def test_a_package_inside_a_tarball_installs(tmp_path) -> None:
    """The realistic publishing shape: `tar czf` of a directory.

    Distinct from the directory install above because a real `tar czf` emits
    **directory members**, whose names tar stores with a trailing slash. The
    extractor judges every member name, and a check that read `foo/` as "does
    not name a file" would refuse every package ever published while a flat
    single-module plugin kept working.
    """

    build = tmp_path / "pkgbuild" / "netfmt-2.0.0"
    package = build / "netfmt"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        _PLUGIN_SOURCE.format(name="netfmt"), encoding="utf-8"
    )
    (package / "rules.py").write_text("RULES = ()\n", encoding="utf-8")
    (build / MANIFEST_NAME).write_text(
        _manifest_text(
            "netfmt",
            {
                "netfmt/__init__.py": digest_file(package / "__init__.py"),
                "netfmt/rules.py": digest_file(package / "rules.py"),
            },
        ),
        encoding="utf-8",
    )

    archive = tmp_path / "netfmt-2.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(build, arcname="netfmt-2.0.0")

    result = install(str(archive))
    assert result.destination == user_plugin_dir() / "netfmt"
    assert (result.destination / "rules.py").is_file()
    assert result.record.is_package


def test_a_local_py_installs_without_a_manifest(tmp_path) -> None:
    """The documented `cp`, under another name, so it is not refused."""

    module = tmp_path / "handmade.py"
    module.write_text(_PLUGIN_SOURCE.format(name="handmade"), encoding="utf-8")
    result = install(str(module))
    assert result.destination == user_plugin_dir() / "handmade.py"
    assert not result.manifested


def test_an_archive_without_a_manifest_is_refused(tmp_path, tarball) -> None:
    """A tarball arrived from elsewhere and has to declare itself."""

    root = tmp_path / "bare"
    root.mkdir()
    (root / "bare.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(InstallError) as caught:
        install(str(tarball(root, "bare.tar.gz")))
    assert MANIFEST_NAME in str(caught.value)


def test_a_supplied_digest_is_checked_before_anything_is_unpacked(
    source_dir, tarball
) -> None:
    """The one number that did not come from inside the archive.

    Every other check here reads something the archive says about itself, and
    an archive replaced in transit says whatever its replacer wanted — it can
    rewrite the files and the manifest's checksums together. A digest the
    operator got from the download page cannot be rewritten that way.
    """

    archive = tarball(source_dir("pinned"), "pinned.tar.gz")
    with pytest.raises(InstallError) as caught:
        install(str(archive), expected_sha256="0" * 64)
    assert "does not match the digest you gave" in str(caught.value)
    assert not (user_plugin_dir() / "pinned.py").exists()

    # And the correct digest installs.
    result = install(str(archive), expected_sha256=digest_file(archive))
    assert result.destination.is_file()


def test_a_malformed_digest_is_refused(source_dir, tarball) -> None:
    archive = tarball(source_dir("badhash"), "badhash.tar.gz")
    with pytest.raises(InstallError) as caught:
        install(str(archive), expected_sha256="not-a-digest")
    assert "is not a sha256 digest" in str(caught.value)


def test_a_digest_makes_no_sense_for_a_directory(source_dir) -> None:
    with pytest.raises(InstallError) as caught:
        install(str(source_dir("dirhash")), expected_sha256="a" * 64)
    assert "--sha256 is for an archive or a URL" in str(caught.value)


def test_a_checksum_mismatch_aborts_before_anything_is_copied(source_dir) -> None:
    """The order is the assertion: nothing lands, then the error."""

    root = source_dir("mismatched")
    (root / "mismatched.py").write_text("TAMPERED = True\n", encoding="utf-8")
    with pytest.raises(InstallError) as caught:
        install(str(root))
    assert "does not match its own manifest" in str(caught.value)
    assert not (user_plugin_dir() / "mismatched.py").exists()


def test_installing_twice_is_refused_without_force(source_dir) -> None:
    root = source_dir("twice")
    install(str(root))
    with pytest.raises(InstallError) as caught:
        install(str(root))
    assert "--force" in str(caught.value)


def test_force_replaces_and_says_so(source_dir, capsys) -> None:
    root = source_dir("replaceable")
    install(str(root))
    code, out, _ = _run(capsys, "plugin", "install", "--force", str(root))
    assert code == cli.EXIT_OK
    assert out.startswith("replaced replaceable")


def test_an_unsatisfiable_requirement_warns_at_install(source_dir, capsys) -> None:
    """At install, not only at load: the operator is present and holding it."""

    root = source_dir("futuristic", requires_clv=">=99.0")
    code, out, _ = _run(capsys, "plugin", "install", str(root))
    assert code == cli.EXIT_OK
    assert "warning:" in out
    assert ">=99.0" in out
    assert (user_plugin_dir() / "futuristic.py").is_file()


def test_a_manifest_that_scatters_files_is_refused(tmp_path) -> None:
    root = tmp_path / "scattered"
    root.mkdir()
    for name in ("one.py", "two.py"):
        (root / name).write_text("X = 1\n", encoding="utf-8")
    (root / MANIFEST_NAME).write_text(
        _manifest_text(
            "scattered",
            {
                "one.py": digest_file(root / "one.py"),
                "two.py": digest_file(root / "two.py"),
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(InstallError) as caught:
        install(str(root))
    assert "single-module plugin" in str(caught.value)


# --- hostile archives --------------------------------------------------------


@pytest.fixture
def outside(tmp_path):
    """A canary directory beside the staging area, asserted to stay empty."""

    target = tmp_path / "outside"
    target.mkdir()
    return target


def _assert_refused(archive: Path, staging: Path, outside: Path, fragment: str):
    before = sorted(p.name for p in outside.iterdir())
    with pytest.raises(InstallError) as caught:
        unpack(archive, staging)
    assert fragment in str(caught.value)
    # The property that matters: a refusal that already wrote the file is not
    # a refusal.
    assert sorted(p.name for p in outside.iterdir()) == before
    assert not (outside / "pwned").exists()
    return caught.value


def test_a_traversing_member_is_refused(tmp_path, outside) -> None:
    archive = _crafted(
        tmp_path / "traverse.tar",
        lambda b: _add_bytes(b, "../../outside/pwned", b"owned"),
    )
    _assert_refused(archive, tmp_path / "stage1", outside, "climbs out")


def test_an_absolute_member_is_refused(tmp_path, outside) -> None:
    archive = _crafted(
        tmp_path / "absolute.tar",
        lambda b: _add_bytes(b, f"{outside}/pwned", b"owned"),
    )
    _assert_refused(archive, tmp_path / "stage2", outside, "is absolute")


def test_a_symlink_is_refused(tmp_path, outside) -> None:
    def build(bundle):
        info = tarfile.TarInfo("link.py")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)

    archive = _crafted(tmp_path / "symlink.tar", build)
    _assert_refused(archive, tmp_path / "stage3", outside, "a symbolic link")


def test_a_hard_link_is_refused(tmp_path, outside) -> None:
    def build(bundle):
        _add_bytes(bundle, "real.py", b"X = 1\n")
        info = tarfile.TarInfo("hard.py")
        info.type = tarfile.LNKTYPE
        info.linkname = "real.py"
        bundle.addfile(info)

    archive = _crafted(tmp_path / "hardlink.tar", build)
    _assert_refused(archive, tmp_path / "stage4", outside, "a hard link")


def test_a_device_node_is_refused(tmp_path, outside) -> None:
    def build(bundle):
        info = tarfile.TarInfo("dev")
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
        bundle.addfile(info)

    archive = _crafted(tmp_path / "device.tar", build)
    _assert_refused(archive, tmp_path / "stage5", outside, "a character device")


def test_a_decompression_bomb_is_stopped_at_the_cap(tmp_path, outside, monkeypatch) -> None:
    """Stopped *while* expanding, not after.

    The cap is lowered rather than a real bomb built: the assertion is that the
    running total stops the copy, and a genuine 64 MB expansion would only make
    the test slow enough that someone eventually deleted it.
    """

    monkeypatch.setattr(install_mod, "MAX_EXTRACTED_BYTES", 4096)
    archive = _crafted(
        tmp_path / "bomb.tar",
        lambda b: _add_bytes(b, "big.py", b"\0" * 65536),
    )
    _assert_refused(archive, tmp_path / "stage6", outside, "expands to more than")


def test_too_many_members_is_stopped(tmp_path, outside, monkeypatch) -> None:
    monkeypatch.setattr(install_mod, "MAX_MEMBERS", 3)

    def build(bundle):
        for index in range(10):
            _add_bytes(bundle, f"m{index}.py", b"X = 1\n")

    archive = _crafted(tmp_path / "many.tar", build)
    _assert_refused(archive, tmp_path / "stage7", outside, "more than 3 entries")


def test_a_zip_is_refused_by_name(tmp_path) -> None:
    """By name, with what to repackage as — not as 'not a tarball'."""

    import zipfile

    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("x.py", "X = 1\n")
    with pytest.raises(InstallError) as caught:
        unpack(archive, tmp_path / "stage8")
    assert "is a zip" in str(caught.value)
    assert "tar czf" in str(caught.value)


def test_a_hostile_archive_writes_nothing_into_the_plugin_directory(
    tmp_path, outside
) -> None:
    """The end-to-end version: through `install`, not through `unpack`."""

    plugins = user_plugin_dir()
    plugins.mkdir(parents=True, exist_ok=True)
    before = sorted(p.name for p in plugins.iterdir())
    archive = _crafted(
        tmp_path / "evil.tar",
        lambda b: _add_bytes(b, "../../outside/pwned", b"owned"),
    )
    with pytest.raises(InstallError):
        install(str(archive))
    assert sorted(p.name for p in plugins.iterdir()) == before
    assert not (outside / "pwned").exists()


def test_a_local_directory_with_a_symlink_is_refused(tmp_path) -> None:
    root = tmp_path / "linky"
    root.mkdir()
    (root / "linky.py").write_text("X = 1\n", encoding="utf-8")
    (root / "escape").symlink_to("/etc/passwd")
    with pytest.raises(InstallError) as caught:
        install(str(root))
    assert "symbolic link" in str(caught.value)


def test_a_wrapping_directory_is_stripped(source_dir, tarball) -> None:
    """`tar czf` produces `name-version/`; a manifest at the top is found anyway."""

    archive = tarball(source_dir("wrapped"), prefix="wrapped-1.0.0/")
    result = install(str(archive))
    assert result.destination == user_plugin_dir() / "wrapped.py"


# --- URLs --------------------------------------------------------------------


def test_http_is_refused_by_name(tmp_path) -> None:
    with pytest.raises(InstallError) as caught:
        install_mod.fetch("http://example.org/x.tar.gz", tmp_path)
    assert "plain http" in str(caught.value)


def test_an_unsupported_scheme_is_refused(tmp_path) -> None:
    with pytest.raises(InstallError) as caught:
        install_mod.fetch("ftp://example.org/x.tar.gz", tmp_path)
    assert "ftp" in str(caught.value)


def test_a_cross_host_redirect_is_refused() -> None:
    """Never opens a socket: the handler is the unit under test."""

    handler = install_mod._SameHostRedirect()

    class _Request:
        full_url = "https://good.example/x.tar.gz"

    with pytest.raises(InstallError) as caught:
        handler.redirect_request(
            _Request(), None, 302, "Found", {}, "https://evil.example/x.tar.gz"
        )
    assert "a different host" in str(caught.value)


def test_a_downgrade_redirect_is_refused() -> None:
    handler = install_mod._SameHostRedirect()

    class _Request:
        full_url = "https://good.example/x.tar.gz"

    with pytest.raises(InstallError) as caught:
        handler.redirect_request(
            _Request(), None, 302, "Found", {}, "http://good.example/x.tar.gz"
        )
    assert "https only" in str(caught.value)


def test_an_oversized_download_is_stopped(tmp_path, monkeypatch) -> None:
    """Bounded while streaming, and the partial file is not left behind."""

    monkeypatch.setattr(install_mod, "MAX_DOWNLOAD_BYTES", 1024)

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, url, timeout=None):
            return _Response(b"\0" * 65536)

    with pytest.raises(InstallError) as caught:
        install_mod.fetch(
            "https://example.org/big.tar.gz", tmp_path, opener=_Opener()
        )
    assert "larger than" in str(caught.value)
    assert not (tmp_path / "big.tar.gz").exists()


# --- verifying ---------------------------------------------------------------


def test_verify_passes_on_a_clean_install(source_dir, capsys) -> None:
    install(str(source_dir("clean")))
    code, out, _ = _run(capsys, "plugin", "verify")
    assert code == cli.EXIT_OK
    assert "clean: ok — 1 file matches" in out


def test_verify_names_the_file_that_changed(source_dir, capsys) -> None:
    install(str(source_dir("mutable")))
    target = user_plugin_dir() / "mutable.py"
    target.write_text(target.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    code, out, _ = _run(capsys, "plugin", "verify", "mutable")
    assert code == cli.EXIT_FAILURE
    assert "mutable.py: changed since it was packaged" in out


def test_verify_reports_a_missing_file(source_dir, capsys) -> None:
    install(str(source_dir("vanishing")))
    (user_plugin_dir() / "vanishing.py").unlink()
    code, out, _ = _run(capsys, "plugin", "verify", "vanishing")
    assert code == cli.EXIT_FAILURE
    assert "vanishing.py: missing" in out


def test_verify_says_when_there_is_nothing_to_check(capsys) -> None:
    code, out, _ = _run(capsys, "plugin", "verify")
    assert code == cli.EXIT_OK
    assert "nothing to verify" in out


def test_doctor_reports_a_tamper(source_dir, capsys) -> None:
    """The check an operator is actually told to run.

    `clv plugin verify` is the explicit command; `clv doctor` is the one a bug
    report asks for, so a tamper only the first could find would be a check
    that effectively does not exist.
    """

    install(str(source_dir("watched")))
    _enable_key("watched")
    target = user_plugin_dir() / "watched.py"
    target.write_text("# replaced\n", encoding="utf-8")
    code, out, _ = _run(capsys, "doctor")
    assert code == cli.EXIT_OK
    assert "does not match its manifest" in out
    assert "watched.py" in out


# --- signatures --------------------------------------------------------------

_HAS_KEYGEN = shutil.which("ssh-keygen") is not None
_needs_keygen = pytest.mark.skipif(
    not _HAS_KEYGEN, reason="ssh-keygen is not installed"
)


@pytest.fixture
def signing_key(tmp_path):
    """A throwaway Ed25519 key. Local only — nothing here touches a network."""

    key = tmp_path / "signing_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, payload: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", "clv-plugin", "-f", str(key), str(payload)],
        check=True,
        capture_output=True,
    )
    return payload.with_name(payload.name + ".sig")


def test_an_unsigned_plugin_installs_and_says_so(source_dir, capsys) -> None:
    code, out, _ = _run(capsys, "plugin", "install", str(source_dir("plain")))
    assert code == cli.EXIT_OK
    assert "signature: unsigned" in out


@_needs_keygen
def test_a_plugin_signed_by_an_untrusted_key_is_reported(
    source_dir, signing_key, capsys
) -> None:
    root = source_dir("stranger")
    _sign(signing_key, root / MANIFEST_NAME)
    code, out, _ = _run(capsys, "plugin", "install", str(root))
    assert code == cli.EXIT_OK
    assert "signature: untrusted" in out
    assert (user_plugin_dir() / "stranger.py").is_file()


@_needs_keygen
def test_a_plugin_signed_by_a_trusted_key_verifies(
    source_dir, signing_key, capsys
) -> None:
    public = (signing_key.with_suffix(".pub")).read_text(encoding="utf-8").split()
    _run(capsys, "plugin", "trust", f"alice@example.com {public[0]} {public[1]}")
    root = source_dir("friend")
    _sign(signing_key, root / MANIFEST_NAME)
    code, out, _ = _run(capsys, "plugin", "install", str(root))
    assert code == cli.EXIT_OK
    assert "signature: verified — signed by alice@example.com" in out


@_needs_keygen
def test_trusting_a_key_afterwards_turns_untrusted_into_verified(
    source_dir, signing_key, capsys
) -> None:
    """The reason the manifest is kept rather than paraphrased into the record.

    A signature covers exact bytes. Keeping them is what lets an install that
    reported `untrusted` in March report `verified` in April without being
    reinstalled — which is the entire point of trusting keys rather than files.
    """

    root = source_dir("later")
    _sign(signing_key, root / MANIFEST_NAME)
    install(str(root))
    assert read_record("later").signature == "untrusted"

    public = (signing_key.with_suffix(".pub")).read_text(encoding="utf-8").split()
    _run(capsys, "plugin", "trust", f"bob@example.com {public[0]} {public[1]}")

    code, out, _ = _run(capsys, "plugin", "verify", "later")
    assert code == cli.EXIT_OK
    assert "signature: verified — signed by bob@example.com" in out


@_needs_keygen
def test_force_over_a_signed_plugin_does_not_inherit_its_signature(
    source_dir, signing_key, tmp_path, capsys
) -> None:
    """A reused name must not inherit the previous occupant's provenance.

    ``--force`` installs over an existing plugin under the same name. The
    manifest and signature CLV keeps for verification are filed under that name
    too — so one left behind by the previous install would outlive the plugin it
    described and be read as the new one's, reporting a stranger's signature
    against code they never signed.
    """

    public = (signing_key.with_suffix(".pub")).read_text(encoding="utf-8").split()
    _run(capsys, "plugin", "trust", f"alice@example.com {public[0]} {public[1]}")

    signed = source_dir("shapeshifter")
    _sign(signing_key, signed / MANIFEST_NAME)
    install(str(signed))
    assert read_record("shapeshifter").signature == "verified"

    # Now replace it with a bare .py, which carries no manifest at all.
    plain = tmp_path / "shapeshifter.py"
    plain.write_text(_PLUGIN_SOURCE.format(name="shapeshifter"), encoding="utf-8")
    install(str(plain), force=True)

    assert read_record("shapeshifter").signature == "unsigned"
    code, out, _ = _run(capsys, "plugin", "info", "shapeshifter")
    assert code == cli.EXIT_OK
    assert "alice@example.com" not in out
    assert "signature now: unsigned" in out


@_needs_keygen
def test_a_broken_signature_refuses_the_install(
    source_dir, signing_key, capsys
) -> None:
    """A broken signature is evidence; an absent one is a choice."""

    public = (signing_key.with_suffix(".pub")).read_text(encoding="utf-8").split()
    _run(capsys, "plugin", "trust", f"mallory@example.com {public[0]} {public[1]}")
    root = source_dir("broken")
    signature = _sign(signing_key, root / MANIFEST_NAME)
    # Re-point the manifest at a different file, so the signature no longer
    # covers what it claims to.
    module = root / "broken.py"
    module.write_text(_PLUGIN_SOURCE.format(name="broken") + "# later\n", encoding="utf-8")
    (root / MANIFEST_NAME).write_text(
        _manifest_text("broken", {"broken.py": digest_file(module)}), encoding="utf-8"
    )
    assert signature.is_file()

    with pytest.raises(InstallError) as caught:
        install(str(root))
    assert "does not match" in str(caught.value)
    assert not (user_plugin_dir() / "broken.py").exists()


@_needs_keygen
def test_a_broken_signature_is_bad_even_when_the_signer_is_a_stranger(
    source_dir, signing_key
) -> None:
    """"Signed by a stranger" and "the payload changed" are different facts.

    `find-principals` finds nothing in both cases, so reporting from it alone
    would call a tampered manifest merely unvouched-for — a sentence saying the
    signature is fine and only the signer is unknown, when the bytes do not
    match what was signed. `check-novalidate` is what separates them, and this
    is the case that proves it: nobody is trusted here at all.
    """

    root = source_dir("stranger_tampered")
    _sign(signing_key, root / MANIFEST_NAME)
    module = root / "stranger_tampered.py"
    module.write_text("TAMPERED = True\n", encoding="utf-8")
    (root / MANIFEST_NAME).write_text(
        _manifest_text("stranger_tampered", {"stranger_tampered.py": digest_file(module)}),
        encoding="utf-8",
    )
    assert read_trusted_signers() == (), "this case is about trusting nobody"

    with pytest.raises(InstallError) as caught:
        install(str(root))
    assert "does not match the manifest" in str(caught.value)
    assert "not a question of whose key it is" in str(caught.value)
    assert not (user_plugin_dir() / "stranger_tampered.py").exists()


def test_a_signature_without_ssh_keygen_is_unverifiable_not_unsigned(
    source_dir, monkeypatch, capsys
) -> None:
    """Two different facts, and they must not collapse into one.

    "We could not check" is not "there was nothing to check", and an operator
    acts differently on each. Asserted with ssh-keygen forced absent so this
    holds on a machine that has it.
    """

    monkeypatch.setattr(manifest_mod.shutil, "which", lambda name: None)
    root = source_dir("uncheckable")
    (root / SIGNATURE_NAME).write_text("-----BEGIN SSH SIGNATURE-----\n", encoding="utf-8")
    code, out, _ = _run(capsys, "plugin", "install", str(root))
    assert code == cli.EXIT_OK
    assert "signature: unverifiable" in out
    assert "openssh-client" in out


# --- the trust store ---------------------------------------------------------


def test_trust_refuses_a_line_that_is_not_a_signer(capsys) -> None:
    code, _, err = _run(capsys, "plugin", "trust", "nonsense")
    assert code == cli.EXIT_USAGE
    assert "three fields" in err


def test_trust_refuses_an_unknown_key_type(capsys) -> None:
    code, _, err = _run(capsys, "plugin", "trust", "who ssh-magic AAAA")
    assert code == cli.EXIT_USAGE
    assert "not a key type" in err


def test_trust_lists_and_removes(capsys) -> None:
    line = "alice@example.com ssh-ed25519 " + "A" * 68
    code, _, _ = _run(capsys, "plugin", "trust", line)
    assert code == cli.EXIT_OK
    assert [s.principal for s in read_trusted_signers()] == ["alice@example.com"]

    code, out, _ = _run(capsys, "plugin", "trust", "--list")
    assert code == cli.EXIT_OK
    assert "alice@example.com — ssh-ed25519" in out

    code, out, _ = _run(capsys, "plugin", "trust", "--remove", "alice@example.com")
    assert code == cli.EXIT_OK
    assert read_trusted_signers() == ()


def test_trusting_the_same_key_twice_changes_nothing(capsys) -> None:
    line = "bob@example.com ssh-ed25519 " + "B" * 68
    _run(capsys, "plugin", "trust", line)
    code, out, _ = _run(capsys, "plugin", "trust", line)
    assert code == cli.EXIT_OK
    assert "already trusted" in out
    assert len(read_trusted_signers()) == 1


def test_the_trust_store_is_not_world_readable(capsys) -> None:
    _run(capsys, "plugin", "trust", "carol@example.com ssh-ed25519 " + "C" * 68)
    mode = trust_store_path().stat().st_mode & 0o077
    assert mode == 0, "the trust store is a trust decision anyone could extend"


def test_clv_ships_no_trusted_keys() -> None:
    """Stated as a test, because it is a promise rather than an oversight.

    A bundled key would make CLV the arbiter of which plugins are legitimate,
    which is the hosted-index commitment arriving through a side door.
    """

    assert read_trusted_signers() == ()
    assert not trust_store_path().exists()


# --- removing ----------------------------------------------------------------


def test_remove_deletes_files_and_disables(source_dir, capsys) -> None:
    install(str(source_dir("goner")))
    _enable_key("goner, keeper")

    code, out, _ = _run(capsys, "plugin", "remove", "goner")
    assert code == cli.EXIT_OK
    assert not (user_plugin_dir() / "goner.py").exists()

    from clv.services.config import load_config

    # Dropped from the enable list, because leaving it would report the plugin
    # as named-but-missing on every launch from here on.
    assert load_config().plugins == ("keeper",)
    assert read_record("goner") is None


def test_remove_preserves_the_config_section(source_dir, capsys) -> None:
    install(str(source_dir("configured")))
    path = user_config_path()
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n[plugin:configured]\nsome = value\n",
        encoding="utf-8",
    )
    code, out, _ = _run(capsys, "plugin", "remove", "configured")
    assert code == cli.EXIT_OK
    assert "kept [plugin:configured]" in out
    assert "[plugin:configured]" in path.read_text(encoding="utf-8")


def test_purge_removes_the_config_section(source_dir, capsys) -> None:
    install(str(source_dir("purged")))
    path = user_config_path()
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[plugin:purged]\nsome = value\n",
        encoding="utf-8",
    )
    code, out, _ = _run(capsys, "plugin", "remove", "--purge", "purged")
    assert code == cli.EXIT_OK
    assert "removed the [plugin:purged] section" in out
    assert "[plugin:purged]" not in path.read_text(encoding="utf-8")


def test_removing_something_that_is_not_installed_fails(capsys) -> None:
    code, _, err = _run(capsys, "plugin", "remove", "absent")
    assert code == cli.EXIT_FAILURE
    assert "is not installed" in err


def test_remove_handles_a_duplicated_plugins_key(source_dir, capsys) -> None:
    """configparser honours the last of a duplicated key; this module the first.

    A settings file can legally carry `plugins` twice — the template ships one
    and an operator following the README may append another rather than editing
    it. Editing only one of them would report success and change nothing.
    """

    install(str(source_dir("doubled")))
    path = user_config_path()
    _enable_key("doubled")
    text = path.read_text(encoding="utf-8")
    # A second assignment inside [log_viewer], which is what an append produces
    # before any [plugin:*] section exists — and what an operator following the
    # README does when they add the key rather than editing the one they have.
    assert "plugins = doubled\n" in text
    path.write_text(
        text.replace("plugins = doubled\n", "plugins = doubled\nplugins = doubled\n", 1),
        encoding="utf-8",
    )
    _run(capsys, "plugin", "remove", "doubled")

    from clv.services.config import load_config

    assert load_config().plugins == ()


# --- info --------------------------------------------------------------------


def test_info_reports_a_manifest(source_dir, capsys) -> None:
    install(str(source_dir("described", author="Alice", homepage="https://x.test")))
    code, out, _ = _run(capsys, "plugin", "info", "described")
    assert code == cli.EXIT_OK
    assert "version: 1.0.0" in out
    assert "author: Alice" in out
    assert "homepage: https://x.test" in out
    assert "signature now:" in out


def test_info_names_the_plugins_own_settings_section(source_dir, capsys) -> None:
    """The commonest reason a working plugin does nothing is a missing setting."""

    install(str(source_dir("needy")))
    path = user_config_path()
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[plugin:needy]\ntarget = /tmp/x\n",
        encoding="utf-8",
    )
    code, out, _ = _run(capsys, "plugin", "info", "needy")
    assert code == cli.EXIT_OK
    assert "[plugin:needy]" in out
    assert "target = /tmp/x" in out


def test_info_on_a_hand_copied_plugin_says_there_is_no_record(capsys) -> None:
    root = user_plugin_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "byhand.py").write_text(_PLUGIN_SOURCE.format(name="byhand"), encoding="utf-8")
    code, out, _ = _run(capsys, "plugin", "info", "byhand")
    assert code == cli.EXIT_OK
    assert "copied in by hand" in out


def test_info_on_something_absent_fails(capsys) -> None:
    code, _, err = _run(capsys, "plugin", "info", "nowhere")
    assert code == cli.EXIT_FAILURE
    assert "is not installed" in err


# --- the import-free promise -------------------------------------------------


_SENTINEL_PLUGIN = """\
import pathlib
pathlib.Path({sentinel!r}).write_text("imported")

from clv.api import FilterStage

class Stage(FilterStage):
    name = "sentinel"

    def apply(self, entry, context):
        return entry
"""


@pytest.mark.parametrize(
    "argv",
    [
        ("plugin", "list"),
        ("plugin", "info", "sentinel"),
        ("plugin", "verify"),
        ("plugin", "verify", "sentinel"),
        ("plugin", "trust", "--list"),
        ("plugin", "remove", "sentinel"),
    ],
)
def test_no_plugin_command_imports_plugin_code(tmp_path, capsys, argv) -> None:
    """Requirement 2's teeth, across every subcommand.

    A plugin sitting in the directory has not been consented to, and a command
    that describes it must not be able to run it. The plugin writes a file at
    import time; the assertion is that the file does not exist.
    """

    sentinel = tmp_path / "sentinel-was-imported"
    root = user_plugin_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sentinel.py").write_text(
        _SENTINEL_PLUGIN.format(sentinel=str(sentinel)), encoding="utf-8"
    )
    # Named in the enable list, so nothing is relying on it being unlisted:
    # these commands must not import even a plugin the operator *has* enabled.
    _enable_key("sentinel")

    cli.main(list(argv))
    capsys.readouterr()
    assert not sentinel.exists(), f"{' '.join(argv)} imported the plugin"


def test_install_does_not_import_what_it_installs(tmp_path, capsys) -> None:
    sentinel = tmp_path / "install-imported"
    source = tmp_path / "srcimport"
    source.mkdir()
    module = source / "importer.py"
    module.write_text(_SENTINEL_PLUGIN.format(sentinel=str(sentinel)), encoding="utf-8")
    (source / MANIFEST_NAME).write_text(
        _manifest_text("importer", {"importer.py": digest_file(module)}),
        encoding="utf-8",
    )
    code, _, _ = _run(capsys, "plugin", "install", str(source))
    assert code == cli.EXIT_OK
    assert not sentinel.exists()


# --- the closed table --------------------------------------------------------


def test_the_plugin_subcommand_table_is_closed() -> None:
    """A plugin must never be able to add one.

    The same assertion `tests/test_cli.py` makes about the top-level table, one
    level down: an installed file must not change what a shell command does
    (``PLUGIN_TODO.md`` Requirement 13), so a later change that built these from
    a registry fails here however reasonable it looked.
    """

    import argparse

    parser = cli.build_parser()
    top = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    plugin = top[0].choices["plugin"]
    nested = [
        action
        for action in plugin._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(nested) == 1
    assert tuple(sorted(nested[0].choices)) == tuple(sorted(cli.PLUGIN_SUBCOMMANDS))


def test_every_non_drop_in_module_here_is_excluded_from_the_walk() -> None:
    """`clv/plugins/` holds CLV's own machinery beside the drop-ins.

    Phase 15 added two modules there and both were promptly walked, imported
    and reported to the operator as broken bundled plugins — their dataclasses
    are classes in a module beside the drop-ins and the walk cannot tell the
    difference. This is what stops the next one repeating it.
    """

    import pkgutil

    import clv.plugins as package

    from clv.plugins import _LOCAL_SUBPACKAGES, _LOADER_MODULES

    found = {
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.ispkg and not info.name.startswith("_")
    }
    assert found <= set(_LOADER_MODULES), (
        "a module in clv/plugins/ is not declared in _LOADER_MODULES and will "
        "be walked as a drop-in plugin: " + ", ".join(sorted(found - set(_LOADER_MODULES)))
    )
    assert set(_LOCAL_SUBPACKAGES).isdisjoint(_LOADER_MODULES)
