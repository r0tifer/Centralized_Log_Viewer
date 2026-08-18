"""The filesystem seam: one contract, and the two rules that make it worth having.

``clv/services/backend.py`` exists so that "read this source" stops meaning
"call ``os``". The tests here are in four groups, and the first is the one with
a life beyond this phase.

**The contract suite** (:class:`BackendContract`) is written against the
protocol and knows nothing about the local filesystem. Phase 4 of
``SSH_TODO.md`` subclasses it, supplies a remote backend and a remote workspace,
and inherits every assertion below unedited. That is what makes "parity" a
passing test rather than a claim: a remote regression fails the same assertion
its local twin passes.

**The degradation tests** pin what happens when a backend cannot produce a
stable identity — an SFTP client's attributes carry no inode, and a BusyBox
``stat`` may not offer one. Rotation detection degrades to shrink-only, and it
degrades in the *conservative* direction. That is a documented fact here rather
than a surprise in the field.

**The blocking guard** is the Requirement 3 regression test. ``poll()`` runs on
a timer at ``refresh_hz``, so a backend method that blocks there is a UI frozen
twice a second. The guard makes it an exception instead, and this proves the
exception actually fires — through the real ``SourceReader`` → ``SourceBuffer``
path, not a mock of it.

**The seam guard** is an AST walk asserting nothing in ``clv/services/`` reaches
past the backend to ``os`` again. Same shape as the guard in ``test_refs.py``,
including the two tests that prove the detector itself still works: a guard that
asserts an empty result is indistinguishable from a broken one.
"""

from __future__ import annotations

import ast
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from clv.services.backend import (
    GUARANTEED_CHEAP,
    LOCAL,
    MAY_BLOCK,
    PROTOCOL_METHODS,
    BackendCapabilities,
    BlockingCallError,
    LocalBackend,
    blocking,
    blocking_methods,
    cheap,
    cheap_only,
)
from clv.services.reader import SourceReader
from clv.services.session import SourceBuffer


# ==========================================================================
# The contract suite
# ==========================================================================


@dataclass
class Workspace:
    """A tree a backend can read, however it happens to be materialised.

    The contract suite never touches the filesystem directly — it asks the
    workspace to make a file and gets back a ref. A remote subclass supplies one
    that writes over its own connection, and every test below is then reading a
    remote tree with no edit.
    """

    root: object
    #: ``(relative_path, text) -> ref``. Creates parent directories.
    write: Callable[[str, str], object]
    #: ``(relative_path) -> ref`` for a directory.
    mkdir: Callable[[str], object]
    #: ``(relative_path) -> None``. Removes a file or a whole directory.
    remove: Callable[[str], None]
    #: ``(relative_path) -> ref`` without creating anything.
    ref: Callable[[str], object]
    #: ``(from_relative, to_relative) -> None``. Renames within the workspace.
    #:
    #: Exists so a test can rotate a log the way ``logrotate`` does — moving it
    #: aside rather than deleting it — which is both the realistic shape and the
    #: only one that reliably yields a **new inode**. See
    #: :meth:`BackendContract.test_identity_changes_when_the_name_points_at_a_new_file`.
    rename: Callable[[str, str], None]


