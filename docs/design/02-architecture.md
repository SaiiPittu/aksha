# Aksha architecture

*Status: intermediate design as of 26 Nov 2026. This page describes the architecture as designed on that date, before the first code merged. Each part carries the status its architecture decision record (ADR) gives it: Accepted, Accepted in part, Proposed or Open, as defined in the [index](README.md). Where the maintainer's decision log settles a point that an ADR has not yet absorbed, the page names the decision by its number. As of 26 Nov 2026, four primitives have code with tests on a branch in review: the event store and projections (ADR-P01), the agent definition and extension API (ADR-P20), the tool registry (ADR-P10) and the policy gate (ADR-P11). Everything else here is designed only.*

Aksha is one runtime that many agents share, and every fact any of them produces goes into one append-only, hash-chained event store. Everything else in the system, from a Task's status to the trace a person inspects, is a projection computed from that store. The architecture is easiest to read at three levels of zoom, then through the life of one run:

| Level | Question it answers | Section |
|---|---|---|
| Context | What does the runtime talk to? | 1 |
| High-level design (HLD) | Which layers make up the runtime, and where does each of the 30 primitives sit? | 2 |
| Low-level design (LLD) of the foundation | How do the ports, the event store, projections, agent definitions, the tool registry and the policy gate fit together? | 3 |
| Run lifecycle | What happens from start to tool call, interrupt, crash, resume and invalidation? | 4 |
| Deployment and operations | Server and embedded modes, headless mode, telemetry, storage, code layout | 5 to 9 |

Two example agents carry the design through every section. Both serve Stillroom, a fictional hardware venture. The research agent searches competitors, fetches pages and reviews, and writes a competitor matrix with a quoted excerpt behind every finding. The patent-disclosure agent extracts inventions from engineering notes, compares them with published patent families, prepares disclosure packets and asks for approval before a mock send to outside counsel. A third agent, a contrast coding agent that edits a Git repository in a sandbox, runs tests and pushes a branch, is designed but not built. It exists to keep the first two honest: the rule of three says no abstraction enters the runtime until all three agents need it without agent-specific code (decision 16).

## 1. Context: one headless process between people, models and outside systems

The runtime is a headless Python process: it has no user interface of its own, and every interface reaches it through one session API. People send it commands and watch an event stream. It calls models, a sandbox and outside systems, and it records every call before and after.

```mermaid
flowchart LR
  U["User<br/>(CLI or web app)"] -->|"commands, each with a command ID"| RT["Aksha runtime<br/>one headless Python process"]
  RT -->|"event stream with a cursor<br/>and a synchronized marker"| U
  RV["Reviewer"] -->|"approve an exact action by signature;<br/>accept or reject a diff review"| RT
  RT -->|"approval requests,<br/>diff reviews with redlines"| RV
  RT -->|"generate, extract, classify<br/>as durable model steps"| M["Model providers<br/>(fakes and replay offline)"]
  RT -->|"exec: fresh box, network off"| SB["Sandbox backend"]
  RT -->|"fetch through the egress policy;<br/>effects through the outbox"| EXT["Outside tools and<br/>systems of record"]
  EXT -->|"receipts; lookup by key<br/>after a crash"| RT
  RT <-->|"append and read"| DB[("Event store<br/>PostgreSQL or SQLite")]
```

Caption: every arrow into or out of the runtime is either a command that gets a receipt or a call that is recorded in the event store, so nothing the runtime does is invisible to replay.

The model providers include two families. Generation models (System 2, the slow class) write prose and plans. Extraction and classification models (System 1, the fast class) pull typed fields out of text and sort inputs into labels, and they are routed like any other model (decision 15). Offline, every model class has a fake and a replay model read from a recorded store (ADR-P22, Proposed).

## 2. HLD: seven layers, with every primitive placed

The runtime splits into seven layers. Each layer answers one question, and each of the 30 primitives belongs to exactly one layer.

