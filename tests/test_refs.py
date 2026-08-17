"""Source identity: the two boundaries, and the seam that must not rot back.

``clv/services/refs.py`` exists because CLV persists a source as a string and
rebuilt it with a bare ``Path(entry)`` in twelve places. Every one of those
turns a remote identifier into a plausible local path on restore — silently,
and only on the launch after the operator stopped watching.

The tests here fall into four groups, and the second and fourth are the ones
that would have caught the bug: ``normalize_ref`` must leave a scheme ref alone,
and two hosts with the same path must be two identities.
"""

from __future__ import annotations

import ast
import typing
from dataclasses import dataclass
from pathlib import Path

import pytest

from clv.services import refs
from clv.services.marks import mark_key
from clv.services.refs import (
    format_ref,
    identity,
    normalize_ref,
    parse_ref,
    ref_key,
    scheme_of,
    split_scheme,
)


def _protocol_members(protocol: type) -> set[str]:
    """The protocol's declared members, across the 3.11-3.14 typing churn.

    ``typing.get_protocol_members`` arrived in 3.13 and ``__protocol_attrs__``
    in 3.12; 3.11 has only the private helper. All three agree on the answer.
    """

    try:
        return set(typing.get_protocol_members(protocol))
    except AttributeError:
        return set(typing._get_protocol_attrs(protocol))


@dataclass(frozen=True)
class _StubRemoteRef:
    """A ``SourceRef`` that is not a ``Path``, standing in for Phase 4's.

    Only what the tests below exercise is real; the IO surface raises, because
    a stub that quietly answered ``is_file()`` would let a test pass for the
    wrong reason. Frozen, so it hashes by value — which is the property the
    starred and merged sets need and the thing being asserted.
    """

    host: str
    path: str

    def __str__(self) -> str:
        return f"ssh:{self.host}{self.path}"

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def parent(self) -> "_StubRemoteRef":
        return _StubRemoteRef(self.host, self.path.rsplit("/", 1)[0] or "/")

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(part for part in self.path.split("/") if part)

    @property
    def suffix(self) -> str:
        name = self.name
        return name[name.rindex(".") :] if "." in name[1:] else ""

    def with_name(self, name: str) -> "_StubRemoteRef":
        return _StubRemoteRef(self.host, f"{self.parent.path.rstrip('/')}/{name}")

    def with_suffix(self, suffix: str) -> "_StubRemoteRef":
        stem = self.name[: -len(self.suffix)] if self.suffix else self.name
        return self.with_name(f"{stem}{suffix}")

    def relative_to(self, other: object) -> "_StubRemoteRef":  # pragma: no cover
        raise NotImplementedError

    def __truediv__(self, other: str) -> "_StubRemoteRef":
        return _StubRemoteRef(self.host, f"{self.path.rstrip('/')}/{other}")

    def exists(self) -> bool:  # pragma: no cover
        raise NotImplementedError("a stub ref performs no IO")

    def is_file(self) -> bool:  # pragma: no cover
        raise NotImplementedError("a stub ref performs no IO")

    def is_dir(self) -> bool:  # pragma: no cover
        raise NotImplementedError("a stub ref performs no IO")

    def stat(self):  # pragma: no cover
        raise NotImplementedError("a stub ref performs no IO")

    def open(self, mode: str = "r", **kwargs):  # pragma: no cover
        raise NotImplementedError("a stub ref performs no IO")


# --------------------------------------------------------------------------
# The persistence boundary
# --------------------------------------------------------------------------


def test_format_ref_is_the_string_form_and_never_resolves() -> None:
    """Resolution is ``identity``'s job, and keeping them apart is load-bearing.

    A resolving ``format_ref`` would also change what ``tag_origins`` writes
    into ``ORIGIN_FIELD``, moving every ``source:`` query and every mark key
    with it.
    """

    assert format_ref(Path("/a/../b/c.log")) == "/a/../b/c.log"
    assert format_ref(Path("relative/c.log")) == "relative/c.log"


