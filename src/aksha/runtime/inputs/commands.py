"""The ``steer`` command (ADR-P27 D1, D2, D5): a person's message to an open run, in a lane.

It goes through the session API's one write path (``session.commands.dispatch``), so its
facts commit with ``command.accepted`` under the command's idempotency key (U4) and a
retry returns the first receipt. Its decision, in order:

1. ``lane_unknown`` for a lane outside the three; ``text_required`` for empty text.
2. ``not_found`` for a run the owner does not have (another owner's reads the same);
   ``run_ended`` for a run that has ended: a new message starts a new run instead (D6).
3. ``instance_not_open`` when ``instance`` names no instance of the run, one that has
   settled (done, blocked or withdrawn), which no model call of its will read again, or
   a parent that takes no turns of its own (not a supervisor), which never reads input.
4. ``steer`` and ``follow_up``: ``input.queued{input_id, lane, text, instance}``. Context
   assembly renders a pending steer before the target's next model call and consumes it
   in that step's result; stop-policy evaluation turns the target's proposed finish into
   CONTINUE while a follow-up is pending, and consumes it in that decision.
5. ``amend``: the text amends the run's last answered ``input`` interrupt (of
   ``instance``, when named) through :mod:`aksha.runtime.interrupts.amend`, in the same
   transaction as its ``input.queued`` and an ``input.consumed{by: amend}``. Refused
   ``nothing_to_amend`` when the run has no answered question, and ``answers_required``
   when that interrupt asks several (``AnswerInterrupt(amend=True)`` names each answer).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.inputs.types import (
    ANSWERS_REQUIRED,
    INPUT_CONSUMED,
    INPUT_PREFIX,
    INPUT_QUEUED,
    INSTANCE_NOT_OPEN,
    LANE_UNKNOWN,
    LANES,
    NOTHING_TO_AMEND,
    RUN_ENDED,
    TEXT_REQUIRED,
)
from aksha.runtime.interrupts.amend import amendment_facts, last_answered, questions
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session.commands import dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.status import all_instances, run_state
from aksha.runtime.tasks.supervisor import is_supervisor
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import SETTLED

STEER_BOUNDARY = "U4"
NOT_FOUND = "not_found"


def run_session(view: LogView, run: str) -> str | None:
    """The session ``run`` was opened in, from its ``run.started``."""
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            return e.session
    return None


def steer_spec(trees: Mapping[str, CompiledTree], ids: IdSource) -> CommandSpec:
    """The ``steer`` command over the loaded agents' compiled trees, by agent name."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        a = command.args
        lane, text, run = str(a["lane"]), a.get("text"), str(a["run"])
        if lane not in LANES:
            return CommandRefused(LANE_UNKNOWN, f"a lane is one of {sorted(LANES)}, not {lane!r}")
        if not isinstance(text, str) or not text.strip():
            return CommandRefused(TEXT_REQUIRED, "a steer carries the person's text")
        summary = rebuild(RUNS, view).get(run)
        if summary is None:
            return CommandRefused(NOT_FOUND, f"no {run}")
        if summary.outcome is not None:
            return CommandRefused(RUN_ENDED, f"{run} ended {summary.outcome}; send a new message")
        instance = a.get("instance")
        tree = trees.get(summary.agent)
        if instance is not None:
            why = _not_open(view, tree, run, str(instance))
            if why is not None:
                return CommandRefused(INSTANCE_NOT_OPEN, f"{run}/{instance}: {why}")
        ident = ids.next(view.owner, INPUT_PREFIX)
        by = str(a["by"])
        payload: dict[str, Any] = {"input_id": ident, "lane": lane, "text": text,
                                   "instance": None if instance is None else str(instance),
                                   "by": by, "command_id": str(command.command_id)}  # fmt: skip
        session = run_session(view, run)
        events: list[NewEvent] = [NewEvent(INPUT_QUEUED, payload, run=run, session=session)]
        if lane == "amend":
            amended = _amend(view, run, payload, ident, by, str(command.command_id))
            if isinstance(amended, CommandRefused):
                return amended
            events.extend(amended)
        return CommandAccepted(tuple(events))

    return CommandSpec("steer", STEER_BOUNDARY, decide)


def _not_open(view: LogView, tree: CompiledTree | None, run: str, instance: str) -> str | None:
    """Why ``instance`` of ``run`` would never read a message, or None (step 3)."""
    if tree is None:
        return "no such instance"
    state = run_state(view, tree, run)
    ref = next((r for r in all_instances(state) if r.instance == instance), None)
    if ref is None:
        return "no such instance"
    status = state.status(ref)
    if status in SETTLED:
        return status
    node = tree.node(ref.task_path)
    if not node.is_leaf and not is_supervisor(node.task):
        return "a parent that takes no turns; steer one of its children"
    return None


def _amend(
    view: LogView, run: str, payload: Mapping[str, Any], ident: str, by: str, command_id: str
) -> tuple[NewEvent, ...] | CommandRefused:
    instance = payload["instance"]
    i = last_answered(view, run, None if instance is None else str(instance))
    if i is None:
        return CommandRefused(NOTHING_TO_AMEND, f"{run} has no answered question to amend")
    asked = questions(i)
    if len(asked) > 1:
        return CommandRefused(ANSWERS_REQUIRED, f"{i.id} asks {list(asked)}: amend it with "
                              "AnswerInterrupt(amend=True) and an answer per question")  # fmt: skip
    facts = amendment_facts(view, i, {asked[0]: str(payload["text"])}, by=by,
                            command_id=command_id, input_id=ident)  # fmt: skip
    if isinstance(facts, CommandRefused):
        return facts
    used = NewEvent(INPUT_CONSUMED, {"input_id": ident, "by": "amend", "interrupt": i.id}, run=run)
    return (*facts, used)


def steer(
    store: EventStore,
    owner: str,
    run: str,
    text: str,
    *,
    trees: Mapping[str, CompiledTree],
    ids: IdSource,
    lane: str = "steer",
    instance: str | None = None,
    by: str,
    command_id: str | None = None,
) -> CommandReceipt:
    """Queue ``text`` for ``run`` in ``lane`` (module doc)."""
    args: dict[str, Any] = {"run": run, "text": text, "lane": lane, "by": by}
    if instance is not None:
        args["instance"] = instance
    session = run_session(load_view(store, owner), run) or owner
    return dispatch(store, Command("steer", owner, session, args, command_id),
                    steer_spec(trees, ids))  # fmt: skip