```mermaid
flowchart TB
  subgraph IF["Interfaces"]
    P19["P19 Session API"]
    P23["P23 Request router"]
  end
  subgraph AG["Agents and extensions"]
    P20["P20 Extension API and<br/>agent definition"]
  end
  subgraph CT["Control"]
    P15["P15 Task model<br/>and scheduler"]
    P16["P16 Stop policy"]
    P17["P17 Interrupts<br/>and approvals"]
    P18["P18 Cancellation"]
    P24["P24 Partial results"]
    P27["P27 Steering"]
  end
  subgraph TH["Thinking"]
    P06["P06 Agent loop"]
    P07["P07 Model routing<br/>by class"]
    P08["P08 Context assembly"]
    P09["P09 Memory"]
  end
  subgraph AS["Acting safely"]
    P10["P10 Tool registry"]
    P11["P11 Policy gate"]
    P26["P26 Egress policy"]
    P12["P12 Sandbox"]
    P28["P28 Environment image"]
    P13["P13 Grounding"]
    P14["P14 Budgets and limits"]
  end
  subgraph DS["Durable state"]
    P01["P01 Event store<br/>and projections"]
    P02["P02 Durable execution"]
    P03["P03 Outbox and<br/>idempotency keys"]
    P04["P04 Artifact store<br/>and lineage"]
    P05["P05 Invalidation"]
    P25["P25 Durable timers"]
    P29["P29 External record state"]
    P30["P30 Erasure"]
  end
  subgraph PF["Proof"]
    P21["P21 Tracing"]
    P22["P22 Evals, fake and<br/>replay models"]
  end
  IF -->|"commands: new run, answers,<br/>approvals, stop, steer"| CT
  AG -->|"declared Task tree, tools,<br/>permissions (load time)"| CT
  AG -.->|"model aliases, context policy"| TH
  AG -.->|"tool specs, egress policies"| AS
  CT -->|"scheduler starts a turn"| TH
  TH -->|"turn end goes to<br/>stop-policy evaluation"| CT
  TH -->|"every tool call<br/>passes the gate"| AS
  AS -->|"step results, artifact saves,<br/>outbox entries"| DS
  CT -->|"stop decisions,<br/>interrupt facts"| DS
  DS -->|"projections: status, holds,<br/>invalidation, budgets"| CT
  DS -->|"traces and replay read<br/>the recorded store"| PF
```

Caption: every layer writes facts down into durable state and reads projections back up, so durable state is the only layer every other layer depends on.

| Layer | Question it answers | Primitives |
|---|---|---|
| Interfaces | How does a person or program reach a run? | P19, P23 |
| Agents and extensions | How does a new agent arrive without touching the runtime? | P20 |
| Control | Who starts work, who decides it is finished, who may pause or stop it? | P15, P16, P17, P18, P24, P27 |
| Thinking | What does the model see, and which model answers? | P06, P07, P08, P09 |
| Acting safely | What may the agent do, where, for how long, with what evidence? | P10, P11, P12, P13, P14, P26, P28 |
| Durable state | What is true, and what is out of date, after any crash? | P01, P02, P03, P04, P05, P25, P29, P30 |
| Proof | How does anyone check what happened, offline? | P21, P22 |

Status as of 26 Nov 2026: four ADRs are Accepted in part (P04, P05, P15, P16), and three are Proposed with named parts Accepted (P06, P13, P20). P30, erasure in an append-only store, is Open. Every other ADR is Proposed. The decisions inside these records that are Accepted share one shape: structured artifacts are the core data model (decision 6), the recursive unit of work is the Task (decision 7), the agent loop never decides a stop itself, and an agent opts into built-in stop policies by configuration rather than writing its own (decision 10).

The build order follows the dependencies. Every path in the dependency graph starts at P01, so the event store comes first, then the extension API, the tool registry and the gate, then durable execution and the agent loop, then the control primitives, then the interfaces.

## 3. LLD of the foundation

