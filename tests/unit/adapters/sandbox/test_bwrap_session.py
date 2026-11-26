"""The bubblewrap backend's session workspaces (decision 37): one stateful ``/work`` per session.

The box tests run a real program in a real box and are skipped, with the probe's reason,
where the backend does not probe available. The workspace tests (folders, git worktrees,
clean) need no box and always run; the git ones need ``git`` on the host.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.adapters.sandbox.workspace import SessionFolders, folder_name
from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.exec import ExecUnit
from aksha.runtime.sandbox.types import BoxRequest, BoxResult, ExecPolicy, Limits, SessionRef

PROBE = BwrapSandbox().probe()
needs_box = pytest.mark.skipif(not PROBE.available, reason=f"bwrap: {PROBE.reason}")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
QUICK = Limits(wall_ms=10_000)
S1 = SessionRef("o", "S-0001")
S2 = SessionRef("o", "S-0002")
WHOLE = ExecPolicy.of(writable=(".",), limits=QUICK)


class NoBlobs:
    def get(self, ref: str) -> bytes:
        raise KeyError(ref)


def sh(script: str) -> tuple[str, ...]:
    return ("sh", "-c", script)


def run(
    backend: BwrapSandbox,
    argv: tuple[str, ...],
    session: SessionRef | None = S1,
    *,
    policy: ExecPolicy | None = None,
    files: dict[str, bytes] | None = None,
    name: str = "R-1/t#a1/x1",
) -> BoxResult:
    request = BoxRequest(name, argv, policy or ExecPolicy.of(limits=QUICK), files=files or {},
                         session=session)  # fmt: skip
    return backend.run(request, NoBlobs(), ExecUnit())


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True)  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A synthetic git repository with one commit and an uncommitted change."""
    root = tmp_path / "SYN-repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "syn@example.org")
    git(root, "config", "user.name", "SYN")
    (root / "app.py").write_text("x = 1\n")
    git(root, "add", "app.py")
    git(root, "commit", "-q", "-m", "SYN first")
    (root / "app.py").write_text("x = 'uncommitted'\n")
    return root


# --- always: the workspaces themselves ------------------------------------------------------


def test_a_workspace_is_made_once_under_the_root_and_kept(tmp_path: Path) -> None:
    folders = SessionFolders(tmp_path / "sandbox")
    work, meta, created = folders.ensure(S1)
    assert created and work == tmp_path / "sandbox" / "o" / "S-0001" / "work" and work.is_dir()
    assert meta["origin"] == {"kind": "empty"}
    (work / "note.txt").write_text("kept")
    again, _, created_again = folders.ensure(S1)
    assert again == work and not created_again and (work / "note.txt").read_text() == "kept"
    assert folders.sessions() == (S1,)


def test_clean_removes_one_session_and_leaves_the_others(tmp_path: Path) -> None:
    folders = SessionFolders(tmp_path / "sandbox")
    one, _, _ = folders.ensure(S1)
    two, _, _ = folders.ensure(S2)
    (one / "a").write_text("1")
    (one / "a").chmod(0o000)
    assert folders.clean(S1) is not None
    assert not one.parent.exists() and two.is_dir()
    assert folders.clean(S1) is None and folders.sessions() == (S2,)


def test_names_that_are_not_plain_become_hashed_folders(tmp_path: Path) -> None:
    assert folder_name("S-ab12") == "S-ab12"
    for odd in ("..", "../x", "a/b", ".hidden", "x" * 200):
        name = folder_name(odd)
        assert name.startswith("h-") and "/" not in name
    folders = SessionFolders(tmp_path / "sandbox")
    work, _, _ = folders.ensure(SessionRef("../up", "a/b"))
    assert tmp_path / "sandbox" in work.parents
    # A plain name shaped like another session's hashed folder is hashed too, never shared.
    forged = folder_name("a/b")
    assert folder_name(forged) != forged
    other, _, _ = folders.ensure(SessionRef("../up", forged))
    assert other != work


def test_clean_never_changes_a_file_a_symlink_points_at(tmp_path: Path) -> None:
    outside = tmp_path / "SYN-outside.txt"
    outside.write_text("keep")
    outside.chmod(0o600)
    folders = SessionFolders(tmp_path / "sandbox")
    work, _, _ = folders.ensure(S1)
    locked = work / "locked"
    locked.mkdir()
    (locked / "link").symlink_to(outside)
    locked.chmod(0o500)  # the box took away write permission, so unlinking the link fails once
    assert folders.clean(S1) is not None
    assert not folders.home(S1).exists()
    assert outside.read_text() == "keep" and (outside.stat().st_mode & 0o777) == 0o600