class BackendContract:
    """Every assertion a ``SourceBackend`` must satisfy, whatever it reads.

    Not collected: the name does not start with ``Test``. Subclass it, override
    the two fixtures, and the whole suite runs against the new backend.
    """

    @pytest.fixture
    def backend(self):
        raise NotImplementedError("a contract subclass supplies its backend")

    @pytest.fixture
    def workspace(self) -> Workspace:
        raise NotImplementedError("a contract subclass supplies its workspace")

    # --- walk --------------------------------------------------------------

    def test_walk_finds_every_file_once_with_its_size(self, backend, workspace) -> None:
        workspace.write("a.log", "alpha\n")
        workspace.write("nested/b.log", "bravo bravo\n")
        workspace.write("nested/deeper/c.log", "c\n")

        entries = list(backend.walk(workspace.root))

        by_name = {entry.ref.name: entry for entry in entries}
        assert sorted(by_name) == ["a.log", "b.log", "c.log"]
        assert len(entries) == 3, "a file must be yielded exactly once"
        assert by_name["a.log"].size == len("alpha\n")
        assert by_name["b.log"].size == len("bravo bravo\n")
        assert all(entry.unreadable is False for entry in entries)

    def test_walk_yields_files_and_never_directories(self, backend, workspace) -> None:
        workspace.mkdir("empty")
        workspace.write("nested/b.log", "bravo\n")

        names = {entry.ref.name for entry in backend.walk(workspace.root)}

        assert names == {"b.log"}, "walk yields files only; directories are derived"

    def test_walk_returns_a_true_iterator(self, backend, workspace) -> None:
        """Structurally lazy: a backend that built a list cannot satisfy this.

        The weaker half of the laziness contract, and the one that holds even
        when a backend's underlying source is a single command's output.
        """

        workspace.write("a.log", "alpha\n")

        walker = backend.walk(workspace.root)

        assert iter(walker) is walker

    def test_walk_does_not_pay_for_what_the_caller_never_asks_for(
        self, backend, workspace
    ) -> None:
        """The half that matters: ``max_files`` must bound work, not just output.

        Observed without counting syscalls, so it holds for any backend: pull
        one entry, delete the rest of the tree, and count what still arrives. An
        eager walk captured everything before the deletion and hands back ten;
        a lazy one never looks in the directory that is now gone.
        """

        for index in range(5):
            workspace.write(f"a/f{index}.log", "x\n")
            workspace.write(f"z/g{index}.log", "x\n")

        walker = backend.walk(workspace.root)
        first = next(walker)
        workspace.remove("z")
        rest = list(walker)

        assert first.ref.name.startswith("f")
        assert len([first, *rest]) < 10, "the walk was eager; max_files cannot bound it"

    def test_walk_of_a_missing_root_yields_nothing_and_does_not_raise(
        self, backend, workspace
    ) -> None:
        """Unlike ``list_dir``. The caller reports an unreadable *root*; a
        directory that vanished mid-walk is not worth taking a pass down for."""

        assert list(backend.walk(workspace.ref("nope"))) == []

    # --- kind --------------------------------------------------------------

    def test_kind_tells_a_file_from_a_directory_from_nothing(
        self, backend, workspace
    ) -> None:
        log = workspace.write("a.log", "alpha\n")
        folder = workspace.mkdir("nested")

        assert backend.kind(log) == "file"
        assert backend.kind(folder) == "dir"
        assert backend.kind(workspace.ref("nope")) == "missing"

    # --- access ------------------------------------------------------------

    def test_access_agrees_with_a_real_read(self, backend, workspace) -> None:
        log = workspace.write("a.log", "alpha\n")

        assert backend.access(log, os.R_OK) is True
        with backend.open(log, "rb") as handle:
            assert handle.read() == b"alpha\n"

    def test_access_is_false_for_something_that_is_not_there(
        self, backend, workspace
    ) -> None:
        assert backend.access(workspace.ref("nope"), os.R_OK) is False

    # --- open --------------------------------------------------------------

    def test_open_returns_a_seekable_binary_handle(self, backend, workspace) -> None:
        """Seekability is not incidental. ``read_last_lines`` seeks to the end
        and steps backwards, and ``zipfile`` refuses a non-seekable stream."""

        log = workspace.write("a.log", "alpha\nbravo\n")

        with backend.open(log, "rb") as handle:
            assert handle.seekable() is True
            handle.seek(0, os.SEEK_END)
            assert handle.tell() == len("alpha\nbravo\n")
            handle.seek(6)
            assert handle.read() == b"bravo\n"

    def test_open_raises_oserror_for_a_missing_ref(self, backend, workspace) -> None:
        with pytest.raises(OSError):
            backend.open(workspace.ref("nope"), "rb")

    # --- stat and identity --------------------------------------------------

    def test_stat_matches_what_open_reads(self, backend, workspace) -> None:
        log = workspace.write("a.log", "alpha\nbravo\n")

        info = backend.stat(log)

        assert info is not None
        with backend.open(log, "rb") as handle:
            assert info.size == len(handle.read())
        assert info.mtime_ns > 0

    def test_stat_of_a_missing_ref_is_none_rather_than_an_exception(
        self, backend, workspace
    ) -> None:
        """``poll()`` calls this on every tick. A source that vanished must be
        a quiet ``None``, not an exception the timer has to catch."""

        assert backend.stat(workspace.ref("nope")) is None

    def test_stat_tracks_growth(self, backend, workspace) -> None:
        log = workspace.write("a.log", "alpha\n")
        before = backend.stat(log)
        workspace.write("a.log", "alpha\nbravo\n")
        after = backend.stat(log)

        assert before is not None and after is not None
        assert after.size > before.size

    def test_identity_is_stable_for_one_file_and_distinguishes_two(
        self, backend, workspace
    ) -> None:
        first = workspace.write("a.log", "alpha\n")
        second = workspace.write("b.log", "alpha\n")

        if not backend.capabilities.stable_identity:
            assert backend.identity(first) is None
            pytest.skip("backend declares no stable identity; see the degradation tests")

        assert backend.identity(first) == backend.identity(first)
        assert backend.identity(first) != backend.identity(second)

    def test_identity_survives_the_file_being_appended_to(
        self, backend, workspace
    ) -> None:
        """The property rotation detection rests on: growing is not becoming a
        different file. Its absence is exactly why ``(size, mtime)`` cannot
        stand in for an inode."""

        log = workspace.write("a.log", "alpha\n")
        if not backend.capabilities.stable_identity:
            pytest.skip("backend declares no stable identity")

        before = backend.identity(log)
        workspace.write("a.log", "alpha\nbravo\n")

        assert backend.identity(log) == before

    def test_identity_changes_when_the_name_points_at_a_new_file(
        self, backend, workspace
    ) -> None:
        """The log is moved aside, not deleted — which is what rotation *is*.

        This used to unlink ``a.log`` and write it again, and that is not a
        reliable way to obtain a new inode: a filesystem is free to hand the
        just-freed one straight back, and ext4 routinely does. The assertion
        therefore held on tmpfs and btrfs and failed on the CI runner, which is
        the worst possible distribution of a test failure.

        Renaming instead keeps the old inode alive under the rotated name, so a
        new one is guaranteed everywhere — and it models what actually happens:
        ``logrotate`` moves ``app.log`` to ``app.log.1`` and creates a fresh
        ``app.log`` beside it.
        """

        log = workspace.write("a.log", "alpha\n")
        if not backend.capabilities.stable_identity:
            pytest.skip("backend declares no stable identity")

        before = backend.identity(log)
        workspace.rename("a.log", "a.log.1")
        workspace.write("a.log", "alpha\n")

        assert backend.identity(log) != before

    def test_identity_of_a_missing_ref_is_none(self, backend, workspace) -> None:
        assert backend.identity(workspace.ref("nope")) is None

    # --- list_dir -----------------------------------------------------------

    def test_list_dir_is_one_level_and_includes_directories(
        self, backend, workspace
    ) -> None:
        workspace.write("a.log", "alpha\n")
        workspace.write("nested/b.log", "bravo\n")

        names = {ref.name for ref in backend.list_dir(workspace.root)}

        assert names == {"a.log", "nested"}, "one level, and not files only"

    def test_list_dir_raises_where_walk_would_skip(self, backend, workspace) -> None:
        """The whole reason it exists: ``check_access`` reports this error to
        the operator, so it must not be swallowed the way ``walk`` swallows it."""

        with pytest.raises(OSError):
            list(backend.list_dir(workspace.ref("nope")))

    # --- the declared costs -------------------------------------------------

    def test_capabilities_match_the_marks_on_the_methods(self, backend) -> None:
        """The declaration is derived, so it cannot drift from the code."""

        assert backend.capabilities.blocking == blocking_methods(type(backend))

    def test_nothing_guaranteed_cheap_is_declared_blocking(self, backend) -> None:
        """``poll()`` calls these on every tick, on every backend."""

        assert not (backend.capabilities.blocking & GUARANTEED_CHEAP)

    def test_every_declared_blocking_method_actually_refuses_under_the_guard(
        self, backend, workspace
    ) -> None:
        """A mark that does not fire is a comment with extra steps."""

        declared = backend.capabilities.blocking
        if not declared:
            pytest.skip("this backend declares nothing blocking")

        with cheap_only():
            for name in sorted(declared):
                with pytest.raises(BlockingCallError):
                    _call_with_placeholder_arguments(
                        getattr(backend, name), workspace.ref("a.log")
                    )

    def test_the_cheap_methods_still_work_under_the_guard(
        self, backend, workspace
    ) -> None:
        log = workspace.write("a.log", "alpha\n")

        with cheap_only():
            assert backend.stat(log) is not None
            backend.identity(log)

    def test_reachability_answers_under_the_guard_and_says_what_it_knows(
        self, backend, workspace
    ) -> None:
        """Requirement 3, extended to the member the status line reads.

        Asked on every render and on every empty pane, so it has to answer from
        inside the guard — and it has to answer *honestly*, which for a backend
        with nothing wrong means saying so rather than declining to say. A
        backend that could only report trouble would leave "is this source live"
        unanswerable, which is the question the pane is asking.
        """

        with cheap_only():
            state = backend.reachability()

        assert state.state in {"connected", "connecting", "unreachable", "disabled"}
        assert state.ok is (state.state == "connected")

    def test_a_working_backend_reports_itself_reachable(
        self, backend, workspace
    ) -> None:
        """Nothing has gone wrong, so nothing may claim it has.

        The paired assertion to the failure cases: a reachability that defaulted
        to pessimism would put a warning beside every healthy source, and a
        warning that is always there is one nobody reads.
        """

        log = workspace.write("a.log", "alpha\n")
        assert backend.stat(log) is not None

        state = backend.reachability()
        assert state.ok
        assert state.reason == ""
        assert not state.exhausted