The foundation is the six pieces every other primitive stands on: the ports and adapters frame, the event store with its hash chain, projections, the agent definition, the tool registry and the policy gate.

### 3.1 Ports and adapters keep the runtime free of every backend

The runtime is the domain in the middle. It states what it needs from the outside world as `typing.Protocol` classes, called ports, and concrete backends, called adapters, implement them from outside (ports and adapters, also called hexagonal architecture). A composition root, `src/aksha/app.py`, is the only module that knows which adapter fills which port (decision 18, recorded in [codebase-structure.md](../architecture/codebase-structure.md)).

```mermaid
classDiagram
  direction LR
  class EventStore {
    <<port>>
    append(boundary, events)
    read(owner, after_seq)
    subscribe(after_seq)
  }
  class AnchorSink {
    <<port>>
    publish(store_id, seq, hash)
  }
  class Reducer {
    <<port>>
    initial()
    apply(state, event)
  }
  class Clock {
    <<port>>
    now()
    sleep()
    mono_ms()
  }
  class PostgresEventStore
  class SQLiteEventStore
  class FileAnchorSink
  class SystemClock
  class FakeClock
  EventStore <|.. PostgresEventStore
  EventStore <|.. SQLiteEventStore
  AnchorSink <|.. FileAnchorSink
  Clock <|.. SystemClock
  Clock <|.. FakeClock
```

Caption: the ports that have adapters as of 26 Nov 2026; every later port (a model adapter, a sandbox backend, an HTTP client for egress, a system of record for the outbox) follows the same pattern.

The method lists above are simplified; the ports' exact signatures are in the code. Each port has one contract test suite, and every adapter of that port must pass it unchanged. If an adapter cannot pass, the port or the suite is wrong, and that is a design change to raise rather than a test to skip.

### 3.2 The event store is append-only and hash-chained

The event store (ADR-P01, Proposed) is an append-only table of facts that happened: a run started, a Task instance entered, a model step completed, an interrupt raised, an approval granted, an artifact saved. No row is ever updated or deleted. Its rules, each enforced by the storage adapter:

1. Append-only. Database triggers refuse every update and delete.
2. One writer. A single-writer lock means only one process appends; every other connection is read-only.
3. Transaction boundaries. A named group of events and artifact rows commits in one transaction or not at all.
4. Deduplication keys. A rule such as "this step's result is recorded once" is a unique index the database enforces, not a check in Python.
5. Tenant first. Every read names the owner whose data it wants.

The store is hash-chained (decision 20, built now rather than later). Each event carries `prev_hash`, the hash of the event before it, and `hash`, a SHA-256 over the canonical JSON of its own record. The first event's `prev_hash` is a genesis hash derived from a random store ID, so two stores never share a chain. An anchor copies the latest `(seq, hash)` to a sink outside the store that the store's writer cannot rewrite.

```mermaid
flowchart LR
  G["genesis<br/>hash of store_id"] --> E1["seq 1<br/>prev_hash = genesis<br/>hash = H1"]
  E1 --> E2["seq 2<br/>prev_hash = H1<br/>hash = H2"]
  E2 --> E3["seq 3<br/>prev_hash = H2<br/>hash = H3"]
  E3 -. "copied every N events" .-> A["anchor sink<br/>(seq 3, H3)<br/>outside the store"]
```

Caption: editing seq 2 changes H2, so seq 3's `prev_hash` no longer matches; deleting seq 3 leaves a valid shorter chain that only the anchor reveals.

The chain makes the record tamper-evident, not tamper-proof. A worked example from ADR-P01: someone with write access edits seq 40 and re-hashes every row after it. `verify()` alone passes, because the rewritten chain is internally valid. With an anchor at seq 1,000, `verify(anchors=...)` finds that seq 1,000 now hashes to something else and reports `anchor_mismatch`, which bounds the edit to seqs 1 to 1,000. Write-once (WORM) storage for the anchors is what makes the record write-once in fact. The reason for paying one SHA-256 per append is that the same harness must serve one person on a laptop and run unattended in a software factory, where the record is read later by someone who was not there.