@pytest.mark.parametrize(
    "stored",
    [
        "/var/log/syslog",
        "logs/app.log",
        " /var/log/padded.log ",
        "/var/log/two words.log",
        "/var/log/2026-08-16T10:00:00.log",
        "/var/log/quote'd.log",
        "journal:all",
        "journal:unit/sshd.service",
        "journal:boot/-1",
        "ssh:web01/var/log/syslog",
    ],
)
def test_parse_ref_round_trips_everything_format_ref_can_produce(stored: str) -> None:
    """The exact property persistence depends on, including the padded case.

    Whitespace padding survives, which is why ``parse_ref`` must not strip: a
    trailing space is a legal filename component, and tidying it here would
    make such a file unreachable.
    """

    assert format_ref(parse_ref(stored)) == stored


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("./logs/a.log", "logs/a.log"),
        ("logs//a.log", "logs/a.log"),
        ("logs/", "logs"),
        ("", "."),
    ],
)
def test_parse_ref_normalises_only_what_path_normalises(
    raw: str, normalised: str
) -> None:
    """The round trip is exact on ``format_ref`` output, not on arbitrary input.

    Documented rather than hidden, because someone will otherwise "fix"
    ``parse_ref`` into a string-preserving wrapper — which would break equality
    between a stored ref and a freshly discovered one, a far worse bug than
    these four shapes. One pass reaches a fixed point, which is all persistence
    needs.
    """

    once = format_ref(parse_ref(raw))
    assert once == normalised
    assert format_ref(parse_ref(once)) == once


def test_parse_ref_touches_neither_the_filesystem_nor_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract that makes a scheme ref safe to read back.

    ``parse_ref`` is not merely careful about ``~`` and the cwd — it contains no
    operation that consults either. This is the inverse of the ``normalize_ref``
    test below, and the pair is the whole two-boundary design.
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert format_ref(parse_ref("~/logs/a.log")) == "~/logs/a.log"
    assert format_ref(parse_ref("logs/a.log")) == "logs/a.log"
    assert not parse_ref("logs/a.log").is_absolute()


def test_normalize_ref_expands_user_and_absolutises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a person typed does get expanded — that is the other boundary."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))

    assert normalize_ref("~/logs/a.log") == home / "logs" / "a.log"
    assert normalize_ref("./logs") == tmp_path / "logs"
    assert normalize_ref(tmp_path / "logs") == tmp_path / "logs"


def test_normalize_ref_does_not_strip_whitespace() -> None:
    """Its two callers already strip, and a trailing space is a legal name."""

    padded = normalize_ref(" spaced.log ")
    assert padded.name == " spaced.log "


# --------------------------------------------------------------------------
# Schemes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("journal:all", "journal"),
        ("journal:unit/sshd.service", "journal"),
        ("journal:boot/-1", "journal"),
        ("ssh:web01/var/log/syslog", "ssh"),
        # Path collapses the double slash to `ssh:/web01/...`, so this shape
        # cannot round trip and must never be accepted.
        ("ssh://web01/var/log", None),
        ("wat:thing", None),
        ("backup:old/a.log", None),
        ("logs:2026/a.log", None),
        ("/var/log/2026-08-16T10:00:00.log", None),
        ("2026-08-16T10:00:00.log", None),
        ("Journal:all", None),
        ("/journal:all", None),
        ("", None),
    ],
)
def test_scheme_of_recognises_only_registered_single_colon_shapes(
    text: str, expected: str | None
) -> None:
    assert scheme_of(text) == expected


def test_split_scheme_returns_the_selector() -> None:
    assert split_scheme("journal:unit/sshd.service") == ("journal", "unit/sshd.service")
    assert split_scheme("journal:all") == ("journal", "all")
    assert split_scheme("/var/log/syslog") == (None, "/var/log/syslog")


