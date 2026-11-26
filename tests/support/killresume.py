"""A resumed rebuild that a real ``SIGKILL`` ends after its Nth commit (decision 36).

``python -m tests.support.killresume <dir> <n>`` builds the resume bench's request run in
``dir`` (setting ``reentry: "resume"``), changes C1's source record, then drives the
rebuild runs and kills itself right after the ``n``-th commit of that phase, so nothing
in Python runs after it. It prints ``COMPLETED_WITHOUT_KILL`` if the phase has fewer than
``n`` commits. :func:`recover` is the next process: recovery on startup, the lazy resume
through the agent loop's driver, then any rebuild run still to open, and it returns what
the log holds.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any

from aksha.runtime.context.reentry import reentry_divergences
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.execution.types import StepId
from aksha.runtime.loop.drive import LoopDriver
from tests.support.rebuild import AGENT, DEFINITION, OWNER
from tests.support.resume import ResumeRig

COMPLETED = "COMPLETED_WITHOUT_KILL"
LOADED = {AGENT: DEFINITION}


def child(d: Path, kill_after: int) -> None:
    r = ResumeRig(d / "a.sqlite")
    r.start(reentry="resume")
    r.drive()
    r.change("C1", "2")
    real = r.store.append
    count = {"n": 0}

    def append(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        count["n"] += 1
        if count["n"] == kill_after:
            os.kill(os.getpid(), signal.SIGKILL)
        return out

    r.store.append = append  # type: ignore[method-assign]
    r.rebuild()
    print(COMPLETED, flush=True)


def recover(d: Path) -> dict[str, Any]:
    """Restart: recover on startup, resume the owner, open what is left; read the log."""
    r = ResumeRig(d / "a.sqlite")
    r.world.versions["C1"] = "2"  # the outside world kept the change across the restart
    try:
        recover_on_startup(r.store, LOADED)
        resume_owner(r.store, OWNER, LoopDriver(r.setup), clock=r.clock, loaded=LOADED)
        r.rebuild()
        view = r.view()
        rebuild_runs = [
            str(e.run) for e in view.events
            if e.type == "run.started" and e.payload.get("cause") == "rebuild"
        ]  # fmt: skip
        models: list[str] = []
        for e in view.events:
            if e.type != "step.completed" or e.step is None or e.run not in rebuild_runs:
                continue
            sid = StepId.parse(e.step)
            if sid.turn is not None and sid.call is None and sid.sub is None:
                models.append(e.step)
        latest = r.setup.artifacts.latest(view, "demo.bundle", "C1")  # type: ignore[union-attr]
        return {
            "rebuild_runs": rebuild_runs,
            "entries": [
                (i, c["mode"], c.get("reason")) for run in rebuild_runs for i, c in r.entries(run)
            ],
            "models": models,
            "ended": [
                (str(e.run), e.payload["outcome"]) for e in view.events if e.type == "run.ended"
            ],
            "faults": [
                e.payload
                for e in view.events
                if e.type == "stop.decided" and e.payload.get("reason") == "step_mismatch"
            ],
            "bundle": None if latest is None else dict(latest.content),
            "divergences": reentry_divergences(view, r.setup.tree, r.setup.policy()),
        }
    finally:
        r.close()


if __name__ == "__main__":
    child(Path(sys.argv[1]), int(sys.argv[2]))
