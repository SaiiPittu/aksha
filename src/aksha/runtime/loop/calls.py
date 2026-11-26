"""One turn's call positions (ADR-P06 D5): the gate, then the durable step executor, in order.

:func:`run_calls` runs the calls of one model reply one at a time, in the order the model
listed them. For each position ``tNN/cMM``:

1. A position already recorded returns its recorded outcome: nothing is checked or run
   again (a resumed turn).
2. Once an earlier position signalled (a ``finish``, a question, an approval request or a
   ``budget_exhausted`` refusal), the position is recorded ``skipped`` with
   ``after_<signal>`` and its body never runs, so nothing happens after the run has asked
   a person something.
3. Otherwise the policy gate checks the call against the log as it stood before the
   position started (so a resumed position is judged as it was). A refusal is recorded as
   the step's one fact. An allow runs along its route:

   =========  ================================================================
   Route      What runs
   =========  ================================================================
   dispatch   the body (read, store and exec tools), its saves through the one
              save path, the capped model view, any span drafts minted
   reserved   ``finish`` (signal ``finish``) or ``ask`` (``interrupt.raised``)
   approval   a granted approval of this exact action runs it through the
              outbox; otherwise ``approval.requested`` and its interrupt
   outbox     the effect through the outbox and its idempotency key
   model      not built: the model reads ``route_not_built``
   =========  ================================================================

Every write goes through the executor: this module holds no store handle of its own
beyond reading the log, and appends nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aksha.runtime.artifacts.errors import SaveRefused
from aksha.runtime.artifacts.types import WriterId
from aksha.runtime.budgets.limits import attempt_number
from aksha.runtime.budgets.meter import meter
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.context.turns import as_of
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ids import highest
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy, StepResult
from aksha.runtime.finalization.reserve import held_back
from aksha.runtime.grounding.errors import MintRefused
from aksha.runtime.grounding.spans import mint_for_result, span_index
from aksha.runtime.interrupts.approvals import (
    ApprovalTool,
    approval_signature,
    approvals,
    build_body,
    request_approval,
)
from aksha.runtime.interrupts.raising import ask_request, raised
from aksha.runtime.interrupts.types import Refusal
from aksha.runtime.loop.classify import position_outcome
from aksha.runtime.loop.types import SIGNAL_FIELD, LoopSetup, PositionOutcome, Signal, skip_code
from aksha.runtime.models.types import ToolCall as ReplyCall
from aksha.runtime.policy.events import args_hash, decision_event
from aksha.runtime.policy.gate import check
from aksha.runtime.policy.types import Allow, GateInput, StopPolicyNarrowing
from aksha.runtime.projections.view import LogView
from aksha.runtime.storage.rows import ArtifactRow
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.bodies import call_body
from aksha.runtime.tools.results import VIEW_FIELD, render_result, view_json
from aksha.runtime.tools.types import Err, Ok, ToolCall, ToolContext, ToolOutcome, ToolSpec

ROUTE_NOT_BUILT = "route_not_built"
"""The code of an allowed call whose route the loop does not run yet (``model``)."""

STORE_UNAVAILABLE = "store_unavailable"
"""The code of a save asked for when the loop was given no artifact store."""

SAVE_NOT_ALLOWED = "class_violation"
"""The code of a save returned by a tool whose effect class may not save."""

SAVING_EFFECTS = frozenset({"store", "exec"})
"""The effect classes whose bodies may ask for saves (ADR-P10)."""

UNRECORDED_DEFINITION = "unrecorded"
"""The definition hash an approval body names when neither the loop nor the run has one."""


def tool_policy(spec: ToolSpec) -> StepPolicy:
    """The step policy of a dispatched call of ``spec``: its timeout, retries and backoff."""
    retry = spec.retry
    delays = retry.backoff_ms or (0,)
    base = delays[0]
    factor = max(1, delays[1] // base) if len(delays) > 1 and base > 0 else 2
    return StepPolicy(
        timeout_ms=retry.timeout_ms,
        max_retries=retry.max_retries,
        retry_on=retry.retry_on,
        backoff=Backoff(base_ms=base, factor=factor, cap_ms=max(delays)),
    )


def _outcome(position: int, tool: str, result: StepResult) -> PositionOutcome:
    payload: dict[str, Any] = {"outcome": result.outcome, "code": result.code}
    if result.value is not None:
        payload["value"] = result.value
    return position_outcome(position, tool, payload)


def _shown(
    outcome: ToolOutcome,
    cap: int,
    *,
    signal: Signal | None = None,
    events: tuple[NewEvent, ...] = (),
    extra: Mapping[str, Any] | None = None,
) -> StepOutput:
    """A step output whose recorded value is the capped view of ``outcome`` (ADR-P10 D7)."""
    value: dict[str, Any] = {VIEW_FIELD: view_json(render_result(outcome, cap)), **(extra or {})}
    if signal is not None:
        value[SIGNAL_FIELD] = signal
    if isinstance(outcome.result, Err):
        return StepOutput(value, outcome="error", code=outcome.result.code, events=events)
    return StepOutput(value, events=events)


def run_calls(
    setup: LoopSetup,
    executor: DurableStepExecutor,
    ref: InstanceRef,
    turn: int,
    calls: tuple[ReplyCall, ...],
    narrowing: StopPolicyNarrowing,
) -> tuple[PositionOutcome, ...]:
    """Run (or replay) every call position of one reply, in listed order (module doc)."""
    out: list[PositionOutcome] = []
    signal: Signal | None = None
    for n, call in enumerate(calls, start=1):
        step = StepId(ref.run, ref.instance, turn=turn, call=n)
        if signal is not None:
            out.append(_outcome(n, call.name, executor.skip(step, "tool", call.name,
                                                            skip_code(signal))))  # fmt: skip
            continue
        position = run_position(
            setup, executor, ref, step, ToolCall(call.name, call.arguments, n), narrowing
        )
        out.append(position)
        signal = position.signal
    return tuple(out)


def _started(view: LogView, run: str) -> Mapping[str, Any]:
    for e in view.events:
        if e.run == run and e.type == "run.started":
            return e.payload
    return {}


def run_position(
    setup: LoopSetup,
    executor: DurableStepExecutor,
    ref: InstanceRef,
    step: StepId,
    call: ToolCall,
    narrowing: StopPolicyNarrowing,
) -> PositionOutcome:
    """Run (or replay) one call position through the gate and the executor (module doc)."""
    n = call.position
    found = executor.recorded(step)
    if found is not None:
        return _outcome(n, call.name, found)
    view = as_of(setup.view(), step)
    started = _started(view, ref.run)
    gate = GateInput(
        call=call,
        agent=setup.agent,
        task=setup.task(ref),
        ceiling=setup.permissions(),
        narrowing=narrowing,
        meter=held_back(view, setup.tree, ref.run, meter(view, ref.run)).reading(),
        sources=setup.sources,
        sandbox=setup.sandbox,
        headless=bool(started.get("headless", False)),
    )
    decision = check(gate, setup.registry)
    if not isinstance(decision, Allow):
        event = decision_event(decision, call, run=ref.run, instance=ref.instance, step=str(step))
        return _outcome(n, call.name, executor.refuse(step, event))
    decl = setup.registry.get(call.name)
    spec = decl.spec
    extra = {"position": n, "route": decision.route, "counted": decision.counted}
    policy = tool_policy(spec)
    hashed = args_hash(call.arguments)
    cap = setup.policy().result_cap_chars

    def record(work: Callable[[], StepOutput]) -> PositionOutcome:
        def timed(deadline: Deadline) -> StepOutput:
            return work()

        result = executor.step(step, "tool", call.name, hashed, timed, policy, extra=extra)
        return _outcome(n, call.name, result)

    if decision.route == "dispatch":
        ctx = ToolContext(setup.owner, setup.agent.name, ref.task_path, ref.key, str(step),
                          attempt_number(view, str(step)))  # fmt: skip
        args = call.arguments
        return record(lambda: _dispatched(setup, ref, step, spec, call_body(decl, ctx, args)))
    if decision.route == "reserved" and call.name == "finish":
        done = ToolOutcome(Ok({"finish": "proposed"}))
        return record(lambda: _shown(done, cap, signal="finish"))
    if decision.route == "reserved":
        return record(lambda: _asked(setup, ref, step, call.arguments, cap))
    if decision.route == "outbox" and spec.external is not None:
        result = executor.effect_step(step, call.name, call.arguments, spec.external, extra=extra)
        return _outcome(n, call.name, result)
    if decision.route == "approval":
        return _approval(setup, executor, ref, step, call, spec, view, started, record)
    msg = f"{call.name}: the {decision.route} route is not built in the agent loop yet"
    return record(lambda: _shown(ToolOutcome(Err(ROUTE_NOT_BUILT, msg)), cap))


def _dispatched(
    setup: LoopSetup, ref: InstanceRef, step: StepId, spec: ToolSpec, outcome: ToolOutcome
) -> StepOutput:
    """A body's outcome as its step records it: saves, minted spans and the capped view.

    A refused save or mint turns the whole result into the error the model reads, and
    nothing it asked for commits.
    """
    cap = setup.policy().result_cap_chars
    if outcome.saves and spec.effect not in SAVING_EFFECTS:
        msg = f"{spec.name} is a {spec.effect} tool; only store and exec tools save"
        return _shown(ToolOutcome(Err(SAVE_NOT_ALLOWED, msg)), cap)
    view = setup.view()
    events: list[NewEvent] = []
    rows: list[ArtifactRow] = []
    if outcome.saves:
        store = setup.artifacts
        if store is None:
            msg = "this loop was given no artifact store"
            return _shown(ToolOutcome(Err(STORE_UNAVAILABLE, msg)), cap)
        task = setup.task(ref)
        writes = frozenset(setup.tree.kind(w) for w in task.writes)
        writer = WriterId(setup.agent.name, ref.task_path, ref.key)
        for request in outcome.saves:
            try:
                saved = store.save(
                    view, request, writer=writer, run=ref.run, instance=ref.instance,
                    step=str(step), writes=writes,
                )  # fmt: skip
            except SaveRefused as refused:
                err = Err(refused.code, refused.message, paths=(refused.path,))
                return _shown(ToolOutcome(err), cap)
            events.extend(saved.events)
            rows.extend(saved.rows)
    try:
        minted = mint_for_result(
            outcome, (), spec=spec, index=span_index(view), run=ref.run,
            instance=ref.instance, step=str(step), cap_chars=cap,
        )  # fmt: skip
    except MintRefused as refused:
        return _shown(ToolOutcome(Err(refused.code, refused.message)), cap)
    out = _shown(outcome, cap, events=(*events, *minted.minted.events))
    return StepOutput(out.value, out.outcome, out.code, out.events, tuple(rows))


def _asked(
    setup: LoopSetup, ref: InstanceRef, step: StepId, args: Mapping[str, Any], cap: int
) -> StepOutput:
    """The ``ask`` call: ``interrupt.raised`` on this instance, committed with the step."""
    declared = setup.task(ref).interrupt
    if declared is None:  # the gate offers ask only with a declared interrupt
        return _shown(ToolOutcome(Err("not_in_task", "this Task declares no interrupt")), cap)
    ident = f"I-{highest(setup.view().events, 'I') + 1:04d}"
    request = ask_request(declared, args, run=ref.run, instance=ref.instance)
    event = raised(request, ident, step=str(step))
    shown = ToolOutcome(Ok({"interrupt": ident, "status": "waiting"}))
    return _shown(shown, cap, signal="interrupt", events=(event,), extra={"interrupt": ident})


def _approval(
    setup: LoopSetup,
    executor: DurableStepExecutor,
    ref: InstanceRef,
    step: StepId,
    call: ToolCall,
    spec: ToolSpec,
    view: LogView,
    started: Mapping[str, Any],
    record: Callable[[Callable[[], StepOutput]], PositionOutcome],
) -> PositionOutcome:
    """Run an approved action, or ask a person to approve this exact one (ADR-P17 D5, D6)."""
    cap = setup.policy().result_cap_chars
    tree = setup.tree
    tool = ApprovalTool(spec, view_of=lambda log: TaskView(log, tree, ref))
    definition = setup.definition_hash or str(started.get("definition_hash") or "")
    definition = definition or UNRECORDED_DEFINITION
    body = build_body(view, tool, call.arguments, definition_hash=definition)
    if isinstance(body, Refusal):
        return record(lambda: _shown(ToolOutcome(Err(body.code, body.message)), cap))
    signature = approval_signature(body)
    granted = next(
        (a for a in approvals(view).values() if a.state == "granted" and a.signature == signature),
        None,
    )
    if granted is not None and spec.external is not None:
        result = executor.effect_step(
            step, call.name, call.arguments, spec.external,
            extra={"position": call.position, "route": "approval"},
            approval_id=granted.approval_id,
        )  # fmt: skip
        return _outcome(call.position, call.name, result)

    def requested() -> StepOutput:
        now = setup.view()
        ident = f"A-{highest(now.events, 'A') + 1:04d}"
        facts = request_approval(now, tool, call.arguments, ident, definition_hash=definition,
                                 run=ref.run, instance=ref.instance, step=str(step))  # fmt: skip
        if isinstance(facts, Refusal):
            return _shown(ToolOutcome(Err(facts.code, facts.message)), cap)
        shown = ToolOutcome(Ok({"approval": ident, "status": "waiting"}))
        return _shown(shown, cap, signal="approval", events=facts, extra={"approval": ident})

    return record(requested)
