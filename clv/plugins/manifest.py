"""What a distributed plugin declares about itself, and whether it still is it.

**Not to be confused with** :func:`clv.plugins.manifest_for` next door, which
describes a *live plugin object* across the isolation host's pipe. This module
is about a plugin as a thing someone packaged and someone else installed: a
``clv-plugin.toml`` beside the code, the SHA-256 of every file it claims, an
optional detached signature, and the record CLV keeps of where the whole thing
came from. The two never meet, and only the word is shared.

**Nothing here imports plugin code.** That is the whole promise of the ``clv
plugin`` commands -- listing, inspecting or verifying a plugin must not be able
to run it -- and it holds structurally rather than by care: this module reads
bytes and shells out to ``ssh-keygen``, and has no import machinery in it at
all.

Three things are deliberately *not* CLV's to decide.

*Who is legitimate.* CLV ships no trust root and never will. A signature is
checked against :func:`trust_store_path`, which is empty until the operator
runs ``clv plugin trust``. A bundled key would make CLV the arbiter of which
plugins are real, which is the hosted-index commitment arriving through a side
door.

*Who signed it.* The manifest does not get to assert its own signer.
``ssh-keygen -Y find-principals`` answers that from the signature and the
operator's own allowed-signers file, so the claim and the check cannot come
apart.

*What a signature is for.* Every verification passes ``-n clv-plugin``, and
that namespace is the security property rather than a label: without it a
signature its author made over some other file for some other purpose would
replay as a plugin signature.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from . import PluginStatus

#: The file an author writes and a tarball must carry.
MANIFEST_NAME = "clv-plugin.toml"

#: The detached signature, beside the manifest. Detached rather than embedded
#: because the signed bytes have to be exactly the file as shipped -- a
#: signature inside the document it signs means defining which bytes are
#: covered, and every format that has tried has had a vulnerability about it.
SIGNATURE_NAME = MANIFEST_NAME + ".sig"

#: ``ssh-keygen -Y``'s namespace argument. See the module docstring: this is
#: what stops a signature made for git, or for email, or for anything else,
#: counting as an author's word that this is their plugin.
SIGNATURE_NAMESPACE = "clv-plugin"

#: Where CLV's own records live, inside the plugin directory they describe.
#:
#: Dot-prefixed, so ``pkgutil.iter_modules`` never yields it and the plugin
#: count keeps meaning "plugins someone installed" -- the same argument
#: ``config.PLUGIN_EXAMPLES_DIR`` makes for the examples directory beside it.
RECORD_DIR = ".installed"

#: Bumped only if the record's shape changes incompatibly. A record from the
#: future is read for what this version understands rather than refused: the
#: plugin it describes works either way, and refusing would make a downgrade
#: look like a tamper.
RECORD_VERSION = 1

#: Read size for hashing. Large enough that a package of small modules is one
#: read each, small enough that a file someone put in their plugin directory by
#: mistake does not become a resident buffer.
_CHUNK = 128 * 1024

#: Every state a signature can be in, as every command prints them.
#:
#: Five rather than two, because "we could not check" is not "there was nothing
#: to check" and an operator acts differently on each. ``bad`` is the only one
#: that refuses an install: an absent signature is a choice the author made and
#: a broken one is evidence.
SIGNATURE_STATES = ("unsigned", "verified", "untrusted", "bad", "unverifiable")

#: Key types ``clv plugin trust`` accepts, matching what ``ssh-keygen -Y`` will
#: actually verify with. An allowlist rather than a pattern so a typo in a key
#: type is caught while the operator is looking at it, rather than at the next
#: install.
TRUSTED_KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)

#: How long ``ssh-keygen`` gets. It is a local signature check over a few
#: hundred bytes; a second is already generous, and a hung child must not make
#: an install hang with it.
_SSH_KEYGEN_TIMEOUT = 10.0

_MODULE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class ManifestError(Exception):
    """A manifest CLV cannot use, with a sentence saying what to fix.

    Every raise site phrases the message for the operator who ran ``clv plugin
    install``, not for the author who wrote the file -- they are usually not the
    same person, and the one holding the tarball is the one reading the error.
    A traceback is never what they get.
    """


# --- the manifest ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """One file a plugin claims, and what it hashed to when it was packaged."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """``clv-plugin.toml``, parsed.

    :attr:`extra` keeps every top-level key this version does not know about.
    A newer manifest installing on an older CLV is the ordinary case once
    anything is published at all, and refusing an unknown key would make every
    future addition a breaking one.
    """

    name: str
    version: str
    files: tuple[ManifestFile, ...]
    requires_api: Optional[str] = None
    requires_clv: Optional[str] = None
    kinds: tuple[str, ...] = ()
    author: str = ""
    homepage: str = ""
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


def parse_manifest(data: bytes, *, origin: str = MANIFEST_NAME) -> Manifest:
    """Parse *data* as ``clv-plugin.toml``, or raise :class:`ManifestError`.

    *origin* is what the message calls the file, so an error from inside a
    downloaded tarball says which tarball.

    ``tomllib`` is standard library from 3.11, which is CLV's floor, so this
    costs no dependency (Requirement 7). Its own errors are wrapped rather than
    propagated: a ``TOMLDecodeError`` reads as a parser's complaint about a
    byte offset, and the person holding it needs to know it is *this file* that
    is wrong.
    """

    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{origin} is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{origin} is not valid TOML: {exc}") from exc

    name = _required_string(raw, "name", origin)
    if not _MODULE_NAME_RE.match(name):
        # It is what goes in `plugins = ` and what `import` is handed, so a
        # name that is not a module name is not a naming quibble -- the plugin
        # could never be enabled.
        raise ManifestError(
            f"{origin}: name {name!r} is not a usable plugin name. It has to be "
            f"a Python module name — letters, digits and underscores, not "
            f"starting with a digit — because it is what you write in `plugins` "
            f"and what CLV imports."
        )
    version = _required_string(raw, "version", origin)

    files = _parse_files(raw, origin)

    return Manifest(
        name=name,
        version=version,
        files=files,
        requires_api=_optional_string(raw, "requires_api", origin),
        requires_clv=_optional_string(raw, "requires_clv", origin),
        kinds=_string_tuple(raw, "kinds", origin),
        author=_optional_string(raw, "author", origin) or "",
        homepage=_optional_string(raw, "homepage", origin) or "",
        description=_optional_string(raw, "description", origin) or "",
        extra={
            key: value
            for key, value in raw.items()
            if key
            not in {
                "name",
                "version",
                "files",
                "requires_api",
                "requires_clv",
                "kinds",
                "author",
                "homepage",
                "description",
            }
        },
    )


def _required_string(raw: Mapping[str, Any], key: str, origin: str) -> str:
    if key not in raw:
        raise ManifestError(f"{origin} has no `{key}`, which is required.")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(
            f"{origin}: `{key}` has to be a non-empty string, not "
            f"{type(value).__name__}."
        )
    return value.strip()


def _optional_string(raw: Mapping[str, Any], key: str, origin: str) -> Optional[str]:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str):
        raise ManifestError(
            f"{origin}: `{key}` has to be a string, not {type(value).__name__}."
        )
    return value.strip() or None


def _string_tuple(raw: Mapping[str, Any], key: str, origin: str) -> tuple[str, ...]:
    if key not in raw:
        return ()
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{origin}: `{key}` has to be a list of strings.")
    return tuple(item.strip() for item in value if item.strip())


def _parse_files(raw: Mapping[str, Any], origin: str) -> tuple[ManifestFile, ...]:
    """The ``files`` table array, which is what a signature ultimately covers.

    Validated hard, because everything downstream trusts it: the install
    re-hashes exactly these paths, and a manifest that could name a path outside
    its own directory would make the checksum check a way to *read* one.
    """

    if "files" not in raw:
        raise ManifestError(
            f"{origin} has no `files`, which is required. List every file the "
            f"plugin ships, each with its sha256."
        )
    entries = raw["files"]
    if not isinstance(entries, list) or not entries:
        raise ManifestError(
            f"{origin}: `files` has to be a non-empty list of "
            f"{{ path = \"…\", sha256 = \"…\" }} tables."
        )

    parsed: list[ManifestFile] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ManifestError(
                f"{origin}: files[{index}] is not a table. Each entry looks like "
                f"{{ path = \"module.py\", sha256 = \"…\" }}."
            )
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ManifestError(f"{origin}: files[{index}] has no usable `path`.")
        path = path.strip()
        fault = unsafe_relative(path)
        if fault:
            raise ManifestError(f"{origin}: files[{index}] path {path!r} {fault}.")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest.strip().lower()):
            raise ManifestError(
                f"{origin}: files[{index}] ({path}) has no usable `sha256`. It "
                f"has to be 64 hex characters — `sha256sum {path}` prints one."
            )
        key = path.lower()
        if key in seen:
            raise ManifestError(f"{origin}: {path!r} is listed twice in `files`.")
        seen.add(key)
        parsed.append(ManifestFile(path=path, sha256=digest.strip().lower()))
    return tuple(parsed)


def unsafe_relative(path: str) -> Optional[str]:
    """Why *path* may not be used as a relative path, or ``None`` if it may.

    Shared by the manifest and by the archive extractor, so a traversal cannot
    be refused in one and accepted in the other. Deliberately textual: it judges
    the string as written, before anything resolves it against a real directory,
    because the resolution is the step that would follow the ``..``.
    """

    if not path:
        return "is empty"
    if path.startswith(("/", "\\")):
        return "is absolute"
    if re.match(r"[A-Za-z]:", path):
        return "is a drive-qualified path"
    parts = re.split(r"[\\/]+", path)
    if any(part == ".." for part in parts):
        return "climbs out of the plugin directory with `..`"
    if any(part in ("", ".") for part in parts[:-1]):
        return "has an empty path component"
    if parts[-1] in ("", "."):
        return "does not name a file"
    return None


def digest_file(path: Path) -> str:
    """The SHA-256 of *path*, lowercase hex, read in chunks.

    One definition, used by the install, by ``clv plugin verify`` and by ``clv
    doctor``. Three implementations of "matches" would be three answers to
    whether a file had changed.
    """

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def check_files(root: Path, files: Sequence[ManifestFile]) -> tuple[str, ...]:
    """Re-hash *files* under *root*, returning one sentence per problem.

    Empty means everything matched. Used both before an install copies anything
    and long afterwards by ``verify`` and ``doctor``, which is why it reports
    rather than raises: at install time a problem aborts, and at verify time it
    is a list the operator reads.
    """

    problems: list[str] = []
    for entry in files:
        target = root / entry.path
        try:
            actual = digest_file(target)
        except FileNotFoundError:
            problems.append(f"{entry.path}: missing")
            continue
        except OSError as exc:
            problems.append(f"{entry.path}: could not be read ({exc.strerror})")
            continue
        if actual != entry.sha256:
            problems.append(f"{entry.path}: changed since it was packaged")
    return tuple(problems)


# --- the trust store ---------------------------------------------------------


def trust_store_path() -> Path:
    """The operator's allowed-signers file, beside ``settings.conf``.

    OpenSSH's own ``allowed_signers`` format, not a CLV invention: the operator
    can read it, ``ssh-keygen`` consumes it directly, and a line copied out of
    one they already keep for git works unchanged.
    """

    from ..services.config import get_xdg_config_home

    return get_xdg_config_home() / "clv" / "plugin-signers"


@dataclass(frozen=True, slots=True)
class TrustedSigner:
    principal: str
    key_type: str
    key: str

    def line(self) -> str:
        return f"{self.principal} {self.key_type} {self.key}"


def parse_signer_line(line: str) -> TrustedSigner:
    """Validate one allowed-signers line, or raise :class:`ManifestError`.

    Checked while the operator is looking at it rather than at the next install,
    because a key that never matches anything is indistinguishable from a plugin
    nobody signed -- and the second is a thing CLV says out loud, so the first
    would be read as a fact about the plugin.
    """

    parts = line.strip().split()
    if len(parts) < 3:
        raise ManifestError(
            "a trusted signer looks like `<who> <key-type> <key>` — three "
            "fields, the same shape as a line in ~/.ssh/allowed_signers. Try "
            "`clv plugin trust \"me@example.com $(cat ~/.ssh/id_ed25519.pub)\"`."
        )
    principal, key_type, key = parts[0], parts[1], parts[2]
    if key_type not in TRUSTED_KEY_TYPES:
        raise ManifestError(
            f"{key_type!r} is not a key type CLV can verify with. Supported: "
            f"{', '.join(TRUSTED_KEY_TYPES)}."
        )
    try:
        base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError(
            f"the key for {principal} is not valid base64 — it looks truncated. "
            f"Copy the whole second field of the .pub file."
        ) from exc
    return TrustedSigner(principal=principal, key_type=key_type, key=key)


def read_trusted_signers(store: Optional[Path] = None) -> tuple[TrustedSigner, ...]:
    """Every signer the operator trusts, skipping comments and unreadable lines.

    Never raises. An absent store is the default state of every installation and
    means "nothing is trusted yet", which is a state, not a failure.
    """

    path = trust_store_path() if store is None else store
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    signers: list[TrustedSigner] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            signers.append(parse_signer_line(stripped))
        except ManifestError:
            # A line ssh-keygen may still understand and CLV does not. Skipped
            # here rather than reported: this function answers "what can CLV
            # show you", and `ssh-keygen -Y` reads the real file regardless.
            continue
    return tuple(signers)


def add_trusted_signer(signer: TrustedSigner, store: Optional[Path] = None) -> bool:
    """Append *signer*, returning whether anything was written.

    ``False`` means that exact key was already trusted for that principal --
    which is a no-op worth reporting as one, because the operator who ran this
    twice should not be told they added something twice.
    """

    path = trust_store_path() if store is None else store
    for existing in read_trusted_signers(path):
        if existing.key == signer.key and existing.principal == signer.principal:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if fresh:
            handle.write(
                "# Plugin signers you trust, in OpenSSH allowed_signers format.\n"
                "# CLV ships no keys: everything here you put here.\n"
            )
        handle.write(signer.line() + "\n")
    try:
        # Not a secret — these are public keys — but it is a trust decision, and
        # a world-writable trust store is one anybody on the box can extend.
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def remove_trusted_signer(principal: str, store: Optional[Path] = None) -> int:
    """Drop every line for *principal*, returning how many went.

    By principal rather than by key: the operator knows who they stopped
    trusting, and asking them to paste the key back in to remove it is asking
    them to keep the thing they are trying to get rid of.
    """

    path = trust_store_path() if store is None else store
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    kept: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split()
            if fields and fields[0] == principal:
                removed += 1
                continue
        kept.append(line)
    if removed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


# --- signatures --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignatureResult:
    """What CLV can say about who packaged this.

    :attr:`state` is one of :data:`SIGNATURE_STATES`; :attr:`detail` is a
    sentence for the operator, present on every state that is not a plain
    ``verified``.
    """

    state: str
    signer: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether an install may proceed.

        Everything but ``bad``. An unsigned plugin is the overwhelmingly common
        case and refusing it would make signing compulsory, which needs an
        ecosystem that does not exist; a *broken* signature is evidence rather
        than an absence.
        """

        return self.state != "bad"


