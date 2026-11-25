# ADR-P19: Session API (one write entry point with idempotent receipts, event streams with a cursor and a catch-up marker, and one capability record per runtime)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. The React web shell in web/ exists, built on 25 Nov 2026 against a mock session API with no backend. One call in the decisions log shapes this ADR and is the maintainer's: Aksha is a general headless harness with several interfaces (decision 1). No entry in the log settles the write entry point, the receipt, the stream rules or the capability record, and TC-2 (forward-compatible streams with a catch-up marker) is a decision for the maintainer that is not applied, so every part below stays Proposed. Amended 25 Nov 2026 after the three-agent stress test: S1 to S4 below are new and Proposed.

## Terms

Primitive PR-19 in the primitives catalogue, cluster E (interfaces and proof). Depends on PR-01 (every command and every stream item is a fact in the event store) and PR-17 (most commands answer an interrupt). Needed by PR-18 (the stop command travels through it) and PR-22 (the eval runner drives agents through it). It refines v2 ADR-023, which stays the source for the HTTP endpoint table, the exit codes and the CLI-versus-web decision; this ADR changes what sits underneath those.

Terms used here, in the industry names of the design glossary:

- **Session API** is the one programmatic surface every interface uses: The CLI, the web server, the demo script, the eval runner and any later editor plug-in or bot. No interface reaches the runtime core another way.
- **Command** is a request to change state, such as "send this message" or "approve this action". Commands form one closed, tagged union of frozen dataclasses.
- **Command ID** is an idempotency key the client generates for each command. A **command receipt** is the stored record that a command with that ID was accepted or refused, and with what result. A retried command with the same ID returns the first receipt and does nothing else.
- **Committed, not done** is the receipt's promise: The command's facts are durably in the event store and will not be applied twice. The receipt does not promise that the model turn, the tool call or the side effect it leads to has happened; the client learns that from the stream.
- **Stream** is the ordered delivery of the events a client may see, each carrying its sequence number. The **cursor** is the last sequence number the client holds. A **resync** tells the client to refetch every view and move its cursor, used when replay would be too large. The **catch-up marker** (T3 Code calls it `synchronized`) says replay has reached the live tail.
- **Forward-compatible decoding** means a client that receives a stream item type it does not know skips it, advances its cursor and keeps working.
- **Capability record** is a structured description of what this runtime, as configured, can do: which commands it accepts, which interrupt kinds and model classes it has, which stream features it sends. A client branches on the record, never on a backend's or agent's name.
- **Tenant** is the principal whose data a fact belongs to (Aksha's code calls it the owner). Every call names it first.
- **Single writer** is the one runtime thread that appends to the event store, whichever interface is in use.

## Context: without one session API, every interface becomes its own runtime

The product requirements ask for one interface (a web chat, CLI or bot), routing with a clarification when a request is unclear, a label on every reply naming the agent, separate user sessions, and runs a person can inspect. A general harness has several interfaces at once, and each of them asks the same four questions. Each question breaks in a known way when the answer lives in the interface instead of the runtime core.

| Question an interface asks | What breaks without a shared answer | Where the comparison saw the fix |
|---|---|---|
| Did my click count, and only once? | A double-click on Approve, or a retry after a dropped connection, records two approvals or two messages. v2 has a request ID only on `user.message`, and its uniqueness token waits for cut row 11 (HLD 10.2 gap 3) | T3 Code: a `commandId` on every command gives an idempotent receipt |
| Did the decision and what it caused both happen, or neither? | An interface that writes a decision and then calls the runtime can crash between the two | T3 Code: one transaction commits events, projections, receipts and outbox rows (PR-03 owns this) |
| I reconnected; what did I miss, and when have I caught up? | A browser that reconnects either refetches everything blindly or patches views from payloads and drifts; an older web build crashes on an event type added later; the UI cannot tell when to stop showing "reconnecting" | T3 Code: cursor plus snapshot, a `synchronized` marker, `unknown-event` decoding (TC-2) |
| Which features may I show? | A client that asks "is this the Patent Agent?" or "is this the live model?" grows one branch per agent and per backend, which is agent-specific code in an interface (P-1) | T3 Code's capability record per adapter; the Language Server Protocol and MCP negotiate capabilities the same way (general usage) |

The contrast coding agent adds three pressures the product requirements agents do not:

1. Its runs last for hundreds of turns, so a design where the one writer runs a command "until the run yields" (05e section 2.4) holds the writer for minutes. A Stop or an answer typed during that time would wait behind the whole run.
2. It produces live output a person wants to watch (test logs, token streams) that is not a fact worth storing.
3. An editor becomes a third client beside the CLI and the web, with its own idea of which buttons to show.

## Decision

1. **One write entry point, `dispatch(command) -> CommandReceipt`, over a closed union of commands.** Every state change any interface makes is one `Command` value. The named functions of v2 HLD 14.1 (`send`, `approve`, `answer` and the rest) stay, as one-line constructors over `dispatch`, so the CLI and the tests read the same as before. `boot` leaves the union, because no client sends it; the runtime thread runs it at start.
2. **Every command carries a client-generated command ID, and every command gets a receipt.** In the command's first transaction the runtime core appends one fact, `command.accepted` or `command.refused`, with a deduplication key `cmd:{tenant}/{command_id}` under a unique constraint (PR-03 owns the constraint). A second dispatch with the same ID and the same argument hash returns the stored receipt with `replayed=True` and appends nothing. The same ID with different arguments is refused with `command_id_reused`. This generalises v2's `request_id` on `user.message` to every command, and it replaces that field.
3. **A receipt means committed, not done.** `dispatch` returns when the command's own facts commit (`user.message`, `approval.granted`, `run.started` and so on). Turns, tool calls and outbox work happen after, on the runtime thread, and reach the client through the stream. For the CLI and the tests, `wait_for_yield(tenant, session, after_seq)` blocks until the run yields to a person or ends and returns the same `Yielded` view v2 designed, so exit codes do not change.
4. **The single writer admits commands between turns, not only between runs.** The runtime thread runs scheduler work one turn at a time and drains the command queue after every turn's commit, so an answer, an approval or a stop typed during a long run is accepted within one turn, not after the run. This is a change to 05e section 2.4, which ran each command to its yield.
5. **One stream per session, resumable from a cursor, with a resync, a catch-up marker and forward-compatible decoding (TC-2 option A).** `subscribe(tenant, session, after_seq)` replays every stored event of that tenant and session with a greater sequence number, then sends `Synchronized(seq)` once, then follows live commits. If the gap exceeds `max_replay` (default: unbounded, because the whole event store is local), it sends `Resync(seq)` instead of the replay: The client refetches every view and moves its cursor. Because clients never build views from payloads (FR-IF-11), a resync needs no snapshot payload. Clients decode an unknown item type as `Unknown`, which advances the cursor and invalidates nothing. The runtime core never renames or removes an event type without bumping the stream's `schema_version`.
6. **Live deltas are transient and never facts.** Token streams and command output arrive as `Transient` items on the stream while a step runs. They are not stored, never replayed, and carry no cursor; a reconnecting client sees the completed step instead. T3 Code makes the same split: its only unpersisted deltas are command output and prompt suggestions (code, `ProviderAdapter.ts:194-209`, through the research note).
7. **One capability record per runtime, derived from the composition root and the loaded agent definitions, never stored.** `capabilities(tenant)` returns which commands `dispatch` accepts, the interrupt kinds, the model classes with the adapter bound to each, the stream features, the sandbox backends, the run mode and, per agent, its interrupt labels, its effectful tools and its opted-in stop policies. An interface hides a button whose command is absent, so a first-milestone build without cancellation simply reports no `stop` command. The honesty rule is T3 Code's: a capability that is claimed and fails at runtime is a bug, so every claimed capability maps to a conformance test.
8. **The stop command is reserved in the union; its meaning belongs to PR-18.** `Stop(run_id, reason)` is part of the union so the receipt, the stream and the capability record already handle it. Whether it ships in the first milestone is TC-1 (lean B in the first milestone), and the capability record says so either way.
9. **Tenant first, and another tenant's ID is "not found".** Every command and every read names the tenant, and an ID that belongs to another tenant gets the same `not_found` refusal as an unknown one (FR-IF-14), unchanged from v2.

Worked example, a double-clicked Approve after a dropped connection. The browser sends `Approve(approval_id="A-0001", shown_signature="sha256:3f9c...", command_id="c-7f3a")`. The runtime thread commits `approval.granted` and `command.accepted{command_id: "c-7f3a"}` at sequence 4127 and resolves the receipt, but the response is lost. The browser retries with the same command ID. `dispatch` finds the deduplication key, returns the stored receipt `{status: accepted, seq: 4127, ids: ("A-0001",), replayed: True}`, and appends nothing. The event store holds one `approval.granted`. Without the receipt, the second call would have reached the approval code, which refuses it with `already_decided`; the client would then show an error for a click that worked.

Worked example, a reconnect. A browser tab holds cursor 412 and sleeps; the run commits up to 450. On reconnect the tab subscribes with `after_seq=412`. The server replays 413 to 450, the client refetches the views those 38 events name, the server sends `Synchronized(450)` and the tab hides its "reconnecting" banner, then live events follow from 451. One of the 38 is `run.stop_requested`, a type the cached web build predates: it decodes as `Unknown`, the cursor moves past it, and nothing breaks.

```mermaid
sequenceDiagram
  autonumber
  participant C as Client (CLI, web, editor)
  participant S as Session API
  participant Q as Command queue
  participant RT as Runtime thread (single writer)
  participant ES as Event store
  C->>S: dispatch(Approve, command_id c-7f3a)
  S->>Q: command plus future
  RT->>ES: approval.granted, command.accepted (one transaction)
  RT-->>C: CommandReceipt(accepted, seq 4127)
  Note over C,RT: committed, not done
  RT->>ES: turn 1 events (handoff run starts)
  RT->>Q: drain queue between turns
  RT->>ES: turn 2 events
  ES-->>C: stream: events 4128 onward
  C->>S: subscribe(after_seq 4127) after a reconnect
  S-->>C: replay 4128..4150, then Synchronized(4150), then live
```

Caption: The receipt returns at the first commit, the work continues on the single writer, which drains the queue after every turn, and the client follows the work through the stream.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, a source change is one change set. The product requirements' demo changes two sources (publications and contributor records), and `ApplySourceChange` named one. Two commands with turns admitted between them can rebuild against half the change and raise two selection or packet reviews for one demo. `ApplySourceChange(change_set)` applies a change set over several sources in one transaction through `record_source_changes` (ADR-P05 S2); the receipt's `ids` lists the sources it moved.
2. S2, a message to a session with an open run needs a lane. Nothing defined what `SendMessage` means while a run is open: a new request, an amendment of the answered scope, or steering for the running instance. A bare `SendMessage` to a session with an open run is refused `lane_required`, and the interface asks the person once. `Steer(run_id, text, lane)` carries the answer: `steer` and `follow_up` are ADR-P27's lanes; `amend` offers the text to the run's intake Task as an amendment of its last answered interrupt, the same path as `AnswerInterrupt(amend=True)`, so invalidation reopens scope and its dependents. A message to an ended run starts a new routed run, as before.
3. S3, the routing question is an interrupt of kind `route`, scoped to the session (ADR-P23). `route.decided` gets an owner there.
4. S4, `Finalize(run_id)` answers a budget interrupt with partial finalization (ADR-P14 S1, ADR-P24).
5. Names per ADR-P01 S1: `shown_signature`, and the command receipt type is `CommandReceipt`.

## Alternatives considered

| Option | What it is | Prior art | For | Against | Verdict |
|---|---|---|---|---|---|
| A. One function per action, request ID only on messages | v2 HLD 14.1 as written | v2 ADR-023; 05e section 2.4 | Readable; already designed | A retried approve, grant or review accept has no receipt; each new command repeats the dedupe question | Kept as sugar over D |
| B. A JSON-RPC protocol over standard input and output, one method per verb | Each harness exposes its own headless protocol | Codex `codex app-server` (code, `codexLaunchArgs.ts:15-21` in T3 Code at `ca81b7b9`); `opencode serve` (code, `OpenCodeRuntime.ts:613`); `pi --mode rpc` (code, `packages/provider-pi/src/server/mcpInjection.ts:274-282`) | Language-neutral; a process boundary | A wire format, not a contract: The receipt, replay and capability questions remain; adds a process per client | Rejected for the core; a stdio JSON-RPC front end over `dispatch` is a later interface |
| C. A control protocol driving a closed loop | The client drives the harness as a subprocess | Claude Agent SDK control protocol (code, `src/claude_agent_sdk/_internal/query.py:351-378` at `6fc539e0`) | Hooks and permission callbacks reach the client | State lives in the subprocess; the client's callback is part of the decision path | Rejected: The gate and the stop policy stay inside the runtime core |
| D. One dispatch over a tagged union, every command with an ID and an idempotent receipt (my lean) | `dispatch(Command) -> CommandReceipt` | T3 Code `orchestration.dispatchCommand` over about 60 command types (code, `apps/server/src/ws.ts:2058`; `packages/contracts/src/orchestrationV2.ts:4100-4124`); `commandId` receipts (docs, `docs/internals/00_glossary.md:27`); acknowledgement means intent committed (docs, `docs/internals/01_architecture.md:40-42`) | One dedupe rule for every command; one place to add `Stop`; interfaces hold no behaviour | One more record per command | Chosen |
| E. Durable intake with idempotency keys and leases | A turn queue that dedupes for 24 hours and promises a wakeup before accepting | Exo `TurnAdmission` (code, `crates/exoharness/src/turn_coordinator.rs:89-100` at `e1e5669`) | A written intake contract | Dedupe by time window, not forever; leases matter only with several workers | Adapted: The deduplication key is permanent, and the single writer needs no lease |

Streams:

| Option | Prior art | For | Against | Verdict |
|---|---|---|---|---|
| Replay by `Last-Event-ID` only | v2 panel stream (05e section 3.4); Server-Sent Events, general usage | Exact, because the event store is append-only | No signal for "caught up"; a new event type can crash an old client | Kept as the base |
| Cursor, bounded replay or snapshot, `synchronized`, `unknown-event` (TC-2 A, my lean) | T3 Code `subscribeThread` with `afterSequence`, replay capped at 128 events and 1 MiB, then a snapshot (code, `orchestrationV2.ts:4335-4345`, `ThreadStream.ts:18`); `synchronized` on request (code, `ws.ts:1010-1011`); `unknown-event` (code, `orchestrationV2.ts:4413-4446`) | Old clients survive new servers; the UI knows when replay ends | About 10 more lines (estimate) | Chosen, with the snapshot replaced by `Resync` because Aksha's clients refetch views |
| A bidirectional WebSocket with many methods | T3 Code: 259 RPC methods, 33 streaming (counted by the research note) | One socket for everything | A product-scale surface; one local browser needs POST plus SSE (t3code/01 section 3.3, item 6) | Rejected in the first milestone |
| Chat stream only | The AI SDK UI message stream, v2 contract section 3 | One stream | Exists only while a request is open, so a change applied from the CLI never reaches the panels | Rejected; the chat stream stays beside the session stream |

Capabilities:

| Option | Prior art | For | Against | Verdict |
|---|---|---|---|---|
| No record; clients branch on names | v2 (t3code/01 section 3.3 skipped it) | Nothing to build | Every new agent or backend adds client branches (P-1 fails at the interface) | Rejected |
| A twelve-group record per adapter | T3 Code (code, `orchestrationV2.ts:323-448`), including `terminalStatusQuality: strong, weak or none` (code, `:340`, `:718-729`) | Proven at product scale | Twelve groups describe wrapped harnesses Aksha does not have | Adapted |
| One small record per runtime with per-agent rows, derived from config (my lean) | LSP and MCP capability negotiation, general usage | Lets the first-milestone build say "no stop" honestly; the editor and web read one shape | Must stay true; the conformance test enforces it | Chosen |

## Interface

```python
# aksha/runtime/session.py (proposal; design only). Exported through aksha/runtime/api.py.
WaitKind = Literal["input", "choice", "approval", "budget", "review", "route"]   # S3: route, ADR-P23

@dataclass(frozen=True)
class CommandBase:
    tenant: str
    command_id: str                     # client-generated idempotency key; CLI derives it (below)

@dataclass(frozen=True)
class OpenSession(CommandBase): session_id: str | None = None
@dataclass(frozen=True)
class SendMessage(CommandBase): session_id: str; text: str; agent: str | None = None
@dataclass(frozen=True)
class AnswerInterrupt(CommandBase):     # input and choice interrupts only (IE-10)
    interrupt_id: str; text: str | None = None; keys: tuple[str, ...] = (); amend: bool = False
@dataclass(frozen=True)
class Approve(CommandBase): approval_id: str; shown_signature: str
@dataclass(frozen=True)
class Reject(CommandBase): approval_id: str; note: str | None = None
@dataclass(frozen=True)
class EditAction(CommandBase): approval_id: str; changes: Mapping[str, object]
@dataclass(frozen=True)
class Handoff(CommandBase): approval_id: str
@dataclass(frozen=True)
class AcceptReview(CommandBase): review_id: str; edits: Mapping[str, object] | None = None
@dataclass(frozen=True)
class RejectReview(CommandBase): review_id: str
@dataclass(frozen=True)
class GrantBudget(CommandBase): run_id: str; grant: "Grant"
@dataclass(frozen=True)
class ApplySourceChange(CommandBase): change_set: str   # S1: one change set over one or more sources
@dataclass(frozen=True)
class Stop(CommandBase): run_id: str; reason: str = "Stopped by user"  # reserved; PR-18 owns it
@dataclass(frozen=True)
class Finalize(CommandBase): run_id: str                               # S4, ADR-P24
@dataclass(frozen=True)
class Steer(CommandBase):                                              # S2, ADR-P27
    run_id: str; text: str; instance: str | None = None
    lane: Literal["steer", "follow_up", "amend"] = "steer"

Command = (OpenSession | SendMessage | AnswerInterrupt | Approve | Reject | EditAction | Handoff
           | AcceptReview | RejectReview | GrantBudget | ApplySourceChange | Stop
           | Finalize | Steer)

@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    command: str                        # the union member's name, "Approve"
    status: Literal["accepted", "refused"]
    code: "RefusalCode | None"          # v2's codes, plus "command_id_reused" and "unsupported"
    seq: int                            # sequence number of the command's commit
    ids: tuple[str, ...]                # created or affected IDs: "A-0002", "R-0004"
    run: str | None                     # the run the command started or resumed
    replayed: bool                      # True when this is the stored receipt of an earlier dispatch

def dispatch(cmd: Command) -> CommandReceipt: ...                       # returns at commit, not at the turn end
def wait_for_yield(tenant: str, session_id: str, after_seq: int,
                   timeout_s: float | None = None) -> "Yielded": ...  # v2's Yielded, a projection
def receipt(tenant: str, command_id: str) -> CommandReceipt | None: ...  # a projection over command.* events

# Sugar kept from v2 HLD 14.1, each one line over dispatch, for example:
def approve(tenant: str, approval_id: str, shown_signature: str, command_id: str | None = None) -> CommandReceipt: ...

def cli_command_id(session_id: str, cmd_name: str, args: Mapping[str, object],
                   answers: str | None) -> str: ...  # sha256 over these, so an up-arrow retry is the same command

# Streams
@dataclass(frozen=True)
class EventItem:
    seq: int; event_type: str; run: str | None; step: str | None; at: str
    payload: Mapping[str, object]       # source text and citation-span text left out (v2 contract 4)
@dataclass(frozen=True)
class Resync: seq: int                  # refetch every view, then cursor = seq
@dataclass(frozen=True)
class Synchronized: seq: int            # replay reached the live tail; sent once per subscription
@dataclass(frozen=True)
class Transient: run: str; step: str; channel: Literal["text", "tool_output"]; data: str  # never stored
@dataclass(frozen=True)
class Heartbeat: pass
@dataclass(frozen=True)
class Unknown: seq: int | None; raw_type: str   # client-side only: what an unknown item decodes to

StreamItem = EventItem | Resync | Synchronized | Transient | Heartbeat

def subscribe(tenant: str, session_id: str, after_seq: int | None,
              max_replay: int | None = None) -> Iterator[StreamItem]: ...
def decode(raw: Mapping[str, object], known: frozenset[str]) -> StreamItem | Unknown: ...

# Capabilities: a projection of the composition root and the loader's output, never stored
@dataclass(frozen=True)
class StreamCapabilities:
    schema_version: int; synchronized_marker: bool; resync: bool; transient: bool
    max_replay: int | None

@dataclass(frozen=True)
class AgentCapabilities:
    name: str; purpose: str; definition_hash: str
    interrupt_labels: tuple[tuple[WaitKind, str], ...]   # ("input", "needs_input")
    effectful_tools: tuple[str, ...]                     # an interface can say "this agent can send"
    stop_policies: tuple[str, ...]                       # opted-in built-ins (PR-16)

@dataclass(frozen=True)
class CapabilityRecord:
    api_version: str                    # the extension API version (ADR-P20)
    runtime_commit: str; dirty: bool
    mode: Literal["fake", "scripted", "replay", "live"]
    commands: frozenset[str]            # union members dispatch accepts here; no "Stop" until PR-18 ships
    interrupt_kinds: frozenset[WaitKind]
    model_classes: Mapping[str, str]    # {"generation": "rule-based"}; extraction and classification at cut row 8
    sandbox_backends: tuple[str, ...]   # ("refuse",) in the first milestone
    stream: StreamCapabilities
    agents: tuple[AgentCapabilities, ...]

def capabilities(tenant: str) -> CapabilityRecord: ...

# The single writer (changes 05e section 2.4)
class RuntimeThread:
    def start(self, app: "App") -> None: ...                    # takes the writer lock, boots, serves
    def submit(self, cmd: Command) -> "Future[CommandReceipt]": ...    # resolved at the command's commit
    def on_commit(self, fn: Callable[[str, str, int], None]) -> None: ...  # (tenant, session, seq)
    def stop(self, timeout_s: float = 5.0) -> None: ...         # finishes the current turn, then exits
```

How the runtime thread works, in order:

1. Take the writer lock, run recovery on startup (PR-02), then loop.
2. If the command queue is not empty, take one command, run its handler in one transaction that also appends `command.accepted` or `command.refused`, and resolve its future with the receipt.
3. Else, if the scheduler has a runnable Task instance, run one turn of it (PR-06), commit, and call every `on_commit` callback.
4. Go to step 2. A command that needs a run to continue (an answer, an approval) only appends facts; the scheduler then finds the run runnable on its next pick.

Events it writes:

| Event | When | Deduplication key |
|---|---|---|
| `command.accepted` (new; name provisional until the PR-01 event catalogue) | In the command's first transaction, beside the command's own facts | `cmd:{tenant}/{command_id}` |
| `command.refused` (new; provisional) | When the handler refuses (`hash_mismatch`, `not_found` and the rest) | `cmd:{tenant}/{command_id}` |
| Each command's own facts, unchanged from v2: `session.opened`, `user.message`, `route.decided`, `run.started`, `interrupt.resolved` (v2: `pause.answered`), `approval.granted`, `approval.rejected`, `approval.voided`, `review.accepted`, `review.rejected`, `budget.granted`, `source.changed` (v2: `world.changed`), and `run.stop_requested` once PR-18 ships | As v2 HLD 5.2 | As v2 HLD 5.3 |

`user.message` loses its `request_id` field; the command ID on `command.accepted` replaces it. `decision.refused` (v2) becomes the `command.refused` record for decisions, so one refusal is one fact.

Projections it reads: The session view (transcript with agent labels), pending interrupts and approvals, run status and the next action, the receipt index by command ID, the stop-policy stack and last stop decision for the run view, and the capability record (from configuration, not the event store).

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Missing primitive | "Who else does camera-free occupancy sensing?" arrives with no agent named; mid-run "also include manual alternatives" | S3 points at ADR-P23 (router); S2 `lane_required` and the `amend` lane |
| Patent | Needs a runtime change | Two fixture sources change for one demo; `ApplySourceChange` names one | S1 one change set, one transaction |
| Coding | Missing primitive | Turn 37 of `fix[S2]`: "use the existing retry helper instead" | S2 `Steer` with lanes; the primitive is ADR-P27 |

Verdict after the amendments: fits for all three once S1 and S2 land and ADR-P23 and ADR-P27 exist. S1, S3 and S4 are first-milestone work; S2's refusal ships the first milestone and the `steer` lane is later.

Open for the maintainer: S2's refusal against a default lane (lean: refuse and ask, because each default is wrong for one of the three agents).
