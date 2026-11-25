# User stories

*Status: intermediate design as of 26 Nov 2026. Every story and acceptance criterion below is designed, not built, unless it says otherwise. Four primitives are built and tested on a branch in review (P01, P10, P11, P20). Thresholds and counts in the criteria are proposals for the maintainer to accept or change, not measurements.*

Each story follows "As a ..., I want ..., so that ...", then numbered acceptance criteria a test can run, then the [scenarios](03-scenarios.md) it appears in. Stories are grouped by the [personas](02-personas.md) they serve. The [features](05-features.md) and [requirements](06-requirements.md) cite them by ID.

## The frame: 31 stories across six personas

| Prefix | Persona | Stories | What they add up to |
|---|---|---|---|
| DEV | Agent developer | DEV-01 to DEV-08 | Declare an agent, get errors at load, test it offline |
| OPS | Operator | OPS-01 to OPS-08 | Run many agents and headless jobs within budget, stop them, trace and verify them |
| REV | Reviewer of agent output | REV-01 to REV-05 | Answer once, approve exact actions, see diffs |
| FND | Venture founder | FND-01 to FND-06 | Grounded research, honest gaps, a selection that stays fixed |
| INV | Engineer-inventor | INV-01 to INV-02 | A disclosure from their own notes, corrections that show |
| CNS | Patent counsel | CNS-01 to CNS-02 | A complete or honestly flagged packet, sent once, with a redline when it changes |

The REV stories hold for every agent on the runtime; the FND, INV and CNS stories are the same guarantees as the example agents' users see them.

---

## Agent developer

### DEV-01. Add an agent by declaring one module

As an agent developer, I want to add an agent by writing its module and tools, so that I can trust the runtime is shared and I never edit it.

1. A test copies a new agent module into a temporary plug-in folder and runs it end to end; the runtime package hash is the same before and after.
2. The module declares its purpose, artifact schemas, typed relations, Task tree, stop-policy opt-ins, model aliases, tools and budget as data, and its checks as pure functions. It subclasses no runtime class except the fake-model bases in its own folder.
3. The new agent's tool list, sandbox policy and stop policies are enforced exactly as the example agents' are.
4. The request router offers the new agent from its declared purpose, with no registration step.

Scenarios: 8.

### DEV-02. Mistakes fail at load, with the field named

As an agent developer, I want the loader to reject a malformed module and name the field, so that I never debug a half-run.

1. The loader refuses, each time naming the module, the Task and the field: a tool not in the registry; a Task that reads an artifact kind no Task writes; a typed relation between undeclared kinds; a child Task whose tools are not a subset of its parent's; an unknown stop-policy name; a generation model bound to an extraction or classification alias; a tool that runs a program with no sandbox policy.
2. Each refusal has a fixture module in the loader tests.
3. Every refusal happens before the run's first event is written.

Scenarios: 8.

### DEV-03. A changed module never resumes an old run

As an agent developer, I want a run started under one version of my module to refuse to resume under another, so that a code change never silently alters an in-flight run.

1. The agent definition version hash is recorded when a run starts.
2. On recovery, a run whose module hash changed is not resumed; it ends `failed` with reason `definition_changed`, and no model turn starts.

Scenarios: 3.

### DEV-04. Declare Tasks with child Tasks and fan-out

As an agent developer, I want to declare Tasks with child Tasks and per-key fan-out, so that each piece of work has its own completion check and its own bounded context.

1. A Task may hold child Tasks, each with its own completion check; there is no separate milestone kind.
2. Fan-out creates one tracked Task instance per key, each with its own model context, its own turn limit and its own events.
3. The scheduler, not a stop policy and not a model, starts instances from the declaration. In the first milestone it runs them one at a time in a fixed order.
4. Keys come from data the runtime can inspect, such as candidate IDs in the fixtures or a saved selection, never from a model's free text.
5. A step ID is a `/`-joined Task path, such as `R-0002/compare[C1]/t03/c02`, so one string identifies a step everywhere.

Scenarios: 1, 2, 6.

### DEV-05. Declare, do not subclass, the stop rules I need