def verify_signature(
    payload: Path, signature: Optional[Path], *, store: Optional[Path] = None
) -> SignatureResult:
    """Resolve the signature state of *payload*, as one of :data:`SIGNATURE_STATES`.

    *signature* is the detached ``.sig``; ``None`` or a missing file means
    unsigned.

    Two orderings carry the meaning. **Is this signature valid over these
    bytes** is settled first, with ``check-novalidate``, because it is a
    question about the file rather than about anyone's keys -- and since CLV
    ships no trust root, trusting nobody is the default state of every
    installation, so a trust check first would let a tampered manifest out as
    merely unvouched-for. **Who signed it** is then answered by
    ``find-principals`` against the operator's own store, never by the manifest,
    so a plugin cannot name its own signer and have CLV repeat the claim.
    """

    if signature is None or not signature.is_file():
        return SignatureResult(
            state="unsigned",
            detail=(
                "nothing was signed, so CLV cannot tell you who packaged this. "
                "Trust it the way you would trust any other program you install."
            ),
        )

    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        return SignatureResult(
            state="unverifiable",
            detail=(
                "a signature is present but `ssh-keygen` is not installed, so "
                "CLV could not check it. Install openssh-client and re-run "
                "`clv plugin verify`."
            ),
        )

    # **Before the trust store**, and deliberately. Whether these bytes are the
    # bytes that were signed is a question about the file, not about who the
    # operator trusts, and it has to be answered even when they trust nobody —
    # which is every installation by default. Checking trust first would send a
    # tampered manifest down the "untrusted" path, whose sentence says the
    # signature is fine and only the signer is unknown.
    if not _well_formed(keygen, signature, payload):
        return SignatureResult(
            state="bad",
            detail=(
                "it carries a signature that does not match the manifest. The "
                "file has been altered since it was signed, or the signature is "
                "for something else. This is not a question of whose key it is."
            ),
        )

    store_path = trust_store_path() if store is None else store
    if not store_path.is_file():
        return SignatureResult(
            state="untrusted",
            detail=(
                f"the signature is valid, but you trust no signers yet — "
                f"{store_path} does not exist, and CLV ships no keys of its own. "
                f"`clv plugin trust \"<who> <their-public-key>\"` is how one gets "
                f"there."
            ),
        )

    principal = _find_principal(keygen, signature, store_path)
    if principal is None:
        return SignatureResult(
            state="untrusted",
            detail=(
                "the signature is valid, but it was not made by any key you "
                "trust. Ask whoever published it for their public key and add "
                "it with `clv plugin trust`, or install it knowing CLV cannot "
                "vouch for who packaged it."
            ),
        )

    completed = _run_keygen(
        [
            keygen,
            "-Y",
            "verify",
            "-f",
            str(store_path),
            "-I",
            principal,
            "-n",
            SIGNATURE_NAMESPACE,
            "-s",
            str(signature),
        ],
        stdin=payload,
    )
    if completed is None:
        return SignatureResult(
            state="unverifiable",
            signer=principal,
            detail="`ssh-keygen` did not answer in time, so the signature was not checked.",
        )
    if completed.returncode == 0:
        return SignatureResult(state="verified", signer=principal)
    return SignatureResult(
        state="bad",
        signer=principal,
        detail=(
            f"the signature does not match the manifest. ssh-keygen said: "
            f"{_first_line(completed.stderr) or 'verification failed'}. The file "
            f"has been altered since {principal} signed it, or it was signed for "
            f"something else."
        ),
    )


