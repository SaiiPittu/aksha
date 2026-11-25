# ADR-P24: Partial results and finalization (lean: a fan-out parent declares whether a blocked child blocks it or leaves an explicit unknown, a budget interrupt can be answered "finalize", and both reach one run outcome, `partial`, that lists every unknown and its blocker)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the Research stress test (the Research stress review, rows 21 and 22, missing primitive M2). ADR-P16 already had the outcome literal `partial`, but no path in ADR-P14, ADR-P15 or ADR-P16 reached it except a fetch failure a child absorbed itself. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026.

## Terms

The product requirements ask the harness to "return a partial result when evidence is insufficient" and says "the run preserves useful partial work". The terms:

- A **partial result** is a run's output in which some keys hold an explicit unknown (ADR-P13 D5) instead of a value, each naming why.
- **Finalization** is the step that turns incomplete work into a partial result: it marks unstarted keys unknown and runs the Tasks that assemble the output.
- A **finalizer Task** is a Task the agent marks `finalizer=True`, such as the Research Agent's `matrix`, which may run on finalization.
- A **reserved budget** is a small share of the run's budget that only finalizer Tasks may spend.

## Context: what breaks without it

The Research review's worked example, on five fixture vendors (toy numbers):

| Step | As designed before this ADR | With this ADR |
|---|---|---|
| `fetch_extract[COMP-01..03, 05]` | Each saves a record; `done` | The same |
| `fetch_extract[COMP-04]` | Turn 10 of 10, check failing; `blocked` | The same |
| Parent `fetch_extract` | Blocked, naming COMP-04 | Done: four children done, one blocked (`on_child_blocked="partial"`) |
| `matrix` | `needs` unmet; never entered | Entered; reads four records and one `Unknown(reason="unverified")` row naming the blocker |
| Run outcome | `blocked`, 0 of 5 matrix rows | `partial`, 4 of 5 rows grounded and 1 explicit unknown |

The budget case is the same shape: a person who declines to grant more budget could only stop, and a stop leaves four vendor records and no competitor table.

## Decision

1. D1, the fan-out parent declares the rule. `Task.on_child_blocked: Literal["block", "partial"] = "block"` (ADR-P15 S1). Under `partial` the parent is done when every child is done or blocked. The default stays `block`, so a partial result is always something the agent chose.
2. D2, a blocked key reads as an explicit unknown. Readers of the parent's output see each blocked key as `Unknown(reason="unverified", failed_step=<the blocker's step>)`. The value is produced by the runtime's read view, never written as a fake artifact, so it cannot disagree with the event store.
3. D3, readers still decide. The readers' own completion checks run as before, so the Research matrix's check ("every cell has a citation or an explicit unknown") passes with the unknown row, while an agent whose check refuses unknowns still blocks. The harness never manufactures completion.
4. D4, the budget interrupt's third answer. `Finalize(run_id)` (ADR-P19 S4) resolves a budget interrupt by marking every unstarted key `Unknown(reason="not_checked")`, voiding instances that have not started, and letting the scheduler run only Tasks marked `finalizer=True`, in declared order, under the reserved budget.
5. D5, the reserved budget. A run's budget sets aside a reserve for finalizers (placeholder: 10% of tool calls and 2 turns per finalizer). Non-finalizer Tasks never draw on it, so finalization always has room to run. Spending the reserve too ends the run `partial` with whatever the finalizers saved.
6. D6, one outcome. A run ends `partial` when it ends with any explicit unknown introduced by D2 or D4, and its report lists each unknown, its reason and its blocker. A run is never labelled `complete` with such an unknown in its output.
7. D7, finalization is an automatic start. The finalizer's entries call the hold predicate (ADR-P16 S3) like any scheduler start; a person's stop request still wins.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. The child absorbs its own failure | Each fan-out child catches failures and saves an unknown itself | No new rule | A turn-limit FAIL never reaches the child's check, so the child cannot absorb it | Kept for tool failures; insufficient alone |
| B. A tolerated failure share on the fan-out | The parent succeeds if no more than N children fail | Familiar | A count says nothing about what readers see for the failed items | Adapted: D1 plus D2's explicit unknowns |
| C. Stop and let the person rerun | Today's behaviour | Nothing to build | Fails the product requirements | Rejected |
| D. Declared parent rule, finalize answer, reserved budget, one `partial` outcome (lean) | D1 to D7 | Every partial result is declared, explicit and reported | Agents must mark finalizers | Chosen |

## Interface

```python
# aksha/runtime/api.py (Task fields, ADR-P15 S1 and S2)
on_child_blocked: Literal["block", "partial"] = "block"
finalizer: bool = False

# aksha/runtime/partial.py (proposal; design only)
@dataclass(frozen=True)
class UnknownEntry:
    key: str
    reason: Literal["unverified", "not_checked"]
    blocker: str | None                          # step ID or instance that blocked it

def unknowns(view: "RunView", run: str) -> tuple[UnknownEntry, ...]: ...   # projection
def finalize(tx: "Transaction", run: str, command_id: str) -> tuple[str, ...]: ...
    # writes run.finalizing{reserve}, voids unstarted instances, returns finalizer Task IDs

# Events: run.finalizing{reserve}; run.ended{outcome: "partial", unknowns: n}
RunOutcome = Literal["complete", "partial", "blocked", "stopped", "failed"]
```

## Stress test (25 Nov 2026)

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits | Rows 21 and 22 of the Research stress review |
| Patent | Fits | One candidate's prior art unverified |
| Coding | Fits | One module stays red; the approval decides whether a partial branch is pushed |
