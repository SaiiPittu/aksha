"""The demo's five acts (package doc), each a function over one ``DemoRuntime``.

Every decision a person would make is answered by :class:`Script`, and recorded in the log
``by="demo-script"``, so the store itself says the approvals were scripted. ``run_demo``
runs the acts in order and returns the :class:`Report` the summary prints; act 4 runs its
send in a child process (``child.py``) that a real ``SIGKILL`` ends.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.app import DEMO_OWNER, DemoRuntime, counsel_mailbox, open_demo
from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.artifacts.versions import current, version
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.interrupts.commands import answer, approve
from aksha.runtime.interrupts.reducers import approvals, open_interrupts
from aksha.runtime.invalidation.commands import accept_review, apply_source_change
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.loop.drive import LoopDriver, drive, drive_rebuilds
from aksha.runtime.loop.types import DriveResult
from aksha.runtime.outbox.outbox import pending_outbox
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.types import SystemReceipt
from aksha.runtime.projections.runs import RunSummary
from aksha.runtime.projections.view import LogView, load_view

from .world import apply_change, copy_base, variant_key

BY = "demo-script"
"""Who every scripted decision is recorded as: the log says it was not a person."""
REQUESTS = {
    "research": (
        "Who sells camera-free occupancy sensing for offices, and at what price? Compare power "
        "(PoE, Ethernet, mains, USB-C, battery powered, battery thermal, AA), battery life "
        "(battery life, years of battery, months at) and privacy (privacy, camera, faces, "
        "identities, image). What do customers say about: battery (batter*, not battery "
        "powered); stillness (sit*, sat, seated, still, do not move, barely move); setup "
        "(cabl*, electrician, socket*, wiring, install*, setup); privacy (privacy, works "
        "council, image)?"
    ),
    "patent": "Assess the four invention candidates and prepare the chosen one for counsel.",
}
KILLED = -signal.SIGKILL
MARKER = ".aksha-demo"
"""The file that marks a folder as the demo's own, so a later run may empty it."""


class DemoFailed(RuntimeError):
    """The story did not go as the demo claims: the message says which step."""


def _require(ok: object, what: object) -> None:
    if not ok:
        raise DemoFailed(str(what))


@dataclass(frozen=True)
class Script:
    """The answers a person would give, typed in advance."""

    research: Mapping[str, str] = field(default_factory=lambda: {"geography": "EU"})
    gap: str = "0.5 m from the vent edge, so its air flow stays outside the footprint."
    choice: str = "C1"


@dataclass
class Report:
    """What happened, in numbers and refs, for the summary and the demo's test."""

    acts: list[tuple[str, float]] = field(default_factory=list)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    reviewed: str = ""
    first: dict[str, dict[str, str]] = field(default_factory=dict)
    change: dict[str, Any] = field(default_factory=dict)
    after: dict[str, dict[str, str]] = field(default_factory=dict)
    rebuilt: list[str] = field(default_factory=list)
    kept: dict[str, list[str]] = field(default_factory=dict)
    cutoff: list[str] = field(default_factory=list)
    next_action: dict[str, str] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    kill: dict[str, Any] = field(default_factory=dict)
    mailbox: dict[str, Any] = field(default_factory=dict)
    chain: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    def as_json(self) -> dict[str, Any]:
        """Plain JSON data."""
        return {k: v for k, v in vars(self).items()}


# --- helpers over the runtime -----------------------------------------------------------


def _view(rt: DemoRuntime) -> LogView:
    return load_view(rt.store, DEMO_OWNER)


def _start(rt: DemoRuntime, agent: str) -> str:
    setup = rt.setup(agent)
    budget = setup.agent.budget
    if budget is None:
        raise DemoFailed(f"{agent} declares no budget")
    run = LogIdSource(rt.store).next(DEMO_OWNER, "R")
    payload = {
        "agent": agent,
        "cause": "request",
        "definition_hash": setup.definition_hash,
        "invalidation": setup.agent.invalidation,
        "budget": {"model_turns": budget.model_turns, "tool_calls": budget.tool_calls,
                   "active_ms": budget.active_ms},
    }  # fmt: skip
    rt.store.append(DEMO_OWNER, "U1", (NewEvent("run.started", payload, run=run),))
    return run