def _find_principal(keygen: str, signature: Path, store: Path) -> Optional[str]:
    """Which trusted principal's key made *signature*, if any.

    ``find-principals`` is the reason the manifest needs no ``signer`` key: the
    signature carries the public key, the store says who that key belongs to,
    and both halves are the operator's.
    """

    completed = _run_keygen(
        [
            keygen,
            "-Y",
            "find-principals",
            "-s",
            str(signature),
            "-f",
            str(store),
            "-n",
            SIGNATURE_NAMESPACE,
        ]
    )
    if completed is None or completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _well_formed(keygen: str, signature: Path, payload: Path) -> bool:
    """Whether *signature* is a valid signature over *payload*, ignoring trust.

    ``-Y check-novalidate`` answers exactly that: the signature parses and the
    bytes match, but it says nothing about whose key made it. It is what lets
    :func:`verify_signature` tell "signed by a stranger" apart from "signed,
    and the payload has since changed" — which read identically from
    ``find-principals`` alone, because both make it find nothing.
    """

    completed = _run_keygen(
        [keygen, "-Y", "check-novalidate", "-n", SIGNATURE_NAMESPACE, "-s", str(signature)],
        stdin=payload,
    )
    # `None` is ssh-keygen failing to run at all. Treated as well-formed so the
    # caller falls through to "untrusted" rather than accusing a plugin of a
    # bad signature on the strength of a subprocess that never started.
    return completed is None or completed.returncode == 0


