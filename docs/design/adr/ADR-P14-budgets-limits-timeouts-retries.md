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

The product requirements require enforced tool-call limits, timeouts and bounded retries, and asks that a run that hits any of them keep its useful partial work. Two findings from the comparison shape the answer.

1. **Budgets stated in prompts do not hold.** Ramp Labs' post of 21 Apr 2026 found coding agents ignore budgets stated in their prompts, so the cap must be in code.
2. **Counters kept in memory reset on restart.** ADK's `max_llm_calls`, LangGraph's step budget and Codex's `stop_hook_active` all reset when the process does (stop-ownership 01, section 1.2; LangGraph `pregel/_loop.py:1935-1936` at `732da520`). Codex's Stop-hook path has no cap at all on a hook that ignores the flag (stop-ownership 01, section 3.1). Claude Code caps consecutive Stop-hook blocks at eight (hooks guide). T3 Code is the counter-example that does it right: at most 3 restart continuations in 5 minutes, counted by walking stored parent links, and 3 resends, counted from outbox attempts (`RestartContinuation.ts:42-43`, `:290-310`; `ProviderRuntimeRecoveryService.ts:62`, `:458-478` at `ca81b7b9`).

The contrast coding agent is where a reset counter does damage: it runs long, crashes more often than a short document run (a test suite that exhausts memory, a laptop that sleeps), and a retry loop that resets on each recovery can spin for hours. A durable harness without durable counters is not bounded.

## Decision

1. **Every cap is a projection over the event store; there is no counter field anywhere.** Budget accounting, every limit and every retry count are computed by reducers over recorded events and cached by last sequence number (PR-01). A restarted process rebuilds the same numbers. This is the rule that makes the rest hold.
2. **What counts, exactly** (05a section 4.5, kept):

   | Dimension | Counted from | Notes |
   |---|---|---|
   | Model turns | Distinct turns named by stop decision records whose verdict is CONTINUE | A turn is charged when the stop policy authorises it, so concurrency above 1 cannot overspend; a resumed turn adds nothing |
   | Tool calls | `step.started{kind: tool, counted: true}`, plus refused calls other than `budget_exhausted` | A retry adds attempts, never calls; refused calls count, so a model cannot loop on free refusals |
   | Active time | `duration_ms` and `backoff_ms` on `step.failed` and `step.completed` | Suspended time costs nothing; fake model mode measures on the injected clock |
   | Allowed | The budget on `run.started` plus the `extra` of every `budget.granted` | A grant is a person's recorded command |