def test_normalize_ref_leaves_a_scheme_ref_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test this whole module exists for.

    Without the guard, ``expanduser()``, ``Path.cwd() / ...`` and ``resolve()``
    each on their own turn ``journal:unit/sshd.service`` into a path under the
    working directory — a plausible-looking local file that nothing answers to.
    """

    monkeypatch.chdir(tmp_path)

    for stored in ("journal:all", "journal:unit/sshd.service", "ssh:web01/var/log"):
        result = format_ref(normalize_ref(stored))
        assert result == stored
        assert str(tmp_path) not in result


def test_a_colon_in_a_filename_is_still_a_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timestamped log name must not be mistaken for an identifier.

    The closed ``KNOWN_SCHEMES`` set is what makes this hold for the relative
    form too, where the leading ``/`` is not there to settle it.
    """

    monkeypatch.chdir(tmp_path)

    absolute = "/var/log/2026-08-16T10:00:00.log"
    assert format_ref(normalize_ref(absolute)) == absolute
    assert normalize_ref("2026-08-16T10:00:00.log") == tmp_path / "2026-08-16T10:00:00.log"
    assert normalize_ref("backup:old/a.log") == tmp_path / "backup:old" / "a.log"


def test_a_relative_path_shadowed_by_a_scheme_is_reachable_absolutely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch, pinned: an absolute name, not ``./``.

    ``Path`` normalises ``./`` away before ``refs`` ever sees it, so
    ``./journal:all`` is not a disambiguation. An absolute name is, and every
    path CLV stores is already absolute.
    """

    monkeypatch.chdir(tmp_path)

    assert format_ref(normalize_ref("./journal:all")) == "journal:all"
    absolute = tmp_path / "journal:all"
    assert normalize_ref(str(absolute)) == absolute
    assert refs.is_local(absolute)


def test_a_scheme_shadowed_relative_dir_is_left_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known defect, pinned as a defect so the fix has something to flip.

    A relative directory whose name begins ``journal:`` is *not* unreachable —
    it opens and reads, because ``Path`` still resolves it against the working
    directory. What it loses is absolutisation: it is the one ``log_dirs``
    entry that comes back unpinned, and so means a different place depending on
    where CLV was started, while every sibling entry is fixed at parse time.

    That is worse than a refusal, because it works right up until someone
    launches from elsewhere. ``SSH_TODO.md`` Phase 3 owes the actionable
    message; this test records the behaviour that message will replace, and is
    expected to be inverted then rather than deleted.
    """

    from clv.services.config import parse_log_dirs

    (tmp_path / "journal:archive").mkdir()
    (tmp_path / "ordinary").mkdir()
    monkeypatch.chdir(tmp_path)

    here = [format_ref(ref) for ref in parse_log_dirs("journal:archive, ordinary")]
    assert here == ["journal:archive", str(tmp_path / "ordinary")]

    # The sibling entry is pinned by cwd; the shadowed one floats with it.
    monkeypatch.chdir(tmp_path.parent)
    elsewhere = [format_ref(ref) for ref in parse_log_dirs("journal:archive, ordinary")]
    assert elsewhere[0] == here[0], "the shadowed entry is unpinned, so it did not move"
    assert elsewhere[1] != here[1], "an ordinary relative entry is pinned at parse time"


def test_the_journald_scheme_is_registered() -> None:
    """Ties the two copies of the scheme together so they cannot drift."""

    from clv.plugins.sources import journald

    assert scheme_of(f"{journald.SCHEME}all") == "journal"
    assert split_scheme(f"{journald.SCHEME}unit/sshd.service") == (
        "journal",
        "unit/sshd.service",
    )


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


def test_path_implements_every_protocol_member() -> None:
    """``Path`` satisfies ``SourceRef`` natively — no shim for local sources."""

    path = Path("/var/log/syslog")
    missing = [member for member in refs.PROTOCOL_MEMBERS if not hasattr(path, member)]
    assert not missing, f"pathlib.Path no longer supplies: {missing}"


def test_the_protocol_members_constant_matches_the_protocol_body() -> None:
    """The drift guard. This project runs no type checker; nothing else checks."""

    assert _protocol_members(refs.SourceRef) == set(refs.PROTOCOL_MEMBERS)


