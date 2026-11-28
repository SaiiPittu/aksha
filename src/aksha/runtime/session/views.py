"""Read views of the session API: run status, what a session waits on, and the inspector.

Every value here is a projection of the owner's log (pattern P-2); nothing is stored.

1. :func:`run_statuses`: every run with its agent, session, outcome, the interrupts it
   waits on and whether it started headless. ``state`` is ``ended`` once ``run.ended``
   is recorded, ``waiting`` while an interrupt of the run (or a route question of its
   session) is open, and ``running`` otherwise.
2. :func:`session_status`: the latest run of a session, or the route question the
   session waits on before any run starts. ``wait_for_yield`` reads it (ADR-P19 D3).
3. :func:`inspect_run`: the run inspector (ADR-P21 D10), a read over a read-only store
   that never boots: the run's start, its steps with their outcomes, its stop decisions,
   its interrupts and approvals, and how it ended.
4. :func:`run_sessions` and :func:`session_summaries`: which sessions each run belongs to
   (its own, or for a rebuild run every session of the runs in its ``for_runs``), and
   every session of the owner with its first message, its runs and what it waits on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from aksha.runtime.canonical import thaw
from aksha.runtime.interrupts.reducers import approvals, interrupts
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS
from aksha.runtime.projections.view import LogView

RunState = Literal["running", "waiting", "ended"]


@dataclass(frozen=True)
class Waiting:
    """One open interrupt a person can answer: its ID, kind, label and what it asks.

    ``questions`` are the interrupt's ``(id, text)`` pairs and ``options`` a choice's
    options, as raised, with ``option_labels`` beside them when the Task's definition built
    them, and ``multiple`` when any subset of them (none included) is an answer; for an
    approval, ``tool`` is the tool it would run and ``facts``
    the facts its approval rule showed (the target and the recipient, for a send).
    """

    interrupt_id: str
    kind: str
    label: str | None = None
    approval_id: str | None = None
    signature: str | None = None
    questions: tuple[tuple[str, str], ...] = ()
    options: tuple[str, ...] = ()
    tool: str | None = None
    facts: tuple[tuple[str, str], ...] = ()
    option_labels: tuple[str, ...] = ()
    multiple: bool = False


@dataclass(frozen=True)
class RunStatus:
    """Where one run stands (module doc, item 1)."""

    run: str
    agent: str
    session: str | None
    state: RunState
    outcome: str | None
    headless: bool
    waiting_on: tuple[Waiting, ...] = ()


@dataclass(frozen=True)
class SessionStatus:
    """A session's latest run, and what the session waits on (module doc, item 2)."""

    session: str
    run: RunStatus | None
    waiting_on: tuple[Waiting, ...]
    seq: int

    @property
    def yielded(self) -> bool:
        """Whether the session waits on a person or its latest run has ended."""
        if self.run is None:
            return bool(self.waiting_on)
        return self.run.state != "running"


def _waiting(view: LogView) -> dict[str | None, list[Waiting]]:
    """Open interrupts by run (session-scoped ones under the key ``None``)."""
    by_approval = {a.approval_id: a for a in approvals(view).values()}
    out: dict[str | None, list[Waiting]] = {}
    for i in interrupts(view).values():
        if not i.is_open:
            continue
        approval_id = i.payload.get("approval")
        approval = by_approval.get(str(approval_id)) if approval_id else None
        questions = tuple(
            (str(q.get("id", "")), str(q.get("text", "")))
            for q in i.payload.get("questions", ()) if isinstance(q, Mapping)
        )  # fmt: skip
        facts = i.payload.get("facts")
        out.setdefault(i.run, []).append(
            Waiting(
                i.id, i.kind, i.payload.get("label"),
                approval_id=None if approval is None else approval.approval_id,
                signature=None if approval is None else approval.signature,
                questions=questions,
                options=tuple(str(o) for o in i.payload.get("options", ())),
                tool=None if approval is None else approval.body.tool,
                facts=tuple(sorted((str(k), str(v)) for k, v in facts.items()))
                if isinstance(facts, Mapping) else (),
                option_labels=tuple(str(o) for o in i.payload.get("option_labels", ())),
                multiple=i.payload.get("multiple") is True,
            )
        )  # fmt: skip
    return out


def run_statuses(view: LogView) -> tuple[RunStatus, ...]:
    """Every run of the view's owner in start order (module doc, item 1)."""
    runs = rebuild(RUNS, view)
    waiting = _waiting(view)
    out: list[RunStatus] = []
    for e in view.events:
        if e.type != "run.started" or e.run is None or e.run not in runs:
            continue
        summary = runs[e.run]
        on = tuple(waiting.get(e.run, ()))
        state: RunState = "ended" if summary.outcome is not None else "waiting" if on else "running"
        out.append(RunStatus(e.run, summary.agent, e.session, state, summary.outcome,
                             e.payload.get("headless") is True, on))  # fmt: skip
    return tuple(out)


def run_status(view: LogView, run: str) -> RunStatus | None:
    """One run's status, or None for a run the owner never started."""
    return next((s for s in run_statuses(view) if s.run == run), None)


