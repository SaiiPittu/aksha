# What I would build next

*Intermediate design as of 26 Nov 2026, first written 25 Nov 2026. Recorded as decisions 19 to 21 in the [decisions log](04-decisions-log.md). X-1 to X-7 are designed only, and nothing in them has been built or run. X-8, the hash chain, is in the first code pull request, which is not yet merged as of 26 Nov 2026. P-numbers refer to the [primitives catalogue](03-primitives.md).*

Aksha has to work in two settings with the same code: one person running it on a laptop, and an unattended software factory where many runs go on with nobody watching. The first release is shaped for the laptop. The eight items below are what the factory setting needs on top of it. Each one extends a primitive that already exists rather than adding a new subsystem, which is the test I hold every addition to.

The problems come from three sources, read on 25 Nov 2026: Katelyn Lesse's X article on the new problems multiagent systems create, and her blog post on sandboxing secure agents; swyx's quote post condensing her list; and John Suh's "Agent platform of 2027" list, which swyx relayed in a reply.

| ID | What | Primitives it extends | Rough size | Status |
|---|---|---|---|---|
| X-1 | Strong isolation for untrusted code | Sandbox (P12) | Medium | Next |
| X-2 | A halt-everything stop that also stops issuing credentials | Cancellation (P18), stop policy (P16), policy gate (P11) | Small to medium | Next |
| X-3 | Repeated denials ask a person instead of killing the run | Budgets (P14), stop policy (P16), interrupts (P17), egress (P26) | Small | Next |
| X-4 | Per-agent identity on every action, and per-agent, per-destination credentials | Tool registry (P10), policy gate (P11), agent definition (P20), outbox (P03) | Medium | Next |
| X-5 | An untrusted-origin mark that follows lineage | Artifacts and lineage (P04), invalidation (P05), context assembly (P08), memory (P09) | Small | Next |
| X-6 | A conversation view and a cross-run approval inbox | Session API (P19), interrupts and approvals (P17) | Medium | Next |
| X-7 | Runs started by incoming events | Durable timers (P25), external record state (P29), stop policy (P16) | Medium | Next (after timers) |
| X-8 | A hash-chained event store | Event store and projections (P01) | Small | In the first code pull request |

Sizes are my estimates, not measurements: small is a field and a rule plus tests, medium is a new adapter or projection, large would be a new primitive, and none of these is large.

## X-1: Containers and bubblewrap are not a boundary for untrusted code

The problem. Lesse's blog ranks isolation levels and argues that a container shares the host kernel, so it is not a security boundary for untrusted code; she prefers a user-space kernel or, better, a microVM. My sandbox design orders backend strength as `none < os-jail < container < vm`, where `os-jail` is an unprivileged namespace jail. Both the jail and a container share the host kernel, so that ordering implies a step up in safety that she disputes.

The design. I add a `user-kernel` strength (a user-space kernel) between `container` and `vm`, and name `vm` as a microVM. A tool whose program text came from a model or from a fetched page declares `min_strength >= user-kernel`, and the loader refuses an agent definition that lowers it. Commands written by the project itself, such as its own test command, may keep `os-jail` or `container`. Nothing else changes: if no available backend meets the minimum, the policy gate still refuses with `sandbox_unavailable` and nothing runs, so there is never a silent fallback to a weaker box.

Extends: the sandbox (P12), through its `Strength` type and the loader's check.

## X-2: A halt-everything stop that also stops handing out credentials

The problem. Lesse asks for kill switches that monitoring can trigger, which stop the whole system at once and cut off its credentials; her blog repeats this as a fleet-wide kill switch. My stop today is per run: a person's stop is a durable `run.stop_requested` event, honoured at the next stop candidate, even after a crash. Nothing stops every run together, and nothing withdraws keys.

The design. I add a `runtime` scope to the stop request, written as a durable `runtime.halt_requested{reason, by}` event, where `by` may be a person or a monitor. The hold predicate, the one function every automatic start already calls, returns `halted` for every scope while the halt stands, so no timer, recovery or rebuild opens new work. Running turns end at their next stop candidate as a run-scoped stop does today. The credential provider reads the same projection and refuses to issue tokens until a person lifts the halt with a separate command, so a monitor can halt the system but cannot un-halt it.

Extends: cancellation (P18), the hold predicate in stop policy (P16), and the policy gate (P11) through the credential provider.

## X-3: Repeated denials of the same kind ask a person instead of killing the run

