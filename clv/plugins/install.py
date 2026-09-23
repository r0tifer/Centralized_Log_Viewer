"""Getting a plugin onto this machine, from something that may be hostile.

The counterpart to :mod:`clv.plugins.manifest`, which says what a plugin
*claims*. This module is what acts on the claim: fetch, unpack, check, and only
then copy anything into the operator's plugin directory.

**A plugin tarball is untrusted input from the internet and is treated as one,
whatever its manifest says about its author.** A signature says who packaged
it; it does not say the archive is well formed, and every check below runs
before the signature is even looked at. The ordering matters: verification
happens on bytes already safely on disk, never on bytes being unpacked.

**Nothing here imports plugin code.** Not to inspect it, not to read its
version, not to find out what it supplies. The manifest is the only thing that
speaks for a plugin before the operator enables it, which is what makes
``clv plugin install`` unable to run the thing it is installing.

Three properties are load-bearing and easy to lose in a refactor.

*Extraction is manual.* ``extractfile()`` and a chunked copy, never
``extractall()``. That is not style: ``tarfile``'s filtering default differs
between CLV's 3.11 floor and the 3.14 it is developed on, and ``data_filter``
does not exist before 3.11.4 at all. Doing the work by hand is what makes the
rules identical on both interpreters, which Requirement 8 needs.

*The archive is read as a stream.* ``r|*`` rather than ``r:*``, because
``getmembers()`` on a seekable archive decompresses the whole thing to build its
index — so a bomb would already have cost what the cap exists to prevent before
the first member was judged. Streaming means a member is judged from its header,
and an over-budget one stops the read.

*Nothing lands in the plugin directory until everything has passed.* Staging is
a temporary directory elsewhere, and the move in is the last step. A refusal
therefore leaves ``~/.config/clv/plugins/`` byte-for-byte as it was, which is
the property the malicious-archive tests assert.
"""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

from . import satisfies
from .manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    InstallRecord,
    Manifest,
    ManifestFile,
    SignatureResult,
    check_files,
    digest_file,
    parse_manifest,
    read_record,
    record_dir,
    recorded_names,
    remove_record,
    stored_manifest_path,
    stored_signature,
    stored_signature_path,
    unsafe_relative,
    verify_signature,
    write_record,
)

#: The most a download may be. Generous for a plugin — the largest thing in
#: CLV's own tree is smaller — and small enough that a mistyped URL pointing at
#: a disk image stops rather than fills ``/tmp``.
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024

#: The most an archive may expand to. This is the decompression-bomb ceiling,
#: and it is enforced **while** writing rather than after: a 10 GB stream of
#: zeros compresses to a few kilobytes, so a check that ran afterwards would
#: run after the damage.
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024

#: The most a single member may be, and the most members there may be. A plugin
#: is source code; anything that needs more than this is shipping data CLV has
#: no business unpacking into an import path.
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 2048

#: How long a fetch gets to make progress. Applies per socket operation, not to
#: the transfer as a whole, which the size cap bounds instead.
FETCH_TIMEOUT = 30.0

#: What ``install`` accepts, and the whole list. ``file`` is here because it is
#: how the tests reach the URL path without a network, and because a file URL is
#: a reasonable thing to type at a mounted share.
ALLOWED_SCHEMES = ("https", "file")

#: Suffixes :func:`unpack` recognises. ``tarfile`` sniffs the compression
#: itself; this is only what decides "is this an archive at all".
ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)

_READ_CHUNK = 128 * 1024


class InstallError(Exception):
    """A refusal, phrased for whoever ran the command.

    Every raise site says what was wrong *and* what to do instead, because the
    person holding a tarball that will not install usually did not build it and
    cannot read the error as a developer would.
    """


# --- results -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallResult:
    name: str
    version: str
    destination: Path
    record: InstallRecord
    signature: SignatureResult
    warnings: tuple[str, ...] = ()
    replaced: bool = False

    @property
    def manifested(self) -> bool:
        return self.record.manifested