As an agent developer, I want to say "keep going until this goal check passes" or "remind the model of its open todos" by configuration, so that I get tested built-in behaviour without writing a stop policy.

1. Every agent gets the Run, Task and interrupt stop policies without declaring them.
2. A Task's configuration may name goal condition, plan mode, forced tool choice and todo reminder, with their parameters, and nothing else; the loader refuses any other name.
3. Every verdict an opt-in policy gives appears in a recorded stop decision.

Scenarios: 6, 8.

### DEV-06. Each model job goes to the model class its answer needs

As an agent developer, I want to declare whether a step needs an extracted value, a classification or generated text, so that typed answers come with spans and probabilities and nothing quietly stands in for them.

1. Three model classes exist, each with its own contract and fake: extraction and classification (System 1, the fast classes) and generation (System 2, the slow class).
2. A Task binds model aliases to classes; the loader refuses a class mismatch.
3. Every System 1 answer lands in one of three bands, accept, reject or abstain, by thresholds declared as data. Only an accepted answer is used.
4. An abstention follows the alias's declared fallback. An adapted model, a System 2 model answering a System 1 contract under a forced schema, may answer only if the alias declares it, and its answer is recorded `native=False` and judged against its own thresholds.
5. When fallbacks run out, a machine-only artifact gets an explicit unknown with a question, and a human-reviewed artifact gets a diff review.
6. No System 1 answer, native or adapted, gates an external effect.
7. `aksha inspect` lists every threshold that was never fitted on data.

Scenarios: 7.

### DEV-07. Test my agent offline against fixtures of its own

As an agent developer, I want fake models per class and fixtures with their own answer key, so that my agent's tests are deterministic and survive renamed inputs.

1. The runtime provides a fake for every model class; the agent subclasses them in its own folder.
2. The agent's tests are graded by its fixtures' answer key, not by a transcript.
3. A second run in fake-model mode gives identical artifact hashes, stop-decision sequences and context hashes.
4. A replay model reads model answers from a recorded event store.
5. Each eval case records its kind and its source (hand-written, a real failure, or a planted trap).

Scenarios: 1, 7, 8.

### DEV-08. Run a program inside a sandbox fed by artifacts

As an agent developer, I want a tool's program confined by the operating system, fed only by artifact versions and returning only artifacts, so that its reach is enforced in code and its outputs are cited like any other.

1. A call to a program-running tool passes the policy gate first. The sandbox decides what the program can reach; it never decides what the agent may do.
2. The effective policy is the intersection of the run's ceiling, the agent's declaration, the Task's narrowing and the tool's needs, and each layer can only narrow it.
3. The workspace is built fresh for each call from input artifact versions by content hash; declared output files come back as artifacts with recorded reads; symlinks and paths resolving outside the workspace are rejected.
4. A blocked write or connect comes back to the model as a structured denial.
5. Network access is off for every call in the first milestone.
6. When no backend on the machine meets the tool's minimum strength, the call fails `sandbox_unavailable` and nothing runs. There is no unconfined fallback.

Scenarios: 5.

---

## Operator

### OPS-01. One work board derived from the record

As an operator, I want one table of every item's status, freshness and key columns, so that I can see a portfolio at a glance without trusting a model's report.

1. The work board is a projection over the event store and the artifact store: one row per item with its key, status, turns used, open interrupt, freshness and up to 12 typed columns the agent declares.
2. Column values are computed by code from the item's typed artifacts wherever a typed source exists.
3. A test compares the live board with a full rebuild from the event store at every step.

Scenarios: 6.

### OPS-02. Review by kind of judgement, with decisions that stay fixed

As an operator, I want open interrupts grouped by kind of judgement across items, and accepted decisions to stay fixed when other rows rebuild, so that one sitting covers one kind of judgement.

1. The review inbox groups open interrupts by Task and kind ("2 candidates need input", "the selection is ready").
2. A cell or selection a person accepted stays fixed when other rows rebuild; if its own inputs change, it comes back as a diff review.

Scenarios: 6.

### OPS-03. Runs that misbehave are stopped with a recorded reason

As an operator, I want loops, overspend and crash loops stopped by the runtime with a recorded reason, so that I am not watching every run.

