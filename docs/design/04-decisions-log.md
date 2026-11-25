# Decisions log

*Intermediate design as of 26 Nov 2026. This is the list of design calls the maintainer has made, oldest first, numbered so the ADRs can cite them. A decision here is Accepted. Anything an ADR proposes that is not in this list stays Proposed. "v2" and its ADR numbers (ADR-002, ADR-014) refer to the previous, unpublished design revision; the ADRs in [adr/](adr/) carry those decisions forward under new names.*

| Range | Theme |
|---|---|
| 1 to 10 | What Aksha is, and the agent model: Tasks, stop policies, artifacts and their graph |
| 11 to 16 | Names, the primitives-first method and the rule of three |
| 17 to 21 | Storage, code layout, future directions, the hash chain and telemetry |
| 22 to 24 | How the runtime is run, headless mode and the order the docs and code land in |
| 26 to 27 | Headless permissions and the durable executor |

## 25 Nov 2026: stop ownership and the agent model

1. **Aksha is a general harness.** The Research agent and the Patent-disclosure agent, example agents for a fictional hardware venture, are the first two proofs. For prior art, Stencil (the stop-policy design of the `omp2` branch of oh-my-pi) weighs more than Decode (a coding-agent course package), and Codex CLI and opencode are compared as well. The maintainer decides.
2. **Agents are defined in Python code.** Structure (steps, reads and writes, tools, children) is declarative data inside the module, and checks are plain pure functions. A UI, if there is one, hands the request to a builder agent that writes the module, and the loader, the tests and a human approval guard the result. This settles v2 ADR-002 towards "Python module", pending the maintainer's confirmation.
3. **General stop policies are built in**, not only workflow frames: goal condition, plan mode, forced tool choice and todo reminder (Stencil's set), alongside the Run root, Step and Wait. This refines option E of v2 ADR-014, which had a closed set of four.
4. **"State" and "milestone" become one recursive unit.** A unit can hold child units, each with its own completion check, and there is no separate milestone kind. The name was settled in decision 7. This refines v2 ADR-014.
5. **People see diffs for changed reviewed artifacts** (from Harvey's document agent). The rebuild writes a new version beside the old one, the review shows the changed inputs and a redline, and only the person's accept creates the new approval. Machine-only artifacts regenerate silently.
6. **Structured artifacts are the core data model.** Raw fixtures are inputs. Extraction produces typed artifacts with excerpt references, and downstream steps read artifacts, never raw files. Each agent declares its own artifact schemas. The runtime's artifact store gives versioning, a content hash, recorded reads (lineage), invalidation, citation validation and approval binding.
7. **The recursive unit is named `Task`.** It replaces "state" and "milestone".
8. **Fan-out (`per=`) creates tracked Task instances, each with its own model context**, a sub-agent in all but name. The runtime's scheduler runs them: sequentially in the first milestone, for deterministic replay, and under a concurrency setting later. Stop policies never spawn work; the scheduler does, from the declaration.
9. **Artifacts form a graph, with typed edges declared alongside the schemas.** There are two kinds. Lineage edges (`derived_from`) are recorded by the runtime from recorded reads. Typed relations are declared by an agent, for example `same_entity_as`, `amends`, `supersedes`, `family_member_of`, `contradicts` and `cites`, and each one carries its own excerpt as evidence. Entity resolution becomes `same_entity_as` edges.
10. **An agent declares its Task tree and opts into the built-in stop policies** (plan mode, goal condition, forced tool choice, todo reminder) by configuration. It does not subclass a stop policy in v1.
11. **Industry-standard names.** The design uses: runtime, extension API, projection built by a reducer, event store, transactional outbox, idempotency key, interrupt, stop policy, completion check, lineage, invalidation, and model routing by class. A glossary maps the older Aksha words to these.
12. **Primitives-first design.** One ADR per harness primitive, each stress-tested against the Research agent, the Patent agent and a contrast coding agent, and then built in dependency order. The result is the [primitives catalogue](03-primitives.md).
13. **Four rules adopted from T3 Code:**
    1. TC-1: a person's stop or cancel is a durable event, and every automatic start respects it.
    2. TC-2: live panels get a snapshot, a cursor and a "synchronized" marker.
    3. TC-3: eval cases are labelled by kind and origin (hand-written, real failure, planted trap).
    4. TC-4: "human state beats every automatic start" is a runtime rule with a test.
14. **Terminology confirmed.**
    1. "Runtime", one word, not runtime core, substrate or kernel.
    2. Event store; projection, built by a reducer; transactional outbox; idempotency key; completion check; artifact; lineage (provenance); model routing across model classes.
    3. **Invalidation** for staleness, not "incremental build".
    4. Interrupt and suspension are both kept, with different meanings. An **interrupt** is the run asking for outside input and stopping itself. A **suspension** is the run durably parked with no process, for any reason: an interrupt, a budget, a scheduled wake or an external event.
    5. The stop-ownership concept must name the agent run (for example "run stop policy"); the final name is still to be picked.
15. **Fast models are first-class.** Extraction and classification models (System 1) are general model classes, routed like the generation models (System 2). Verification is one of their uses, not their only role.
16. **The rule of three is stated in the design docs and the README.** The harness's generality is tested by three different agents (Research, Patent and a contrast coding agent) before any abstraction is accepted.
17. **Storage: PostgreSQL in production, SQLite locally and for the offline run**, both behind one `EventStore` port with a shared contract test suite. The PostgreSQL adapter is an optional extra (psycopg 3). SQLite as the default for the first-milestone run is pending the maintainer's confirmation.
18. **Codebase structure: ports and adapters.** The domain lives in `aksha/runtime` (one module per primitive, with Protocol interfaces per subsystem), adapters in `aksha/adapters`, agents in `aksha/agents` and interfaces in `aksha/interfaces`. A test enforces the dependency rule. The codebase map is [docs/architecture/codebase-structure.md](../architecture/codebase-structure.md).
19. **Future directions X-1 to X-8 are recorded**, drawn from public posts by Katelyn Lesse, swyx and John Suh: strong isolation tiers, a halt-everything stop, repeated denials that ask a person, per-agent identity and credentials, an untrusted-content mark carried through lineage, a conversation view and approval inbox, event-triggered runs, and a hash-chained event store. They are in [future directions](05-future-directions.md) and the README. Two ideas were rejected: agents rewriting their own harness, and a shared filesystem with access control.
20. **X-8 is built now, not later.** The event store is hash-chained: each event carries the hash of the previous one, so the record is tamper-evident the way a Git history is. The README states why: the same harness must run for one user on a laptop and unattended in a software factory, where the record has to be verifiable by someone who did not watch the run.
21. **Two telemetry planes.**
    1. The agent-run trace is a projection of the event store: domain facts, replayable.
    2. The harness's own operational telemetry (HTTP and session API requests, database and queue latency, worker health, resource use, errors, logs and metrics) is live OpenTelemetry instrumentation and is not stored in the event store.
    3. Both export over OTLP to the same backend and are joined by trace and span IDs.

## 26 Nov 2026: running the runtime

22. **Server plus embedded mode.** `aksha serve` is the backend: it hosts the session API and is the single writer. The CLI and the web app are clients. With no server running, `aksha run` starts the runtime in-process for that one command.
23. **Headless mode runs in full bypass**, following Decode. A headless run raises no approval prompts and asks for no permissions, and it is expected to run inside a sandbox, as headless agents usually do. This decision postdates ADR-P11 (a policy gate with no mode that skips it) and ADR-P17 (interrupts and approvals); as of 26 Nov 2026 neither ADR yet says how a bypassed run meets the gate. Amended by decision 26: a headless run never asks, but deny rules are not bypassed.
24. **Design docs land before the first code merges.** A docs pull request (product overview, architecture, primitives catalogue, ADRs) is merged first, and the first code pull request (#40) is rebased onto it.

## 26 Nov 2026: headless permissions and the durable executor

26. **Headless runs never ask, and deny rules always hold** (amends decision 23). In a headless run every ask becomes an allow, so nothing waits for a person, but every explicit deny rule still refuses, and the run executes inside a sandbox. This follows Gemini CLI, which treats "yolo" as an ordinary allow rule that a higher tier can still deny, not Decode's bypass, which skips the deny rules. [ADR-P11](adr/ADR-P11-policy-gate-and-permissions.md) carries it as its decision 12.
27. **ADR-P02 is accepted: an own durable executor behind an executor port.** Temporal is supported later in two ways: as an executor adapter, where an Aksha run executes as a Temporal workflow and its steps as activities, and as an integration, where a business's own Temporal workflow invokes an Aksha run as an activity. Step semantics are named `at_least_once`, `reconcile` and `at_most_once` on the tool specification. A person's durable stop is part of recovery (`stop_requested`). A replay check, which verifies that a re-run matches the recorded history, is designed now and built later. A run suspended while one of its inputs changes gets one chance to re-check on resume (`suspended_with_rebuilds`), in the first milestone.