def test_a_plain_folder_origin_is_copied_in_and_never_touched(tmp_path: Path) -> None:
    origin = tmp_path / "SYN-folder"
    origin.mkdir()
    (origin / "data.csv").write_text("SYN,1\n")
    folders = SessionFolders(tmp_path / "sandbox", origin)
    work, meta, _ = folders.ensure(S1)
    assert meta["origin"] == {"kind": "folder", "path": str(origin)}
    (work / "data.csv").write_text("changed")
    assert (origin / "data.csv").read_text() == "SYN,1\n"


def test_an_origin_that_is_not_a_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxRefused) as refused:
        SessionFolders(tmp_path / "sandbox", tmp_path / "missing").ensure(S1)
    assert refused.value.code == "workspace_unavailable"


@needs_git
def test_a_git_origin_is_a_worktree_on_its_own_branch(tmp_path: Path, repo: Path) -> None:
    folders = SessionFolders(tmp_path / "sandbox", repo)
    work, meta, _ = folders.ensure(S1)
    origin = meta["origin"]
    assert origin["kind"] == "git" and origin["branch"] == "aksha/S-0001"
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "aksha/S-0001"
    assert (work / "app.py").read_text() == "x = 1\n"  # the last commit, not the dirty file
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "app.py").read_text() == "x = 'uncommitted'\n"


@needs_git
def test_a_checkpoint_commits_on_the_session_branch_only(tmp_path: Path, repo: Path) -> None:
    folders = SessionFolders(tmp_path / "sandbox", repo)
    work, meta, _ = folders.ensure(S1)
    main_before = git(repo, "rev-parse", "main")
    assert folders.checkpoint(meta, "aksha: nothing") is None  # nothing changed
    (work / "new.py").write_text("y = 2\n")
    commit = folders.checkpoint(meta, "aksha: R-1/x1")
    assert commit is not None and git(repo, "rev-parse", "aksha/S-0001") == commit
    assert git(repo, "rev-parse", "main") == main_before
    assert git(repo, "log", "-1", "--format=%an %s", commit) == "aksha aksha: R-1/x1"


@needs_git
def test_a_checkpoint_ignores_a_git_file_the_box_rewrote(tmp_path: Path, repo: Path) -> None:
    """The git directory is the one recorded at creation, never what ``.git`` says now."""
    other = tmp_path / "SYN-other"
    other.mkdir()
    git(other, "init", "-q")
    folders = SessionFolders(tmp_path / "sandbox", repo)
    work, meta, _ = folders.ensure(S1)
    (work / ".git").write_text(f"gitdir: {other / '.git'}\n")
    (work / "f.txt").write_text("f")
    commit = folders.checkpoint(meta, "aksha: x")
    assert commit is not None and git(repo, "rev-parse", "aksha/S-0001") == commit
    assert subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                          capture_output=True).returncode != 0  # fmt: skip


@needs_git
def test_clean_keeps_the_branch_and_a_new_workspace_continues_it(
    tmp_path: Path, repo: Path
) -> None:
    folders = SessionFolders(tmp_path / "sandbox", repo)
    work, meta, _ = folders.ensure(S1)
    (work / "kept.py").write_text("k = 1\n")
    commit = folders.checkpoint(meta, "aksha: x")
    removed = folders.clean(S1)
    assert removed is not None and removed["kept_branch"] == "aksha/S-0001"
    assert not work.exists() and git(repo, "rev-parse", "aksha/S-0001") == commit
    assert str(work) not in git(repo, "worktree", "list")
    again, meta2, _ = folders.ensure(S1)
    assert meta2["origin"]["continued"] and (again / "kept.py").read_text() == "k = 1\n"


# --- a real box ---------------------------------------------------------------------------


@needs_box
def test_what_one_exec_writes_is_there_for_the_next(tmp_path: Path) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox")
    first = run(backend, sh("echo one > out/state.txt"))
    assert first.status == "ok" and dict(first.collected) == {"out/state.txt": b"one\n"}
    second = run(backend, sh("cat out/state.txt; echo two > out/more.txt"))
    assert second.stdout == b"one\n"
    assert dict(second.collected) == {"out/more.txt": b"two\n"}  # only what it wrote
    assert (tmp_path / "sandbox" / "o" / "S-0001" / "work" / "out" / "state.txt").is_file()


