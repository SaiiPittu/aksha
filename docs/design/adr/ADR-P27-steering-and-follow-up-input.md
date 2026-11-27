# ADR-P27: Steering and follow-up input to a running run (lean: a person's message to an open run is a durable fact in one of three lanes, steer, follow-up or amend, and the interface must name the lane)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the coding stress test (the coding stress review, missing primitive M-1) and, as a runtime change to ADR-P19, by the Research stress test (the Research stress review, row 28). Nothing defined what a `SendMessage` means while a session has an open run. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026; built 26 Nov 2026 with the amendments below.

## Terms

The terms:

- **Steering** is a person's message to a running Task instance that the runtime renders before that instance's next model call.
- A **follow-up** is a message the runtime renders only when the instance proposes to finish.
- An **amendment** is a message that supersedes the person's last answer to the run's intake Task, so invalidation reopens what depended on it.
- A **lane** is which of the three a message is.

## Context: what breaks without it

| Message | Arrives | Without this ADR |
|---|---|---|
| "Also include manual alternatives like spreadsheets" | During `fetch_extract`, after scope was answered (Research, the product requirements) | It may start a second routed run, or be ignored; scope is not invalidated |
| "Use the existing retry helper instead" | Turn 37 of `fix[S2]` (coding) | The running instance never sees it |
| Either, typed just before a crash | Any time | Lost, unless it is a fact |

For the product requirements' agents a run lasts seconds; for a coding session it lasts hours and the person watching will interject.

## Decision

1. D1, the lane is explicit. A bare `SendMessage` to a session with an open run is refused `lane_required` (ADR-P19 S2), and the interface asks the person once: steer the running work, add a note for when it finishes, change what was asked, or start a new request. `Steer(run_id, text, instance=None, lane=...)` carries the answer.
2. D2, every lane is a fact. `input.queued{run, instance, lane, text}` is written under the command's deduplication key; `input.consumed{input_id, step_id}` is written with the model step that rendered it. Pending input is a projection, so a message typed before a crash reaches the instance after the restart.
3. D3, `steer` renders before the next model call. Context assembly (ADR-P08 S4) renders unconsumed steer inputs as a user-statement block before the target instance's next model call, never in the middle of a tool call. `instance=None` targets the run's active instance.
4. D4, `follow_up` vetoes a finish. A pending follow-up turns a proposed DONE into CONTINUE with the text as the reason (ADR-P16 S2); the precedence stays inside stop-policy evaluation.
5. D5, `amend` goes through invalidation. The text is offered to the run's intake Task as an amendment of its last answered interrupt, the same path as `AnswerInterrupt(amend=True)`: The new user statement supersedes the old one, and the lineage closure invalidates scope and its dependents (ADR-P05).
6. D6, a message to an ended run starts a new routed run (ADR-P23), as today.

### Amendments from the build (26 Nov 2026, Proposed)

