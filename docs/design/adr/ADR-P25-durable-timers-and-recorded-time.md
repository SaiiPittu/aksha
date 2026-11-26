# ADR-P25: Durable timers and time as a recorded input (lean: a timer is a fact on the injected clock, firing is an automatic start gated by the hold predicate, and a read of the current date is a lineage input)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the Research stress test (the Research stress review, row 7, missing primitive M3) and the Patent stress test (the Patent stress review, missing primitive "time as a recorded input"). ADR-P17 kept approval `expires_at` as a null field with no owner, and ADR-P16 named "a scheduled wake that forgets the predicate" as an unguarded path. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026, and none of it is first-milestone work. Built on 26 Nov 2026 in `src/aksha/runtime/timers/`; the amendments from the build below record where the code differs.

## Terms

The terms:

- A **durable timer** is a recorded fact that something should happen at a time on the injected clock, fired by the runtime after a restart as well as before one.
- A **freshness lifetime** (RFC 9111's term) is how long a source record or artifact stays current before it is treated as out of date, here `max_age_s`.
- A **recorded time read** is a lineage input that says "this output depended on today's date".

## Context: what breaks without it

| Without it | What happens | Who |
|---|---|---|
| Change detected only by command | Live research never goes out of date by itself; a pricing page fetched in March is current in June | Research Agent (row 7) |
| "Re-check competitor pricing weekly" | Nothing can schedule it | Research Agent |
| Approval expiry | `expires_at` exists and nothing fires it | Every agent with approvals |
| An output built from today's date | Nothing records the dependency, so a later run on another date reuses it | Patent Agent (grace periods; it avoids this by asking counsel, the product requirements) |

## Decision

1. D1, a timer is a fact. `timer.scheduled{timer_id, scope, due_at, cause}` is written in the transaction that wants it; `timer.fired{timer_id}` or `timer.cancelled{timer_id}` closes it. `due_at` comes from the injected clock, so fake runs are deterministic.
2. D2, firing is an automatic start. The scheduler and recovery fire due timers with cause `timer`. Each firing that starts work calls `hold_reason` first (ADR-P16 S3) and counts toward the automatic-start limit (ADR-P14 S3), so a held scope never wakes and a timer loop is capped. A firing whose cause only voids (`approval_expiry`, D5) starts no work, so it skips the hold check: otherwise the open approval it exists to void would hold it forever.
3. D3, freshness lifetimes become facts. A versioned source or an artifact kind may declare `max_age_s`. A save or fetch schedules a `recheck` timer at `fetched_at + max_age_s`; firing it writes `source.expired{record}` through `record_source_changes` (ADR-P05 S2), and ordinary invalidation follows.
4. D4, the current date is a read and a recorded source. A tool that needs the date calls `observe_time(ctx)`, which returns an `InputRef(kind="time", id="clock", version=<ISO date>)` and records it as a read. The runtime also records the date itself: at every run start, and at each day boundary while a run is open, it writes the current date as the `time/clock` source version through `record_source_changes` (ADR-P05 S2, cause `clock`). Every artifact that read an older date is then invalidated before any reuse decision, whether or not the later run calls `observe_time`, so reuse always checks the recorded time.
5. D5, approval expiry is a timer with cause `approval_expiry`, scoped to the approval itself (its approval ID), not to the instance that raised it; firing voids the approval `expired`. The open approval interrupt therefore never holds its own expiry timer (D2).

### Amendments from the build (26 Nov 2026, Proposed)

1. B1, an interim date for tool bodies (D4). `observe_time` is not built. Until it is, `ToolContext.run_date` (extension API 2.2) gives a tool body the ISO date of its run's recorded `run.started`. The agent loop reads it from the log, so it comes from the injected clock and is the same on every replay. It is not a recorded read, and no day boundary invalidates anything built on it: that is still D4's job. The patent agent's source world counts elapsed days to it when it reads through the session's record reader.
2. B2, `observe_time` is built, and reading `run_date` is the same recorded read (D4; amends B1). `observe_time(ctx)` returns `InputRef(kind="time", id="clock", version=<ISO date>)`, and the lineage input kind `time` is new. Every attempt of a dispatched body gets a fresh `TimeReads` collector (`ToolContext.time_reads`, extension API 2.6), and each read of `ToolContext.run_date` adds `time:clock@<date>` to it, so a body written against B1 gets D4's invalidation with no change: the patent agent's tools, which count elapsed days to `run_date`, now record the date they counted to. The loop records the attempt's time reads with the body's own `Ok.reads`, and an error result records none, as before. `run_date` itself is now the later of the run's recorded `run.started` date and the last date recorded for the owner (D4's `time/clock` record), read from the log as of the step, so a replay sees the first attempt's date and a run open across midnight counts to the new day once it is recorded. Without that, a body in a long run would keep reading the start date after the new day was recorded, and every version it saved would be born invalidated. The cost, until an agent calls `observe_time` only where it counts days: a tool that merely receives the date (the patent tools all build their world from it) records it, so a new day invalidates more than the elapsed-day facts.
3. B3, `after_ms` as well as `due_at` (D1). A timer scheduled inside a step's result has no clock reading at hand: the step's facts are built before the store stamps them. `timer.scheduled` carries exactly one of `due_at` (an ISO time from the injected clock) and `after_ms`, counted from the scheduling fact's own recorded `at`, which the store takes from the injected clock. The projection's `due_at` is that sum, and `due(view, now)` compares it with the caller's clock reading, never a clock of its own. Recheck timers and approval expiry use `after_ms`. The interface's `schedule(tx, timer)` is `timers.facts.scheduled(timer)`: the runtime has no transaction object, so it builds the fact and the caller commits it with the facts that want it.
4. B4, the date is recorded when it can matter, not at every run start (D4). Recording the date at every run start would add a `source.changed` to every run of every agent, including the ones that never read the date. `timers.clock.date_change` records the injected clock's date, through `record_source_changes` with cause `clock`, only when it differs from the last recorded date and either an open run started on an earlier day or a recorded `time:clock` read names an earlier date. A run that starts the same day as every date it could have read needs no record, because its `run.started` date is the date its bodies count to. The session runs this before each pass and each resume (`tick`), so the date is recorded before any stop-policy evaluation can reuse a version. The clock source is recorded even before anything read it, so its first record is not lost.
5. B5, firing by cause, each in its own boundary, one close per timer (D2, D3, D5). An expiry commits in U4 with the approval it voids, a recheck in U8 with the `source.expired` it writes and the revocations and review voids that confirms (ADR-P05 D9), a reminder in U1 with the run it opens. `timer.fired` and `timer.cancelled` share the dedup key `tclose:{timer_id}`, so a firing retried after a crash, or raced by another firing path, writes nothing the second time. An expiry whose approval was already decided is closed `timer.cancelled{reason: moot}`. A granted approval that was not used yet expires too (`approval.voided{reason: expired}`, a new reason), with its interrupt `interrupt.voided{reason: expired}` when still open; a used, rejected or voided one is moot. The lifetime is declared on the tool: `ApprovalRule.expires_after_s` (extension API 2.6), the owner ADR-P17 S7 left open; the loop schedules `approval_expiry:<approval ID>` in the transaction of the request.
6. B6, a reminder opens a run in its session (D2). The ADR named the `reminder` cause but not what its firing starts. A reminder is scoped to a session (`SessionScope`) and names the agent and the request text (`Reminder`); firing writes `run.started{cause: timer, timer, request}` in that session, with the fields a person's start of that agent records, and the run reads its request from `run.started`. It waits, staying pending, while `hold_reason` holds the agent (a person's standing stop) or the session (an open route question), while the session has an open run (its one lane, ADR-P23 D7), and while the agent's timer-opened runs fill the automatic-start window (`Limits.auto_starts`, counted by `budgets.limits.timer_starts`, because no run of its own exists to count against before it opens). `run.started{cause: timer}` is an automatic start (`budgets.records`), and the TC-4 audit judges it against the agent's stop and the session's open questions. No named command or tool schedules a reminder yet.
7. B7, `source.expired` and how the view reads it (D3). Its payload is `{source_version, cause, records, artifacts}`; it counts as a source change (the owner's `source_version` moves). A record read of a version saved before the record's last `source.expired` is `moved` to `expired`, and a version saved after it read the record again and is current: the expiry is of a fetch, so a rebuild that fetches again settles it. A version whose own kind's lifetime ran out (`ArtifactKind.max_age_s`, extension API 2.6) carries one more read, `time:expiry/<ref>`, `moved` to `expired`, so the plan, the rebuild brief and the revocations treat it as any invalidated version; the brief names it as why the version changed, never as a read to take again. A step schedules one recheck per record read from a collection with a lifetime unless one is already pending (the oldest fetch expires first and covers every version saved before it), and one per saved version of a kind with one. The cost of that dedupe: a version built on a later fetch of the same record, saved while the first recheck is pending, expires with the first fetch, early by up to one lifetime and never late. The context's stale-result check judges a record's expiry only through the invalidation view (a tool result has no save to compare with), and it now marks a dated result stale once a later date is recorded.
8. B8, the store finds the owners with pending timers. A timer can be due for an owner with no open run (a reminder, a recheck after the run ended). `EventStore.owners_with_pending_timers()` (both adapters, one SQL query over the dedup keys) lists them, and the runtime thread ticks them with the owners that have open runs.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. An outside scheduler (cron) sends commands | No new runtime code | Not replayable; a crash between cron and command loses the wake; the hold predicate is bypassed unless every caller remembers it | Rejected |
| B. Timers as durable facts in the workflow log | Survives restarts; one owner; testable on a fake clock | One more event family | Chosen |
| C. Freshness by HTTP cache headers only | Standard | Fixture sources have no headers; agent data needs declared lifetimes | Adopted as the name, not the mechanism |

## Interface

```python
# aksha/runtime/timers/ (built; see the amendments from the build)
@dataclass(frozen=True)
class TimerSet:
    timer_id: str
    scope: "HoldScope | ApprovalRef"             # ApprovalRef only for approval_expiry (D5)
    due_at: str                                   # ISO time from the injected clock
    cause: Literal["approval_expiry", "reminder", "recheck"]

def schedule(tx: "Transaction", timer: TimerSet) -> None: ...
def due(view: "LogView", now: str) -> tuple[TimerSet, ...]: ...      # projection
def observe_time(ctx: "ToolContext") -> "InputRef": ...

# VersionedSourceSpec.max_age_s: int | None; ArtifactKind.max_age_s: int | None
```

## Stress test (25 Nov 2026)

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits | Row 7: a March pricing page is invalidated on the fake clock in April |
| Patent | Fits | Approval expiry is D5; grace-period arithmetic stays with counsel |
| Coding | Fits | Reminders and expiry only |

## Sources (public)

1. RFC 9111, HTTP Caching, section 4.2 (freshness lifetime).
