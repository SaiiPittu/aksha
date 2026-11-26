"""The runtime thread: the one writer in server mode (ADR-P19 D4).

Interfaces never touch the writer's store handle. They :meth:`RuntimeThread.submit` a
named command and get a future that resolves with its receipt once the command commits,
or :meth:`RuntimeThread.call` a function of the session (the capability record, a status
read through the writer) and get a future of its result. The thread, in order:

1. Opens the session (``open_session``, so the writer handle and its lock belong to this
   thread), resumes every owner with an open run (recovery, then each in-flight run to
   its yield), then loops.
2. Drains the command queue: each command runs alone, in its own transaction.
3. Runs one pass of one movable run per owner with open runs or pending timers (ADR-P25:
   each pass ticks the owner first, so a due timer fires with no run open). A pass is one
   stop-policy evaluation and at most one turn, so step 2 comes round again within one turn.
4. With nothing queued and nothing movable, waits on the queue.

A pass that raises is recorded in :attr:`RuntimeThread.faults` and its owner is left
alone until the next start, so one broken run cannot spin the writer; the event store
still holds everything the run committed, and ``aksha resume`` picks it up.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from typing import Any

from aksha.runtime.session.named import NamedCommand
from aksha.runtime.session.service import Session
from aksha.runtime.session.types import CommandReceipt

IDLE_WAIT_S = 0.05
"""How long an idle runtime thread waits on its queue before it looks for work again."""


@dataclass(frozen=True)
class Fault:
    """A pass that raised: the owner it was for and the error's text."""

    owner: str
    error: str


class RuntimeThread:
    """The single writer of server mode (module doc)."""

    def __init__(self, open_session: Callable[[], Session]) -> None:
        """Run the session ``open_session`` returns, once :meth:`start` is called."""
        self._open = open_session
        self._queue: queue.Queue[tuple[Callable[[Session], Any], Future[Any]]] = queue.Queue()
        self._stopping = threading.Event()
        self._ready: Future[None] = Future()
        self._thread = threading.Thread(target=self._main, name="aksha-runtime", daemon=True)
        self.faults: list[Fault] = []
        self._faulted: set[str] = set()
        self._commanded: set[str] = set()
        """Owners a command was dispatched for since the last pass (runtime thread only)."""

    def start(self, timeout_s: float | None = 30.0) -> None:
        """Start the thread and wait until its session is open; re-raise its opening error."""
        self._thread.start()
        self._ready.result(timeout=timeout_s)

    def submit(self, command: NamedCommand) -> Future[CommandReceipt]:
        """Queue ``command``; the future resolves with its receipt at its commit.

        Its owner gets the next pass even with no open run, so the rebuild runs a recorded
        source change calls for (an upload) open and move without another command.
        """

        def run(s: Session) -> CommandReceipt:
            self._commanded.add(command.owner)
            return s.dispatch(command)

        return self.call(run)

    def call[T](self, fn: Callable[[Session], T]) -> Future[T]:
        """Run ``fn`` on the runtime thread between passes; the future holds its result."""
        future: Future[T] = Future()
        if self._stopping.is_set():
            future.set_exception(RuntimeError("the runtime thread is stopping"))
            return future
        self._queue.put((fn, future))
        return future

    def stop(self, timeout_s: float = 5.0) -> None:
        """Finish the current pass, then exit and close the session's store."""
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(timeout_s)

    @property
    def alive(self) -> bool:
        """Whether the thread is running."""
        return self._thread.is_alive()

    def _main(self) -> None:
        try:
            session = self._open()
        except BaseException as e:
            self._ready.set_exception(e)
            return
        try:
            for owner in session.store.owners_with_open_runs():
                self._guard(owner, partial(session.resume, owner))
            self._ready.set_result(None)
            while not self._stopping.is_set():
                drained = self._drain(session)
                moved = self._pass(session)
                if not drained and not moved:
                    self._wait(session)
        finally:
            self._fail_queued()
            session.store.close()

    def _drain(self, session: Session) -> bool:
        drained = False
        while True:
            try:
                fn, future = self._queue.get_nowait()
            except queue.Empty:
                return drained
            drained = True
            self._run(session, fn, future)

    def _wait(self, session: Session) -> None:
        try:
            fn, future = self._queue.get(timeout=IDLE_WAIT_S)
        except queue.Empty:
            return
        self._run(session, fn, future)

    @staticmethod
    def _run(session: Session, fn: Callable[[Session], Any], future: Future[Any]) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn(session))
        except BaseException as e:  # the caller gets the error; the writer keeps running
            future.set_exception(e)

    def _pass(self, session: Session) -> bool:
        moved = False
        store = session.store
        waiting = store.owners_with_pending_timers()
        owners = sorted(set(store.owners_with_open_runs()) | set(waiting) | self._commanded)
        self._commanded.clear()
        for owner in owners:
            if owner in self._faulted:
                continue
            moved = bool(self._guard(owner, partial(session.step, owner))) or moved
        return moved

    def _guard(self, owner: str, fn: Callable[[], object]) -> object:
        try:
            return fn()
        except Exception as e:  # recorded; the owner waits for a restart or ``aksha resume``
            self._faulted.add(owner)
            self.faults.append(Fault(owner, f"{type(e).__name__}: {e}"))
            return False

    def _fail_queued(self) -> None:
        while True:
            try:
                _, future = self._queue.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                future.set_exception(RuntimeError("the runtime thread stopped"))