### 3.3 Projections are the only way to read the store

A projection, also called a read model, is a view computed from the event store, such as a Task's status or the set of pending approvals. A reducer builds it: a pure function that starts from an initial state and applies events one at a time in sequence order. A projection cache stores a reducer's answer keyed by the last sequence number it read, so the cache is a snapshot and never a second authority. A full rebuild runs the reducer from sequence 0, and a test checks that the cache always equals a fresh rebuild.

This is the rule the whole design rests on: store facts, derive interpretations. A stored row is something that happened; a status, a hold, a budget remaining or a staleness flag is a projection. Because there is only one authority, there is no second copy that can disagree with it after a crash.

### 3.4 An agent definition is a Python module with declared data

An agent is a Python module that holds one `Agent` value (decision 2, Accepted; ADR-P20, Proposed with three parts Accepted). Its structure is declared data that the runtime can inspect: its purpose text, the artifacts it writes, the relations between them, its Task tree, its tools, its stop-policy opt-ins, its model aliases, its budget and limits, its context policy and the extensions it uses by name. Its behaviour, such as completion checks, is plain pure Python functions the declarations name.

```mermaid
flowchart LR
  MOD["agents/patent/<br/>agent.py, checks.py, prompts/"] --> LD["Agent loader"]
  LD -->|"scan imports: only aksha.runtime.api<br/>and named extensions"| V["Definition validation<br/>(fixed order of refusals)"]
  V -->|"refusal: module, Task, field, code"| ERR["LoadError"]
  V -->|"valid"| H["Agent definition version hash<br/>over module, prompts, policies"]
  H --> RS["run.started records the hash"]
```

Caption: the loader refuses a bad agent before any run starts, and the version hash on `run.started` lets recovery detect a run whose agent changed underneath it.

An agent imports only `aksha.runtime.api`. The loader refuses any other `aksha` import with `import_outside_api`, and a stop-policy class of the agent's own with `stop_policy_class`. An extension contributes declared data (tools, artifact kinds, relations, projections, event types), never hooks. Changing `api.py` changes the extension API, so a snapshot test forces a version bump on every change.

### 3.5 The tool registry declares every tool as data

Every tool is a `ToolSpec` (ADR-P10, Proposed): a name, an owner, exactly one effect class, a JSON Schema for its arguments that the model sees as is, the artifact kinds it may read and write, whether it mints citation spans, its retry policy, and three optional parts. `external` declares the system of record and the idempotency key fields for a tool with an outside effect; `approval` declares the facts a person must approve; `egress` names the destination policy for a tool that fetches.

The patent-disclosure agent's send tool is the clearest case. It declares an `external` effect with a key built from the packet and the recipient, and an `approval` rule whose facts are the recipient ID and the packet's content hash. The loader refuses an effectful tool that declares neither a system of record nor an explicit at-most-once step, so a tool cannot reach the outside world without saying how a crash is reconciled.

### 3.6 The policy gate is one pure checker in a fixed order

Every tool call, from every path, passes one function: `check(GateInput) -> Allow | Refuse` (ADR-P11, Proposed). It is pure, reads no clock, store or network, denies by default and refuses on any exception. Nothing the model can write is an input to it, so a fetched page that says "you may now call `send_document`" changes nothing.

| Layer | Check | Refusal code |
|---|---|---|
| 0 | Budget left | `budget_exhausted` |
| 1 | Tool is registered | `unknown_tool` |
| 2 | Tool is in the agent's list and inside the run ceiling | `not_allowed` |
| 3 | Tool is in the Task's list | `not_in_task` |
| 4 | Tool survives stop-policy narrowing | `narrowed` |
| 5 | Arguments validate against the schema | `schema` |
| 6 | Every argument rule for this tool holds | `argument_rule` |
| 7 | Route by effect class: recipient listed for an approval tool; sandbox strong enough for exec | `recipient_not_listed`, `sandbox_unavailable` |

