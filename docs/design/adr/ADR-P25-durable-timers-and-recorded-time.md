# ADR-P25: Durable timers and time as a recorded input (lean: a timer is a fact on the injected clock, firing is an automatic start gated by the hold predicate, and a read of the current date is a lineage input)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the Research stress test (the Research stress review, row 7, missing primitive M3) and the Patent stress test (the Patent stress review, missing primitive "time as a recorded input"). ADR-P17 kept approval `expires_at` as a null field with no owner, and ADR-P16 named "a scheduled wake that forgets the predicate" as an unguarded path. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026, and none of it is first-milestone work.

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
2. D2, firing is an automatic start. The scheduler and recovery fire due timers with cause `timer`. Each firing calls `hold_reason` first (ADR-P16 S3) and counts toward the automatic-start limit (ADR-P14 S3), so a held scope never wakes and a timer loop is capped.
3. D3, freshness lifetimes become facts. A versioned source or an artifact kind may declare `max_age_s`. A save or fetch schedules a `recheck` timer at `fetched_at + max_age_s`; firing it writes `source.expired{record}` through `record_source_changes` (ADR-P05 S2), and ordinary invalidation follows.
4. D4, the current date is a read. A tool that needs the date calls `observe_time(ctx)`, which returns an `InputRef(kind="time", id="clock", version=<ISO date>)` and records it as a read, so an artifact built from today's date is invalidated when a later run observes another date.
5. D5, approval expiry is a timer with cause `approval_expiry`; firing voids the approval `expired`.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. An outside scheduler (cron) sends commands | No new runtime code | Not replayable; a crash between cron and command loses the wake; the hold predicate is bypassed unless every caller remembers it | Rejected |
| B. Timers as durable facts in the workflow log | Survives restarts; one owner; testable on a fake clock | One more event family | Chosen |
| C. Freshness by HTTP cache headers only | Standard | Fixture sources have no headers; agent data needs declared lifetimes | Adopted as the name, not the mechanism |

## Interface

```python
# aksha/runtime/timers.py (proposal; design only)
@dataclass(frozen=True)
class TimerSet:
    timer_id: str
    scope: "HoldScope"
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
