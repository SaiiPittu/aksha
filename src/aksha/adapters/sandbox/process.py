"""``ProcessGroupUnit``: a child process in its own process group, a ``KillableUnit`` (P18 D6).

The child starts in a new session (``start_new_session=True``), so it leads its own
process group and every process it forks is in it. Settling sends SIGTERM to the group;
terminating sends SIGKILL to the group, so a test runner's workers end with it. A settle
requested before :meth:`start` means the child never starts. Every method is safe to call
from another thread than the one running the body.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
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
        """Whether the child has exited, or was cancelled before it started."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return self._cancelled
        return proc.poll() is not None

    def wait(self, seconds: float) -> bool:
        """Block up to ``seconds`` for the child to exit; return :meth:`settled`."""
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                return False
            if proc.stdout is not None:
                proc.stdout.close()  # the child has exited: nothing more to read
        return self.settled()

    def _signal(self, sig: signal.Signals) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                self._cancelled = True
                return
        if proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):  # the group has already gone
            os.killpg(proc.pid, sig)