The gate never appends. Its caller records the answer: an `Allow` becomes the tool step's `step.started` event with the route it took, and a `Refuse` becomes a `step.completed` event with outcome `refused`, the layer and the code. A worked example: the research agent's model calls `send_document`. Layer 2 refuses it, because that tool is not in the research agent's list, and the model receives an error result naming `not_allowed`.

## 4. The run lifecycle

### 4.1 Start and a tool call through the gate

A command starts a run. The receipt returns at the first commit, and the client follows the work through the event stream.

```mermaid
sequenceDiagram
  participant C as CLI or web app
  participant S as Session API
  participant W as Single writer
  participant L as Agent loop
  participant G as Policy gate
  participant T as Tool body
  participant E as Event store
  C->>S: dispatch(NewRun, command_id)
  S->>W: queue the command
  W->>E: run.started (definition hash)
  W-->>C: receipt at first commit
  W->>L: scheduler starts a turn for one Task instance
  L->>E: model step recorded (step.started, step.completed)
  L->>G: check(tool call)
  G-->>L: Allow, route
  L->>E: step.started (kind tool, args hash)
  L->>T: run the body under timeout and retry
  T-->>L: a value: result, save request or interrupt request
  L->>E: step.completed and artifact.saved in one boundary
  L->>W: turn end
  W->>E: stop.decided (one per stop candidate)
  E-->>C: stream events after the client's cursor
```

Caption: tool bodies return values and never touch the store, so only the single writer appends, and a late result from an abandoned attempt is discarded rather than saved.

Three rules shape this sequence. The scheduler alone starts work; a stop policy never spawns it (decision 8). The agent loop runs one turn of one Task instance and keeps nothing between turns (ADR-P06). Every turn end is a stop candidate that stop-policy evaluation decides, recording one `stop.decided` event (ADR-P16, Accepted in part; the event name itself is Proposed).

### 4.2 Interrupt and suspension

An interrupt and a suspension mean different things (decision 14). An interrupt is the run asking for outside input and stopping itself. A suspension is the run durably parked with no process attached, for any reason: an interrupt, a budget limit, a scheduled wake or an outside event.

```mermaid
sequenceDiagram
  participant L as Agent loop
  participant E as Event store
  participant R as Reviewer
  participant O as Outbox
  participant X as System of record
  L->>E: approval.requested and interrupt.raised (body, signature v1:sha256)
  Note over E: the run is suspended, as a projection shows
  R->>E: approve(signature shown), idempotency key
  E->>E: approval.granted, dedup key decide:{interrupt_id}
  L->>L: rebuild the body from current facts, compare signatures
  L->>O: outbox entry committed before the call
  O->>X: send with idempotency key
  X-->>O: receipt
  O->>E: outbox completed
```

Caption: an approval authorises one exact action by signature, and the runtime re-checks that signature from current facts before the outbox sends anything.

In the patent-disclosure agent's flow, a person approves sending packet version 3 to recipient `counsel-1`. If the recipient record changes before the send, the recomputed signature differs, the send is refused `signature_mismatch`, and a fresh approval is needed (ADR-P17, Proposed). Only a person resolves an interrupt; the runtime may only void one. A held run gets no automatic start of any kind, because one hold predicate reads the same projection that stop-policy evaluation reads (the hold invariant, decision 13).

### 4.3 Crash and resume

Recovery on startup runs in a fixed order (ADR-P02, Proposed). Every step reads the event store; none trusts memory from the dead process.