def _call_with_placeholder_arguments(method, ref):
    """Call *method* with whatever shape its name implies.

    Only reached inside ``cheap_only()``, where the guard raises before the
    arguments are ever looked at — so these need to be well-shaped, not
    meaningful.
    """

    name = getattr(method, "__name__", "")
    if name == "access":
        return method(ref, os.R_OK)
    if name == "walk":
        return list(method(ref))
    if name == "list_dir":
        return list(method(ref))
    return method(ref)


# --------------------------------------------------------------------------
# ...run against the one backend that exists today
# --------------------------------------------------------------------------


def _local_workspace(root: Path) -> Workspace:
    def write(relative: str, text: str) -> Path:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def mkdir(relative: str) -> Path:
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        return target

    def remove(relative: str) -> None:
        target = root / relative
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()

    def rename(source: str, destination: str) -> None:
        (root / source).rename(root / destination)

    return Workspace(
        root=root,
        write=write,
        mkdir=mkdir,
        remove=remove,
        ref=lambda rel: root / rel,
        rename=rename,
    )


class TestLocalBackend(BackendContract):
    """The contract, against ``LocalBackend``. Phase 4 adds a sibling."""

    @pytest.fixture
    def backend(self) -> LocalBackend:
        return LOCAL

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Workspace:
        root = tmp_path / "tree"
        root.mkdir()
        return _local_workspace(root)

    # --- and what only a local filesystem can be asked ----------------------

    def test_local_declares_every_call_cheap(self, backend) -> None:
        """The claim this whole phase rests on for existing behaviour: nothing
        local got slower, because nothing local was ever a round trip."""

        assert backend.capabilities.blocking == frozenset()
        assert backend.capabilities.stable_identity is True

    def test_kind_never_claims_a_file_it_cannot_see(self, tmp_path: Path) -> None:
        """A file under an untraversable parent is refused, never asserted.

        Which refusal it is depends on the interpreter, and that split is
        **older than this seam**: ``Path.exists()`` raises ``PermissionError``
        on 3.11 and returns ``False`` from 3.12 on, so ``check_access`` already
        said "permission denied" on one and "does not exist" on the other.
        ``kind`` reproduces each faithfully rather than papering over it —
        smoothing it here would be a behaviour change in a phase that is
        forbidden one. What must hold on every version is that the answer is a
        refusal.
        """

        if os.geteuid() == 0:
            pytest.skip("root traverses anything")
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "a.log").write_text("alpha\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            assert LOCAL.kind(locked / "a.log") in {"denied", "missing"}
        finally:
            locked.chmod(0o700)

    def test_denied_is_reachable_and_is_not_missing(self) -> None:
        """The ``denied`` branch is not dead code, and does not mean ``missing``.

        Driven from a ref that raises rather than from a ``chmod``, so it holds
        on 3.11 and 3.14 alike and on a filesystem that ignores modes. It is
        ``check_access`` that needs the distinction: "permission denied" and
        "does not exist" are different instructions to the operator.
        """

        class Denying:
            def is_dir(self):
                raise PermissionError(13, "Permission denied")

            def is_file(self):  # pragma: no cover - is_dir raises first
                raise PermissionError(13, "Permission denied")

            def exists(self):  # pragma: no cover - is_dir raises first
                raise PermissionError(13, "Permission denied")

        class Unclassifiable(Denying):
            def is_dir(self):
                raise OSError(5, "I/O error")

        assert LOCAL.kind(Denying()) == "denied"
        assert LOCAL.kind(Unclassifiable()) == "other"
        assert LOCAL.kind(Path("/nonexistent-by-construction")) == "missing"

    def test_kind_reports_other_for_something_that_is_neither(
        self, tmp_path: Path
    ) -> None:
        fifo = tmp_path / "pipe"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError, NotImplementedError):
            pytest.skip("no fifos on this platform")

        assert LOCAL.kind(fifo) == "other"

    def test_walk_order_is_alphabetical_and_depth_first(self, tmp_path: Path) -> None:
        """Pinned because ``max_files`` truncation is *defined* by this order.

        ``LocalBackend.walk`` reimplements the traversal over ``os.scandir``
        rather than ``os.walk`` — a ``DirEntry`` carries its own stat, so an
        entry costs one syscall instead of two. Reproducing the order exactly is
        the price of that, and this is where it is paid.
        """

        for relative in ("b.log", "a.log", "zed/z.log", "mid/m.log", "mid/deep/d.log"):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n", encoding="utf-8")

        found = [
            str(entry.ref.relative_to(tmp_path)) for entry in LOCAL.walk(tmp_path)
        ]

        assert found == [
            "a.log",
            "b.log",
            os.path.join("mid", "m.log"),
            os.path.join("mid", "deep", "d.log"),
            os.path.join("zed", "z.log"),
        ]

    def test_a_symlinked_directory_is_never_yielded_as_a_file(
        self, tmp_path: Path
    ) -> None:
        """``os.walk`` classifies it as a directory and simply does not descend.
        Yielding it as a file instead would put a directory in the tree."""

        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "a.log").write_text("alpha\n", encoding="utf-8")
        try:
            os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no usable symlinks")

        names = [entry.ref.name for entry in LOCAL.walk(tmp_path)]

        assert names == ["a.log"], "the symlinked directory leaked into the file list"

    def test_a_symlink_cycle_terminates_when_links_are_followed(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a.log").write_text("alpha\n", encoding="utf-8")
        try:
            os.symlink(root, root / "sub" / "loop", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no usable symlinks")

        seen: set[object] = set()
        found = list(LOCAL.walk(root, follow_symlinks=True, seen=seen))

        assert [entry.ref.name for entry in found] == ["a.log"]

    def test_the_visited_set_is_shared_across_walks(self, tmp_path: Path) -> None:
        """Two configured roots that overlap must not walk the shared subtree
        twice — which is why the set is the caller's, not the walk's."""

        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "a.log").write_text("alpha\n", encoding="utf-8")

        seen: set[object] = set()
        first = list(LOCAL.walk(tmp_path, follow_symlinks=True, seen=seen))
        second = list(LOCAL.walk(tmp_path, follow_symlinks=True, seen=seen))

        assert [entry.ref.name for entry in first] == ["a.log"]
        assert second == [], "the second walk re-entered a directory already seen"

    def test_an_unlistable_subdirectory_is_skipped_not_raised(
        self, tmp_path: Path
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root lists anything")
        (tmp_path / "ok.log").write_text("alpha\n", encoding="utf-8")
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "hidden.log").write_text("nope\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            names = [entry.ref.name for entry in LOCAL.walk(tmp_path)]
        finally:
            locked.chmod(0o700)

        assert names == ["ok.log"]


class DeclaredBlockingBackend(LocalBackend):
    """Reads locally, but declares ``open`` blocking — a remote's *shape*.

    Exists so the contract suite runs against a backend whose costs differ from
    ``LocalBackend``'s **today**, rather than the reusability being a promise
    redeemed in Phase 4. It is also the only thing that exercises the contract's
    own "every declared-blocking method actually refuses" assertion, which would
    otherwise sit skipped and rot.
    """

    @blocking
    def open(self, ref, mode: str = "rb"):
        return ref.open(mode)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name="declared-blocking",
            blocking=blocking_methods(DeclaredBlockingBackend),
            stable_identity=True,
            access_hint="this one is not local",
        )


class TestDeclaredBlockingBackend(BackendContract):
    """The same contract, a different cost profile, not one assertion edited.

    This is the mechanism Phase 4 inherits: ``RemoteBackend`` replaces the two
    fixtures and everything above runs against a machine on the other end of an
    SSH connection.
    """

    @pytest.fixture
    def backend(self) -> DeclaredBlockingBackend:
        return DeclaredBlockingBackend()

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Workspace:
        root = tmp_path / "tree"
        root.mkdir()
        return _local_workspace(root)

    def test_it_declares_the_cost_local_does_not(self, backend) -> None:
        assert backend.capabilities.blocking == frozenset({"open"})
        assert LOCAL.capabilities.blocking == frozenset()


# ==========================================================================
# Degradation: a backend with no stable identity
# ==========================================================================


class NoIdentityBackend(LocalBackend):
    """Local reads, but no inode — the shape of an SFTP client's attributes.

    Phase 4 needs this twice over: a remote whose ``stat`` has no ``%i``, and
    the fallback if the shell transport is ever traded for a library one. What
    it costs is pinned below rather than discovered.
    """

    @cheap
    def stat(self, ref):
        info = super().stat(ref)
        return None if info is None else type(info)(info.size, info.mtime_ns, None)

    @cheap
    def identity(self, ref):
        return None

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name="no-identity",
            blocking=blocking_methods(NoIdentityBackend),
            stable_identity=False,
            access_hint="nothing to be done about it",
        )


