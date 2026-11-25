"""``ProcessGroupUnit``: a child process in its own process group, a ``KillableUnit`` (P18 D6).

The child starts in a new session (``start_new_session=True``), so it leads its own
process group and every process it forks is in it. Settling sends SIGTERM to the group;
terminating sends SIGKILL to the group, so a test runner's workers end with it. A settle
requested before :meth:`start` means the child never starts. The unit has settled only
when the whole group is gone: a leader that exits while a process it forked runs on in
the group has not. Every method is safe to call from another thread than the one running
the body.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path


class ProcessGroupUnit:
    """One child process group the host can ask to stop and then end."""

    def __init__(self, argv: Sequence[str], *, cwd: Path | None = None) -> None:
        """Run ``argv`` (in ``cwd``) when :meth:`start` is called; stdout is a pipe."""
        self._argv = tuple(argv)
        self._cwd = cwd
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False

    def start(self) -> None:
        """Start the child, unless a settle was already requested."""
        with self._lock:
            if self._cancelled or self._proc is not None:
                return
            self._proc = subprocess.Popen(
                self._argv,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

    def ready_line(self) -> str | None:
        """The child's first line of output, stripped; None if it never started or wrote."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        line = proc.stdout.readline()
        return line.strip() if line else None

    @property
    def returncode(self) -> int | None:
        """The child's exit status (negative for a signal), or None while running or unstarted."""
        proc = self._proc
        return None if proc is None else proc.poll()

    def request_settle(self) -> None:
        """SIGTERM to the group; before start, mark the unit so it never starts."""
        self._signal(signal.SIGTERM)

    def terminate(self) -> None:
        """SIGKILL to the group."""
        self._signal(signal.SIGKILL)

    def settled(self) -> bool:
        """Whether every process of the group has exited, or the unit was cancelled unstarted."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return self._cancelled
        return proc.poll() is not None and not _group_alive(proc.pid)

    def wait(self, seconds: float) -> bool:
        """Block up to ``seconds`` for the whole group to exit; return :meth:`settled`."""
        proc = self._proc
        if proc is None:
            return self.settled()
        until = time.monotonic() + seconds
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        while _group_alive(proc.pid):  # the leader has gone; a forked process may not have
            if time.monotonic() >= until:
                return False
            time.sleep(0.01)
        if proc.stdout is not None:
            proc.stdout.close()  # the group has exited: nothing more to read
        return self.settled()

    def _signal(self, sig: signal.Signals) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                self._cancelled = True
                return
        proc.poll()  # reap an exited leader, so its group answers for the rest alone
        with contextlib.suppress(ProcessLookupError):  # the group has already gone
            os.killpg(proc.pid, sig)


def _group_alive(pgid: int) -> bool:
    """Whether any process is still in group ``pgid`` (signal 0 checks, sends nothing)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # a member exists that this process may not signal
    return True