```mermaid
flowchart TD
  K["Process starts"] --> L["Take the single-writer lock,<br/>check the schema version"]
  L --> R1["Reconcile outbox entries that landed"]
  R1 --> SC["Per owner: did sources move?<br/>Re-execute recorded queries"]
  SC --> R2["Reconcile outbox entries that did not land"]
  R2 --> U["Close dispatched at-most-once steps<br/>as outcome unknown"]
  U --> C{"Classify each open run"}
  C -->|"definition changed"| S1["Resume record: stop, failed"]
  C -->|"stop requested"| S2["Resume record: stop, stopped"]
  C -->|"suspended"| N["Write nothing"]
  C -->|"in flight"| IF["Resume before the owner's next command"]
  S1 --> RB["Open rebuild runs for invalidated work"]
  S2 --> RB
  N --> RB
  IF --> RB
  RB --> DONE["recovery.completed"]
```

Caption: recovery never starts a turn for a suspended or stopped run, so restarting while an approval is open writes nothing and the person sees the same request with the same signature.

Resume works because step IDs are deterministic (run, Task instance, turn, call), every module reads time from an injected clock, and each tool declares its step semantics: at-least-once for reads and model steps, reconcile for tools with a system of record, at-most-once for the rest. A restart that reaches a recorded step returns the recorded result instead of running it again. A run that crash-loops stops `failed, restart_limit` after three resume records with no progress in between, a count that is itself a projection.

### 4.4 Invalidation after a change

Invalidation (ADR-P05, Accepted in part) reopens exactly the work whose lineage reaches a changed input. Lineage is the set of recorded reads on every artifact version, which the artifact store keeps automatically (ADR-P04, Accepted in part).

```mermaid
sequenceDiagram
  participant P as Person
  participant S as Session API
  participant E as Event store
  participant V as Invalidation view
  participant A as Patent-disclosure agent
  P->>S: apply_source_change(owner, change set)
  S->>E: source.changed
  E->>V: reducer recomputes states in save order
  V-->>S: invalidated: comparison C2, packet v3
  S->>E: approval.revoked (packet v3 target confirmed out of date)
  S->>A: rebuild run for the affected work
  A->>E: machine-only comparison regenerated silently
  A->>E: pending revision packet v4 beside v3
  E-->>P: diff review with changed inputs and a redline
  P->>S: accept review
  S->>E: review.accepted, then a fresh approval request
```

Caption: machines regenerate machine-only work silently, while a human-reviewed artifact gets a diff review, and only the person's acceptance makes the revision current (decision 5).

A worked example: a new prior-art reference arrives for one patent family. The comparison that read that family is invalidated, so the packet that read the comparison is too, and the approval to send that packet is revoked in the same transaction. A second comparison that read other families stays current. If the regenerated comparison comes out equal to the old one, early cutoff stops the invalidation there, and nothing downstream is rebuilt.

## 5. Server and embedded modes share one runtime

The runtime runs in two modes (decision 22, Accepted). In server mode, `aksha serve` is the backend: it owns the session API and the single-writer lock, and the CLI and the web app are both clients. In embedded mode, when no server is running, `aksha run` starts the runtime in-process for that one command and exits when it finishes.

```mermaid
flowchart LR
  subgraph SERVER["Server mode: aksha serve"]
    H["HTTP handlers"] -->|"command"| Q["Command queue"] --> W1["Single writer"]
    W1 -->|"append"| DB1[("Event store")]
    H -->|"read-only, events after cursor"| DB1
  end
  CLI1["aksha CLI"] --> H
  WEB["Web app"] --> H
  subgraph EMB["Embedded mode: aksha run, no server"]
    CLI2["aksha run"] -->|"takes the lock, calls in process"| W2["Single writer"]
    W2 --> DB2[("Event store")]
  end
```

Caption: both modes run the same runtime behind the same session API, so the only difference is which process holds the single-writer lock.

While a server holds the lock, a mutating CLI command run in embedded mode is refused, and read-only commands still work. The single writer admits commands between turns, not only between runs, so an answer or a stop typed during a long run is taken within one turn (ADR-P19, Proposed).

## 6. Headless mode never asks, and deny rules still hold