3. **Retry attempts are counted from the event store too (new rule).** The attempt number of a step is one plus the number of `step.failed` events recorded under its deterministic step ID. After a crash, the durable step executor resumes the attempt count rather than starting at one, so `max_retries=2` means three attempts in total across every process that runs the step. v2 recorded attempts but did not say a restart continues the count.
4. **Bounded retry only for failures a retry can fix.** `retry_on` defaults to `{timeout, transient}`. A raising tool body (`tool_exception`), a schema refusal, an invalid model answer and an exec timeout are never retried, because each repeats; the Vercel AI SDK makes the same split with `maxRetries` default 2 for transient provider failures and no retry for invalid answers (`packages/ai/src/extract/extract.ts:75`). Backoff is fixed (base 500 ms, factor 2, cap 4 s, no jitter) and runs on the injected clock in fake model mode, so two fake runs are identical. When attempts run out, the step records an error result, the item it served stays unverified (ADR-P13's `Unknown(reason="unverified")`), and the run continues.
5. **A timeout abandons the attempt; it does not pretend to kill it.** A tool body on a worker thread cannot be killed in Python, so at the deadline the step executor records `step.failed{timeout}` and stops waiting. A late value from the abandoned attempt is recorded as `attempt.discarded` and saved nowhere; tool bodies never write the event store, which is what makes abandonment safe. An external effect started by an abandoned attempt is covered by its idempotency key and reconciliation (PR-03), as a crash would be. An exec's timeout does kill: The sandbox kills the process group at the deadline (ADR-P12).
6. **The gate refuses; the stop policy acts.** When budget accounting shows a dimension spent, the policy gate refuses the next tool call `budget_exhausted` (not counted) and skips the turn's remaining calls; it raises nothing. At the next turn end, stop-policy evaluation replaces the proposed CONTINUE with a budget interrupt and commits `budget.reached`, the interrupt-raised record and the stop decision record in one transaction. A person then grants more (`budget.granted`) or stops; either way the artifacts already saved stay intact, which is the product requirements' "useful partial work".
7. **Limits inside a run, each counted from the event store:**

   | Limit | Default | Counted from | When it bites |
   |---|---|---|---|
   | Turn limit per Task instance | 10 | Authorised turns of that instance | The Task fails with "turn limit reached" |
   | Stop-veto limit per instance entry | 3 | Completion-check refusals since the instance was entered | The Task is blocked, with the failing checks listed |
   | Restart limit (S2: ADR-P02 D8's rule) | 3 | Resume records since the run's last progress (a completed tool step or a saved artifact) | The run ends `failed, restart_limit` through a stop decision, never by boot directly |
   | Automatic-start limit (new, designed) | 3 in 5 minutes per run | Resume records, rebuild-run starts and continuation records, with their recorded timestamps (v2 dropped `run.resumed`, LC-3, so the resume record is the fact) | Further automatic starts are refused and the run is held for a person |

   The automatic-start limit is T3 Code's chain cap made general: any start the runtime core makes without a person's command (recovery on startup, a rebuild run after invalidation, a supervisor re-entry) counts. Its window uses recorded timestamps, so it is still a projection; in fake model mode the injected clock keeps it deterministic. It also feeds the hold invariant of TC-4: a held run never starts automatically, and this limit is the backstop when something is not held but keeps restarting.
8. **Budgets nest by narrowing.** An agent declares a budget; a Task may declare a smaller one for its subtree; a fanned-out Task instance may not exceed its parent's remainder. A supervisor Task's children draw from the supervisor's budget. Nothing widens a budget except a person's grant.
9. **Tokens and money are designed, recorded first.** Model steps record usage (input and output tokens, provider-reported cost) on `step.completed`. Budget dimensions `tokens` and `cost_micro_usd` reduce over those fields once PR-07 ships them. The first milestone enforces turns, tool calls and active time, which are what the product requirements name.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, the budget interrupt has a third answer, `finalize`. D6 offered grant or stop. A stop ends the run `stopped` and the Research Agent's `matrix` never runs, so the saved work is four per-vendor records and no competitor table, which fails the product requirements ("The run preserves useful partial work"). `finalize` marks every unstarted key `Unknown(reason="not_checked")`, runs the Tasks the agent declares `finalizer=True` under a small reserved budget, and ends the run `partial`. The mechanism is ADR-P24's; the command is `Finalize(run_id)` (ADR-P19 S4).
2. S2, the restart limit counts since progress. This ADR counted 3 restarts per run; ADR-P02 D8 counted resume records since the last progress. The coding review's 6-hour session with four crashes, each after real progress, ended `crash_loop` under this ADR and continued under D8. D8's rule is adopted and the limit row above is changed.
3. S3, timers are automatic starts. A durable timer firing (ADR-P25) counts toward the automatic-start limit and calls the hold predicate first, like a resume or a rebuild run.

The coding agent raises the defaults (25 turns, 40 calls, 120 seconds of active time suit document runs). That is declared in the agent's `limits` field (ADR-P20 S1), not a core change.

## Alternatives considered

| Option | What it is | Prior art | For | Against | Verdict |
|---|---|---|---|---|---|
| A. Budget in the prompt | The model is told its limits | Ramp Labs, 21 Apr 2026, showing agents ignore them | No code | Fails the product requirements | Rejected |
| B. In-memory counters in the loop | A field incremented per call | ADK `max_llm_calls`; LangGraph step budget (`pregel/_loop.py:1935-1936` at `732da520`); Codex `stop_hook_active` | Simple, fast | Resets on every restart; a crash loop is unbounded | Rejected |
| C. Per-sample limits in an eval runner | Message, token, time and working-time limits per sample | Inspect AI, `src/inspect_ai/_util/working.py` at `a8b02a1` | Rich dimensions, working time separate from wall time | Scoped to one eval sample, not a durable multi-day run | Taken for the dimensions and for "active time excludes waiting" |
| D. Caps counted from stored records | Walk stored links and attempts to count | T3 Code (`RestartContinuation.ts:290-310`; `ProviderRuntimeRecoveryService.ts:458-478` at `ca81b7b9`); Pi Durable's `restartLimit` and DBOS's `max_recovery_attempts` (durability note) | Survives restarts; auditable | Each count is a query; needs the projection cache | Chosen, generalised to every cap (decision 1) |
| E. Kill a timed-out thread | Interrupt the worker | None safe in CPython; `concurrent.futures` cannot cancel a running call | Frees the worker | Not possible without a process boundary | Rejected; abandon and discard instead, kill only exec process groups |
| F. Retry everything up to N | One retry rule for all failures | Decode raised its tool retry budget from 1 to 5 because the default killed real runs (landscape note section 3) | Fewer spurious failures | Retries bugs and invalid answers, spending budget on repeats | Rejected; retry only `timeout` and `transient` |

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
    turn_limit: int = 10                             # per Task instance
    stop_veto_limit: int = 3                         # per instance entry
    restart_limit: int = 3                           # per run
    auto_starts: tuple[int, int] = (3, 300_000)      # count, window in ms (designed)
    max_retries: int = 2                             # per step, attempts = max_retries + 1
    tool_timeout_ms: int = 10_000
    model_timeout_ms: int = 60_000
    backoff: Backoff = Backoff()

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

## Sources (public)

1. Shaiyon Hariri, "Coding agents ignore their own budgets", Ramp Labs, 21 Apr 2026 (page).