def _rotate(path: Path, text: str) -> None:
    """Rotate *path* the way ``logrotate`` does: move it aside, create it again.

    **Not** unlink-then-write. That asks the filesystem for a new inode without
    anything obliging it to provide one — ext4 hands the just-freed inode
    straight back, so the "rotation" was invisible to ``(st_dev, st_ino)`` and
    the test failed on CI while passing on tmpfs. Keeping the old file alive
    under its rotated name forces a genuinely new inode on every filesystem, and
    is what a real rotation looks like anyway.
    """

    path.rename(path.with_name(path.name + ".1"))
    path.write_text(text, encoding="utf-8")


# ==========================================================================
# Continuity: the assumption behind every incremental read
# ==========================================================================
#
# Tailing rests on one claim — *the bytes at [offset, size) continue the file we
# were reading* — and until now nothing checked it. `stat` cannot: an inode can
# be reused, and `copytruncate` does not change the inode at all. Both cases
# below produced **silently wrong output**: the pane showed a fragment of the
# new file and dropped the rest, which is the one thing Requirement 2 of
# AGENTS.md forbids.
#
# `SourceReader` now re-reads the last `ANCHOR_SIZE` bytes as part of the read it
# was already making, and re-primes when they are not what it left there.


def test_copytruncate_that_refills_fast_does_not_lose_lines(tmp_path: Path) -> None:
    """The common one, and it never involves an inode.

    ``logrotate copytruncate`` copies the log and truncates it **in place** —
    same inode, so the identity test is irrelevant. The shrink test covers it
    only while the file is still shorter than the offset we hold; a small log
    that is rewritten past that between two polls slips through both.

    Before the anchor this returned ``rotated=False`` and the single line
    ``['new 3']``, silently discarding the two before it.
    """

    log = tmp_path / "a.log"
    log.write_text("old 1\nold 2\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10)
    reader.prime()

    with log.open("w", encoding="utf-8") as handle:   # truncate + refill, same inode
        handle.write("new 1\nnew 2\nnew 3\n")

    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["new 1", "new 2", "new 3"]


class ReusedInodeBackend(LocalBackend):
    """Local reads, but every file reports the same identity.

    Simulates what ext4 does when a log is deleted and recreated: the freed
    inode is handed straight back, so ``(st_dev, st_ino)`` cannot tell the new
    file from the old.

    Faked rather than provoked because it is a *filesystem* behaviour, and the
    suite has to assert the same thing on tmpfs, btrfs and ext4. Provoking it
    for real is what produced a test that passed on every developer machine and
    failed on CI — see :func:`_rotate`.
    """

    _FIXED = ("reused", "inode")

    @cheap
    def stat(self, ref):
        info = super().stat(ref)
        return None if info is None else type(info)(
            info.size, info.mtime_ns, self._FIXED
        )

    @cheap
    def identity(self, ref):
        return self._FIXED if super().identity(ref) is not None else None


def test_a_reused_inode_does_not_lose_lines(tmp_path: Path) -> None:
    """The one the CI failure led to.

    A deleted-and-recreated log can be handed the very same inode back, so
    ``(st_dev, st_ino)`` says "same file". If the replacement also passes the old
    offset before the next poll, the shrink test says nothing either — and the
    reader carries on from an offset that means nothing in the new file.

    The identity is held constant by the backend above, so this asserts the
    *anchor* is what catches it on every filesystem, rather than passing on a
    tmpfs that happened to allocate a fresh inode.
    """

    log = tmp_path / "a.log"
    log.write_text("old 1\nold 2\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10, backend=ReusedInodeBackend())
    reader.prime()

    log.unlink()
    log.write_text("new 1\nnew 2\nnew 3\n", encoding="utf-8")

    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["new 1", "new 2", "new 3"]


def test_an_ordinary_append_is_never_mistaken_for_a_replacement(
    tmp_path: Path,
) -> None:
    """The other half, and the one that would make the fix worse than the bug.

    A check that fires on ordinary growth would re-prime on every poll — the
    pane would flicker, the parser would reset, and watch rules would re-fire.
    This is the assertion that keeps the anchor honest.
    """

    log = tmp_path / "a.log"
    log.write_text("line 1\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10)
    reader.prime()

    for index in range(2, 12):
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"line {index}\n")
        result = reader.poll()
        assert result.rotated is False, f"append {index} read as a replacement"
        assert result.lines == [f"line {index}"]


def test_the_anchor_survives_a_write_larger_than_itself(tmp_path: Path) -> None:
    """A burst bigger than ``ANCHOR_SIZE`` must still anchor to its own tail."""

    log = tmp_path / "a.log"
    log.write_text("start\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=500)
    reader.prime()

    with log.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"padding line {index}\n" for index in range(50)))
    assert reader.poll().rotated is False

    with log.open("a", encoding="utf-8") as handle:
        handle.write("after\n")
    result = reader.poll()

    assert result.rotated is False
    assert result.lines == ["after"]


def test_a_file_shorter_than_the_anchor_still_tails(tmp_path: Path) -> None:
    """Near the start of a file there are not ``ANCHOR_SIZE`` bytes to look back
    over. The check uses what exists rather than refusing to read."""

    log = tmp_path / "a.log"
    log.write_text("a\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10)
    reader.prime()

    with log.open("a", encoding="utf-8") as handle:
        handle.write("b\n")
    result = reader.poll()

    assert result.rotated is False
    assert result.lines == ["b"]


def test_the_anchor_costs_no_extra_open(tmp_path: Path) -> None:
    """It rides along in the read that was happening anyway.

    The check exists to be affordable at ``refresh_hz`` per merged source; one
    that doubled the syscalls would be paid for on every poll of every source.
    """

    log = tmp_path / "a.log"
    log.write_text("line 1\n", encoding="utf-8")

    opens = 0

    class Counting(LocalBackend):
        @cheap
        def open(self, ref, mode: str = "rb"):
            nonlocal opens
            opens += 1
            return super().open(ref, mode)

    reader = SourceReader(log, max_lines=10, backend=Counting())
    reader.prime()

    with log.open("a", encoding="utf-8") as handle:
        handle.write("line 2\n")
    before = opens
    reader.poll()

    assert opens - before == 1, "the continuity check cost an extra open"


def test_the_rotation_helper_keeps_the_old_inode_alive(tmp_path: Path) -> None:
    """The guard on the mechanism the two tests below depend on.

    ``_rotate`` must **move** the old log aside rather than delete it. An inode
    is not freed while a link to it exists, so keeping one guarantees the
    replacement gets a different inode — on every filesystem, by the rule rather
    than by luck. Unlink-then-write has no such guarantee: ext4 hands the
    just-freed inode straight back, which is exactly how this suite came to pass
    on tmpfs and fail on CI.

    Asserted here because the difference is invisible in the tests that rely on
    it: they would simply start failing on some machines and not others.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    before = log.stat().st_ino

    _rotate(log, "bravo\n")

    rotated = tmp_path / "a.log.1"
    assert rotated.exists(), "_rotate deleted the old log instead of moving it"
    assert rotated.stat().st_ino == before, "the old inode did not survive"
    assert log.stat().st_ino != before


def test_a_stable_identity_catches_a_same_size_rotation(tmp_path: Path) -> None:
    """The direction that works, so the pair below means something."""

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10)
    reader.prime()

    _rotate(log, "bravo\n")
    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["bravo"]


def test_without_a_stable_identity_a_same_size_rotation_is_missed(
    tmp_path: Path,
) -> None:
    """Documented, not hidden.

    ``(size, mtime)`` cannot be substituted for an inode here: it changes on
    every ordinary append, so using it would report a reload twice a second on a
    live log. The honest degradation is to miss the replacement that leaves the
    size unchanged, and to say so in ``capabilities.stable_identity`` — which is
    what lets Phase 5 report it rather than let an operator infer it.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10, backend=NoIdentityBackend())
    reader.prime()

    _rotate(log, "bravo\n")
    result = reader.poll()

    assert result.rotated is False
    assert result.lines == []


def test_without_a_stable_identity_a_shrink_is_still_caught(tmp_path: Path) -> None:
    """The fallback is useful, not merely safe: truncation still reloads."""

    log = tmp_path / "a.log"
    log.write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10, backend=NoIdentityBackend())
    reader.prime()

    log.write_text("delta\n", encoding="utf-8")
    result = reader.poll()

    assert result.rotated is True
    assert result.lines == ["delta"]


