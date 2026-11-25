# Aksha design documents

*Status: intermediate design as of 26 Nov 2026. These documents record the design as it stood before the first code merged. Each decision carries the status its architecture decision record (ADR) gives it: Accepted where the maintainer has made the call, Proposed everywhere else. Proposed means designed and open to change, not built.*

Aksha is a general agent harness in Python: one runtime that runs many different agents, each defined in its own module, with durable state, enforced tool permissions, approvals that bind to an exact action, and recovery after a crash that never repeats a send. These documents explain what the harness is for, how it is put together, and the primitives it is built from.

## Reading order

| Order | Document | What it answers | Read it if you |
|---|---|---|---|
| 1 | [01-product-overview.md](01-product-overview.md) | What problem a general harness solves, for whom, what it will and will not do, and the principles behind every decision | are new to Aksha |
| 2 | `02-architecture.md` | How the runtime, the agents, the interfaces and the storage fit together | want the system shape before the details |
| 3 | `03-primitives-catalogue.md` | The 30 harness primitives, each in one line, with its status, its dependencies and the build order | want to know what exists and what comes next |
| 4 | `adr/` | One ADR per primitive: the options considered, the decision, its cost, and how three different agents stress-tested it | are reviewing or changing one primitive |

Documents 2 to 4 land in the same documentation change as this index. The code layout, the dependency rule between layers and how to add an agent are in [`../architecture/codebase-structure.md`](../architecture/codebase-structure.md).

## How to read a status

1. **Accepted**: the maintainer decided it, and code may rely on it.
2. **Proposed**: designed and reviewed on paper, not yet decided. Code that depends on it says so.
3. **Accepted in part**: named decisions inside the ADR are Accepted; every other part, including every amendment from the stress test, is Proposed.
4. **Open**: the options are listed and no shape is proposed.

Separately from status, each piece of work is labelled by what happened to it: designed (on paper only), built (code exists with tests), or built and run (exercised end to end). As of 26 Nov 2026, four primitives are built and tested on a branch in review: the event store and projections (ADR-P01), the agent definition and extension API (ADR-P20), the tool registry (ADR-P10) and the policy gate (ADR-P11). Everything else in these documents is designed only.

## Words these documents use

The documents use the industry name for each concept, one name per concept:

| Term | Meaning here |
|---|---|
| Runtime | The harness code that every agent shares; it holds no agent-specific code |
| Event store | The append-only, hash-chained record of every fact that happened; the only source of truth |
| Projection | A view computed from the event store by a pure function, called a reducer |
| Task | The recursive unit of work an agent declares; a Task can hold child Tasks, each with its own completion check |
| Completion check | A pure function that says whether a Task is done |
| Stop policy | A rule that decides, at each turn end, whether the agent may stop |
| Artifact | A typed, immutable, versioned output that records what it read |
| Lineage | The recorded reads of an artifact, so the runtime knows which inputs it came from |
| Invalidation | Marking exactly the work whose lineage reaches a changed input as out of date |
| Interrupt | The run asks for outside input and stops itself |
| Suspension | The run is durably parked with no process attached, for any reason |
| Transactional outbox | Every external effect is recorded before it is attempted, under an idempotency key, so a retry never sends twice |
| Model routing by class | Each model job goes to the kind of model its answer needs: extraction, classification or generation |

## Sources

1. The maintainer's decision log, entries 1 to 24, 25 and 26 Nov 2026.
2. The primitives index and the 30 primitive ADRs, ADR-P01 to ADR-P30, 25 Nov 2026.
3. The glossary that maps older working names to these industry names, 25 Nov 2026.
