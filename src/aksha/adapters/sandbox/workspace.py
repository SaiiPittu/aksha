"""``SessionFolders``: one persistent workspace folder per session (decision 37).

A local backend binds the session's folder as ``/work``, so what one exec writes is there
for the next. The folders live under one root in the data directory (``AKSHA_HOME`` or the
per-user data folder, never ``/tmp``), so a crash or a reboot keeps them:
``<root>/<owner>/<session>/work`` is the workspace and ``workspace.json`` beside it says
where it came from. Owner and session become folder names as they are when they are plain
(letters, digits, ``.``, ``_``, ``-``, and not starting ``h-``), else as a hash of
themselves, so a plain name can never land on another session's hashed folder.

Lifecycle: created on first use, kept after the run, removed by :meth:`clean` (``aksha
sandbox clean <session>``, or erasing the session when that is built).

What a new workspace starts from is the backend's ``origin``:

- **A git repository** gets a git worktree of it on its own branch, ``aksha/<session>``,
  from the repository's ``HEAD`` (an existing branch of that name is checked out instead,
  so a cleaned session continues from its commits). The user's own checkout is never
  touched. After each exec that changed the worktree, the host commits everything as one
  checkpoint on that branch, with hooks off and the git directory named explicitly, so
  nothing the box wrote can redirect it. The worktree's ``.git`` file is bound read-only in
  the box. Cleaning removes the worktree and keeps the branch and its commits for the user.
- **Any other folder** is copied in once (symlinks kept as links).
- **None** is an empty folder.

The box cannot run git on the worktree itself: its ``.git`` points at the repository's git
directory, which is not in the box.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.types import SessionRef

WORKSPACE_UNAVAILABLE = "workspace_unavailable"
META = "workspace.json"
WORK = "work"
BRANCH_PREFIX = "aksha/"
GIT_TIMEOUT_S = 120.0
_PLAIN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "aksha",
    "GIT_AUTHOR_EMAIL": "aksha@localhost",
    "GIT_COMMITTER_NAME": "aksha",
    "GIT_COMMITTER_EMAIL": "aksha@localhost",
    "GIT_TERMINAL_PROMPT": "0",
}
_GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "commit.gpgSign=false")  # fmt: skip


def folder_name(part: str) -> str:
    """``part`` as one folder name: itself when plain, else ``h-`` and a hash of it."""
    if _PLAIN.match(part) and ".." not in part and not part.startswith("h-"):
        return part
    return "h-" + hashlib.sha256(part.encode()).hexdigest()[:24]


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_GIT_ENV)
    return subprocess.run(["git", *_GIT_SAFE, *args], cwd=cwd, capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, env=env, check=False)  # fmt: skip


def _git_top(origin: Path) -> Path | None:
    """The top of the git working tree ``origin`` is, or None when it is not one."""
    if shutil.which("git") is None or not origin.is_dir():
        return None
    done = _git("rev-parse", "--show-toplevel", cwd=origin)
    if done.returncode != 0:
        return None
    top = Path(done.stdout.strip())
    return top if top.resolve() == origin.resolve() else None


class SessionFolders:
    """The session workspaces under ``root``, each started from ``origin`` (module doc)."""

    def __init__(self, root: Path, origin: Path | None = None) -> None:
        """Keep workspaces under ``root``; start each new one from ``origin``."""
        self.root = root
        self.origin = origin
        self._locks: dict[SessionRef, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock(self, ref: SessionRef) -> threading.Lock:
        """The lock one session's boxes take, so two never run in one workspace at once."""
        with self._guard:
            return self._locks.setdefault(ref, threading.Lock())

    def home(self, ref: SessionRef) -> Path:
        """The session's folder (the workspace is its ``work``)."""
        return self.root / folder_name(ref.owner) / folder_name(ref.session)

    def meta(self, ref: SessionRef) -> dict[str, Any] | None:
        """What ``workspace.json`` records of the session, or None when it has no workspace."""
        try:
            return dict(json.loads((self.home(ref) / META).read_text()))
        except (OSError, ValueError):
            return None

    def ensure(self, ref: SessionRef) -> tuple[Path, dict[str, Any], bool]:
        """The session's workspace, made from the origin on first use; whether it was made.

        Raises ``workspace_unavailable`` when the origin cannot give one.
        """
        home = self.home(ref)
        work = home / WORK
        meta = self.meta(ref)
        if meta is not None and work.is_dir():
            return work, meta, False
        home.mkdir(parents=True, exist_ok=True)
        _remove(work)  # a half-made workspace from a crash during creation
        meta = {"owner": ref.owner, "session": ref.session, "folder": str(work),
                **self._make(ref, work)}  # fmt: skip
        tmp = home / (META + ".tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True))
        tmp.replace(home / META)
        return work, meta, True

    def _make(self, ref: SessionRef, work: Path) -> dict[str, Any]:
        origin = self.origin
        if origin is None:
            work.mkdir(parents=True)
            return {"origin": {"kind": "empty"}}
        if not origin.is_dir():
            raise SandboxRefused(WORKSPACE_UNAVAILABLE, f"the workspace origin {origin} is "
                                 "not a folder")  # fmt: skip
        top = _git_top(origin)
        if top is None:
            shutil.copytree(origin, work, symlinks=True)
            return {"origin": {"kind": "folder", "path": str(origin)}}
        return {"origin": self._worktree(ref, top, work)}

    def _worktree(self, ref: SessionRef, repo: Path, work: Path) -> dict[str, Any]:
        branch = BRANCH_PREFIX + ref.session
        if _git("check-ref-format", "--branch", branch, cwd=repo).returncode != 0:
            branch = BRANCH_PREFIX + folder_name(ref.session)
        _git("worktree", "prune", cwd=repo)
        exists = _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo)
        if exists.returncode == 0:
            made = _git("worktree", "add", str(work), branch, cwd=repo)
        else:
            made = _git("worktree", "add", "-b", branch, str(work), "HEAD", cwd=repo)
        if made.returncode != 0:
            first = (made.stderr.strip().splitlines() or ["no output"])[-1]
            raise SandboxRefused(WORKSPACE_UNAVAILABLE, f"git worktree add failed: {first}")
        gitdir = _git("rev-parse", "--absolute-git-dir", cwd=work).stdout.strip()
        base = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
        return {"kind": "git", "path": str(repo), "branch": branch, "base": base,
                "gitdir": gitdir, "continued": exists.returncode == 0}  # fmt: skip

    def protected(self, meta: Mapping[str, Any]) -> tuple[str, ...]:
        """What the box never writes under ``/work``, whatever the policy: a worktree's ``.git``."""
        return (".git",) if meta.get("origin", {}).get("kind") == "git" else ()

    def checkpoint(self, meta: Mapping[str, Any], message: str) -> str | None:
        """Commit everything in a git workspace as one checkpoint; its commit, or None.

        None when the workspace is not a worktree, nothing changed, or git failed; the
        files stay in the folder either way.
        """
        origin = meta.get("origin", {})
        if origin.get("kind") != "git":
            return None
        named = ("--git-dir", str(origin["gitdir"]), "--work-tree", str(meta["folder"]))
        if _git(*named, "add", "-A").returncode != 0:
            return None
        if _git(*named, "diff", "--cached", "--quiet").returncode == 0:
            return None
        done = _git(*named, "commit", "--no-verify", "--quiet", "-m", message)
        if done.returncode != 0:
            return None
        return _git(*named, "rev-parse", "HEAD").stdout.strip() or None

    def sessions(self) -> tuple[SessionRef, ...]:
        """Every session with a workspace, by owner and session."""
        out: list[SessionRef] = []
        if not self.root.is_dir():
            return ()
        for meta_file in sorted(self.root.glob(f"*/*/{META}")):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                meta = json.loads(meta_file.read_text())
                out.append(SessionRef(str(meta["owner"]), str(meta["session"])))
        return tuple(out)

    def clean(self, ref: SessionRef) -> dict[str, Any] | None:
        """Remove the session's workspace (module doc); what was removed, or None."""
        home = self.home(ref)
        if not home.exists():
            return None
        with self.lock(ref):
            meta = self.meta(ref) or {"owner": ref.owner, "session": ref.session}
            origin = meta.get("origin", {})
            if origin.get("kind") == "git":
                repo = Path(str(origin["path"]))
                _git("worktree", "remove", "--force", str(home / WORK), cwd=repo)
                _remove(home)
                _git("worktree", "prune", cwd=repo)
                meta["kept_branch"] = origin.get("branch")
            else:
                _remove(home)
        return meta


def _remove(path: Path) -> None:
    """Delete ``path``, even where a program took away permissions."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return

    def unlock(func: Callable[..., object], name: str, _exc: object) -> None:
        # Restore the parent's permissions and, for a real folder, its own; never chmod
        # through a symlink the box left, which would change a file outside the folder.
        with contextlib.suppress(OSError):
            if os.path.abspath(name) != os.path.abspath(path):
                os.chmod(os.path.dirname(name), 0o700)
            if not os.path.islink(name):
                os.chmod(name, 0o700)
            func(name)

    shutil.rmtree(path, onexc=unlock)