def test_without_a_stable_identity_appends_still_stream(tmp_path: Path) -> None:
    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=10, backend=NoIdentityBackend())
    reader.prime()

    with log.open("a", encoding="utf-8") as handle:
        handle.write("bravo\n")
    result = reader.poll()

    assert result.rotated is False, "an append must not be mistaken for a rotation"
    assert result.lines == ["bravo"]


# ==========================================================================
# The Requirement 3 guard
# ==========================================================================


class SlowBackend(LocalBackend):
    """Reads locally but declares ``open`` blocking — a remote's shape.

    ``open`` genuinely sleeps, so a test that reached it would be measurably
    slower rather than merely wrong. The point is that nothing under the guard
    ever does.
    """

    DELAY = 0.25

    @blocking
    def open(self, ref, mode: str = "rb"):
        time.sleep(self.DELAY)
        return ref.open(mode)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name="slow",
            blocking=blocking_methods(SlowBackend),
            stable_identity=True,
            access_hint="wait for it",
        )


def _buffer_on(log: Path, backend) -> SourceBuffer:
    reader = SourceReader(log, max_lines=100, backend=backend)
    buffer = SourceBuffer(log, max_lines=100, reader=reader)
    buffer.prime()
    return buffer


def test_priming_may_block_because_it_runs_in_a_worker(tmp_path: Path) -> None:
    """The escape hatch has to work, or the guard is just a ban.

    ``prime()`` is driven from ``run_worker(thread=True)``, as discovery already
    is. It is allowed to take a round trip; that is the whole design.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")

    buffer = _buffer_on(log, SlowBackend())

    assert [entry.raw for entry in buffer.entries] == ["alpha"]


def test_a_poll_that_finds_nothing_costs_nothing(tmp_path: Path) -> None:
    """``stat`` is cheap on every backend, so an idle tick never opens."""

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    buffer = _buffer_on(log, SlowBackend())

    outcome = buffer.poll()

    assert outcome.entries == []


def test_a_blocking_read_from_the_poll_path_raises_instead_of_freezing(
    tmp_path: Path,
) -> None:
    """The regression test this phase exists for.

    Not a mock of the poll path — the real ``SourceReader.poll`` reaching
    ``_read_from`` after the file grew, driven through the real
    ``SourceBuffer.poll`` the app's timer calls. On a remote backend that
    ``open`` is a round trip on the event loop, at ``refresh_hz``, per merged
    source. Here it is an exception with a name.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    buffer = _buffer_on(log, SlowBackend())

    with log.open("a", encoding="utf-8") as handle:
        handle.write("bravo\n")

    with pytest.raises(BlockingCallError) as caught:
        buffer.poll()

    assert "SlowBackend.open" in str(caught.value)
    assert "worker thread" in str(caught.value)