@dataclass(frozen=True, slots=True)
class RemoveResult:
    name: str
    removed: tuple[Path, ...]
    disabled: bool
    section_kept: Optional[str]
    section_purged: bool


@dataclass(frozen=True, slots=True)
class VerifyResult:
    name: str
    problems: tuple[str, ...]
    signature: SignatureResult
    record: Optional[InstallRecord]
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


# --- fetching ----------------------------------------------------------------


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to leave the host the operator named.

    The consent in a URL install is the operator typing that URL. A redirect to
    somewhere else is code arriving from a host they never approved, and a
    redirect to ``http`` is the same bytes with the transport guarantee removed
    — so both are refused rather than followed and reported.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https":
            raise InstallError(
                f"{req.full_url} redirects to {target.scheme}://… — CLV follows "
                f"https only. Ask whoever published it for a direct https URL."
            )
        if target.netloc.lower() != source.netloc.lower():
            raise InstallError(
                f"{req.full_url} redirects to {target.netloc}, a different host. "
                f"CLV downloads only from the host you named. If that is where "
                f"the plugin really lives, install from that URL directly."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_SameHostRedirect)


def fetch(url: str, into: Path, *, opener=None) -> Path:
    """Download *url* into *into*, bounded, and return the file.

    Standard library ``urllib`` per Requirement 7. The scheme allowlist is
    checked before anything opens a socket, and ``http`` is refused by name
    rather than as an unsupported scheme — it is the one someone will actually
    try, and "use https" is a better answer than a list.
    """

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "http":
        raise InstallError(
            f"{url} is plain http. CLV downloads plugins over https only — "
            f"anything on the path between you and that server could replace "
            f"the file. Ask for an https URL, or download it yourself and "
            f"install from the file."
        )
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise InstallError(
            f"{url} uses {parsed.scheme or 'no'} scheme. CLV installs from "
            f"{' or '.join(ALLOWED_SCHEMES)} URLs, or from a path on this machine."
        )

    name = Path(urllib.parse.unquote(parsed.path)).name or "plugin.tar.gz"
    fault = unsafe_relative(name)
    if fault:
        # A URL path ending in `..` or a separator. Nothing good is downstream
        # of accepting it as a filename.
        raise InstallError(f"{url} does not name a file to download.")
    destination = into / name

    director = _opener() if opener is None else opener
    try:
        with director.open(url, timeout=FETCH_TIMEOUT) as response:
            _copy_bounded(
                response,
                destination,
                limit=MAX_DOWNLOAD_BYTES,
                what=f"the download from {url}",
            )
    except InstallError:
        raise
    except urllib.error.HTTPError as exc:
        raise InstallError(f"{url} returned {exc.code} {exc.reason}.") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"{url} could not be reached: {exc.reason}.") from exc
    except OSError as exc:
        raise InstallError(f"{url} could not be read: {exc}.") from exc
    return destination


def _copy_bounded(source, destination: Path, *, limit: int, what: str) -> int:
    """Stream *source* to *destination*, stopping the moment it exceeds *limit*.

    The partially written file is removed on the way out. Returning early with
    it in place would leave a truncated archive that the next step would report
    as corrupt, which is a true statement about the wrong thing.
    """

    written = 0
    try:
        with destination.open("wb") as handle:
            while True:
                block = source.read(_READ_CHUNK)
                if not block:
                    break
                written += len(block)
                if written > limit:
                    raise InstallError(
                        f"{what} is larger than {limit // (1024 * 1024)} MB and "
                        f"was stopped. A plugin is source code; something this "
                        f"size is not one."
                    )
                handle.write(block)
    except InstallError:
        destination.unlink(missing_ok=True)
        raise
    return written


# --- unpacking ---------------------------------------------------------------