@pytest.mark.parametrize(
    "member",
    ["resolve", "expanduser", "is_absolute", "__fspath__", "__lt__", "glob", "iterdir", "stem"],
)
def test_the_protocol_omits_what_a_remote_ref_cannot_honestly_keep(member: str) -> None:
    """Each absence is a decision, and each would be tempting to "complete".

    ``resolve`` / ``expanduser`` / ``is_absolute`` destroy a scheme ref, and
    their callers now live inside ``refs``. ``__fspath__`` would promise every
    ref works with ``open()`` and ``os.scandir``. ``__lt__`` would let a mixed
    local/remote list be sorted natively, which is where the ``TypeError``
    would first appear. The rest have no source-facing caller at all.
    """

    assert member not in refs.PROTOCOL_MEMBERS


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_identity_resolves_a_symlink_to_its_target(tmp_path: Path) -> None:
    """A symlink and its target are one log — what starring always meant."""

    target = tmp_path / "real.log"
    target.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link.log"
    link.symlink_to(target)

    assert identity(link) == identity(target)
    assert ref_key(link) == str(target.resolve())


def test_parse_ref_does_not_resolve_a_symlink(tmp_path: Path) -> None:
    """The restore path must reconstruct, not canonicalise.

    This is the one way the refactor could have moved behaviour without any
    existing test noticing. ``action_open_merged`` and ``_merged_display_paths``
    did not resolve before and must not now: writing them as
    ``identity(parse_ref(...))`` would make a merged member open its symlink's
    target instead of the file the operator picked. Every merged-view test uses
    ``tmp_path`` with no symlinks in it, so nothing else would have caught it.
    """

    target = tmp_path / "real.log"
    target.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link.log"
    link.symlink_to(target)

    assert parse_ref(str(link)) == link
    assert parse_ref(str(link)) != target
    # ...while identity, which is a different question, does resolve it.
    assert identity(parse_ref(str(link))) == target


def test_identity_falls_back_to_the_ref_when_resolve_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old ``_resolve`` swallowed OSError; ``identity`` keeps that exactly."""

    def boom(self, *args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(Path, "resolve", boom)
    path = Path("/var/log/syslog")
    assert identity(path) == path
    assert ref_key(path) == "/var/log/syslog"


def test_identity_of_a_scheme_ref_is_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving one would invent a cwd path for something with no location."""

    monkeypatch.chdir(tmp_path)
    ref = parse_ref("journal:unit/sshd.service")
    assert identity(ref) == ref
    assert ref_key(ref) == "journal:unit/sshd.service"


def test_identity_of_a_non_path_ref_is_itself() -> None:
    stub = _StubRemoteRef("web01", "/var/log/syslog")
    assert identity(stub) is stub
    assert ref_key(stub) == "ssh:web01/var/log/syslog"
    assert not refs.is_local(stub)


# --------------------------------------------------------------------------
# The colon regression, through StateStore
# --------------------------------------------------------------------------


def test_a_colon_ref_survives_a_state_store_round_trip(tmp_path: Path) -> None:
    """Written now, against stub remote refs, so Phase 4 need not discover it.

    A second ``StateStore`` on the same root is the restart being modelled.
    """

    from clv.storage import SessionState, StateStore

    starred = ("journal:unit/sshd.service", "ssh:web01/var/log/syslog")
    merged = ("/var/log/local.log", "ssh:db02/var/log/postgresql/postgresql.log")
    StateStore(root=tmp_path).save(SessionState(starred=starred, merged=merged))

    restored = StateStore(root=tmp_path).load()

    assert restored.starred == starred
    assert restored.merged == merged
    for stored in restored.starred + restored.merged:
        assert format_ref(parse_ref(stored)) == stored


def test_a_colon_ref_survives_a_saved_view_round_trip(tmp_path: Path) -> None:
    """``SavedView.from_dict`` is a second, separately-coded annotation dispatch."""

    from clv.storage import SavedView, SessionState, StateStore

    view = SavedView(
        name="remote errors",
        query="level:error",
        source="ssh:web01/var/log/syslog",
        merged=("journal:all", "ssh:web01/var/log/syslog"),
    )
    StateStore(root=tmp_path).save(SessionState(views=(view,)))

    restored = StateStore(root=tmp_path).load()

    assert restored.views == (view,)
    assert restored.views[0].source == "ssh:web01/var/log/syslog"
    assert restored.views[0].merged == ("journal:all", "ssh:web01/var/log/syslog")