def test_the_same_read_outside_the_poll_path_is_allowed(tmp_path: Path) -> None:
    """Proof the guard is the context and not the backend: identical call,
    no guard, no exception."""

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    reader = SourceReader(log, max_lines=100, backend=SlowBackend())
    reader.prime()
    with log.open("a", encoding="utf-8") as handle:
        handle.write("bravo\n")

    assert reader.poll().lines == ["bravo"]


def test_a_local_backend_is_untouched_by_the_guard(tmp_path: Path) -> None:
    """What this phase cost the existing behaviour: nothing."""

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    buffer = _buffer_on(log, LOCAL)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("bravo\n")

    outcome = buffer.poll()

    assert [entry.raw for entry in outcome.entries] == ["bravo"]


def test_the_guard_is_per_thread(tmp_path: Path) -> None:
    """A worker must not inherit a guard the event loop is holding.

    Otherwise the recommended fix — "drive it from a thread" — would be
    forbidden by the mechanism that recommends it.
    """

    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    backend = SlowBackend()
    outcome: list[object] = []

    def worker() -> None:
        try:
            with backend.open(log, "rb") as handle:
                outcome.append(handle.read())
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
            outcome.append(exc)

    with cheap_only():
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        with pytest.raises(BlockingCallError):
            backend.open(log, "rb")

    assert outcome == [b"alpha\n"]