1. B1, the module. The ADR's `aksha/runtime/inputs.py` is the package `runtime/inputs/`: `types.py` (lanes, `QueuedInput`), `reducers.py` (`queued_inputs`, `pending_input`, `consumed_at`, `consumed_by_step`) and `commands.py` (`steer`). `pending_input(view, ref, lane=None)` reads the owner's `LogView`; there is no separate `RunView`. The amendment's facts live in `interrupts/amend.py`, beside the answer they correct.
2. B2, the instance is resolved late. `instance=None` is recorded as None, not resolved to an instance when the command commits: the first instance of the run that reaches the lane's point (a model call for `steer`, a proposed finish for `follow_up`) consumes it. A named instance must exist, not be settled, and take turns of its own: a leaf or a supervisor. A plain parent never calls the model or proposes a finish, so a message to it would never be read (`instance_not_open`).
3. B3, where each lane is consumed. `input.consumed{input_id, by, step_id}`: a steer with the model step whose context rendered it (U2, `by: context`); a follow-up in the stop decision that vetoed the finish (U3, `by: stop_policy`), naming the step of the turn that decision authorises; an amendment in its own transaction (U4, `by: amend`, naming the interrupt). The input stands in the instance's later contexts where it was read: before the reply of the turn whose step consumed it.
4. B4, a pending steer also vetoes a finish. A steer still unread when its target proposes to finish turns DONE into CONTINUE like a follow-up, so a person's message is never dropped by a run that ends first. Such a CONTINUE (code `person_input`, `stop.decided{inputs}`) is not a completion-check veto: the stop-veto limit does not count it, and it skips the completion check. The turn limit and the budget still apply. A supervisor (a parent that takes turns) whose check passes while input for it is pending takes one more turn the same way instead of finishing, so a message named to it is read too.
5. B5, the follow-up's reason. The reason is `the person added a follow-up:` and the text on its own line, rendered by the existing continue reason; the input block is not repeated in that turn.
6. B6, amendment mechanics. `Steer(lane="amend")` amends the run's last answered `input` interrupt of an instance (of `instance`, when named); a run with none is refused `nothing_to_amend`, and an interrupt with several questions `answers_required`, because one text cannot be split between them. `AnswerInterrupt(amend=True)` names the interrupt and its answers by question ID (refusals `not_answered`, `not_amendable`, `answer_missing`). Both mint a statement span per question whose `supersedes` names the old span, and commit `interrupt.amended{interrupt, by, answers}`; the context shows the amended answer after the one it replaces.
7. B7, statements in lineage. The context records the person's current answers it shows as `statement` lineage inputs (`read.recorded{via: context}`), so a version saved after reading an answer depends on it. The invalidation view reads a statement input as `moved` once a later span supersedes its span, unless the same version also read the replacement. This builds the "superseded statements" row ADR-P05 left open.
8. B8, an ended run. `Steer` to an ended run is refused `run_ended`, and so is `AnswerInterrupt(amend=True)` on an interrupt of an ended run, because nothing would rebuild; the interface sends a new message, which D6 routes. The CLI's `lane_required` refusal prints the three `aksha steer` forms (D1's one question).

### Amendments from the design review (27 Nov 2026, Proposed)

1. R1, the lane has no default (D1). The interface gave `Steer.lane` the default `"steer"`, which contradicts D1 and option C's rejection of a default lane: a scope change sent without a lane would steer the running instance instead of reaching `amend`, and invalidation would never reopen the answered scope. `lane` is required here and in ADR-P19's declaration (ADR-P19 R1); a request without one is refused `lane_required`. The built command still defaults to `steer` until this amendment is built, because removing the default is a major extension API change.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. Lanes by key binding in the interface only | Proven lanes | In memory; lost on crash; each interface reimplements it | Adopted as the lanes, made durable |
| B. Queue or steer a message during a run | Server-owned | No amendment lane | Adopted for steer and follow-up |
| C. A default lane | One less question | Each default is wrong for one agent: steer loses the Research scope change, amend reopens a coding session's plan | Rejected |
| D. Three durable lanes, explicit choice (lean) | Survives a crash; replay sees the same inputs | One more question for the person | Chosen |

## Interface

```python
# aksha/runtime/session.py (ADR-P19 S2)
@dataclass(frozen=True)
class Steer(CommandBase):
    run_id: str
    text: str
    instance: str | None = None                   # None: The run's active instance
    lane: Literal["steer", "follow_up", "amend"]  # required, no default (D1; R1, Proposed: built code defaults to "steer")

# aksha/runtime/inputs.py (proposal; design only)
@dataclass(frozen=True)
class QueuedInput:
    input_id: str; run: str; instance: str | None; lane: str; text: str; seq: int

def pending_input(view: "RunView", ref: "InstanceRef") -> tuple[QueuedInput, ...]: ...   # reducer
```

## Stress test (25 Nov 2026)

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits | Row 28's "also include manual alternatives" through `amend` |
| Patent | Fits | A corrected note through `amend` |
| Coding | Fits | Turn 37's steer; replay reads the same `input.queued` facts |