def test_a_saved_view_summary_names_a_remote_ref_by_its_last_component() -> None:
    from clv.storage import SavedView

    summary = SavedView(name="n", source="ssh:web01/var/log/syslog").summary()
    assert summary.endswith("syslog")
    assert "ssh:web01" not in summary


# --------------------------------------------------------------------------
# Two hosts, one path
# --------------------------------------------------------------------------


def test_two_remote_refs_with_one_path_are_distinct_identities() -> None:
    """Why a ref string is host-qualified, and why that is not cosmetics.

    ``ORIGIN_FIELD`` stores ``format_ref`` output on every entry of a merged
    set, so an unqualified ``/var/log/syslog`` would make
    ``source:/var/log/syslog`` match every machine at once.
    """

    web01 = _StubRemoteRef("web01", "/var/log/syslog")
    web02 = _StubRemoteRef("web02", "/var/log/syslog")

    assert web01 != web02
    assert len({web01, web02}) == 2
    assert format_ref(web01) != format_ref(web02)
    assert web01 == _StubRemoteRef("web01", "/var/log/syslog")
    assert len({web01, _StubRemoteRef("web01", "/var/log/syslog")}) == 1


def test_tag_origins_gives_two_hosts_two_origin_values() -> None:
    from clv.services.parsing import parse_lines
    from clv.services.session import ORIGIN_FIELD, tag_origins

    web01 = _StubRemoteRef("web01", "/var/log/syslog")
    web02 = _StubRemoteRef("web02", "/var/log/syslog")
    entries = parse_lines(["boom", "boom"])

    tagged = tag_origins(entries, [web01, web02])

    origins = [entry.fields[ORIGIN_FIELD] for entry in tagged]
    assert origins == ["ssh:web01/var/log/syslog", "ssh:web02/var/log/syslog"]
    assert origins[0] != origins[1]


def test_mark_keys_differ_for_one_line_read_from_two_hosts() -> None:
    """The two-host distinction has to reach marks, not just origins."""

    from clv.services.parsing import parse_lines

    entry = parse_lines(["identical line"])[0]
    web01 = _StubRemoteRef("web01", "/var/log/syslog")
    web02 = _StubRemoteRef("web02", "/var/log/syslog")

    assert mark_key(web01, entry) != mark_key(web02, entry)


@pytest.mark.parametrize(
    "ref",
    [
        "journal:all",
        "journal:unit/sshd.service",
        "ssh:web01/var/log/syslog",
    ],
)
def test_a_scheme_ref_string_carries_no_comma_and_no_nul(ref: str) -> None:
    """Two encodings the ref string shares with things that split on them.

    ``config.parse_log_dirs`` splits ``log_dirs`` on ``,`` and
    ``marks.mark_key`` separates on NUL. This binds **scheme** refs only: a
    local filename may legally contain a comma, and ``log_dirs`` already mangles
    one — pre-existing, and not this module's to fix.
    """

    assert "," not in ref
    assert "\0" not in ref


# --------------------------------------------------------------------------
# The seam guards
# --------------------------------------------------------------------------
#
# Two of them, because they fail for different reasons. The negative guard
# catches a `Path(...)` reappearing inside a function that reads persisted
# state; it cannot see a *new* boundary function that never had one. The
# positive guard is an explicit list, so adding a boundary means adding a line
# where a reviewer sees it. The maintenance cost is the feature.
#
# Both work on the AST rather than on lines. A line grep is not viable here:
# `Path(tagged)` in `session.origin_of` is three lines below the ORIGIN_FIELD
# read that taints it, and the literal text `Path(` appears in docstrings and
# comments across the package — including the one in `plugins/__init__.py`
# explaining this very rule. A grep would need a whitelist, and a whitelist is
# how a guard rots.

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "clv"