def test_the_guard_nests_without_unsetting_itself(tmp_path: Path) -> None:
    log = tmp_path / "a.log"
    log.write_text("alpha\n", encoding="utf-8")
    backend = SlowBackend()

    with cheap_only():
        with cheap_only():
            pass
        with pytest.raises(BlockingCallError):
            backend.open(log, "rb")

    with backend.open(log, "rb") as handle:
        assert handle.read() == b"alpha\n"


# --- and the declaration itself --------------------------------------------


def test_an_unmarked_method_is_refused(tmp_path: Path) -> None:
    """A method that declares no cost is a caller guessing."""

    class Unmarked(LocalBackend):
        def open(self, ref, mode: str = "rb"):
            return ref.open(mode)

    with pytest.raises(TypeError, match="neither @cheap nor @blocking"):
        blocking_methods(Unmarked)


def test_a_guaranteed_cheap_method_may_not_be_declared_blocking() -> None:
    """``stat`` is called from ``poll()`` on every backend. A backend that
    cannot make it cheap is a reason to change the reader, not the contract."""

    class Wrong(LocalBackend):
        @blocking
        def stat(self, ref):
            return LocalBackend.stat(self, ref)

    with pytest.raises(TypeError, match="must be cheap"):
        blocking_methods(Wrong)


