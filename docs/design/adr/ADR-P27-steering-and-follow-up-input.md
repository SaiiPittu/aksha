# ADR-P27: Steering and follow-up input to a running run (lean: a person's message to an open run is a durable fact in one of three lanes, steer, follow-up or amend, and the interface must name the lane)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the coding stress test (the coding stress review, missing primitive M-1) and, as a runtime change to ADR-P19, by the Research stress test (the Research stress review, row 28). Nothing defined what a `SendMessage` means while a session has an open run. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026.

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
    lane: Literal["steer", "follow_up", "amend"] = "steer"

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