def _answer_open(rt: DemoRuntime, agent: str, run: str, script: Script, report: Report) -> bool:
    """Answer the run's open question as the script says; False when nothing is open."""
    setup = rt.setup(agent)
    for i in open_interrupts(_view(rt), run):
        if i.kind == "input" and agent == "research":
            ids = [str(q["id"]) for q in i.payload.get("questions", ())]
            given = {q: script.research[q] for q in ids}
            receipt = answer(rt.store, DEMO_OWNER, i.id, by=BY, answers=given, tree=setup.tree)
            report.asked.append(
                "research asked " + ", ".join(f"{q}: {a}" for q, a in given.items())
            )
        elif i.kind == "input":
            receipt = answer(rt.store, DEMO_OWNER, i.id, by=BY, text=script.gap, tree=setup.tree)
            report.asked.append(f"{agent} asked the inventor one gap question: answered")
        elif i.kind == "choice":
            receipt = answer(rt.store, DEMO_OWNER, i.id, by=BY, text=script.choice,
                             tree=setup.tree)  # fmt: skip
            report.asked.append(f"{agent} asked which candidate to prepare: chose {script.choice}")
        else:
            continue
        _require(receipt.accepted, receipt)
        return True
    return False


def _approve_pending(rt: DemoRuntime, run: str, report: Report) -> bool:
    """Approve the run's one requested approval, for the signature shown; False if none."""
    for a in approvals(_view(rt)).values():
        if a.state != "requested" or a.run != run:
            continue
        receipt = approve(rt.store, DEMO_OWNER, a.approval_id, a.signature, by=BY)
        _require(receipt.accepted, receipt)
        report.approvals.append({"approval": a.approval_id, "run": run,
                                 "targets": list(a.body.target_refs()), "by": BY})  # fmt: skip
        return True
    return False


def _settle(rt: DemoRuntime, agent: str, run: str, script: Script, report: Report) -> DriveResult:
    """Drive ``run`` to its end, answering each question and approval as the script says."""
    result = drive(rt.setup(agent), rt.executor(), run)
    for _ in range(10):
        if result.state == "ended":
            break
        moved = _answer_open(rt, agent, run, script, report) or _approve_pending(rt, run, report)
        _require(moved, f"{run} holds on nothing the script answers: {result}")
        result = drive(rt.setup(agent), rt.executor(), run)
    report.runs[run] = {"agent": agent, "cause": "request", "outcome": result.outcome}
    return result


def _current(rt: DemoRuntime, agent: str) -> dict[str, str]:
    """Every slot the agent saved into, to its current version's ref."""
    view = _view(rt)
    slots = {
        (str(e.payload["kind"]), e.payload.get("key"))
        for e in view.events
        if e.type == "artifact.saved" and str(e.payload["kind"]).startswith(f"{agent}.")
    }
    out: dict[str, str] = {}
    for kind, key in sorted(slots, key=lambda s: (s[0], s[1] or "")):
        found = current(view, kind, key, blobs=rt.research.blobs)
        if found is not None:
            out[f"{kind}/{key}" if key else kind] = str(found.ref)
    return out


def _model_calls(view: LogView, run: str) -> int:
    return sum(
        1
        for e in view.events
        if e.type == "step.completed" and e.run == run and e.payload.get("kind") == "model"
    )


def _timed(report: Report, title: str, act: Callable[[], None]) -> None:
    t = time.monotonic()
    act()
    report.acts.append((title, round(time.monotonic() - t, 2)))


# --- the acts ---------------------------------------------------------------------------