#: Reading one of these is reading state that came off disk, or off the entry
#: stream where `format_ref` output lives. Matched on the dotted attribute
#: chain, so `self.state.merged` and `app.state.merged` both count while the
#: method named `_merged_paths` does not.
_PERSISTED_READS = (
    "state.starred",
    "state.merged",
    "view.source",
    "view.merged",
    "self.source",  # SavedView.summary
    "self.merged",  # SavedView.summary
)
_PERSISTED_NAMES = ("ORIGIN_FIELD",)

#: Every function that crosses the persistence boundary, and the helper it must
#: name. `identity` is not listed: it is a comparison, not a boundary, and
#: several of these use it as well.
_BOUNDARY_FUNCTIONS = {
    ("clv/app.py", "_starred_paths"): "parse_ref",
    ("clv/app.py", "_merged_paths"): "parse_ref",
    ("clv/app.py", "_merged_display_paths"): "parse_ref",
    ("clv/app.py", "_merged_name"): "parse_ref",
    ("clv/app.py", "action_open_merged"): "parse_ref",
    ("clv/app.py", "_open_starred_on_launch"): "parse_ref",
    ("clv/app.py", "_origins"): "parse_ref",
    ("clv/app.py", "_apply_view"): "parse_ref",
    ("clv/app.py", "_capture_view"): "format_ref",
    ("clv/app.py", "action_toggle_star"): "ref_key",
    ("clv/app.py", "action_toggle_merge"): "ref_key",
    ("clv/app.py", "_sync_star_button"): "ref_key",
    ("clv/storage.py", "summary"): "parse_ref",
    ("clv/services/session.py", "tag_origins"): "format_ref",
    ("clv/services/session.py", "origin_of"): "parse_ref",
}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "?")
    return ".".join(reversed(parts))


def _reads_persisted_state(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and _dotted(node).endswith(_PERSISTED_READS):
            return True
        if isinstance(node, ast.Name) and node.id in _PERSISTED_NAMES:
            return True
    return False


def _called_names(func: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, list[str]]]:
    found = []
    for source in sorted(PACKAGE.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        relative = source.relative_to(REPO_ROOT).as_posix()
        found.extend(_functions_in(text, relative))
    return found


def _functions_in(
    text: str, relative: str
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, list[str]]]:
    tree = ast.parse(text, filename=relative)
    body = text.splitlines()
    return [
        (relative, node, body)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _bare_path_offenders(text: str, relative: str) -> set[str]:
    """Every ``Path(...)`` inside a function that reads persisted state.

    Split out of the test below so that the detector itself can be tested. A
    guard whose own breakage is invisible is not a guard: empty
    ``_PERSISTED_READS``, a broken ``_dotted``, or a changed call-name match
    would each make this return nothing forever, and the suite would stay green
    while the seam rotted.
    """

    offenders: set[str] = set()
    for _, func, body in _functions_in(text, relative):
        if not _reads_persisted_state(func):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else None
            )
            if name == "Path":
                offenders.add(f"{relative}:{node.lineno}: {body[node.lineno - 1].strip()}")
    return offenders


def test_no_bare_path_reconstructs_persisted_state() -> None:
    """A source read back from disk is built with ``parse_ref``, never ``Path``.

    ``Path(entry)`` over ``state.merged`` is how ``journal:unit/sshd.service``
    becomes ``/current/dir/journal:unit/sshd.service`` — silently, and only on
    the restart after the operator stopped watching. The seam that fixes it is
    one function call wide, so it can rot back in one line.

    Scoped to functions that actually *read* persisted state, so the two dozen
    legitimate ``Path(...)`` calls elsewhere in ``clv/`` — the config file, the
    settings template, ``os.walk`` results, the export destination, journald's
    own scheme literals — are never considered. It found exactly the ten sites
    this phase fixed, and nothing else.
    """

    offenders: set[str] = set()
    for source in sorted(PACKAGE.rglob("*.py")):
        relative = source.relative_to(REPO_ROOT).as_posix()
        offenders |= _bare_path_offenders(source.read_text(encoding="utf-8"), relative)

    assert not offenders, (
        "These functions read persisted source state and rebuild it with Path(). "
        "Use clv.services.refs.parse_ref: it is the exact inverse of format_ref "
        "and cannot turn an identifier into a path under the working directory. "
        "Found:\n  " + "\n  ".join(sorted(offenders))
    )