A headless run has no person watching it, so it cannot wait for one. Decision 26 (Accepted, 26 Nov 2026, amending decision 23) settles what happens: a headless run never asks, because every ask becomes an allow, but every explicit deny rule still holds, and the run executes inside a sandbox. In one line: headless never asks, but it never ignores a deny. This is Gemini CLI's treatment of "yolo" as an ordinary allow rule that a higher tier can still deny, not Decode's bypass, whose headless mode never reaches the deny rules.

ADR-P11 carries it as its decision 12 (the decision is Accepted; the interface is Proposed):

1. The run carries a `headless` flag, fixed when the run starts and recorded on `run.started`.
2. The policy gate runs every layer unchanged, so a tool outside the agent's list, a narrowed tool, a failing argument rule, a recipient outside the recipients source and a missing sandbox all refuse headless exactly as they do in a watched run. There is still no path that skips the check.
3. Only an allow routed to `approval` changes, and in one place: it takes the route the tool would take without approval (the outbox for an external effect), and no approval interrupt is raised, so a headless run never suspends on one.
4. The sandbox, not a person, is the wall around what a headless run can reach. ADR-P11 proposes that a headless start with no available sandbox backend is refused.
5. The event store records every tool call in headless mode exactly as in any other mode, so the record of what a headless run did is as complete and as verifiable as any other.

What is not yet written down is listed in section 10.

## 7. Two telemetry planes, one backend

Telemetry splits into two planes (decision 21, Accepted; the instruments, names and packaging in ADR-P21 are Proposed).

```mermaid
flowchart LR
  subgraph P1["Plane 1: the agent-run trace"]
    ES[("Event store")] --> BT["build_trace()<br/>pure function"]
  end
  subgraph P2["Plane 2: operational telemetry"]
    HTTP["Session API and HTTP spans"]
    DBS["Database and queue spans"]
    WK["Worker health"]
    MET["Metrics and logs"]
  end
  HTTP -->|"command carries the request's trace ID"| ES
  BT -->|"OTLP, after each segment"| B["One OTLP backend"]
  P2 -->|"OTLP, live"| B
```

Caption: the two planes meet only at trace and span IDs; the event store holds one correlation reference per command, never spans, metrics or logs.

Plane 1 is the agent-run trace: runs, Tasks, turns, model calls, tool calls, stop decisions, interrupts and outbox effects. It is a projection of the event store, named by the OpenTelemetry GenAI semantic conventions, so it is replayable and survives a crash. Plane 2 is the harness's own health: request latency, lock waits on appends, worker restarts, queue depth, errors. It is live OpenTelemetry instrumentation, never written to the event store, and lost if the process dies before export, which is acceptable because nothing reads it as truth. Both export over OTLP (the OpenTelemetry Protocol) to the same backend, so an operator can go from "this request was slow" to "this is the run it started" and back. Model token use appears in both: the event store's copy is authoritative for budgets, and the live copy feeds dashboards.

## 8. Storage: PostgreSQL and SQLite behind one port

Both backends implement the one `EventStore` port and pass one shared contract suite (decision 17). PostgreSQL is the production backend, installed as an optional extra with psycopg 3. SQLite needs no setup and serves local runs, offline runs and the tests.

| Rule | PostgreSQL adapter | SQLite adapter |
|---|---|---|
| Append-only | A PL/pgSQL trigger refuses `UPDATE`, `DELETE` and `TRUNCATE` | `BEFORE UPDATE/DELETE` triggers raise `append-only` |
| One writer | Session advisory lock | `fcntl.flock` on a lock file, which the kernel releases however the process dies |
| Readers | Read-only transactions by default | Read-only file mode plus `query_only` |
| Dedup keys | Unique index on `(owner, uniq)` | `UNIQUE (owner, uniq)` |
| Subscribers | `LISTEN` and `pg_notify` inside the append transaction | Woken by this handle's commits, otherwise a 50 ms poll |
| Payloads | `JSONB` | Canonical JSON text |
| Hash chain | `prev_hash` and `hash`, both unique, plus a one-row store metadata table with the genesis | The same |