@needs_box
def test_the_workspace_is_recorded_and_sessions_are_apart(tmp_path: Path) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox")
    one = run(backend, sh("echo a > out/a"))
    assert one.workspace is not None and one.workspace["created"] is True
    assert one.workspace["folder"] == str(tmp_path / "sandbox" / "o" / "S-0001" / "work")
    assert run(backend, ("true",)).workspace["created"] is False  # type: ignore[index]
    other = run(backend, sh("ls out"), S2)
    assert other.stdout == b"" and backend.sessions() == (S1, S2)
    assert backend.clean(S1) is not None and backend.sessions() == (S2,)


@needs_box
def test_a_request_with_no_session_is_still_a_fresh_box(tmp_path: Path) -> None:
    scratch = tmp_path / "boxes"
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox", scratch=scratch)
    got = run(backend, sh("echo x > out/a"), None)
    assert got.status == "ok" and got.workspace is None
    assert list(scratch.iterdir()) == [] and not (tmp_path / "sandbox").exists()


@needs_box
def test_the_policy_still_holds_in_a_session_workspace(tmp_path: Path) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox")
    got = run(backend, sh("echo x > top.txt; echo y > out/ok.txt"))
    assert dict(got.collected) == {"out/ok.txt": b"y\n"}
    assert any(d.kind == "write" for d in got.denials)


@needs_box
def test_the_storage_budget_counts_what_the_exec_adds(tmp_path: Path) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox")
    policy = ExecPolicy.of(limits=Limits(wall_ms=10_000, max_collect_bytes=3_000_000))
    for i in range(2):  # each adds 2 MB: the session holds 4 MB, over the cap, and both pass
        got = run(backend, sh(f"head -c 2000000 /dev/zero > out/f{i}"), policy=policy)
        assert got.status == "ok", got.denials
    over = run(backend, sh("head -c 3500000 /dev/zero > out/big; sleep 5"), policy=policy)
    assert over.status == "output_rejected"


@needs_box
def test_an_input_never_follows_a_symlink_the_box_left(tmp_path: Path) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox")
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "sandbox" / "o" / "S-0001" / "work"
    run(backend, sh(f"ln -s {outside} out/link; ln -s {outside}/f out/file"))
    got = run(backend, sh("cat out/file; cat out/link/x"),
              files={"out/file": b"in", "out/link/x": b"in2"})  # fmt: skip
    assert got.stdout == b"inin2" and list(outside.iterdir()) == []
    assert not (work / "out" / "link").is_symlink()


@needs_git
def test_an_input_over_the_worktree_git_file_is_refused(tmp_path: Path, repo: Path) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox", origin=repo)
    if not PROBE.available:
        pytest.skip(f"bwrap: {PROBE.reason}")
    with pytest.raises(SandboxRefused) as refused:
        run(backend, ("true",), policy=WHOLE, files={".git": b"gitdir: /elsewhere\n"})
    assert refused.value.code == "exec_invalid"
    work = backend.folders.home(S1) / "work" if backend.folders else None
    assert work is not None and (work / ".git").read_text().startswith("gitdir: ")
    assert "elsewhere" not in (work / ".git").read_text()


@needs_box
@needs_git
def test_a_git_workspace_runs_checkpoints_and_keeps_git_read_only(
    tmp_path: Path, repo: Path
) -> None:
    backend = BwrapSandbox(workspaces=tmp_path / "sandbox", origin=repo)
    got = run(backend, sh("cat app.py; echo 'x = 2' > app.py; echo evil > .git"), policy=WHOLE)
    assert got.stdout.startswith(b"x = 1") and any(d.kind == "write" for d in got.denials)
    assert dict(got.collected) == {"app.py": b"x = 2\n"}
    record = got.workspace
    assert record is not None and record["origin"]["kind"] == "git"
    assert "gitdir" not in record["origin"]
    assert record["checkpoint"] == git(repo, "rev-parse", "aksha/S-0001")
    assert git(repo, "show", "aksha/S-0001:app.py") == "x = 2"
    assert (repo / "app.py").read_text() == "x = 'uncommitted'\n"
    assert "checkpoint" not in (run(backend, ("true",), policy=WHOLE).workspace or {})