1. Each agent declares a budget of tool calls, model turns and active seconds, and each Task instance has a turn limit (10 by default). An agent that declares none gets the runtime default.
2. A timed-out tool call is retried up to the limit (2 retries, 10 s each, by default) and then recorded as failed.
3. Every count is a projection of the event store, so a restart never resets it.
4. Hitting a budget raises a budget interrupt and keeps every saved artifact; a run that ends on a limit ends `partial`.
5. A run resumed 3 times without progress ends `failed` with reason `restart_limit`.

Scenarios: 3, 11.

### OPS-04. A stop that holds across every restart

As an operator, I want a run I stopped to stay stopped, whatever crashes or automatic triggers follow, so that my decision is never overridden by the machine.

1. `aksha stop RUN`, the web app's Stop button and the first Ctrl-C all send one stop command, which appends a stop request under an idempotency key.
2. The stop is honoured at the next stop candidate, including the resume candidate after a crash; the run's open interrupts are voided and the run ends `stopped` with no model turn.
3. After the stop, no automatic start (recovery, a rebuild after invalidation, a timer) starts that agent's work for that owner until the person's next command naming the agent.
4. A pending approval stays decidable, because executing it is a person's command.

Scenarios: 9.

### OPS-05. Run headless jobs inside a sandbox

As an operator, I want to start runs with nobody watching inside a sandbox, so that the same harness serves an unattended factory as well as a laptop.

1. A headless run raises no approval interrupt and asks for no permission, and every deny rule still refuses exactly as in a watched run.
2. The run records its mode and the sandbox capability record at start.
3. Every tool call in a headless run is recorded exactly as in a watched run, and every external effect still goes through the outbox with an idempotency key.
4. The open points (what an `input` interrupt does headless, whether a run outside a sandbox is refused) are listed in the design, not settled silently.

Scenarios: 5.

### OPS-06. Trace any run to any OTLP backend

As an operator, I want a run's trace and the server's own telemetry in the backend my team already runs, so that I can go from "this request was slow" to "this is the run it started".

1. The agent-run trace is built by a pure function over the event store and named by the OpenTelemetry GenAI semantic conventions; exporting it twice overwrites rather than duplicates.
2. The harness's operational telemetry is live OpenTelemetry instrumentation and is never written to the event store.
3. Both planes export over OTLP and share trace and span IDs.
4. Export happens only when the operator configures an endpoint explicitly; an inherited environment variable alone never turns it on.

Scenarios: 10.

### OPS-07. Inspect and verify any run

As an operator, I want to see everything a run did and to check that its record was not altered, so that an unattended run can be checked rather than trusted.

1. `aksha inspect` shows a run's agent, Tasks, model turns, tool calls, refusals, input versions, approvals, artifact saves, stop decisions and errors.
2. Each event carries the hash of the event before it and a hash of its own record.
3. `aksha verify` reports the first break in the chain, and with anchors it also detects a rewritten tail.

Scenarios: 5, 10.

### OPS-08. Run embedded or as a server with the same results

As an operator, I want one command for a quick local run and a server for many clients, so that the same scenario behaves the same either way.

1. `aksha serve` owns the session API and is the single writer to the event store.
2. With no server running, `aksha run` starts the runtime in-process for that one command.
3. The CLI and the web app call one `dispatch(command)` entry point, and the same scenario driven through each produces the same events apart from request IDs.

Scenarios: 2, 3.

---

## Reviewer of agent output

### REV-01. See the exact action before approving it

As a reviewer of agent output, I want the approval request to show exactly what will happen, so that I approve an action, not a tool name.

1. The approval request shows the tool, the recipient, the artifact version and the approval signature, a hash over the canonical action.
2. Recipients come from configured records, never from model text.
3. Before the outbox sends anything, the runtime rebuilds the action from current facts and compares signatures; a mismatch is refused `signature_mismatch` and needs a fresh approval.

Scenarios: 2.

### REV-02. Reject or edit without side effects

As a reviewer of agent output, I want a rejection to change nothing and an edit to ask again, so that I can say no safely.

1. A rejection changes nothing but the record of the rejection.
2. An edit to the action voids the old approval and raises a new approval request.
3. A second decision on an approval already decided is refused `already_decided` and changes nothing.