def session_status(view: LogView, session: str) -> SessionStatus:
    """The latest run of ``session`` and its open route question (module doc, item 2)."""
    mine = [s for s in run_statuses(view) if s.session == session]
    route = tuple(
        w for w in _waiting(view).get(None, ())
        if interrupts(view)[w.interrupt_id].session == session
    )  # fmt: skip
    latest = mine[-1] if mine else None
    waiting = route + (latest.waiting_on if latest is not None else ())
    return SessionStatus(session, latest, waiting, view.seq_read)


def inspect_run(view: LogView, run: str) -> Mapping[str, Any] | None:
    """The run inspector's report of ``run`` (module doc, item 3), or None if unknown."""
    status = run_status(view, run)
    if status is None:
        return None
    started: Mapping[str, Any] = {}
    ended: Mapping[str, Any] | None = None
    steps: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    for e in view.events:
        if e.run != run:
            continue
        p = e.payload
        if e.type == "run.started":
            started = thaw(p)
        elif e.type == "run.ended":
            ended = thaw(p)
        elif e.type == "step.started" and e.step is not None:
            steps[e.step] = {"step": e.step, "kind": p.get("kind"), "name": p.get("name"),
                             "outcome": None, "seq": e.seq}  # fmt: skip
        elif e.type in ("step.completed", "step.failed") and e.step in steps:
            done = steps[e.step]
            done["outcome"] = p.get("outcome", "failed" if e.type == "step.failed" else None)
            if p.get("code") is not None:
                done["code"] = p.get("code")
        elif e.type == "stop.decided":
            decisions.append({"seq": e.seq, "instance": e.instance,
                              "verdict": p.get("verdict"), "cause": p.get("cause")})  # fmt: skip
    return {
        "run": run,
        "agent": status.agent,
        "session": status.session,
        "state": status.state,
        "outcome": status.outcome,
        "headless": status.headless,
        "definition_hash": started.get("definition_hash"),
        "run_mode": started.get("run_mode"),
        "budget": started.get("budget"),
        "steps": list(steps.values()),
        "stop_decisions": decisions,
        "waiting_on": [w.__dict__ for w in status.waiting_on],
        "ended": ended,
    }


@dataclass(frozen=True)
class SessionSummary:
    """One session of the owner (module doc, item 4).

    ``runs`` are its runs in start order, rebuild runs opened for them included; ``latest``
    is the last of them. ``waiting_on`` is every open interrupt of those runs, after the
    session's own route question. ``first_message`` is the text of its first
    ``user.message``; ``started_at`` and ``last_at`` are the times of its first and last
    event, and ``last_seq`` the last event's sequence number.
    """

    session: str
    first_message: str | None
    started_at: str
    last_at: str
    last_seq: int
    runs: tuple[str, ...]
    latest: RunStatus | None
    waiting_on: tuple[Waiting, ...]


def run_sessions(view: LogView) -> Mapping[str, frozenset[str]]:
    """Each run to the sessions it belongs to (module doc, item 4).

    A run opened in a session belongs to it; a rebuild run (``run.started`` with no
    session) belongs to every session of a run in its ``for_runs``, so a rebuild of a
    rebuild follows the chain. A run that belongs to no session maps to the empty set.
    """
    out: dict[str, frozenset[str]] = {}
    for e in view.events:
        if e.type != "run.started" or e.run is None:
            continue
        if e.session is not None:
            out[e.run] = frozenset({e.session})
        else:
            for_runs = e.payload.get("for_runs") or ()
            out[e.run] = frozenset(s for r in for_runs for s in out.get(str(r), ()))
    return MappingProxyType(out)


def session_summaries(view: LogView) -> tuple[SessionSummary, ...]:
    """Every session of the owner that has a message or a run, by its first event."""
    belongs = run_sessions(view)
    statuses = {s.run: s for s in run_statuses(view)}
    open_route: dict[str, list[Waiting]] = {}
    raised = interrupts(view)
    for w in _waiting(view).get(None, ()):
        session = raised[w.interrupt_id].session
        if session is not None:
            open_route.setdefault(session, []).append(w)
    first: dict[str, str | None] = {}
    times: dict[str, list[str]] = {}
    last_seq: dict[str, int] = {}
    runs: dict[str, list[str]] = {}
    for e in view.events:
        if e.type == "user.message" and e.session is not None:
            first.setdefault(e.session, str(e.payload.get("text", "")))
        if e.type == "run.started" and e.run is not None:
            for session in sorted(belongs.get(e.run, ())):
                runs.setdefault(session, []).append(e.run)
                first.setdefault(session, None)
        owners = belongs.get(e.run, frozenset()) if e.run is not None else frozenset()
        if e.session is not None:
            owners = owners | {e.session}
        for session in owners:
            if session in first:
                times.setdefault(session, [e.at, e.at])[1] = e.at
                last_seq[session] = e.seq
    out: list[SessionSummary] = []
    for session, text in first.items():
        mine = tuple(runs.get(session, ()))
        latest = statuses.get(mine[-1]) if mine else None
        waiting = tuple(open_route.get(session, ())) + tuple(
            w for r in mine if r in statuses for w in statuses[r].waiting_on
        )
        started, last = times[session]
        out.append(SessionSummary(session, text, started, last, last_seq[session], mine,
                                  latest, waiting))  # fmt: skip
    return tuple(out)