def test_a_missing_method_is_refused() -> None:
    class Partial:
        @cheap
        def stat(self, ref):
            return None

    with pytest.raises(TypeError, match="does not implement"):
        blocking_methods(Partial)


def test_the_protocol_split_covers_every_method_exactly_once() -> None:
    assert GUARANTEED_CHEAP | MAY_BLOCK == frozenset(PROTOCOL_METHODS)
    assert not (GUARANTEED_CHEAP & MAY_BLOCK)
    assert GUARANTEED_CHEAP == frozenset({"stat", "identity", "reachability"})


def test_local_resolves_to_itself() -> None:
    """``LOCAL`` is both the default backend and the default resolver, which is
    what lets every signature in this phase take one keyword instead of two."""

    assert LOCAL.for_ref(Path("/var/log/syslog")) is LOCAL


# ==========================================================================
# The seam guard
# ==========================================================================
#
# Same shape as the persistence guard in `test_refs.py`, and here for the same
# reason: the seam is a handful of call sites wide, and a later change that
# reaches past it to `os` would be invisible in review and silent at runtime —
# right up until the first remote source, where it reads the *local* machine.
#
# An AST walk rather than a grep, because `os.walk` and `.open("rb")` both
# appear in prose in these modules, including in the docstrings explaining this
# very rule.

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES = REPO_ROOT / "clv" / "services"

#: The module that is *allowed* to call `os` directly. It is the implementation
#: of the seam; everything else goes through it.
SEAM_OWNER = "backend.py"

#: Direct filesystem access against a *source*. Deliberately not a ban on `os`
#: wholesale: `os.SEEK_END`, `os.fspath` and `os.R_OK` are not IO, and
#: `config.py` reading the settings file is not reading a source.
_BANNED_CALLS = ("walk", "access", "scandir", "stat", "listdir")


def _os_offenders(text: str, relative: str) -> set[str]:
    """Every ``os.<io>()`` call and every ``.open("rb")`` outside the seam.

    Split out so the detector can itself be tested — a guard that asserts an
    empty set is indistinguishable from a guard that stopped looking.
    """

    offenders: set[str] = set()
    body = text.splitlines()
    for node in ast.walk(ast.parse(text, filename=relative)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not isinstance(target, ast.Attribute):
            continue

        described = f"{relative}:{node.lineno}: {body[node.lineno - 1].strip()}"

        if (
            isinstance(target.value, ast.Name)
            and target.value.id == "os"
            and target.attr in _BANNED_CALLS
        ):
            offenders.add(described)
            continue

        if target.attr == "open" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in ("rb", "r"):
                offenders.add(described)
    return offenders


def test_nothing_in_services_reaches_past_the_backend_to_os() -> None:
    """Reading a source goes through a ``SourceBackend``. Always.

    The failure this prevents is not a crash. A ``path.open("rb")`` that creeps
    back into ``reader.py`` works perfectly — on local files — and silently
    reads *this* machine when handed a remote ref, which is the worst available
    outcome for a viewer whose whole claim is that it is centralized.
    """

    offenders: set[str] = set()
    for source in sorted(SERVICES.rglob("*.py")):
        if source.name == SEAM_OWNER:
            continue
        relative = source.relative_to(REPO_ROOT).as_posix()
        offenders |= _os_offenders(source.read_text(encoding="utf-8"), relative)

    assert not offenders, (
        "These call the filesystem directly instead of going through the "
        "SourceBackend. Use backend.walk / .access / .stat / .open, and add the "
        "keyword to the signature if it is not there yet. Found:\n  "
        + "\n  ".join(sorted(offenders))
    )


_ROTTED_OS = '''
import os

def skip_reason(path, root, settings, *, backend=None):
    if not os.access(path, os.R_OK):
        return "unreadable"
    return None
'''

_ROTTED_OPEN = '''
def looks_binary(path, *, backend=None):
    with path.open("rb") as handle:
        return b"\\x00" in handle.read(8192)
'''

_INNOCENT_OS = '''
import os

#: os.walk and path.open("rb") in a comment must not count.
def read_last_lines(path, max_lines, *, backend=None):
    """Nor must os.access( in a docstring."""
    with backend.open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        return handle.tell()

def settings_text(path):
    return path.read_text(encoding="utf-8")

def hint(backend):
    return backend.capabilities.access_hint
'''


@pytest.mark.parametrize(
    ("label", "source"),
    [("os call", _ROTTED_OS), ("path.open", _ROTTED_OPEN)],
)
def test_the_seam_guard_catches_a_rotted_seam(label: str, source: str) -> None:
    assert _os_offenders(source, "rotted.py"), f"the guard stopped catching {label}"


def test_the_seam_guard_ignores_what_is_not_source_io() -> None:
    """``os.SEEK_END`` is a constant, ``read_text`` on the settings file is not
    a source, and a backend-mediated ``open`` is the thing being asked for."""

    assert _os_offenders(_INNOCENT_OS, "innocent.py") == set()


def test_the_banned_call_list_is_not_silently_empty() -> None:
    assert len(_BANNED_CALLS) >= 4
    assert SEAM_OWNER == "backend.py"