def unpack(archive: Path, into: Path) -> Path:
    """Extract *archive* under *into* and return the directory to read from.

    Every member is judged from its header before a byte of its data is read.
    A single wrapping directory — the ``foo-1.2.0/`` that ``tar czf`` produces —
    is stripped, so a manifest at the top of the archive is found either way.
    """

    if zipfile.is_zipfile(archive):
        raise InstallError(
            f"{archive.name} is a zip. CLV installs from tar archives — "
            f"`tar czf {archive.stem}.tar.gz …` — which is one extractor and "
            f"one set of rules rather than two. Repackage it, or unpack it and "
            f"install from the directory."
        )

    payload = into / "payload"
    payload.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(archive, "r|*") as bundle:
            _extract_stream(bundle, payload)
    except InstallError:
        raise
    except tarfile.TarError as exc:
        raise InstallError(
            f"{archive.name} is not a readable tar archive ({exc}). CLV accepts "
            f".tar, .tar.gz, .tar.bz2 and .tar.xz."
        ) from exc
    except OSError as exc:
        raise InstallError(f"{archive.name} could not be read: {exc}.") from exc

    entries = sorted(payload.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return payload


def _extract_stream(bundle: tarfile.TarFile, destination: Path) -> None:
    """Judge and extract every member of a streaming tar.

    The refusals are by *kind* and by *name*, and both matter. A symlink is
    refused even when it points somewhere harmless, because the check that
    decides "harmless" runs against this filesystem at this moment and the file
    is read later; a hardlink because it can reach a file outside the payload
    that the extractor never wrote; a device node because nothing a plugin ships
    is one.
    """

    total = 0
    count = 0
    for member in bundle:
        count += 1
        if count > MAX_MEMBERS:
            raise InstallError(
                f"the archive has more than {MAX_MEMBERS} entries and was "
                f"stopped. A plugin is source code, not a filesystem."
            )

        fault = unsafe_relative(member.name)
        if fault:
            raise InstallError(
                f"the archive contains {member.name!r}, which {fault}. Nothing "
                f"was extracted. An archive that writes outside the directory "
                f"it is unpacked into is not a packaging mistake."
            )

        if member.isdir():
            _mkdir_within(destination, member.name)
            continue

        if not member.isreg():
            raise InstallError(
                f"the archive contains {member.name!r}, which is "
                f"{_member_kind(member)}. CLV extracts regular files and "
                f"directories only. Nothing was extracted."
            )

        if member.size > MAX_MEMBER_BYTES:
            raise InstallError(
                f"{member.name} is larger than "
                f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB and was stopped."
            )

        target = _resolve_within(destination, member.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = bundle.extractfile(member)
        if source is None:  # pragma: no cover - tarfile returns None for non-files
            continue
        total = _copy_member(source, target, member.name, total)


def _copy_member(source, target: Path, name: str, total: int) -> int:
    """Copy one member, holding the running decompressed-byte total.

    The cap is checked per chunk rather than against the header's ``size``,
    because the header is the archive's own claim about itself and this is the
    number that has to be true.
    """

    with target.open("wb") as handle:
        while True:
            block = source.read(_READ_CHUNK)
            if not block:
                break
            total += len(block)
            if total > MAX_EXTRACTED_BYTES:
                raise InstallError(
                    f"the archive expands to more than "
                    f"{MAX_EXTRACTED_BYTES // (1024 * 1024)} MB and was stopped "
                    f"while reading {name}. Nothing was installed."
                )
            handle.write(block)
    return total


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "a symbolic link"
    if member.islnk():
        return "a hard link"
    if member.ischr():
        return "a character device"
    if member.isblk():
        return "a block device"
    if member.isfifo():
        return "a named pipe"
    return "not a regular file"


def _resolve_within(base: Path, name: str) -> Path:
    """*base* joined with *name*, proven to stay inside *base*.

    Belt and braces over :func:`unsafe_relative`, which judges the string.
    This judges the result, so a name that is textually innocent but lands
    outside anyway — through a directory the archive itself created — is still
    caught.
    """

    candidate = (base / name).resolve()
    root = base.resolve()
    if candidate != root and root not in candidate.parents:
        raise InstallError(
            f"the archive contains {name!r}, which resolves outside the "
            f"directory it is unpacked into. Nothing was extracted."
        )
    return candidate


def _mkdir_within(base: Path, name: str) -> None:
    _resolve_within(base, name).mkdir(parents=True, exist_ok=True)


# --- installing --------------------------------------------------------------


def install(
    source: str,
    *,
    force: bool = False,
    expected_sha256: Optional[str] = None,
    root: Optional[Path] = None,
) -> InstallResult:
    """Install the plugin at *source*, which is a path, an archive or a URL.

    *expected_sha256* is a digest the operator got **out of band** — from the
    page they found the download on, or from whoever sent it to them. It is
    checked against the archive as a file, before a single member is read, and
    it is the only check here that does not come from inside the archive
    itself: a manifest's checksums prove the files match what the manifest
    says, which a tampered archive can arrange for by rewriting both.

    Never enables what it installs. ``PLUGIN_TODO.md`` Requirement 2 at the
    CLI: a plugin present in the directory is inert until the operator names
    it, and an install command that also enabled would make "installing is not
    consent" false exactly where it is most convenient to break it.
    """

    from ..services.config import ensure_user_plugin_dir, user_plugin_dir

    target_root = user_plugin_dir() if root is None else root
    with tempfile.TemporaryDirectory(prefix="clv-plugin-install-") as staging_name:
        staging = Path(staging_name)
        payload, origin, from_archive = _stage(source, staging, expected_sha256)
        manifest, manifest_bytes, signature_file = _read_manifest(payload, from_archive)
        signature = (
            verify_signature(payload / MANIFEST_NAME, signature_file)
            if manifest_bytes
            else SignatureResult(
                state="unsigned",
                detail=(
                    "installed from a path on this machine with no manifest, so "
                    "there is nothing signed and nothing to check later."
                ),
            )
        )
        if not signature.ok:
            raise InstallError(f"{origin}: {signature.detail}")

        name, is_package, files = _layout(payload, manifest, origin)
        warnings = list(_constraint_warnings(manifest, name))

        if manifest is not None:
            # Before anything is copied, which is the whole point of doing it
            # here rather than after the move.
            problems = check_files(payload, files)
            if problems:
                raise InstallError(
                    f"{origin} does not match its own manifest:\n  "
                    + "\n  ".join(problems)
                    + "\nNothing was installed. The archive was altered after it "
                    "was packaged, or it was packaged wrong."
                )

        if root is None:
            ensure_user_plugin_dir()
        target_root.mkdir(parents=True, exist_ok=True)
        destination = target_root / (name if is_package else f"{name}.py")
        replaced = destination.exists()
        if replaced and not force:
            existing = read_record(name, target_root)
            known = f" ({existing.version})" if existing and existing.version else ""
            raise InstallError(
                f"{name}{known} is already installed at {destination}. "
                f"`clv plugin install --force {source}` replaces it; "
                f"`clv plugin remove {name}` takes it out first."
            )

        _move_into_place(payload, destination, name, is_package)

        record = InstallRecord.of(
            manifest,
            name=name,
            origin=origin,
            signature=signature,
            is_package=is_package,
            files=files,
        )
        write_record(record, target_root)
        _store_payload(name, manifest_bytes, signature_file, target_root)

    return InstallResult(
        name=name,
        version=record.version,
        destination=destination,
        record=record,
        signature=signature,
        warnings=tuple(warnings),
        replaced=replaced,
    )


def _stage(
    source: str, staging: Path, expected_sha256: Optional[str] = None
) -> tuple[Path, str, bool]:
    """Get *source* into *staging* and say where to read it and what it was.

    Returns the directory to read the plugin from, the origin string the record
    will remember, and whether it arrived as an archive — which is what decides
    whether a manifest is compulsory.
    """

    if _is_url(source):
        downloaded = fetch(source, staging)
        _check_expected(downloaded, expected_sha256, source)
        return unpack(downloaded, staging), source, True

    path = Path(source).expanduser()
    if not path.exists():
        raise InstallError(f"{source} does not exist.")

    if path.is_file() and _looks_like_archive(path):
        _check_expected(path, expected_sha256, source)
        return unpack(path, staging), str(path.resolve()), True

    if expected_sha256:
        raise InstallError(
            "--sha256 is for an archive or a URL. A directory has no single "
            "digest, and a .py file you already have on disk is not in transit."
        )

    landing = staging / "payload"
    landing.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        _refuse_symlinks(path)
        shutil.copytree(path, landing, dirs_exist_ok=True, symlinks=False)
    elif path.suffix == ".py":
        shutil.copy2(path, landing / path.name)
    else:
        raise InstallError(
            f"{source} is neither a .py file, a directory, nor a tar archive. "
            f"A plugin is a single module, a package directory, or either of "
            f"those packaged with `tar czf`."
        )
    return landing, str(path.resolve()), False


def _check_expected(archive: Path, expected: Optional[str], source: str) -> None:
    """Compare *archive* against a digest the operator supplied themselves.

    Before anything is unpacked, which is the point: every other check here
    reads something the archive says about itself, and an archive that was
    replaced in transit says whatever its replacer wanted. This is the one
    number that came from somewhere else.
    """

    if not expected:
        return
    wanted = expected.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", wanted):
        raise InstallError(
            f"{expected!r} is not a sha256 digest. It is 64 hex characters — "
            f"what `sha256sum` prints."
        )
    actual = digest_file(archive)
    if actual != wanted:
        raise InstallError(
            f"{source} does not match the digest you gave.\n"
            f"  expected {wanted}\n"
            f"  actually {actual}\n"
            f"Nothing was unpacked. This is what a file replaced in transit "
            f"looks like — do not install it until you know why."
        )


def _refuse_symlinks(directory: Path) -> None:
    """Refuse a local source directory containing a symlink.

    The same rule the archive extractor applies, for the same reason and one
    weaker one. A local directory is the operator's own and ``cp -r`` would
    have followed the link too — but the plugin directory is an import path,
    and a link there means the file CLV imports next month is not the file
    anybody reviewed this month.
    """

    for entry in directory.rglob("*"):
        if entry.is_symlink():
            raise InstallError(
                f"{entry} is a symbolic link. CLV installs regular files and "
                f"directories, so that what is in your plugin directory is what "
                f"was reviewed. Copy the real file in instead."
            )


def _is_url(source: str) -> bool:
    scheme = urllib.parse.urlsplit(source).scheme
    # A bare Windows-style drive letter is not a scheme, and neither is the
    # first component of a relative path with a colon in it.
    return len(scheme) > 1 and scheme.isalpha()


def _looks_like_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES) or (
        zipfile.is_zipfile(path)
    )


def _read_manifest(
    payload: Path, from_archive: bool
) -> tuple[Optional[Manifest], bytes, Optional[Path]]:
    """Read ``clv-plugin.toml`` from *payload*, if there is one and if there must be.

    Compulsory for an archive or a URL and optional for a local path. That
    asymmetry is the whole distinction between the two doors: a local ``foo.py``
    is the documented ``cp`` under another name and the operator is looking
    right at it, while a tarball arrived from somewhere else and has to say what
    it is before CLV unpacks it into an import path.
    """

    candidate = payload / MANIFEST_NAME
    if not candidate.is_file():
        if from_archive:
            raise InstallError(
                f"there is no {MANIFEST_NAME} in this archive. A packaged plugin "
                f"has to declare its name, its version and the sha256 of every "
                f"file it ships — that is what CLV checks it against later. "
                f"`clv/plugins/AGENTS.md` has the format under Publishing."
            )
        return None, b"", None

    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise InstallError(f"{MANIFEST_NAME} could not be read: {exc}.") from exc
    manifest = parse_manifest(data, origin=MANIFEST_NAME)
    signature = payload / SIGNATURE_NAME
    return manifest, data, signature if signature.is_file() else None


def _layout(
    payload: Path, manifest: Optional[Manifest], origin: str
) -> tuple[str, bool, tuple[ManifestFile, ...]]:
    """Decide what is being installed and under what name.

    Two shapes and no others: a single module ``<name>.py``, or a package
    directory ``<name>/`` with everything under it. A manifest that scattered
    files across the plugin directory would be installing several plugins under
    one name, and ``remove`` would have nothing coherent to undo.
    """

    if manifest is None:
        return _unmanifested_layout(payload, origin)

    name = manifest.name
    module = f"{name}.py"
    prefix = f"{name}/"
    paths = [entry.path.replace("\\", "/") for entry in manifest.files]

    if paths == [module]:
        if not (payload / module).is_file():
            raise InstallError(
                f"{origin}: the manifest lists {module}, which is not in the "
                f"archive."
            )
        return name, False, manifest.files

    if all(path.startswith(prefix) for path in paths):
        if not (payload / name).is_dir():
            raise InstallError(
                f"{origin}: the manifest lists files under {prefix}, which is "
                f"not a directory in the archive."
            )
        if not (payload / name / "__init__.py").is_file():
            raise InstallError(
                f"{origin}: {prefix} has no __init__.py, so Python cannot import "
                f"it as a package."
            )
        return name, True, manifest.files

    raise InstallError(
        f"{origin}: `files` has to be either exactly [{module}] for a "
        f"single-module plugin, or entirely under {prefix} for a package. It "
        f"lists {', '.join(paths[:4])}"
        f"{'…' if len(paths) > 4 else ''}, which is neither."
    )


def _unmanifested_layout(
    payload: Path, origin: str
) -> tuple[str, bool, tuple[ManifestFile, ...]]:
    """The local, undeclared case: work the layout out from what is there.

    No checksums are recorded, and ``verify`` later says there is nothing to
    check rather than pretending a hash CLV computed from the file itself is
    evidence about it. A digest is only worth something if it came from
    somewhere the file did not.
    """

    entries = [entry for entry in payload.iterdir() if entry.name != MANIFEST_NAME]
    modules = [entry for entry in entries if entry.is_file() and entry.suffix == ".py"]
    packages = [
        entry
        for entry in entries
        if entry.is_dir() and (entry / "__init__.py").is_file()
    ]

    if len(modules) == 1 and not packages:
        return modules[0].stem, False, ()
    if len(packages) == 1 and not modules:
        return packages[0].name, True, ()
    raise InstallError(
        f"{origin} has no {MANIFEST_NAME}, and CLV cannot tell what to install "
        f"from it: it needs to be one .py file, or one directory with an "
        f"__init__.py. Add a manifest if it is more than that."
    )


def _constraint_warnings(manifest: Optional[Manifest], name: str) -> Iterator[str]:
    """Warn — never refuse — about a constraint this build does not satisfy.

    At install rather than only at load, which is the whole point: the operator
    is present, holding the thing, and able to go and find a different version.
    A warning rather than a refusal because the constraint is the author's
    guess about a build they have never seen, and being wrong about it should
    not be CLV's reason to stop someone installing a file on their own machine.
    """

    if manifest is None:
        return
    from . import PLUGIN_API_VERSION
    from .. import __version__

    for label, current, constraint in (
        ("plugin API", PLUGIN_API_VERSION, manifest.requires_api),
        ("CLV", __version__, manifest.requires_clv),
    ):
        if not constraint:
            continue
        try:
            ok = satisfies(current, constraint)
        except ValueError as exc:
            yield f"{name} declares an unreadable {label} constraint: {exc}."
            continue
        if not ok:
            yield (
                f"{name} asks for {label} {constraint} and this is {current}. It "
                f"will install, but CLV will decline to load it — the same "
                f"check runs again at startup."
            )


def _move_into_place(
    payload: Path, destination: Path, name: str, is_package: bool
) -> None:
    """Put the staged plugin where it belongs, replacing what is there.

    The old copy is moved aside first and deleted only once the new one has
    landed, so a failure halfway leaves the operator with the plugin they had
    rather than with neither.
    """

    source = payload / name if is_package else payload / f"{name}.py"
    backup = destination.with_name(destination.name + ".clv-replacing")
    _discard(backup)
    if destination.exists():
        os.replace(destination, backup)
    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        if backup.exists():
            os.replace(backup, destination)
        raise InstallError(f"{name} could not be written to {destination}: {exc}.")
    _discard(backup)


def _store_payload(
    name: str, manifest_bytes: bytes, signature: Optional[Path], root: Path
) -> None:
    """Keep the manifest and signature so ``verify`` can re-check them later.

    Byte-for-byte, because a signature covers exactly these bytes. See
    :func:`manifest.stored_manifest_path` for why re-checking has to be possible
    at all.

    **Both are cleared when there is nothing to keep**, rather than skipped.
    ``--force`` over an existing plugin reuses its name, so a manifest left
    behind by the *previous* install would outlive the plugin it described and
    be read as this one's — reporting a stranger's signature against code they
    never signed. Writing a name means owning every file under it.
    """

    directory = record_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    kept_manifest = stored_manifest_path(name, root)
    kept_signature = stored_signature_path(name, root)

    if not manifest_bytes:
        kept_manifest.unlink(missing_ok=True)
        kept_signature.unlink(missing_ok=True)
        return

    kept_manifest.write_bytes(manifest_bytes)
    if signature is not None:
        shutil.copyfile(signature, kept_signature)
    else:
        kept_signature.unlink(missing_ok=True)


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


# --- removing ----------------------------------------------------------------


def remove(
    name: str,
    *,
    purge: bool = False,
    root: Optional[Path] = None,
    settings_path: Optional[Path] = None,
) -> RemoveResult:
    """Delete *name*'s files and its record, and stop it being enabled.

    Dropping the name from ``plugins`` is the symmetry with install, which
    deliberately does not add it: removing un-enables. Leaving it would report
    the plugin as named-but-absent on every launch from then on — accurate, and
    indistinguishable from a bug to whoever just ran this command.

    The ``[plugin:<name>]`` section is **kept** unless *purge*. An operator
    reinstalling a plugin should not have to set it up again, and settings are
    the part they wrote themselves.
    """

    from ..services.config import PLUGIN_SECTION_PREFIX, user_config_path, user_plugin_dir

    target_root = user_plugin_dir() if root is None else root
    module = target_root / f"{name}.py"
    package = target_root / name

    removed: list[Path] = []
    if package.is_dir() and not package.is_symlink():
        shutil.rmtree(package)
        removed.append(package)
    if module.is_file():
        module.unlink()
        removed.append(module)

    if not removed:
        raise InstallError(
            f"{name} is not installed in {target_root}. "
            f"`clv plugin list` shows what is."
        )

    remove_record(name, target_root)

    settings = user_config_path() if settings_path is None else settings_path
    disabled = _disable(name, settings)
    section = f"{PLUGIN_SECTION_PREFIX}{name}"
    purged = _purge_section(section, settings) if purge else False
    kept = section if (not purged and _has_section(section, settings)) else None

    return RemoveResult(
        name=name,
        removed=tuple(removed),
        disabled=disabled,
        section_kept=kept,
        section_purged=purged,
    )


def _disable(name: str, settings: Path) -> bool:
    """Drop *name* from the ``plugins`` key, returning whether it was there.

    Through ``SettingsDocument`` like every other edit CLV makes to that file,
    so the operator's comments, their spacing and the keys this version does not
    know about all survive — see :mod:`clv.services.settings_file`.
    """

    from ..services.settings_file import DEFAULT_SECTION, SettingsDocument

    if not settings.exists():
        return False
    document = SettingsDocument.load(settings)
    # The last, not the first: configparser is what actually loads this file
    # and it takes the last of a duplicated key, so that is the list currently
    # in force. See `SettingsDocument.values`.
    assigned = document.values(DEFAULT_SECTION, "plugins")
    if not assigned:
        return False
    raw = assigned[-1]
    kept = [
        piece.strip()
        for piece in raw.split(",")
        if piece.strip() and piece.strip().casefold() != name.casefold()
    ]
    if len(kept) == len([p for p in raw.split(",") if p.strip()]):
        return False
    # `every`, because a settings file can carry `plugins` twice and
    # configparser honours the last while this module honours the first —
    # editing one of the two would report success and change nothing. See
    # `SettingsDocument.set`.
    #
    # An emptied list is written as `plugins =` rather than deleted. The two
    # mean the same thing to `config.py`, but the shipped template carries the
    # key with its explanatory comment above it, and removing the key would
    # leave that comment describing something that is no longer there.
    document.set(DEFAULT_SECTION, "plugins", ", ".join(kept), every=True)
    document.save(settings)
    return True


def _has_section(section: str, settings: Path) -> bool:
    from ..services.settings_file import SettingsDocument

    if not settings.exists():
        return False
    return SettingsDocument.load(settings).has_section(section)


def _purge_section(section: str, settings: Path) -> bool:
    from ..services.settings_file import SettingsDocument

    if not settings.exists():
        return False
    document = SettingsDocument.load(settings)
    if not document.remove_section(section):
        return False
    document.save(settings)
    return True


# --- verifying ---------------------------------------------------------------


def verify(
    names: Sequence[str] = (), root: Optional[Path] = None
) -> tuple[VerifyResult, ...]:
    """Re-check what is installed against what it said it was.

    With no *names*, everything with a record. Both halves are re-run: the
    checksums, and the signature against the trust store **as it is now** —
    which is why the manifest is kept. A key added since the install turns an
    ``untrusted`` plugin into a ``verified`` one without reinstalling it, and a
    key removed does the reverse, which is the entire reason to trust keys
    rather than files.
    """

    from ..services.config import user_plugin_dir

    target_root = user_plugin_dir() if root is None else root
    wanted = tuple(names) if names else recorded_names(target_root)

    results: list[VerifyResult] = []
    for name in wanted:
        record = read_record(name, target_root)
        if record is None:
            results.append(
                VerifyResult(
                    name=name,
                    problems=(
                        f"{name} has no installation record. It was copied in by "
                        f"hand, or installed before this version — there is "
                        f"nothing to check it against.",
                    ),
                    signature=SignatureResult(state="unsigned"),
                    record=None,
                )
            )
            continue
        problems = list(check_files(target_root, record.files))
        signature = stored_signature(name, target_root)
        if signature.state == "bad":
            problems.append(signature.detail)
        results.append(
            VerifyResult(
                name=name,
                problems=tuple(problems),
                signature=signature,
                record=record,
                checked=len(record.files),
            )
        )
    return tuple(results)


__all__ = [
    "ALLOWED_SCHEMES",
    "ARCHIVE_SUFFIXES",
    "FETCH_TIMEOUT",
    "MAX_DOWNLOAD_BYTES",
    "MAX_EXTRACTED_BYTES",
    "MAX_MEMBERS",
    "MAX_MEMBER_BYTES",
    "InstallError",
    "InstallResult",
    "RemoveResult",
    "VerifyResult",
    "fetch",
    "install",
    "remove",
    "unpack",
    "verify",
]