The problem. Lesse names the hard part of monitoring: telling well-meant persistence apart from an attack. Her example is an agent that keeps trying a domain outside the allowlist. swyx puts it as kill switches that must not fire false positives by mistaking persistence for malice. A limit that kills the run on that pattern throws away hours of honest work; no limit at all lets an injected instruction keep probing.

The design. I add one budget kind: N denials of the same kind (the same egress destination, or the same gate rule) inside one Task instance. When it trips, its answer is an interrupt to a person, not a stop. The run suspends durably, the person sees the denied requests and either widens the rule through an agent definition change, tells the agent to stop trying, or stops the run. Every denial is already an event, so the budget counts from the event store like every other cap and needs no new monitor.

Extends: budgets (P14), stop policy (P16), interrupts (P17) and egress policy (P26).

## X-4: Every action records which agent acted, with credentials per agent and per destination

The problem. Lesse separates authentication (who took an action) from authorization, and notes that agents usually act on behalf of a person with that person's credentials, so the record cannot tell whether the agent or the person acted. swyx puts the same point bluntly: an agent that holds your password is bad, and delegating to the agent's own identity is better. My effect events record the owner, the person the run works for, but not which agent and which version acted.

The design. Every effect event, meaning a completed `external` or `exec` step, an approval used and an outbox entry, carries `acting_agent = (agent name, agent version hash, run, task)` beside `owner`. The version hash already exists, because the loader hashes every agent definition. External tools get credentials from a provider port that issues them per agent and per destination, and never hands over the owner's raw secret. Lesse's blog describes the same shape at the network layer: an egress proxy injects credentials scoped to each destination and the sandbox only sees placeholders. A workload-identity service can sit behind the port later without changing the events.

Extends: the tool registry (P10), the policy gate (P11), the agent definition and its version hash (P20), and the outbox (P03).

## X-5: Content from fetched pages is marked untrusted, the mark follows lineage, and invalidation can withdraw it

The problem. Lesse warns that a compromised agent can pass injected instructions to its peers, and that those instructions can land in memory that later agents trust. My design already has tests that an injected instruction in memory changes no action and cannot create an approval. Those defences stop actions; they do not stop later steps from trusting the content.

The design. Every artifact carries an origin class: `trusted` for project and person input, `untrusted` for anything derived from a fetched page or an external record. Lineage already records what each artifact read, so the class propagates for free: anything that read an untrusted input is untrusted unless a person approves it. Context assembly renders untrusted text as quoted data, and memory recall shows the mark. If a source later proves hostile, marking it withdrawn makes invalidation reach exactly the work whose lineage touches it, and machine-only outputs rebuild while reviewed ones go back to a person with a diff.

Extends: artifacts and lineage (P04), invalidation (P05), context assembly (P08) and memory (P09).

## X-6: The web UI shows a conversation and an approval inbox; the raw event stream becomes the debug view

The problem. Suh argues that showing users the raw agent session and its tool calls is wrong: users should see a conversation, like a chat channel or an email thread, and content meant for them should be posted through a tool call. He also expects people to work from an inbox of approvals and questions, which swyx summarises as a "user approval inbox". My session API today streams the raw event stream, and each run's interrupts are visible only inside that run.

The design. The web UI's main view becomes a conversation projection: the messages an agent posts to the person through one declared tool, interleaved with the interrupts and approvals of that run. A second projection, the inbox, lists every open interrupt and approval across all of one owner's runs, so a person running ten agents answers them in one place. The raw event stream stays exactly as it is and becomes `aksha inspect` and the debug view. Both new views are projections built by reducers over the same event store, so they cost no new state.

Extends: the session API (P19) and interrupts and approvals (P17).

## X-7: An incoming event can start a run the way a timer does, with the hold rule still applying

The problem. Suh describes listeners and monitors: agents define per-tenant event streams (WebSocket, Kafka, change data capture) and turn them into signals, instead of hourly cron jobs or one-off webhook plugins. My only automatic start besides recovery is a durable timer.

The design. An incoming event from a webhook or a change-data-capture feed is first written as a durable fact, deduplicated by an idempotency key taken from the source's own event ID. Firing it is an automatic start, so it goes through the same hold predicate as a timer: a person's stop, an open interrupt or a halt (X-2) holds it. The run it opens records the triggering fact as a lineage input, so invalidation can later answer which outputs came from which outside event. This waits for durable timers, because it reuses their start path.