def act_1_first_runs(rt: DemoRuntime, script: Script, report: Report) -> None:
    """Both agents run on the base world to completion; the patent agent sends once."""
    for agent in ("research", "patent"):
        run = _start(rt, agent)
        result = _settle(rt, agent, run, script, report)
        _require((result.state, result.outcome) == ("ended", "complete"), result)
        report.first[agent] = _current(rt, agent)


def act_2_change(rt: DemoRuntime, sources: Path, report: Report) -> None:
    """Apply the variant's change set to the folder and record what moved (one command)."""
    change = apply_change(sources)
    receipt = apply_source_change(rt.store, DEMO_OWNER, [rt.patent_source], "stillroom-v2",
                                  run_query=rt.run_query)  # fmt: skip
    _require(receipt.accepted, receipt)
    view = _view(rt)
    (moved,) = [e.payload for e in view.events if e.type == "source.changed"]
    queries = [e.payload for e in view.events if e.type == "query.reexecuted"]
    iv = invalidation_view(view)
    report.change = {
        "added": list(change.added),
        "edited": [rel for rel, _, _ in change.edited],
        "records_moved": sorted(moved["records"]),
        "queries_rerun": len(queries),
        "queries_changed": sum(1 for q in queries if q["changed"]),
        "invalidated": list(iv.refs("invalidated")),
        "dirty": list(iv.refs("dirty")),
    }


def act_3_rebuild(rt: DemoRuntime, script: Script, report: Report) -> str:
    """Rebuild what the change reached, accept the packet's diff review, approve the send.

    Returns the rebuild run that now waits to send the new packet.
    """
    ids = LogIdSource(rt.store)
    for agent in ("research", "patent"):
        for result in drive_rebuilds(rt.setup(agent), rt.executor(), ids):
            report.runs[result.run] = {"agent": agent, "cause": "rebuild",
                                       "outcome": result.outcome}  # fmt: skip
    reviews = list(open_reviews(_view(rt)).values())
    _require(len(reviews) == 1, reviews)
    (review,) = reviews
    receipt = accept_review(rt.store, DEMO_OWNER, review.interrupt, review.card_hash, ids,
                            tools=rt.send_tools)  # fmt: skip
    _require(receipt.accepted, receipt)
    report.reviewed = f"{review.old} -> {review.revision}"
    opened = drive_rebuilds(rt.patent, rt.executor(), ids)
    held = [r for r in opened if r.state == "held"]
    _require(len(held) == 1, opened)
    run = held[0].run
    report.runs[run] = {"agent": "patent", "cause": "rebuild", "outcome": None}
    _require(_approve_pending(rt, run, report), "no fresh approval was asked")
    return run