def _run_keygen(
    argv: Sequence[str], *, stdin: Optional[Path] = None
) -> Optional[subprocess.CompletedProcess]:
    """Run ``ssh-keygen``, returning ``None`` if it could not be run at all.

    Bounded by :data:`_SSH_KEYGEN_TIMEOUT` and given its input from a real file
    handle rather than a pipe: ``-Y verify`` reads the signed payload from
    stdin, and a pipe would mean feeding it while also waiting for it.
    """

    handle = None
    try:
        if stdin is not None:
            handle = stdin.open("rb")
        return subprocess.run(  # noqa: S603 - a fixed argv, no shell
            list(argv),
            stdin=handle if handle is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_SSH_KEYGEN_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if handle is not None:
            handle.close()


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# --- the record of an install ------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallRecord:
    """What CLV remembers about a plugin it installed.

    Written as JSON rather than TOML, which is the one place this module parts
    company with the format it reads. ``tomllib`` parses and does not write, so
    honouring ``.toml`` here would mean hand-rolling a serializer for a file CLV
    then re-parses -- and an apostrophe in an author's name is enough to make
    that a correctness bug in a file the integrity check depends on. The
    manifest an author writes is TOML and is read exactly as specified; this is
    CLV's own bookkeeping and nobody hand-edits it.
    """

    name: str
    version: str = ""
    installed_from: str = ""
    installed_at: str = ""
    signature: str = "unsigned"
    signer: str = ""
    requires_api: Optional[str] = None
    requires_clv: Optional[str] = None
    kinds: tuple[str, ...] = ()
    author: str = ""
    homepage: str = ""
    description: str = ""
    files: tuple[ManifestFile, ...] = ()
    is_package: bool = False

    @classmethod
    def of(
        cls,
        manifest: Optional[Manifest],
        *,
        name: str,
        origin: str,
        signature: SignatureResult,
        is_package: bool,
        files: Sequence[ManifestFile] = (),
    ) -> "InstallRecord":
        """Build a record from what the install learned.

        *manifest* is ``None`` for a local file installed without one -- the
        documented ``cp`` by another name. The record still exists, because
        "where did this come from" is worth answering even when the answer is
        "a path on this machine, undeclared".
        """

        return cls(
            name=name,
            version=manifest.version if manifest else "",
            installed_from=origin,
            installed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            signature=signature.state,
            signer=signature.signer,
            requires_api=manifest.requires_api if manifest else None,
            requires_clv=manifest.requires_clv if manifest else None,
            kinds=manifest.kinds if manifest else (),
            author=manifest.author if manifest else "",
            homepage=manifest.homepage if manifest else "",
            description=manifest.description if manifest else "",
            files=tuple(files or (manifest.files if manifest else ())),
            is_package=is_package,
        )

    @property
    def manifested(self) -> bool:
        """Whether there is anything to verify against."""

        return bool(self.files)


def record_dir(root: Optional[Path] = None) -> Path:
    from ..services.config import user_plugin_dir

    return (user_plugin_dir() if root is None else root) / RECORD_DIR


def record_path(name: str, root: Optional[Path] = None) -> Path:
    return record_dir(root) / f"{name}.json"


def write_record(record: InstallRecord, root: Optional[Path] = None) -> Path:
    """Write *record*, through a sibling temp file then ``os.replace``.

    The same shape ``storage.py`` and ``export.py`` use, and for the same
    reason: a half-written record is a plugin that reports as tampered with
    forever after a power cut.
    """

    path = record_path(record.name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_version": RECORD_VERSION,
        "name": record.name,
        "version": record.version,
        "installed_from": record.installed_from,
        "installed_at": record.installed_at,
        "signature": record.signature,
        "signer": record.signer,
        "requires_api": record.requires_api,
        "requires_clv": record.requires_clv,
        "kinds": list(record.kinds),
        "author": record.author,
        "homepage": record.homepage,
        "description": record.description,
        "is_package": record.is_package,
        "files": [
            {"path": entry.path, "sha256": entry.sha256} for entry in record.files
        ],
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)
    return path


def read_record(name: str, root: Optional[Path] = None) -> Optional[InstallRecord]:
    """The record for *name*, or ``None``.

    Never raises. A record that is missing, unreadable or corrupt reads as
    absent, because a working plugin must not stop working -- or start looking
    tampered with -- over CLV's own bookkeeping.
    """

    try:
        raw = json.loads(record_path(name, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    files: list[ManifestFile] = []
    for entry in raw.get("files") or ():
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        digest = entry.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            files.append(ManifestFile(path=path, sha256=digest))
    kinds = tuple(
        item for item in (raw.get("kinds") or ()) if isinstance(item, str)
    )
    return InstallRecord(
        name=str(raw.get("name") or name),
        version=str(raw.get("version") or ""),
        installed_from=str(raw.get("installed_from") or ""),
        installed_at=str(raw.get("installed_at") or ""),
        signature=str(raw.get("signature") or "unsigned"),
        signer=str(raw.get("signer") or ""),
        requires_api=raw.get("requires_api") or None,
        requires_clv=raw.get("requires_clv") or None,
        kinds=kinds,
        author=str(raw.get("author") or ""),
        homepage=str(raw.get("homepage") or ""),
        description=str(raw.get("description") or ""),
        files=tuple(files),
        is_package=bool(raw.get("is_package")),
    )


def stored_manifest_path(name: str, root: Optional[Path] = None) -> Path:
    """Where the manifest CLV installed from is kept, byte-for-byte.

    Kept at all because ``clv plugin verify`` re-resolves the *signature* as
    well as the checksums, and it cannot do that from a record: a signature
    covers the manifest's exact bytes, so paraphrasing them into JSON and
    reconstructing later would verify something the author never signed. The
    trust store moves — a key gets added, a key gets revoked — and an install
    that reported ``untrusted`` in March has to be able to report ``verified``
    in April without being reinstalled.
    """

    return record_dir(root) / f"{name}.{MANIFEST_NAME}"


def stored_signature_path(name: str, root: Optional[Path] = None) -> Path:
    """Where the detached signature is kept, beside the manifest it covers."""

    return record_dir(root) / f"{name}.{SIGNATURE_NAME}"


def stored_signature(name: str, root: Optional[Path] = None) -> SignatureResult:
    """Re-resolve *name*'s signature against the trust store as it is now.

    ``unsigned`` when nothing was kept, which covers both a plugin that shipped
    without a signature and one installed before CLV kept them.
    """

    manifest = stored_manifest_path(name, root)
    if not manifest.is_file():
        return SignatureResult(
            state="unsigned",
            detail="no manifest was kept for this plugin, so there is nothing to check.",
        )
    signature = stored_signature_path(name, root)
    return verify_signature(manifest, signature if signature.is_file() else None)


def remove_record(name: str, root: Optional[Path] = None) -> bool:
    """Delete *name*'s record and the manifest and signature kept beside it.

    All three or none: a record without its manifest would make ``verify``
    report a signature it cannot re-check, and a manifest without its record
    would outlive the plugin it describes and be inherited by the next one
    installed under the same name.
    """

    removed = False
    for path in (
        record_path(name, root),
        stored_manifest_path(name, root),
        stored_signature_path(name, root),
    ):
        try:
            path.unlink()
        except OSError:
            continue
        removed = True
    return removed


def recorded_names(root: Optional[Path] = None) -> tuple[str, ...]:
    """Every plugin with a record, sorted. Reads no plugin code."""

    try:
        entries = sorted(record_dir(root).glob("*.json"))
    except OSError:
        return ()
    return tuple(entry.stem for entry in entries)


# --- what the UI and the report say about provenance -------------------------


def describe_record(record: Optional[InstallRecord]) -> str:
    """One line saying where a plugin came from and who vouched for it.

    Empty when there is no record, which is the hand-copied plugin: CLV has
    nothing to say about it, and inventing "installed by hand" would be a
    verdict on a file it never saw arrive.
    """

    if record is None:
        return ""
    where = record.installed_from or "an unrecorded source"
    version = f" {record.version}" if record.version else ""
    if record.signature == "verified" and record.signer:
        vouched = f"signed by {record.signer}"
    elif record.signature == "untrusted":
        # Anchored to when it was installed, because this one drifts: trusting
        # the signer afterwards makes it verified, and a line that still said
        # "not by a key you trust" would be stating yesterday's fact in the
        # present tense. `clv plugin verify` re-resolves it; so does
        # `clv plugin info`.
        vouched = "signed by a key you did not trust when you installed it"
    elif record.signature == "unverifiable":
        vouched = "signed, but CLV could not check it at install"
    elif record.signature == "bad":
        vouched = "signature did not match"
    else:
        vouched = "unsigned"
    return f"installed{version} from {where} — {vouched}"


def annotate(
    rows: Sequence["PluginStatus"], root: Optional[Path] = None
) -> tuple["PluginStatus", ...]:
    """Fill :attr:`PluginStatus.provenance` from the records on disk.

    One definition and two callers -- the ``P`` dialog and ``clv doctor`` -- for
    exactly the reason :meth:`PluginRegistry.status` gives for living where it
    does: two places deriving this would be two answers about one plugin.

    It reads records and **never hashes**, so opening the dialog costs a few
    small reads rather than work proportional to what is installed. The
    integrity check is ``verify``'s and ``doctor``'s, where the operator asked
    for it.
    """

    annotated: list["PluginStatus"] = []
    for row in rows:
        if row.source != "user":
            # Bundled plugins and entry points are not installed by this
            # machinery and have no record to find. Saying so per row would be
            # noise on every build.
            annotated.append(row)
            continue
        described = describe_record(read_record(row.name, root))
        annotated.append(
            dataclasses.replace(row, provenance=described) if described else row
        )
    return tuple(annotated)


def integrity_problems(name: str, root: Optional[Path] = None) -> tuple[str, ...]:
    """Re-hash what *name*'s record claims, returning one sentence per problem.

    Empty when clean, when there is no record, or when the record carries no
    files -- "nothing to check" and "checked and fine" are both silence here,
    and the commands that care say which they got.
    """

    record = read_record(name, root)
    if record is None or not record.manifested:
        return ()
    from ..services.config import user_plugin_dir

    base = user_plugin_dir() if root is None else root
    return check_files(base, record.files)


__all__ = [
    "MANIFEST_NAME",
    "RECORD_DIR",
    "SIGNATURE_NAME",
    "SIGNATURE_NAMESPACE",
    "SIGNATURE_STATES",
    "TRUSTED_KEY_TYPES",
    "InstallRecord",
    "Manifest",
    "ManifestError",
    "ManifestFile",
    "SignatureResult",
    "TrustedSigner",
    "add_trusted_signer",
    "annotate",
    "check_files",
    "describe_record",
    "digest_file",
    "integrity_problems",
    "parse_manifest",
    "parse_signer_line",
    "read_record",
    "read_trusted_signers",
    "record_dir",
    "record_path",
    "stored_manifest_path",
    "stored_signature",
    "stored_signature_path",
    "recorded_names",
    "remove_record",
    "remove_trusted_signer",
    "trust_store_path",
    "unsafe_relative",
    "verify_signature",
    "write_record",
]