Extends: durable timers (P25), external record state (P29) and the hold predicate in stop policy (P16).

## X-8: The event store is hash-chained, like a git history

The problem. Lesse calls for tracing across agents with a guarantee that no agent can modify its own trace or another agent's, and her blog asks that tool calls, commands and network activity be logged somewhere the sandbox cannot change. In Aksha the event store is the record: only the runtime appends events, no tool can, and the store file sits outside every sandbox. That stops agents from editing the record, but it does not let anyone prove later that nobody else edited it, for example someone with access to the database on the factory host.

The design. Each event stores the hash of the event before it inside a record that is itself hashed: `hash = SHA-256(canonical_bytes(record))`, where the record holds `prev_hash` and every envelope field (chain version, seq, type, payload, recorded time, dedup key, owner, run, instance, step, session, boot and schema version) in the RFC 8785 subset encoding of [ADR-P01](adr/ADR-P01-event-store-and-projections.md) H2 and B11. It is not a concatenation of the previous hash and the event bytes. This is the structure of a git commit chain or a blockchain, without any consensus or distributed ledger, so it is a few lines in the append path. Changing, deleting or reordering any past event breaks every hash after it, and a verify command walks the chain and names the first event that does not match. Appends are already serialized per store, so there is one chain per store: on SQLite the append runs under `BEGIN IMMEDIATE`, and on PostgreSQL it takes `pg_advisory_xact_lock` on the store's key (ADR-P01 H5). I put this in the first release, not later, because the same harness has to serve one person on a laptop and an unattended factory, and in the factory the record has to be verifiable by someone who did not watch the run.

The catch. A chain is tamper-evident, not tamper-proof: someone who can rewrite the whole store can recompute every hash. The fix is to copy the head hash somewhere the store's host cannot rewrite, such as the OpenTelemetry export or a signed checkpoint, and that part is next, not now. The chain also has to fit erasure in an append-only store (P30, still open): if erasure removes a payload, the chain must hash a payload digest rather than the raw bytes, so I will keep the digest in the hashed fields from the start.

Scope. The chain covers the event store, which is the agent-run record and its trace projection. The harness's own operational telemetry (session API requests, database and queue latency, worker health, errors, logs and metrics) is a separate plane: live OpenTelemetry instrumentation that is not stored in the event store (decision 21). Both export over OTLP to the same backend and are joined by trace and span IDs.

Extends: the event store and projections (P01). Status: in the first code pull request, not yet merged as of 26 Nov 2026 ([ADR-P01](adr/ADR-P01-event-store-and-projections.md), amendment H).

## Considered and rejected

1. Agents rewriting their own harness. Suh's list imagines a self-improving harness that writes its own extensions or hooks on the fly. I rejected it because agents in Aksha are Python modules, guarded by the loader, the tests and a human approval (decision 2). A harness that an agent can rewrite at run time removes the one place where a person reviews what the agent is allowed to do, and the version hash on every action (X-4) would stop meaning anything. An agent may still draft a change to an agent module; it lands only through the same loader, tests and approval.
2. A shared filesystem with access control. Suh proposes a shared cloud POSIX filesystem with role-based access, separate from compute. I rejected it because the sandbox design makes a workspace a saved snapshot: an artifact version, with the box keeping nothing between runs. A shared mutable filesystem would bring back state outside the event store, which breaks replay, lineage and invalidation. The content-addressed workspace tree already lets runs share files, and each read is recorded.

## Sources

- Katelyn Lesse, [The new set of problems for engineers to solve](https://x.com/katelyn_lesse/status/2114542280535318557), X article, 26 Oct 2026. Read 25 Nov 2026.
- Katelyn Lesse, [Secure Agents: Architecture and Sandboxing](https://www.katelynlesse.com/blog/secure-agents-architecture-and-sandboxing), blog post, 13 Sep 2026. Read 25 Nov 2026.
- swyx, [quote post on Lesse's article](https://x.com/swyx/status/2125371136943022423), 25 Nov 2026, and its self-reply relaying John Suh's list.
- John Suh, [Agent platform of 2027](https://x.com/john_ssuh/status/2124336124172935336), X post with four images, 22 Nov 2026, as relayed by swyx and read 25 Nov 2026.
- Decisions 2, 14 and 17 to 21: [decisions log](04-decisions-log.md).
- Primitive ADRs P01, P03 to P05, P08 to P12, P14, P16 to P20, P25, P26, P29 and P30: [adr/](adr/).