Which backend a local run uses by default is not settled: decision 17 leaves SQLite as the local default pending the maintainer's confirmation, and the composition root currently defaults to PostgreSQL.

## 9. Codebase layout and the dependency rule

The package lives at `src/aksha/` and follows ports and adapters (decision 18). The full module tree and how to add an adapter, an agent or an extension are in [codebase-structure.md](../architecture/codebase-structure.md).

```text
src/aksha/
├── app.py            composition root: picks the adapters
├── runtime/          the domain: agent-neutral, stdlib and jsonschema only
│   ├── api.py        the public runtime API; agents and extensions import only this
│   ├── events/  storage/  projections/  definitions/  tools/  policy/
│   └── one package per later primitive, each with types.py and ports.py
├── adapters/         storage/ (postgres, sqlite), anchor/, clock/, later models and sandboxes
├── extensions/       one folder per extension
├── agents/           one folder per agent
└── interfaces/       the CLI and the HTTP server, through the session API
```

| Layer | May import | Never imports |
|---|---|---|
| `runtime/` | itself, the standard library, `jsonschema` | adapters, agents, extensions, interfaces, `aksha.app` |
| `adapters/<family>/` | the runtime's ports and values, its own family, any library | another adapter family, agents, extensions, interfaces |
| `extensions/<name>/` | `aksha.runtime.api`, itself | any other runtime module, adapters, agents, interfaces |
| `agents/<name>/` | `aksha.runtime.api`, the extensions it names, itself | any other runtime module, adapters, interfaces |
| `interfaces/` | the runtime, `aksha.app`, itself | adapters, agents and extensions directly |
| `app.py` | anything | nothing is refused |

A test enforces this rule on every module, and it plants one violation of each row in a temporary copy of the package to show the guard fails before anyone trusts it. Two more guards check that every `Protocol` lives in a `ports.py` and that every module appears in the codebase map.

## 10. Where this stops

The open points in this design as of 26 Nov 2026:

1. Headless mode and interrupts. Decision 26 settles the gate: every layer holds and only asks become allows (section 6). Still undecided: what a headless run does when a Task declares an `input` or `choice` interrupt, which no allow rule can answer. Refusing a headless start outside a sandbox is ADR-P11's proposal, not yet a decision.
2. The default backend for a local run (section 8).
3. Event type renames such as `walk.decided` to `stop.decided` and `pause.*` to `interrupt.*` (ADR-P01 D8) are Proposed. This page uses the proposed names.
4. Erasure in a hash-chained, append-only store (ADR-P30) is Open. Two of its options keep the hashed bytes unchanged and so keep the chain valid; any option that rewrites payloads breaks the chain by design.
5. Nothing on this page has been measured. No latency, throughput or cost figure appears here because none has been taken.

## Sources

1. The maintainer's decision log, entries 1 to 24, 25 and 26 Nov 2026, in particular 2, 5, 6, 8, 13 to 18 and 20 to 23.
2. ADR-P01 (event store, projections, Amendment H: the hash chain), ADR-P02 (durable execution), ADR-P05 (invalidation), ADR-P10 (tool registry), ADR-P11 (policy gate), ADR-P17 (interrupts and approvals), ADR-P19 (session API), ADR-P20 (agent definition and extension API) and ADR-P21 (tracing, two telemetry planes), all 25 Nov 2026.
3. The primitives index and catalogue, 25 Nov 2026, for the 30 primitives, their layers, statuses and dependencies.
4. The glossary of industry names, 25 Nov 2026.
5. The v2 high-level design, 25 Nov 2026, for the context, the boot sequence and the single-writer diagram, read through the glossary.
6. The architecture canvas, 25 Nov 2026, for the context and layer views.
7. [codebase-structure.md](../architecture/codebase-structure.md), status 25 Nov 2026, for the module tree, the dependency rule and the storage adapters table.
8. `src/aksha/runtime/policy/events.py`, for how a gate decision becomes an event.