Scenarios: 3.

### REV-03. A pending approval survives a restart and executes once

As a reviewer of agent output, I want my pending approval to be there after a crash and to execute once, so that a restart never asks me twice or sends twice.

1. After a restart, the same approval request reappears with the same signature, and the run's recorded stop decision is HOLD.
2. Recovery starts no model turn for a suspended run.
3. A send interrupted after the receiver recorded it is reconciled by idempotency key and not repeated.

Scenarios: 3.

### REV-04. Answer once, and the answer reaches the work that asked

As a reviewer of agent output, I want each question asked once and my answer to reach the work that asked, so that I am not asked again after a restart or by another item.

1. An interrupt is one of five kinds: `input`, `choice`, `approval`, `budget` or `review`. An agent declares a label for each, never a new kind.
2. Only the Task instance that asked waits; other instances continue.
3. An answer is saved as a user statement and cited wherever it is used.
4. A retried submission after a crash is recognised by its command ID and recorded once.

Scenarios: 1, 2.

### REV-05. See a diff when work I reviewed goes out of date

As a reviewer of agent output, I want the old and new versions side by side with what changed, so that I can re-approve in one look and nothing I approved is replaced quietly.

1. When a human-reviewed version is invalidated, the runtime writes a pending revision beside it; the old version keeps its lifecycle state.
2. The diff review shows the changed inputs and a field-level redline.
3. Only the person's accept makes the pending revision current. An accept never grants an approval; it raises a fresh approval request when the old version carried one.
4. When one reviewed artifact reads another, the upstream diff review opens first, and the downstream one opens only after the upstream accept.
5. A rejection leaves the old version out of date, the revision pending, and nothing that depends on it sendable.

Scenarios: 4.

---

## Venture founder

### FND-01. Name an agent or just describe the request

As a venture founder, I want to name an agent or describe my request in plain words, so that I do not need to know the system's structure.

1. An explicit command picks an agent directly.
2. Free text goes to the request router, which picks from the loaded agents' purposes and records the choice as a step.
3. When a request fits both agents or neither, the router asks one clarification question.
4. The router has no tool access.
5. Every message carries the agent label, including after a resume and a restart.

Scenarios: 1, 8.

### FND-02. A competitor picture built from fetched pages

As a venture founder, I want every claim tied to a page a tool fetched, with duplicates merged and conflicts kept, so that the count is honest and I can check any cell.

1. Claims come from fetched pages; a failed fetch leaves the claim `unverified`.
2. Every value is labelled fact, vendor claim, inference or unknown.
3. Two records for one company are merged by a `same_entity_as` relation carrying the excerpt that justifies the merge; the runtime never merges records because their names look alike.
4. Conflicting values stay side by side, linked by a `contradicts` relation with each value's excerpt.
5. Reviews are reported as "N of M in this sample", never as demand.

Scenarios: 1, 11, 12.

### FND-03. A run that runs out of evidence ends partial

As a venture founder, I want a run that cannot find something to stop with the gap named, so that I am never shown a completion that is not real.

1. Missing stays missing: a null with a question, never a zero or a typical value.
2. A fan-out parent declares whether a blocked child blocks it or leaves an explicit unknown; the default is to block.
3. A budget interrupt can be answered "finalise", which marks unstarted keys "not checked" and runs only the Tasks marked as finalisers, on a reserved share of the budget.
4. A partial run lists its completed Tasks, its blockers and the next action.

Scenarios: 11.

### FND-04. Choose candidates from a recommendation table

As a venture founder, I want one recommendation per candidate and my choice saved before preparation starts, so that a restart does not ask again and preparation matches what I chose.

1. A rule table in code picks each recommendation (prioritise, collect more evidence, deprioritise); the model writes only the explanation, including what would change it.
2. The selection is a `choice` interrupt over the recommendation table.
3. The selection artifact records the candidate IDs and the recommendation versions it was based on, before any form is written.
4. Selecting creates no approval to send.

Scenarios: 2, 6.

### FND-05. A late fact changes only the work that read it

As a venture founder, I want a changed input to reopen only the work whose lineage reaches it, so that I keep everything unaffected and see exactly what moved.

