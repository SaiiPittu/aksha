# ADR-P16: Stop policy and stop-policy evaluation (a stack of pure stop policies derived from the event store, one recorded stop decision per stop candidate, four built-in policies an agent opts into, and one hold predicate that puts human state ahead of every automatic start)

*Intermediate design as of 26 Nov 2026. Status: Accepted in part. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Accepted in part, 25 Nov 2026, by the maintainer. Accepted: The stack of stop policies is an interpretation computed as a pure function over the event store (pattern P-2, which settles v2 ADR-014 towards its option E); goal condition, plan mode, forced tool choice and todo reminder are built-in stop policies beside the Run, Task and interrupt policies (decisions log item 3); an agent opts into them by configuration and never subclasses one in v1 (item 10). These carry v2 ADR-014 forward under decision 11's names. Proposed: The hold invariant (D13-4), the shared hold predicate that implements it (D6), and every sub-decision in "Decisions for the maintainer". Primitive PR-16 in the primitives catalogue, cluster D (Control). Nothing here was built as of 25 Nov 2026. Amended 25 Nov 2026 after the three-agent stress test: S1 to S3 below are new and Proposed; S1 narrows D13-4's hold invariant. Amended 27 Nov 2026 by the portfolio slice (ADR-P15 S5): the recitation policy, S4 below, is Proposed.

## Terms

Plan mode, goal mode, todo reminders, forced tool calls, a completion check, a budget and a person's pending approval all want the same line of the agent loop: The moment the model ends a turn and the harness must decide whether to run another, wait, or stop. Written as separate `if`s, their order becomes a precedence nobody chose; with six such mechanisms there are 15 pairs whose order someone must get right. This primitive gives that line one owner. The terms it defines:

- A **stop candidate** (v2: yield) is a point where control returns to the runtime: a turn ended with tool calls, a finish request, an instance entered or ended, a step faulted, a run resumed after a crash, a context overflowed. The kinds are a closed set of eleven.
- A **stop policy** is a pure rule that may refuse or allow the end of the work at a stop candidate: "the plan is not written", "the tests have not passed", "an approval is pending". Aksha keeps the active policies on a stack walked innermost first, derived from the event store.
- The **stop-policy stack** is the ordered set of policies active for one Task instance: The Run policy outermost, then the Task policy, then any opted-in policies, then an open interrupt's policy innermost. It is derived from events at every evaluation; no push or pop event exists.
- **Stop-policy evaluation** (v2: The walk) is the one function that offers a stop candidate to the stack, combines the answers with a fixed precedence, and commits one **stop decision record** (`stop.decided`, v2 `walk.decided`, as ADR-P01 D8 proposes).
- A **verdict** is one of seven answers: PASS (no opinion), CONTINUE (another model turn, with a reason the model reads), PUSH (a child policy decides), HOLD (wait for a person), DONE (this level's goal is met), FAIL (this level gives up), STOP (end the run).
- **Human state** is any fact that a person, not the runtime, must change before work may go on: a stop request (ADR-P18), an open interrupt such as a pending approval or question (ADR-P17), and, if Aksha later adds them, snooze and archive.
- The **hold invariant** (D13-4), `inv.no_auto_start_while_held`: while human state holds a scope, nothing automatic may start work in that scope.

## Context: what breaks without it

| Without this primitive | What a user or a test sees | Requirement |
|---|---|---|
| Each mechanism decides "continue or stop" in its own branch | A goal check and a budget limit disagree, and which wins depends on line order; v1's designed loop decided at six places and had a seventh with no home | a stated requirement |
| Counters kept in memory | Three vetoes become three per process; a loop guard held in a local variable resets every turn | a stated requirement |
| No record of who decided | `inspect` cannot say why the run stopped or why it took turn 14 | a stated requirement |
| Automatic starts that read human state each in their own way | A restart, a rebuild run or a usage-limit resume starts a turn while an approval is pending; a second start path that repeats the restart predicate's checks inline, rather than calling it, drifts from the first | a stated requirement |
| A third author must edit the loop to add plan mode | The harness is general only for agents already written | a stated requirement |

The three agents press on it differently:

1. The Research Agent uses only the Run, Task and interrupt policies; its pressure is the budget limit that must keep partial work.
2. The Patent Agent's pressure is a kill during an approval wait, and a source change that would start a rebuild run while a person still has a card open.
3. The contrast coding agent uses the opt-in policies most: plan mode before any edit, a goal condition ("all tests pass") that keeps it working across many turns, todo reminders in a long session, and a person who wants it to stop now.

## Decision

Seven decisions. D1, D2 and D4 are the maintainer's (P-2, items 3 and 10). D3, D5, D6 and D7 are my proposals, carried from v2 ADR-014's Proposed sub-decisions or new.

1. D1, a stop policy is a pure judge with three methods. `prepare(view) -> Prep` says what the policy adds to the next model request (guidance lines, tool narrowing), called outer to inner. `judge(view, candidate) -> Verdict` answers one stop candidate, called inner to outer. `limit(view, candidate, proposed) -> Verdict | None` may replace a proposed CONTINUE when continuing would cost what this level cannot pay, called outer to inner. A Task limit that ends a leaf after its turn replaces it with DONE when the leaf's completion check passes on what is saved, and with FAIL otherwise (S5). Policies are frozen dataclasses built fresh from the definition at every evaluation, and read only the view and the candidate: never a clock, a store, a model or randomness. A policy that raises yields FAIL with `check_raised`.
2. D2, the stack is a view of the event store. Run exists from `run.started` to `run.ended`; a Task policy exists for the instance the candidate concerns; an un-keyed child Task's policy exists while it is the first child whose check fails; opted-in policies exist with their Task; an interrupt policy exists from `interrupt.raised` to its closing event. An edit, a revocation or a key withdrawal therefore needs no stack code.
3. D3, one evaluation per stop candidate, one record. Evaluation reads the budget accounting first, derives the stack, judges inner to outer (DONE and FAIL pop and re-offer the same candidate to the parent; PUSH offers it to the child), applies limits outer to inner to a proposed CONTINUE (budget first, then the turn limit and the stop-veto limit), and commits one transaction holding `stop.decided` and what it implies (`completion_check.evaluated`, including the one a Task limit records `on: limit` (S5), `task.entered`, `budget.reached` with the budget interrupt's `interrupt.raised`, `interrupt.voided`, `run.ended`). Only CONTINUE spends budget, one model turn each; only the Run policy returns STOP. The record stores the trail, the final verdict, which limit bit and the counts behind it, never seconds left, so two fake-model runs give identical records.
4. D4, a closed built-in set, opted into by configuration. Run, Task (including un-keyed child Tasks) and interrupt are always present. Four more are built in and opt-in with parameters, never subclassed:

| Built-in policy (v2 name) | Opted in with | Judges | Verdicts | Adds to the request |
|---|---|---|---|---|
| Goal condition (Goal) | `Goal(check)` on a Task; a run-level goal is one of the Run policy's completion checks | A pure goal check; vetoes a finish while it fails | CONTINUE with the check's message, PASS | One line naming the goal |
| Plan mode (Plan) | `Plan(kind, accept)` | Holds the first non-plan tool turn until a `plan` artifact passes the accept rule | CONTINUE, PASS | "Write the plan first"; tools narrowed to the plan save |
| Forced tool choice (ForceTool) | `ForcedTool(tool, when)` | When the condition holds, narrows the next turn to one tool; `finish` and `ask` always stay | PASS | The narrowed tool list and one line saying so; a `tool_choice` hint live adapters may pass |
| Todo reminder (TodoReminder) | `TodoReminder(every)` | Never vetoes | PASS | Every N turns, the open todos derived from the Task's unmet child checks |

5. D5, a judgement that needs a model call is not a stop policy. It becomes a recorded step (a classification call through ADR-P07) that saves an artifact, and a policy's pure check reads that artifact. "Is the user's goal met?" for an open-ended coding request is therefore a classification step that writes `goal.assessment`, and the goal check reads it; replay returns the recorded answer.
6. D6, the hold predicate (D13-4, Proposed). One function, `hold_reason(view, scope)`, returns why a scope is held by human state, or None. Scopes are an instance, a run, an (owner, agent) pair, and a session (for the request router's `route` interrupt, ADR-P23). Reasons are a closed set: `stop_requested`, `interrupt_open` (any of ADR-P17's six kinds on that instance, run or session), and `stopped_by_person` (a standing hold after a person's stop, ADR-P18 D4). Every automatic start path calls this one function before it writes: The Run policy before it relays `next_instance` or proposes CONTINUE for an instance, recovery on startup before it writes a resume record, the rebuild-run opener before `run.started{cause: rebuild}`, and the restart limit's path. A start caused by a person's command carries `cause: command:<name>` with its idempotency key and is not automatic. One shared predicate, rather than a convention, matters because two start paths that each repeat some of the checks inline (archived, settled, a pending request, snooze) will check deleted work, pending secret requests or a stored stop on one path and not the other, so the two drift.
7. D7, a named invariant proved by an audit. `inv.no_auto_start_while_held` holds when, for every `run.started`, `task.entered`, model `step.started` and resume record whose cause is not a person's command, `hold_reason(view at seq - 1, its scope)` is None. An audit runs it over every log the suite produces, and a mutation test removes the predicate's call from one start path and asserts the audit fails, because a guard nobody has seen fail is a guard nobody has proved.

One evaluation, as a diagram:

```mermaid
flowchart TD
  Y["Stop candidate:<br/>kind, instance, after_turn, seq"] --> M["1. Read the budget accounting"]
  M --> S["2. Derive the stack, outer to inner"]
  S --> H{"3. hold_reason for the run<br/>and the instance?"}
  H -->|"stop_requested"| ST["Run policy: STOP stopped"]
  H -->|"interrupt_open"| HO["Interrupt policy: HOLD"]
  H -->|"None"| J["4. Judge inner to outer"]
  J -->|"PASS"| J
  J -->|"DONE or FAIL"| P["Pop, re-offer to the parent"] --> J
  J -->|"PUSH"| C["Offer to the child"] --> J
  J -->|"CONTINUE"| L["5. Limits outer to inner:<br/>budget, turn limit, stop-veto limit"]
  L -->|"a limit bites"| R["Replace: PUSH budget interrupt, or FAIL"] --> J
  L -->|"none bites"| K
  ST --> K["6. Commit one transaction:<br/>stop.decided and the events it implies"]
  HO --> K
  J -->|"HOLD or STOP"| K
  K --> A["7. Act: next turn, suspend, pick the next instance, or end"]
```

Caption: box 3 is the hold predicate placed before any policy can propose work, so human state wins by position, not by each policy remembering to check it; every branch that ends a turn, an instance or a run passes through box 6.

Takeaway: one function owns the stop, it writes once per candidate, and human state is checked by one predicate before anything automatic can start.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, a hold covers the held work, not the whole agent. The Patent review's most serious finding: The product requirements' demo (select, prepare, change, restart; the product requirements) naturally leaves the approval request for `packet/C1@v1` open when the change lands. D6 counted any open interrupt for the (owner, agent) scope, so the rebuild opener opened nothing, and the founder had to decide a card for a packet already known to be dirty before anything was rechecked, which inverts the product requirements. Nothing was sent, but the order was wrong. The invariant D13-4 now reads: human state beats every automatic start of the held work. An instance's interrupt holds that instance and the rebuild entries it writes; writers upstream of it still rebuild inside the run (ADR-P02 S1); `approve` is refused `target_changing` while the target is dirty (ADR-P17 S1). The catch: a rebuild can now run while a person reads a card about the same candidate, so the card refetches on the invalidation events the stream already carries (ADR-P19 D5), and the CLI prints the target's freshness on `aksha pending`.
2. S2, follow-up input vetoes a finish. A pending `follow_up` input (ADR-P27) turns a proposed DONE into CONTINUE with the text as the reason. The precedence stays here: The Task policy reads `pending_input`, so no second owner of the stop appears.
3. S3, every new automatic start calls the predicate. Durable timers (ADR-P25) and the partial-result finalizer (ADR-P24) are automatic starts and call `hold_reason` first. The `partial` outcome literal, which no path reached, is now reached by ADR-P24.

### Amendment from the build: the recitation policy (27 Nov 2026, S4, Proposed)

D4's closed set gains a fifth opt-in, `Recite(render, max_chars)`, built for the supervisor Task's work board (ADR-P15 S5).

1. `render` is a pure function over the instance's `TaskView` that returns lines. On every turn of that Task, `prepare` joins them, cuts the text at `max_chars` (default 12,000) with a line saying so, and returns it as `Prep.data`, a new trailing field. A render that raises recites one line naming the error; it never widens tools or fails the turn.
2. Context assembly places each recitation as a `user` message after the request, under a fence line (`RECITATION`: data the runtime derived from the event store, which gives no instructions), never among the system instructions. A recitation is not recorded: it is a projection of the log as of the turn, so a restart renders it again identically.
3. It never vetoes and sets no limit. What the supervisor may do with what it reads stays bounded by its Task's tool list (only `commit_round`) and by that tool's checks.

The catch: the cap bounds characters, not reads. A board of thousands of items needs the scale path's read budget and paging, which are not built.

### Amendment from the build: a limit ends an instance as a finish would (28 Nov 2026, S5, Proposed)

A live patent run on a general model ended `blocked` with correct work saved. `recommend[C2]` was refused twice on finish, saved a third version that met its completion check on its sixth and last allowed turn, and asked for nothing else. The next stop evaluation applied the turn limit to the proposed CONTINUE and failed the instance, and its parent with it, without running the check against that version. D2 of ADR-P15 runs the check only on entry and on a finish, and D3 above applied a limit as an unconditional FAIL, so a model that saves passing work on its last turn and does not also call `finish` lost it.

1. S5.1, the check first. When the Task policy's turn limit bites on a leaf instance on a candidate for that instance that follows one of its turns (a `tools` or `malformed` candidate at the end of its own turn, or a `next` candidate when the scheduler picks the still-active leaf again, after a hold is answered or a resume), it evaluates the leaf's completion check exactly as a finish would, on the saved state the evaluation reads. When the check passes, the limit's verdict is DONE; when it fails, FAIL, as before. Either way the evaluation is recorded in the same transaction as `completion_check.evaluated{on: "limit"}`, with the check's message when it fails, and `stop.decided` still names `limit: turn_limit`, its counts and the CONTINUE it replaced. The check is the same pure function over the same view, so a replay of the log decides the same.
2. S5.2, where it does not apply. A finish or an entry candidate keeps the FAIL: the judge has just evaluated the check there and it failed, and a second evaluation would only repeat the fact. A follow-up or steer still pending for the instance keeps the FAIL, since it would veto a finish (ADR-P27 D4); the limit reads the pending input itself, as the finish does. Parents and supervisors take no turns of their own and already evaluate their checks on every candidate. The stop-veto limit bites only on a finish whose check just failed. The budget limit is a HOLD that keeps the work, not a FAIL, and is unchanged.
3. S5.3, the catch. A log recorded before this rule may hold a `stop.decided` that failed an instance at its turn limit with passing work saved; driving that log again decides DONE there, so such a recording is stale. The bundled recordings hold no turn-limit decision. A check that passes on a partial save now ends the instance where it used to fail it: the check, not the turn count, says what done means, as it already does on a finish.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. Hand-ordered flags, first match wins | Simple | The precedence is line order; a third author edits the handler | Rejected |
| B. Stop hooks that block with a reason | A familiar pattern; a reason the model reads | No order between two blocking hooks; an in-memory loop guard resets each turn; the hook is configuration beside the Task it judges | Rejected as the owner; its "reason the model reads" is kept as CONTINUE's reason |
| C. A hook that admits a message, any message continues | Flexible | Any extension can keep the run going; no veto record | Rejected |
| D. Graph edges, any trigger beats the end | Precedence is fixed in one place | The recursion limit is a per-invocation counter that a resume resets; "done" is "nothing triggered" | Rejected |
| E. A stack journalled with push and pop entries | Named ownership; composes by position | Push and pop are stored, so the stack can disagree with what the events imply; the ADR's own "no path decides outside the walk" is not held in code | Kept as the shape; rejected as storage |
| F. A stack derived from the event store, one recorded evaluation per candidate (the maintainer's call) | No push or pop event to disagree; every decision is a record replay can compare | About 40 extra events per Research run and 100 per Patent run (estimate); a reader must learn that the stack is a view | Chosen |
| G. Human state checked by each automatic path | Each path is simple | The rule is a convention every new path must remember | Rejected in favour of D6: one predicate, called by every path, proved by D7 |


## Interface

```python
# aksha/runtime/api.py: The extension API's part of this primitive (proposal; design only)
@dataclass(frozen=True)
class StopPolicyOptIn:                          # built-ins only (D4)
    name: Literal["goal", "plan", "forced_tool", "todo_reminder"]
    params: Mapping[str, object]

def Goal(check: "Check") -> StopPolicyOptIn: ...
def Plan(kind: str, accept: "Check") -> StopPolicyOptIn: ...
def ForcedTool(tool: str, when: "Check") -> StopPolicyOptIn: ...
def TodoReminder(every: int) -> StopPolicyOptIn: ...
```

```python
# aksha/runtime/stop.py (proposal; design only)
CandidateKind = Literal["enter", "next", "tools", "finish", "malformed", "interrupted",
                        "approval", "budget", "fault", "resume", "overflow"]
                        # v2 kind "paused" renamed "interrupted"

@dataclass(frozen=True)
class StopCandidate:
    run: str
    seq_no: int                                 # one more than the run's last stop decision
    kind: CandidateKind
    instance: "InstanceRef | None"              # None for run-level "next", "fault", "resume"
    after_turn: int | None
    seq: int                                    # the last event sequence this evaluation may read
    remaining: "Remaining"                      # from the budget accounting, read before any policy judges
    lost_kind: CandidateKind | None = None      # on "resume": The candidate a crash lost

VerdictKind = Literal["pass", "continue", "push", "hold", "done", "fail", "stop"]
Outcome = Literal["complete", "partial", "failed", "stopped"]          # "stopped" is ADR-P18's

@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    reason: str | None = None                   # continue: The model reads it; hold, fail: a person reads it
    child: "PolicyRef | None" = None            # push only
    outcome: Outcome | None = None              # stop only
    checks: tuple[tuple[str, "CheckResult"], ...] = ()
    raises: "InterruptRequest | None" = None    # push of the budget interrupt only

@dataclass(frozen=True)
class Prep:
    guidance: tuple[str, ...] = ()                 # outer policy first
    tools: frozenset[str] | None = None         # None: no narrowing; else intersected, never widened

class StopPolicy(Protocol):
    ref: "PolicyRef"
    def prepare(self, view: "RunView") -> Prep: ...
    def judge(self, view: "RunView", c: StopCandidate) -> Verdict: ...
    def limit(self, view: "RunView", c: StopCandidate, proposed: Verdict) -> Verdict | None: ...

@dataclass(frozen=True)
class StopDecision:                             # the stop.decided record
    seq_no: int
    candidate: CandidateKind
    instance: "InstanceRef | None"
    stack: tuple[str, ...]                      # policy ids, outer to inner, as derived
    trail: tuple[tuple[str, VerdictKind], ...]
    decided_by: str
    verdict: Verdict                            # only CONTINUE, HOLD or STOP end an evaluation
    replaced: tuple[str, VerdictKind] | None    # which limit bit, and what it replaced
    counts: Mapping[str, int]                   # turns, calls, vetoes behind any limit

def derive_stack(view: "RunView", c: StopCandidate) -> tuple[StopPolicy, ...]: ...
def evaluate_stop(view: "RunView", c: StopCandidate) -> StopDecision:
    """Pure. Steps 1 to 5 of the diagram. Writes nothing."""
def commit_stop_decision(store: "Store", owner: str, d: StopDecision) -> None:
    """Step 6, one transaction. AlreadyRecorded on the stop:{run}/{seq_no} key means a restart got here first."""
def compose_prep(stack: tuple[StopPolicy, ...], view: "RunView") -> Prep: ...

# D6, the hold predicate
HoldScope = Union["InstanceRef", "RunRef", "AgentScope", "SessionRef"]   # AgentScope = (owner, agent)
HoldReason = Literal["stop_requested", "interrupt_open", "stopped_by_person"]

@dataclass(frozen=True)
class Hold:
    reason: HoldReason
    by: str                                     # the event id that holds it
    scope: HoldScope

def hold_reason(view: "RunView", scope: HoldScope) -> Hold | None:
    """The one predicate every automatic start path calls (D6). Pure; a reducer over the event store.
    S1: InstanceRef: an open interrupt on that instance, or a hold on its run.
    RunRef: a stop request or an open run-scoped interrupt (the budget interrupt) only.
    AgentScope: a stop request or a standing hold (ADR-P18 D4) only, never an instance's interrupt.
    SessionRef: an open route interrupt on that session (ADR-P23 D4); no run opens for that message."""

def rebuild_blocked(view: "RunView", entry: "RebuildEntry") -> Hold | None:
    """S1: holds one rebuild entry, not the agent: its writer instance is suspended, or its run or
    (owner, agent) is held. Writers upstream of a suspended instance still rebuild."""

AutoCause = Literal["scheduler", "resume", "rebuild", "restart"]       # automatic
PersonCause = str                                                      # "command:send", "command:handoff"
```

Events this primitive writes, all inside `commit_stop_decision`'s transaction: `stop.decided` (`walk.decided`); and as the decision implies, `completion_check.evaluated`, `task.entered`, `budget.reached` with the budget interrupt's `interrupt.raised`, `interrupt.voided{reason: run_ended | run_stopped}`, and `run.ended{outcome}`.

Projections it reads: Task tree and instance status (ADR-P15), open interrupts and approvals (ADR-P17), stop requests and standing holds (ADR-P18), the budget accounting (ADR-P14), earlier stop decisions (veto counts, turn counts, the restart limit's resume count, ADR-P02), artifacts and their freshness for checks (ADR-P04, ADR-P05), and the agent definition version hash (ADR-P20).

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Fits | The model says done after `matrix` with two blank cells | Nothing new: The Task policy returns CONTINUE with the check's message; the hold predicate holds the run while the scope question is open |
| Patent | Needs a runtime change | The change set lands while A-0001 is `requested`; `hold_reason((owner, patent))` is `interrupt_open`, so no rebuild opens | S1 narrows the hold to the held instance and its readers; `rebuild_blocked` per entry |
| Coding | Fits | The model says done before running tests | Nothing new: The goal condition reads the latest test report; S2 adds follow-up input |

Verdict after the amendments: fits for all three once S1 lands. S1 is first-milestone work, because it is on the Patent demo's path.

Open for the maintainer: S1 changes D13-4's wording from "every automatic start" to "every automatic start of the held work" (lean: accept).