def test_every_persistence_boundary_names_its_ref_helper() -> None:
    """Each boundary says out loud which side of the seam it is on.

    The negative guard above cannot see a boundary that never had a ``Path()``
    in it — a new function that reads ``state.merged`` and does its own string
    munging would pass it. This one fails until the new function is listed,
    which puts the decision in front of a reviewer.
    """

    seen: dict[tuple[str, str], set[str]] = {}
    for relative, func, _ in _functions():
        key = (relative, func.name)
        if key in _BOUNDARY_FUNCTIONS:
            seen[key] = _called_names(func)

    missing = sorted(key for key in _BOUNDARY_FUNCTIONS if key not in seen)
    assert not missing, (
        "_BOUNDARY_FUNCTIONS names functions that no longer exist — renamed or "
        f"removed, and the list was not updated: {missing}"
    )

    wrong = sorted(
        f"{path}::{name} should call {helper}(), calls {sorted(seen[(path, name)] & {'parse_ref', 'format_ref', 'ref_key'}) or 'none of them'}"
        for (path, name), helper in _BOUNDARY_FUNCTIONS.items()
        if helper not in seen[(path, name)]
    )
    assert not wrong, "\n  ".join(["Persistence boundaries reaching past refs:", *wrong])


# --- and the guard that the guard still works -----------------------------
#
# Both tests above assert an *empty* result, which is what a broken detector
# also returns. These feed it code that must be caught, so the day
# `_PERSISTED_READS` is emptied or `_dotted` stops walking an attribute chain,
# something fails immediately instead of never.

_ROTTED = '''
from pathlib import Path

class App:
    def _starred_paths(self):
        return {Path(entry) for entry in self.state.starred}
'''

_ROTTED_ACROSS_LINES = '''
from pathlib import Path

ORIGIN_FIELD = "source"

class Session:
    def origin_of(self, entry):
        tagged = entry.fields.get(ORIGIN_FIELD)
        # several
        # lines
        # later
        if tagged:
            return Path(tagged)
        return None
'''

_INNOCENT = '''
from pathlib import Path

def user_config_path():
    """Path(...) in a docstring must not count, nor must a real config path."""
    # ...and neither must Path( in a comment
    return Path.home() / ".config" / "clv" / "settings.conf"

def _walk(root):
    return [Path(entry) for entry in root.iterdir()]
'''


@pytest.mark.parametrize(
    ("label", "source"),
    [("same line", _ROTTED), ("read and rebuild lines apart", _ROTTED_ACROSS_LINES)],
)
def test_the_bare_path_guard_catches_a_rotted_seam(label: str, source: str) -> None:
    """The detector must fail on code that reintroduces the bug.

    The second case is why this is an AST walk and not a grep: the
    ``ORIGIN_FIELD`` read that makes the function a boundary is five lines above
    the ``Path(tagged)`` that breaks it, so no single line looks wrong.
    """

    found = _bare_path_offenders(source, "rotted.py")
    assert found, f"the guard stopped catching a rotted seam ({label})"
    assert any("Path(" in entry for entry in found)


def test_the_bare_path_guard_ignores_paths_outside_the_boundary() -> None:
    """And it must stay quiet on the two dozen legitimate ``Path(...)`` calls.

    A guard that fires on the config file or an ``iterdir`` result gets a
    whitelist bolted on within a week, and a whitelist is how a guard rots.
    """

    assert _bare_path_offenders(_INNOCENT, "innocent.py") == set()


def test_the_boundary_map_is_not_silently_empty() -> None:
    """``_BOUNDARY_FUNCTIONS`` emptied would make its test pass vacuously."""

    assert len(_BOUNDARY_FUNCTIONS) >= 15
    assert _PERSISTED_READS and _PERSISTED_NAMES