def act_4_kill_and_recover(work: Path, sources: Path, run: str, report: Report) -> None:
    """Send the new packet in a child that a ``SIGKILL`` ends; recover in this process."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.run(
        [sys.executable, "-m", "aksha.interfaces.demo.child", str(work), str(sources), run],
        capture_output=True, text=True, timeout=120, check=False, env=env,
    )  # fmt: skip
    report.kill = {"returncode": child.returncode, "killed": child.returncode == KILLED}
    if child.returncode != KILLED:
        raise DemoFailed(f"the send child was not killed: {child.returncode}\n{child.stderr}")
    box = counsel_mailbox(work)
    counted = Counted(box)
    rt = open_demo(work, sources, REQUESTS, mailbox=counted)
    try:
        rows_at_kill = box.deliveries()
        open_at_restart = len(pending_outbox(_view(rt)))
        recover_on_startup(rt.store, rt.loaded, outbox=lambda owner: rt.outbox,
                           owners=(DEMO_OWNER,))  # fmt: skip
        resumed = resume_owner(rt.store, DEMO_OWNER, _Driver(rt), clock=rt.clock,
                               loaded=rt.loaded, outbox=rt.outbox)  # fmt: skip
        view = _view(rt)
        (ended,) = [e.payload for e in view.events if e.type == "run.ended" and e.run == run]
        report.runs[run]["outcome"] = ended["outcome"]
        report.kill.update(
            rows_at_kill=rows_at_kill,
            outbox_open_at_restart=open_at_restart,
            recovery_lookups=counted.lookups,
            recovery_applies=counted.applies,
            resumed=list(resumed),
        )
        report.after = {agent: _current(rt, agent) for agent in ("research", "patent")}
        _final(rt, work, report)
    finally:
        rt.close()


def _final(rt: DemoRuntime, work: Path, report: Report) -> None:
    """What was rebuilt, what was kept, the sends, and the chain check."""
    view = _view(rt)
    iv = invalidation_view(view)
    for agent in ("research", "patent"):
        first, after = report.first[agent], report.after[agent]
        report.kept[agent] = sorted(s for s, ref in first.items() if after.get(s) == ref)
        report.rebuilt += sorted(after[s] for s in first if after.get(s) not in (None, first[s]))
    report.cutoff = list(iv.refs("current_by_cutoff"))
    for run, v in report.runs.items():
        v["model_calls"] = _model_calls(view, run)
    report.next_action = {
        "before": _content(rt, report.first["patent"], "patent.recommendation/C1")["next_action"],
        "after": _content(rt, report.after["patent"], "patent.recommendation/C1")["next_action"],
        "key_expects": variant_key()["expected_after_change"]["C1_next_action"]["after"],
    }
    rows = counsel_mailbox(work).rows()  # what the outside world received
    report.mailbox = {
        "deliveries": len(rows),
        "packets": [r["request"].get("packet") for r in rows.values()],
        "keys": len(set(rows)),
        "outbox_completed": sum(1 for e in view.events if e.type == "outbox.completed"),
    }
    breaks = rt.store.verify()
    seq, head = rt.store.head()
    report.chain = {"ok": breaks is None, "events": seq, "head": head,
                    "break": None if breaks is None else str(breaks)}  # fmt: skip


def _content(rt: DemoRuntime, refs: Mapping[str, str], slot: str) -> Mapping[str, Any]:
    found = version(_view(rt), ArtifactRef.parse(refs[slot]), blobs=rt.research.blobs)
    if found is None:
        raise DemoFailed(f"no version {refs[slot]}")
    return found.content


class _Driver:
    """Resume each run with its own agent's loop setup."""

    def __init__(self, rt: DemoRuntime) -> None:
        self.rt = rt

    def __call__(self, run: RunSummary, steps: DurableStepExecutor, /) -> None:
        LoopDriver(self.rt.setup(run.agent))(run, steps)


class Counted:
    """A system of record that counts the calls made to it (recovery's lookups and applies)."""

    def __init__(self, inner: SystemOfRecord) -> None:
        """Wrap ``inner``."""
        self.inner = inner
        self.lookups = 0
        self.applies = 0

    def ledger_id(self) -> str:
        """The wrapped ledger's ID."""
        return self.inner.ledger_id()

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Count, then apply through the wrapped system."""
        self.applies += 1
        return self.inner.apply(key, request)

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """Count, then look up through the wrapped system."""
        self.lookups += 1
        return self.inner.lookup(key, request)


def run_demo(work: Path, script: Script | None = None) -> Report:
    """Run the five acts in ``work`` (an empty or missing folder) and return the report."""
    script = script or Script()
    report = Report()
    t0 = time.monotonic()
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):  # never mix a run into another run's store
        raise FileExistsError(f"{work} is not empty")
    (work / MARKER).write_text("written by make demo; safe to delete\n", encoding="utf-8")
    sources = copy_base(work)
    rt = open_demo(work, sources, REQUESTS)
    try:
        _timed(report, "first runs", lambda: act_1_first_runs(rt, script, report))
        _timed(report, "change", lambda: act_2_change(rt, sources, report))
        held: list[str] = []
        _timed(report, "rebuild", lambda: held.append(act_3_rebuild(rt, script, report)))
    finally:
        rt.close()
    run = held[0]
    _timed(report, "kill and recover", lambda: act_4_kill_and_recover(work, sources, run, report))
    report.seconds = round(time.monotonic() - t0, 2)
    return report