1. A change set is applied with one command and recorded as one fact.
2. Only work whose lineage reaches the change is invalidated; outputs for other candidates keep their versions.
3. A rebuilt artifact whose content comes out the same keeps its readers current (early cutoff).
4. No external effect runs while anything it depends on is invalidated.
5. One summary lists what changed, what rebuilt, what stayed current, what waits for review and what is unchanged. The summary is a projection, never stored, and is the same after a restart.

Scenarios: 4.

### FND-06. Instructions hidden in fetched content change nothing

As a venture founder, I want text inside a fetched page to stay data, so that a page cannot make the agent send, approve or spend anything.

1. Tool results enter the model context as data and are flagged when they contain instructions aimed at the agent.
2. Tool lists, approvals, recipients, budgets and stop-policy configuration are the same at the end of a run with and without injected text.
3. A call the injected text causes is refused by the policy gate and recorded.

Scenarios: 12.

---

## Engineer-inventor

### INV-01. A disclosure filled from my own dated notes

As an engineer-inventor, I want the disclosure filled from my dated notes, with targeted questions instead of guesses, so that my idea is recorded as I wrote it.

1. Extraction reads the engineering notes and cites them; it never infers a mechanism from a candidate's name.
2. Dates are kept as stated, with a normalised form beside them, and no date arithmetic is done on disclosures.
3. Each missing detail raises one targeted question; an unanswered question stays a question in the packet.
4. Contribution is recorded with its citation and is not flattened into inventorship, which stays "needs counsel confirmation".

Scenarios: 2, 4.

### INV-02. A corrected answer replaces the old one visibly

As an engineer-inventor, I want a correction to my earlier answer to replace it with a visible link, so that only the work that used the old answer changes.

1. A corrected answer is sent to the open run in the `amend` lane; a message to an open run must name its lane.
2. The runtime saves a new user statement and records a `supersedes` provenance link to the one it replaces.
3. Only work whose lineage reaches the old statement is invalidated; a reviewed packet that read it comes back as a diff review.

Scenarios: 4.

---

## Patent counsel

### CNS-01. A packet that is complete, missing or flagged, never guessed

As patent counsel, I want every required field marked complete, missing or needs counsel, so that I know exactly what I still have to establish.

1. Each required field has one of three states: complete, missing or needs counsel confirmation.
2. A packet with a missing field cannot become ready, and "mark it complete" changes nothing.
3. The packet opens with its "needs counsel confirmation" section and a scope note built from the run record.
4. The agent gives no patentability or freedom-to-operate opinion, drafts no claims and computes no deadlines, and no filing tool exists.

Scenarios: 2.

### CNS-02. Know what a packet was built from, receive it once, and see what changed

As patent counsel, I want each packet to record its inputs, to arrive once, and to show what changed since the version I hold, so that I read only what is new.

1. The packet records the ID and version of every input.
2. The send's idempotency key is a hash of the tool, the packet, its version and the recipient, and the ledger holds one entry per key across crashes and retries.
3. A new version carries a field-level redline against the version counsel holds.
4. If the earlier version was already sent, it keeps its `sent` lifecycle, nothing is resent after a restart, and the new version needs its own accept and approval.

Scenarios: 3, 4.

## Where these stories stop

1. Every criterion is designed. The built primitives (P01, P10, P11, P20) carry parts of DEV-01, DEV-02, OPS-07 and FND-06, and those parts have tests on a branch in review; no story is met end to end.
2. Stories that depend on Later primitives (DEV-08, OPS-01, OPS-02, OPS-05) are in the design so the runtime is shaped for them, not because the first milestone delivers them.
3. The founder is assumed to be the authorised client who may approve. No wider role model is designed.

## Sources

1. The product problem-and-users document, version 2, 25 Nov 2026, sections 4 and 5 (jobs, stories and acceptance criteria).
2. ADRs P02, P03, P05, P07, P12, P13, P14, P15, P16, P17, P18, P19, P20, P21, P22, P23, P24 and P27, 25 Nov 2026.
3. The maintainer's [decisions log](../04-decisions-log.md), entries 5, 8, 10, 13, 15, 20, 21, 22 and 23.
