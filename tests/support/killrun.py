"""A run that a real ``SIGKILL`` ends after its Nth commit, for ``test_sigkill_recovery.py``.

``python -m tests.support.killrun <dir> <n>`` opens a SQLite store in ``dir``, starts run
``R-0001`` and drives it; right after the ``n``-th append commits, the process sends
itself ``SIGKILL``, so nothing in Python runs after that commit. It prints
``COMPLETED_WITHOUT_KILL`` if the run has fewer than ``n`` commits. :func:`recover` is the
next process: recovery on startup, then the lazy resume, exactly as a restart does them.

The run: a model call; a read (``at_least_once``) that fails transiently once and is
retried; a hook with no ledger (``at_most_once``); an effectful ``file`` step through the
outbox to a file ledger (``reconcile``); and a ``handoff`` command whose entry the outbox
worker drains. Every body appends a line to ``bodies.log`` (synced), and the ledger is a
JSON file, so the parent can count what really happened in the outside world.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy
from aksha.runtime.outbox.outbox import enqueue, pending_outbox
from aksha.runtime.outbox.types import EffectSpec, SystemReceipt
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.runs import RunSummary
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session.commands import dispatch
from aksha.runtime.session.types import Command, CommandAccepted, CommandRefused, CommandSpec
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START, TEST_TYPES
from tests.support.steps import HASH, started_run

H1 = "sha256:" + "1" * 64
LOADED = {"a": H1}
SEND = EffectSpec(system="mailer", key_fields=("/document", "/to"), semantics="reconcile")
PACKET = {"document": "packet/P1@v2", "to": "r-7"}
COMPLETED = "COMPLETED_WITHOUT_KILL"
QUICK = Backoff(base_ms=1, factor=2, cap_ms=1)


def catalogue() -> Catalogue:
    return Catalogue((*CORE_EVENT_TYPES, *TEST_TYPES))


def mark(d: Path, label: str) -> None:
    """Record that a body ran, durably, before it returns."""
    with open(d / "bodies.log", "a", encoding="utf-8") as f:
        f.write(label + "\n")
        f.flush()
        os.fsync(f.fileno())


class FileLedger:
    """A system of record in a JSON file: one row per idempotency key."""

    def __init__(self, d: Path) -> None:
        self.d = d
        self.path = d / "ledger.json"

    def rows(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def ledger_id(self) -> str:
        return "mailer-1"

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        rows = self.rows()
        if key not in rows:
            rows[key] = {"message_id": f"m-{len(rows) + 1}"}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            os.replace(tmp, self.path)
        mark(self.d, "apply")
        return SystemReceipt(key, self.ledger_id(), rows[key])

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        rows = self.rows()
        return SystemReceipt(key, self.ledger_id(), rows[key]) if key in rows else None


def _decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
    out = enqueue(view, "send", command.args, SEND, session=command.session,
                  command_id=command.command_id)  # fmt: skip
    return CommandAccepted(() if out.event is None else (out.event,))


HANDOFF = CommandSpec("handoff", "U5", _decide)


def worker(d: Path, store: EventStore, clock: FakeClock) -> OutboxWorker:
    return OutboxWorker(store, "o", clock, {"mailer": FileLedger(d)})


class Driver:
    """The run's code. Driving it again after a restart reaches the recorded steps first."""

    def __init__(self, d: Path, store: EventStore, clock: FakeClock) -> None:
        self.d, self.store, self.clock = d, store, clock

    def __call__(self, run: RunSummary, steps: DurableStepExecutor) -> None:
        d = self.d

        def sid(n: int) -> StepId:
            return StepId(run.run, "a", turn=1, call=n)

        def model(deadline: Deadline) -> StepOutput:
            mark(d, "model")
            return StepOutput({"text": "hi"}, usage={"input_tokens": 3})

        def read(deadline: Deadline) -> StepOutput:
            mark(d, "read")
            if (d / "bodies.log").read_text().split().count("read") == 1:
                raise TransientStepError(message="busy")
            return StepOutput({"page": 1})

        def hook(deadline: Deadline) -> StepOutput:
            mark(d, "hook")
            return StepOutput({"status": 202})

        steps.step(sid(1), "model", "draft", HASH, model, StepPolicy())
        steps.step(sid(2), "tool", "fetch", HASH, read, StepPolicy(backoff=QUICK))
        steps.step(sid(3), "tool", "hook", HASH, hook, StepPolicy(semantics="at_most_once"))
        steps.effect_step(sid(4), "file", PACKET, SEND)
        dispatch(self.store, Command("handoff", "o", "s", PACKET, command_id="c-1"), HANDOFF)
        worker(d, self.store, self.clock).drain()
        ended = NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run=run.run)
        with contextlib.suppress(AlreadyRecorded):
            self.store.append("o", "U3", (ended,))


def child(d: Path, kill_after: int) -> None:
    clock = FakeClock(START)
    store = open_store(d / "s.sqlite", clock, writer=True, catalogue=catalogue())
    real = store.append
    count = {"n": 0}

    def append(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        count["n"] += 1
        if count["n"] == kill_after:
            os.kill(os.getpid(), signal.SIGKILL)
        return out

    store.append = append  # type: ignore[method-assign]
    store.append("o", "U1", (started_run("R-0001", definition_hash=H1),))
    steps = StepExecutor(store, "o", clock, outbox=worker(d, store, clock))
    Driver(d, store, clock)(RunSummary("R-0001", "a", "request"), steps)
    print(COMPLETED, flush=True)


def recover(d: Path) -> dict[str, Any]:
    """Restart: recover on startup, then resume the owner; return what the world saw."""
    clock = FakeClock(START)
    store = open_store(d / "s.sqlite", clock, writer=True, catalogue=catalogue())
    try:
        recover_on_startup(store, LOADED, outbox=lambda o: worker(d, store, clock), owners=("o",))
        resume_owner(store, "o", Driver(d, store, clock), clock=clock, loaded=LOADED,
                     outbox=worker(d, store, clock))  # fmt: skip
        events = list(store.read("o"))
        bodies = (d / "bodies.log").read_text().split() if (d / "bodies.log").exists() else []
        hook = [e.payload for e in events
                if e.type == "step.completed" and str(e.step).endswith("c03")]  # fmt: skip
        return {
            "completed": [str(e.step) for e in events if e.type == "step.completed"],
            "bodies": bodies,
            "ledger_rows": len(FileLedger(d).rows()),
            "ended": [dict(e.payload) for e in events if e.type == "run.ended"],
            "hook": dict(hook[0]) if hook else None,
            "outbox_open": [e.key for e in pending_outbox(load_view(store, "o"))],
            "again": recover_on_startup(store, LOADED).owners,
        }
    finally:
        store.close()


if __name__ == "__main__":
    child(Path(sys.argv[1]), int(sys.argv[2]))
