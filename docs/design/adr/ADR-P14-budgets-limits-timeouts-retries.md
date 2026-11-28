# ADR-P14: Budgets, limits, timeouts and bounded retries (every cap is counted from the event store, so a restart never resets it)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. The decisions log records no call of the maintainer's on budgets or limits. The pattern behind the main rule, store facts and derive interpretations (the design patterns note P-2), is in the patterns file but is not itself an accepted decision record, so the whole ADR stays Proposed. Amended 25 Nov 2026 after the three-agent stress test: S1 to S3 below are new and Proposed.

## Terms

Primitive PR-14 in the primitives catalogue, cluster C (acting safely). Depends on PR-01 (the event store every count reads) and PR-02 (the durable step executor, which records attempts). Needed by PR-11 (the policy gate refuses `budget_exhausted`) and PR-16 (stop-policy evaluation replaces a proposed continue when a limit bites).

Terms used here, in the industry names of the design glossary:

- **Budget** caps what one run may spend, in several dimensions: model turns, tool calls, active time, and later tokens and money.
- **Budget accounting** (v2's "budget meter") is the projection, built by a reducer over the event store, that says how much of each dimension a run has used and is allowed.
- **Limit** is a cap on one thing inside a run: The turn limit per Task instance, the stop-veto limit (how many times a completion check may refuse a proposed finish), the restart limit (how many recoveries after crashes) and the automatic-start limit.
- **Timeout** abandons one attempt after a fixed time. **Bounded retry** repeats a failed attempt at most N times, with backoff, only for failures that a retry can fix.
- **Budget interrupt** (v2's "BudgetWait") is the interrupt the stop policy raises when a budget is spent, so a person can grant more or stop with the partial work kept.
- **Active time** is the sum of the durations of recorded steps and backoffs. Time spent suspended on an interrupt is not active.

## Context: a counter in memory turns "three tries" into "three tries per process"

The product requirements require enforced tool-call limits, timeouts and bounded retries, and asks that a run that hits any of them keep its useful partial work. Two observations shape the answer.

1. **Budgets stated in prompts do not hold.** A budget stated only in the prompt is advice the model may ignore, so the cap must be in code.
2. **Counters kept in memory reset on restart.** A model-call limit, a step budget or a stop-hook guard flag held in process memory resets when the process does, and a stop-hook path with no cap at all lets a hook that ignores the flag loop forever. The durable shape is a cap counted from stored records: for example, at most 3 restart continuations in 5 minutes, counted by walking stored parent links, and 3 resends, counted from outbox attempts.

The contrast coding agent is where a reset counter does damage: it runs long, crashes more often than a short document run (a test suite that exhausts memory, a laptop that sleeps), and a retry loop that resets on each recovery can spin for hours. A durable harness without durable counters is not bounded.

## Decision

1. **Every cap is a projection over the event store; there is no counter field anywhere.** Budget accounting, every limit and every retry count are computed by reducers over recorded events and cached by last sequence number (PR-01). A restarted process rebuilds the same numbers. This is the rule that makes the rest hold.
2. **What counts, exactly** (05a section 4.5, kept):

   | Dimension | Counted from | Notes |
   |---|---|---|
   | Model turns | Distinct turns named by stop decision records whose verdict is CONTINUE | A turn is charged when the stop policy authorises it, so concurrency above 1 cannot overspend; a resumed turn adds nothing |
   | Tool calls | `step.started{kind: tool, counted: true}`, plus refused calls other than `budget_exhausted` | A retry adds attempts, never calls; refused calls count, so a model cannot loop on free refusals |
   | Active time | `duration_ms` and `backoff_ms` on `step.failed` and `step.completed` | Suspended time costs nothing; fake model mode measures on the injected clock; an attempt a crash interrupted is charged when its step resumes (B1) |
   | Allowed | The budget on `run.started` plus the `extra` of every `budget.granted` | A grant is a person's recorded command |

3. **Retry attempts are counted from the event store too (new rule).** The attempt number of a step is one plus the number of `step.failed` events recorded under its deterministic step ID (B1: an attempt a crash interrupted is recorded as one when its step resumes). After a crash, the durable step executor resumes the attempt count rather than starting at one, so `max_retries=2` means three attempts in total across every process that runs the step. v2 recorded attempts but did not say a restart continues the count.
4. **Bounded retry only for failures a retry can fix.** `retry_on` defaults to `{timeout, transient}`, except on an `at_most_once` step: once its `step.dispatched` is recorded, a timeout is never retried automatically. The step is closed `outcome_unknown` (ADR-P02 D4, ADR-P03 D4), and only reconciliation through a `lookup` (which an `at_most_once` tool does not have) or a person can say whether the effect landed. The executor checks for the step's `disp:{step_id}` record before every retry. A raising tool body (`tool_exception`), a schema refusal, an invalid model answer and an exec timeout are never retried, because each repeats. Backoff is fixed (base 500 ms, factor 2, cap 4 s, no jitter) and runs on the injected clock in fake model mode, so two fake runs are identical. When attempts run out, the step records an error result, the item it served stays unverified (ADR-P13's `Unknown(reason="unverified")`), and the run continues.
5. **A timeout abandons the attempt; it does not pretend to kill it.** A tool body on a worker thread cannot be killed in Python, so at the deadline the step executor records `step.failed{timeout}` and stops waiting. A late value from the abandoned attempt is recorded as `attempt.discarded` and saved nowhere; tool bodies never write the event store, which is what makes abandonment safe. An external effect started by an abandoned attempt of a `reconcile` tool is covered by its idempotency key and reconciliation (PR-03), as a crash would be; an abandoned attempt of an `at_most_once` tool is recorded `outcome_unknown` and never sent again, so a person or the tool's `on_unknown` interrupt settles it (D4). An exec's timeout does kill: The sandbox kills the process group at the deadline (ADR-P12).
6. **The gate refuses; the stop policy acts.** When budget accounting shows a dimension spent, the policy gate refuses the next tool call `budget_exhausted` (not counted) and skips the turn's remaining calls; it raises nothing. At the next turn end, stop-policy evaluation replaces the proposed CONTINUE with a budget interrupt and commits `budget.reached`, the interrupt-raised record and the stop decision record in one transaction. A person then grants more (`budget.granted`) or stops; either way the artifacts already saved stay intact, which is the product requirements' "useful partial work".
7. **Limits inside a run, each counted from the event store:**

   | Limit | Default | Counted from | When it bites |
   |---|---|---|---|
   | Turn limit per Task instance | none (decision 42; it was 10). An agent may declare one per Task | Authorised turns of that instance since it was last entered (a rebuild re-entry starts the count again, like the stop-veto limit) | The Task ends: done when its completion check passes on what is saved (ADR-P16 S5), otherwise it fails with "turn limit reached" |
   | Stop-veto limit per instance entry | 3 | Completion-check refusals since the instance was entered, the one being judged included (so the first two refusals are given back to the model and the third fails) | The Task is blocked, with the failing checks listed |
   | Restart limit (S2: ADR-P02 D8's rule) | 3 | Resume records since the run's last progress (a completed tool step or a saved artifact) | The run ends `failed, restart_limit` through a stop decision, never by boot directly |
   | Automatic-start limit (new, designed) | 3 in 5 minutes per run | Resume records, rebuild-run starts and continuation records, with their recorded timestamps (v2 dropped `run.resumed`, LC-3, so the resume record is the fact) | Further automatic starts are refused and the run is held for a person |

   The automatic-start limit is a restart-chain cap made general: any start the runtime core makes without a person's command (recovery on startup, a rebuild run after invalidation, a supervisor re-entry) counts. Its window uses recorded timestamps, so it is still a projection; in fake model mode the injected clock keeps it deterministic. It also feeds the hold invariant of D13-4: a held run never starts automatically, and this limit is the backstop when something is not held but keeps restarting.
8. **Budgets nest by narrowing.** An agent declares a budget; a Task may declare a smaller one for its subtree; a fanned-out Task instance may not exceed its parent's remainder. A supervisor Task's children draw from the supervisor's budget. Nothing widens a budget except a person's grant.
9. **Tokens and money are designed, recorded first.** Model steps record usage (input and output tokens, provider-reported cost) on `step.completed`. Budget dimensions `tokens` and `cost_micro_usd` reduce over those fields once PR-07 ships them. The first milestone enforces turns, tool calls and active time, which are what the product requirements name.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, the budget interrupt has a third answer, `finalize`. D6 offered grant or stop. A stop ends the run `stopped` and the Research Agent's `matrix` never runs, so the saved work is four per-vendor records and no competitor table, which fails the product requirements ("The run preserves useful partial work"). `finalize` marks every unstarted key `Unknown(reason="not_checked")`, runs the Tasks the agent declares `finalizer=True` under a small reserved budget, and ends the run `partial`. The mechanism is ADR-P24's; the command is `Finalize(run_id)` (ADR-P19 S4).
2. S2, the restart limit counts since progress. This ADR counted 3 restarts per run; ADR-P02 D8 counted resume records since the last progress. The coding review's 6-hour session with four crashes, each after real progress, ended `crash_loop` under this ADR and continued under D8. D8's rule is adopted and the limit row above is changed.
3. S3, timers are automatic starts. A durable timer firing (ADR-P25) counts toward the automatic-start limit and calls the hold predicate first, like a resume or a rebuild run.

### Amendments from the build (27 Nov 2026, Proposed)

1. B1, an attempt a crash interrupted counts, and its active time is charged (D2, D3). Before this, the attempt number was one plus the `step.failed` and `attempt.discarded` events under the step ID, and active time was summed from `duration_ms` on `step.failed` and `step.completed`. An attempt interrupted by a crash records neither, so recovery computed the same attempt number again (a body that kills the process ran once per restart, bounded only by the restart limit, not by `max_retries`) and its time was never charged (repeated crash-and-recover cycles could run past the active-time budget with the meter low). Now, when the durable step executor finds a dangling `at_least_once` step (a `step.started`, no `step.completed`), it first records the interrupted attempt as `step.failed{attempt: n, code: interrupted}`, where `n` is one plus the highest recorded attempt: the step's last record is its `step.started` or a retried `step.failed`, and in both cases the executor went straight on to attempt `n`. That record is written before the next attempt is dispatched, so the count holds across any number of crashes and `max_retries=2` is three attempts in all; with none left the step ends `error{interrupted}`. Its `duration_ms` is charged conservatively: the time on the injected clock from the attempt's start (the `step.started`, or the last failure plus its backoff) to the resume, capped at the step's `timeout_ms`, because an attempt is abandoned at its deadline and the time the process was down is not active time beyond that. A crash after the attempt began and before the body ran is charged and counted the same way; the count can only err high, never low. A dangling step whose last failure was not retried had no attempt in flight: it ends with that failure's code, and nothing runs. An `at_most_once` step is unchanged (its `step.dispatched` already commits before the body and it is never retried), and an effectful step's retries are the outbox worker's, counted from `outbox.claimed`, which commits before each delivery.
2. B2, live model steps retry patiently, with jitter and the provider's `Retry-After`, bounded by the run's active time (27 Nov 2026, Proposed). Live runs on a real provider ended `blocked` because a short provider spike (Gemini answering 503 "high demand", then timeouts) outlasted the model step's three attempts with backoffs of 0.5 and 1 s, so one model step's error failed a whole Task. D4's fixed backoff is kept for every step, and the generation model step now takes its retries from the agent's `Limits.max_retries` and `Limits.backoff` (declared in the Interface below, unused until now; their defaults are D4's two retries from 500 ms, so fake mode and every offline eval run exactly as before). Live mode raises them (ADR-P07 G7): `AKSHA_LIVE_RETRIES` retries (default 6, or the agent's own if higher), backoff from `AKSHA_LIVE_BACKOFF_MS` (2 s) doubling to `AKSHA_LIVE_BACKOFF_CAP_MS` (60 s), so 2, 4, 8, 16, 32 and 60 s, about two minutes in all. Three things are added to the executor's rule:
   - **Jitter.** `Limits.jitter` (default 0; `StepPolicy.jitter` on the step) is the share of each delay drawn at random; live mode sets 0.25, so a delay of 8 s is between 6 and 8 s and many runs that failed together do not retry together. With 0 there is no randomness, as D4 requires for replay. The runtime reads no ambient randomness (ADR-P02 D3), so the draw comes from `Limits.jitter_source`, which the composition root injects (`random.SystemRandom` in live mode); a jitter with no source is refused. `Backoff` itself is unchanged. The drawn delay is what `step.failed{backoff_ms}` records and what budget accounting charges, so the log says what was slept; a resumed step returns its recorded result and never redraws.
   - **The provider's wait.** A `TransientStepError` (and a `TransientModelError`, which the model step maps onto it) may carry `retry_after_ms`, what the failure asked to wait; the backoff is the larger of the policy's delay and that wait, which is honoured up to ten minutes. The LiteLLM adapter reads it from the response headers LiteLLM keeps (`retry-after-ms`, else `Retry-After` in seconds or as a date) or from a Gemini error body's `retryDelay`.
   - **The wait is bounded by the active-time budget.** `StepPolicy.wait_cap_ms` bounds one step's attempts and backoffs together; a retry whose backoff would reach it is not made, and the step ends with the failure's code (`model_unavailable` for a model call). The agent loop sets it, for each turn's model step and summary step, to what is left of the run's active time when the turn starts (`meter(...).remaining("active_ms")`), so a long outage ends the step instead of spending the budget sleeping, and D6 still decides what follows.

   What is retried is unchanged: only codes in `retry_on`, which for the model step are `model_transient` and `timeout`. A rate limit (429), a 5xx, a timeout and a lost connection are transient (ADR-P07 G6); an authentication error, a bad request, a missing model, a context-window refusal and a content-policy block are permanent and never retried. Each attempt is still its own `step.failed`, with the provider's error. Not covered: a fast-class call made inside a tool body (ADR-P07 G1) is retried by its tool's own policy, which runs the whole body again, so those keep their tool's retries; and `wait_cap_ms` counts within one process, so after a crash the remaining wait is counted again from the resume (the budget still stops the run at the next turn end).

The coding agent raises the defaults (25 turns, 40 calls, 120 seconds of active time suit document runs). That is declared in the agent's `limits` field (ADR-P20 S1), not a core change.

### Amendment L1: limits scaled for a live model (28 Nov 2026, Proposed)

The agents' Task turn limits and run budgets were sized against the offline fakes, which save right on their first call. A live model on the research agent ran out of turns (five for one Task) while fixing refused saves. Live mode now multiplies the counts a slower, fallible model spends more of:

1. L1a, `AKSHA_LIVE_TURN_FACTOR` (a whole number from 1, default 3; anything else is refused `live_config_missing` before any call). In live mode the run budget's `model_turns` and `tool_calls` are multiplied by it at bind (`app.scaled_limits`), and `run.started` records the factor as `turn_factor` beside the scaled `budget`. The stop policy multiplies every Task's `turn_limit` (and `limit_reached` the agent-wide one) by the factor its run's `run.started` records (`budgets.limits.turn_factor`), so the limit is read from the log: a resumed run keeps the factor it started under, and the definition, its hash and the compiled tree are unchanged. The veto, restart and retry limits and the time caps are not scaled.
2. L1b, fake mode, so every offline eval and test, records no factor and runs under the declared limits exactly as before. A recording's manifest keeps each agent's factor (`turn_factors`) and a replay binds the agent under it, so a replay of a live recording runs under the limits the recording ran under. It also keeps each agent's active-time cap, model step timeout, retries and backoff (`limits`, `app.run_limits`), and a replay binds the agent under those with no jitter, never under the replaying machine's live settings.
3. Not done: a portfolio tier's declared effort (the deep budget the supervisor commits against) is not scaled, so in a live run the agent's budget, not the tier's effort, is the bound on deep work.
4. L1c (28 Nov 2026, decision 42). Since Tasks declare no turn limit by default (ADR-P15 S8), the factor scales only the limits that exist: the run budget's `model_turns` and `tool_calls`, and the turn limit of a Task that declares one (the agent-wide `Limits.turn_limit` is not read per instance, [limitations](../../limitations.md) item 24). The research and patent agents declare none, so for them it scales the run budget only. Their budgets stay the runaway backstop and were not raised: live research gets 270 model turns and 420 tool calls, live patent 480 and 2,700, each with at least an hour of active time.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. Budget in the prompt | The model is told its limits | No code | Fails the product requirements | Rejected |
| B. In-memory counters in the loop | A field incremented per call | Simple, fast | Resets on every restart; a crash loop is unbounded | Rejected |
| C. Per-sample limits in an eval runner | Message, token, time and working-time limits per sample | Rich dimensions, working time separate from wall time | Scoped to one eval sample, not a durable multi-day run | Taken for the dimensions and for "active time excludes waiting" |
| D. Caps counted from stored records | Walk stored links and attempts to count | Survives restarts; auditable | Each count is a query; needs the projection cache | Chosen, generalised to every cap (decision 1) |
| E. Kill a timed-out thread | Interrupt the worker | Frees the worker | Not possible without a process boundary | Rejected; abandon and discard instead, kill only exec process groups |
| F. Retry everything up to N | One retry rule for all failures | Fewer spurious failures | Retries bugs and invalid answers, spending budget on repeats | Rejected; retry only `timeout` and `transient` |

## Interface

```python
# aksha/runtime/budgets.py (proposal; design only). Exported through aksha/runtime/api.py.
Dimension = Literal["model_turns", "tool_calls", "active_ms", "tokens", "cost_micro_usd"]

@dataclass(frozen=True)
class Backoff:
    base_ms: int = 500
    factor: int = 2
    cap_ms: int = 4_000                              # no jitter: replay must be identical

@dataclass(frozen=True)
class Budget:                                        # the agent's, a Task's, or the runtime default
    model_turns: int = 25
    tool_calls: int = 40
    active_ms: int = 120_000
    tokens: int | None = None                        # designed; enforced once PR-07 records usage
    cost_micro_usd: int | None = None                # designed
    def narrow(self, other: "Budget") -> "Budget": ...   # field-wise minimum

@dataclass(frozen=True)
class Limits:
    turn_limit: int | None = None                    # per Task instance; none by default (decision 42)
    stop_veto_limit: int = 3                         # per instance entry
    restart_limit: int = 3                           # per run
    auto_starts: tuple[int, int] = (3, 300_000)      # count, window in ms (designed)
    max_retries: int = 2                             # per model step (B2), attempts = max_retries + 1
    tool_timeout_ms: int = 10_000
    model_timeout_ms: int = 60_000
    backoff: Backoff = Backoff()
    jitter: float = 0.0                              # B2: share of each delay drawn at random
    jitter_source: Callable[[], float] | None = None # B2: injected; required when jitter > 0

@dataclass(frozen=True)
class Meter:                                         # budget accounting, a projection
    used: Mapping[Dimension, int]
    allowed: Mapping[Dimension, int]                 # declared budget plus every grant
    def exhausted(self) -> tuple[Dimension, ...]: ...
    def remaining(self, d: Dimension) -> int: ...

def meter(view: "RunView", run_id: str, scope: str | None = None) -> Meter: ...      # reducer; scope = Task path
def attempt_number(view: "RunView", step_id: str) -> int: ...                       # 1 + count of step.failed
def limit_reached(view: "RunView", run_id: str, instance: str | None,
                  limits: Limits) -> tuple["LimitHit", ...]: ...                    # reducer
def auto_start_allowed(view: "RunView", run_id: str, limits: Limits) -> bool: ...   # reducer (designed)
def next_backoff_ms(attempt: int, b: Backoff) -> int: ...                           # pure
```

Events it writes (v2 names, until the event-store ADR): `budget.reached{limit, used, allowed, partial}`, committed by stop-policy evaluation with the budget interrupt; `budget.granted{interrupt_id, by, extra}` from a person's command; `attempt.discarded{step_id, attempt}` for a late result. Limit hits other than budget are entries in the stop decision record's trail, not events of their own.

Projections it reads: The stop decision records (for turns), `step.started`, `step.completed` and `step.failed` (for calls, active time and attempts), resume records (for the restart limit), `run.started` and `budget.granted` (for allowed amounts). Every one is rebuilt from sequence 0 by `meter` and the limit reducers.

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Needs a runtime change | The 40-call budget runs out during `fetch_extract[COMP-04]`; the founder does not want to grant more | S1 `finalize` ends the run `partial` with a competitor table (ADR-P24) |
| Patent | Fits | `fetch_publication` times out on attempt 2, the process dies, attempt 3 runs after restart | Nothing new: attempts are counted from the event store (D3) |
| Coding | Needs a runtime change | Four crashes in six hours, each after progress, end the run `crash_loop` | S2 adopts ADR-P02 D8's restart rule |

Verdict after the amendments: fits for all three once S1 and S2 land. Both are first-milestone work: S1 for the product requirements, S2 because it is one projection either way.

Open for the maintainer: none new.

