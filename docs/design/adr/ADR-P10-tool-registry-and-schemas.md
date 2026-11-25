# ADR-P10: Tool registry and schemas (every tool is declared data with one effect class, and the schema the model sees is the schema the gate checks)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. Two calls in the decisions log shape it and are the maintainer's: agents are defined in Python with their structure as declared data (decision 2), and the runtime core must hold no agent-specific code (decision 1, the design patterns note P-1). Neither chooses the registry's fields, the effect classes or the rule that splits argument-dependent tools, so those stay Proposed. Amended 25 Nov 2026 after the three-agent stress test: S1 to S3 below are new and Proposed.

## Terms

Primitive PR-10 in the primitives catalogue, cluster C (acting safely). Depends on PR-20 (tools arrive with the agent definition). Needed by PR-06 (the agent loop offers tools), PR-09, PR-11 (the policy gate checks against the registry), PR-12 (exec tools) and PR-13 (tools that mint citation spans).

Terms used here, in the industry names of the design glossary:

- **Tool registry** is the frozen, per-run table of every tool an agent may call: a name, a JSON Schema for the input, a description and the metadata the runtime core reasons about.
- **Tool spec** (`ToolSpec`) is one row of the registry, declared as data. The **tool body** is the plain Python function the runtime only calls.
- **Effect class** is the one category of effect a tool has: `read`, `store`, `external`, `exec`, `model` or `reserved`. It decides the route the policy gate gives the call and the recovery policy the durable step executor applies after a crash.
- **Recovery policy** is how a call left dangling by a crash is finished: `rerun` (an at-least-once step, safe to repeat), `reconcile` (look the idempotency key up in the downstream system of record) or `report` (an at-most-once step whose outcome is recorded as unknown).
- **Extension API** is the set of public names agents and extensions import from the runtime core (`aksha/runtime/api.py`).

## Context: without a registry, permissions in code have nothing to check

The product requirements ask that each agent's allowed operations be enforced in code, not prompts. Code can only enforce what it can see. If a tool is a Python function with a docstring, the runtime core cannot tell a page fetch from a document send until the body runs, and by then the send has happened.

What breaks without a declared registry, one failure per consumer:

| Consumer | What it needs from the tool | What breaks without it |
|---|---|---|
| Policy gate (PR-11) | Name, schema, effect class, which agent and Task list it | Permission checks become string matching inside each tool body, and a tool that forgets to check runs unchecked (Decode's `BYPASS` path, landscape note section 4) |
| Durable step executor (PR-02) | Recovery policy, idempotency-key fields | A crash during a send cannot be finished safely; the runtime either resends or drops it |
| Transactional outbox (PR-03) | Whether the call has an external effect | The outbox entry is written for every call or for none |
| Lineage (PR-04) | Which source collections and artifact kinds the body may read and write | Lineage inputs cannot be checked against rights |
| Grounding (PR-13) | Whether the tool mints citation spans | Any tool, including search, could mint citable text |
| Budgets (PR-14) | Timeout and retry policy | Every tool gets the same timeout, or none |
| Trace and evals (PR-21, PR-22) | A stable name and an input hash | Calls cannot be compared across runs |

The contrast coding agent adds the hard case. Its natural tool is `shell(command)`, and the effect of `shell("pytest")` (a read of the workspace) differs from `shell("git push origin fix")` (an external effect on a remote). Effect metadata on a tool whose effect depends on a free-text argument is a guess.

## Decision

1. **A tool is data plus one function (P-3).** Every tool is a frozen `ToolSpec` declared inside the agent definition, plus a body the runtime core calls. The core reasons only about the spec; it never inspects the body.
2. **One effect class per tool, and argument-dependent effects are split, not parsed.** A tool has exactly one effect class. Where a free-text argument would change the effect, the agent declares two tools. The coding agent gets `shell` as an `exec` tool (it runs inside the sandbox of PR-12 with the network off, so `git push` inside it fails as a recorded denial) and `push_branch` as an `external` tool with a receiver, so the push goes through the transactional outbox and is reconciled against the remote's ref after a crash. The runtime core never parses a command string to guess its effect.
3. **The published schema is the validated schema.** The model is shown the exact JSON Schema (Draft 2020-12) the gate validates against, so the two cannot drift; the Claude Agent SDK does the same by validating the dict it publishes (`cas:__init__.py:630-632`). Authors write the schema by hand in the first milestone. A helper that derives it from a Python signature, using only the standard library's `inspect` and `typing`, is later, because the runtime core imports only the standard library and `jsonschema`.
4. **The registry is frozen per run and part of the agent definition version hash.** The registry is built once by the agent loader, and its canonical form goes into the definition hash stored on `run.started`. A run that resumes under a changed tool set ends through the stop policy with `definition_changed`, never with silently different tools.
5. **The loader refuses an unsafe registry before anything runs.** The eight refusals of 05c section 5.4 stay, among them: an `external` tool with neither a receiver nor `recovery="report"` (FR-RE-04), an `exec` tool with no sandbox needs, a citation-minting tool on a Task that does not extract, and a child Task whose tools are not a subset of its parent's.
6. **Tool errors are results, not exceptions.** An unknown tool, bad arguments, a refusal or a raising body each become a recorded `step.completed` whose result the model reads in its next turn, with every failing schema path listed, as Pi does (`pi:packages/agent/src/agent-loop.ts:729`, `:736`). A raising body is `tool_exception` and is not retried, because a bug repeats.
7. **Large results are capped where they are recorded.** The model view of a result is cut at 8,000 characters, head and tail kept, with the full size and hash on `step.completed`, so a rebuilt context recomputes nothing. Exo applies the same 8,000-character inline limit and keeps the full value as an artifact (`exoharness/typescript/harness/tools.ts:12-15`, `:165-175`); storing every read result as an input artifact is the general design (RQ-8), not the first milestone's.
8. **Three implicit tools are added by the loader**, so no agent lists them and each still appears in the permission matrix: `finish` (the model proposes to stop; the stop policy decides), `ask` (raises an interrupt of the Task's declared kind) and `save_artifact` (one generic typed save, restricted to the Task's `writes`).
9. **Model Context Protocol (MCP) tools arrive through an extension, and their effect class is declared by the agent, not trusted from the server.** An MCP adapter extension turns a server's tool list into `ToolSpec` rows at load time. MCP's tool annotations (`readOnlyHint`, `destructiveHint` and others, in the MCP specification of 26 Mar 2025, general usage and not in our notes) are hints a server writes about itself, so the agent definition must state the effect class of each MCP tool it lists, and the loader refuses one that has none. Later, not the first milestone.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, one `ToolSpec` for an effectful tool. The Patent review found `send_document` described with three field sets: this ADR's `recovery`, `receiver`, `idempotency_key_fields` and `needs_approval`; ADR-P03's `EffectSpec` with `system`, `key_fields`, `semantics`; and ADR-P17's `ApprovalRule` with no field on the tool. The four fields are replaced by two: `external: EffectSpec | None` and `approval: ApprovalRule | None`. The loader requires `external` exactly when `effect == "external"`, and `approval` requires `external`. Semantics use ADR-P02's names (`at_least_once`, `reconcile`, `at_most_once`); the `Recovery` literal is gone. Worked example: `send_document` declares `external=EffectSpec(system="send", key_fields=("/document", "/recipient_id"), semantics="reconcile")` and `approval=ApprovalRule(facts=recipient_facts, targets=lambda a: (a["document"],))`; `write_form` declares `external` with no approval.
2. S2, `result_key` for recorded queries. A JSON pointer to a hit's stable identity, so ADR-P05's result-set hash covers identities and ranking noise invalidates nothing (ADR-P05 S6).
3. S3, `egress` for tools that reach the network outside the sandbox. A `read` or `external` tool whose body makes network requests names an egress policy, and the loader refuses one that does not (ADR-P26).

Agent-side, no core change: The coding agent's `git_commit` is a store tool that saves `coding.commit` (tree ref, parent, message); `push_branch`'s body rebuilds the commits from tree blobs outside the box and pushes, so `.git` stays out of the tree.

### Amendments from the build (25 Nov 2026, Proposed)

1. B1, "loader-added" and "reserved" are two words (D8). The three tools the loader adds are `LOADER_ADDED_TOOLS` in `aksha/runtime/tools/loader_added.py`: their names are taken, whatever their effect class. "Reserved" is only the effect class of `finish` and `ask`, which the runtime handles and which have no body. `save_artifact` is loader-added but a `store` tool, so a stop-policy narrowing (plan mode) removes it like any other save, in both the gate (ADR-P11 layer 4) and `ToolRegistry.offered`.
2. B2, only the runtime declares a reserved tool (D5, D8). The loader refuses an agent or extension tool with `effect="reserved"` (`reserved_effect_not_allowed`), with or without a body; otherwise it would be routed `reserved`, which is meant for `finish` and `ask` only.
3. B3, a `ToolUse` override takes effect (D4). `ToolUse.retries` and `ToolUse.timeout_ms` are written into the contributed tool's `RetryPolicy` when the registry is built (`max_retries`, `timeout_ms`), so they enter `canonical_hash` and the definition hash. The loader refuses them on a loader-added tool (`override_not_allowed`) and refuses negative or zero values (`override_invalid`).
4. B4, the matrix test exists. `test_permission_matrix_matches_definitions` (in `tests/architecture/test_definitions.py`) generates `matrix(...).markdown()` for the conformance agent and compares it with the committed `tests/architecture/permission_matrix.md`; a real agent's README table follows the same path when the first agent exists.
5. B5, an agent lists both shapes. `Agent.tools` holds `ToolDecl` (an own tool with its body) and `ToolUse` (a loader-added or extension tool by name); ADR-P20's interface is amended to match.

## Alternatives considered

| Option | What it is | Prior art | For | Against | Verdict |
|---|---|---|---|---|---|
| A. Schema from the function signature | Introspect a Python signature and docstring into a Pydantic model | OpenAI Agents SDK (`oai:function_schema.py:12`, `:40`; validated at `oai:tool.py:2787-2797`); Pydantic AI and ADK (landscape note section 2) | Least typing for authors | Pulls Pydantic into the runtime core; effect class, recovery policy and rights still need declaring somewhere | Rejected for the core; a stdlib-only helper is later |
| B. Declared `ToolSpec` data plus a body, schema hand-written (my lean) | A frozen dataclass per tool, JSON Schema as a dict | Claude Agent SDK `@tool` with `jsonschema.validate` on the published dict (`cas:__init__.py:630-632`); Exo tools declared in a module and loaded by the harness (`exoharness/typescript/harness/tools.ts:12-15`, `tool-modules.ts:141-144` at `e1e5669`) | The gate, loader, step executor and matrix all read one record; no dependency added | Authors type a schema; drift between schema and body is caught only by tests | Chosen |
| C. One `shell` tool, effect decided by parsing the command | Classify `git push` as external, `pytest` as read | Codex's approval policy on commands (`codex:protocol/src/protocol.rs:1167-1190`); opencode's per-pattern rules (`opencode:packages/core/src/permission.ts`, at `ff4a652`) | One tool the model already knows | A parser guesses at shell syntax (pipes, `sh -c`, aliases); a wrong guess turns an external effect into an unrecorded one | Rejected for effect class; pattern rules on arguments survive as narrowing in PR-11 |
| D. MCP as the only tool format | Every tool is an MCP server | T3 Code's MCP toolkits, among them `delegate_task` (`mcp/toolkits/orchestrator/tools.ts:89`, `packages/contracts/src/orchestratorMcp.ts:171-203` at `ca81b7b9`) | Cross-vendor; tools reusable outside Aksha | A process boundary for every fixture tool; annotations are self-reported, so the effect class still has to be declared | Rejected as the only format; MCP comes in through an extension |
| E. Tools without effect metadata, safety in prompts | The model is told which tools are dangerous | Pi has no approval step before tool calls (`pi:packages/coding-agent/docs/security.md:3`) | Simplest | Fails the product requirements directly | Rejected |

## Interface

```python
# aksha/runtime/tools.py (proposal; design only). Exported through aksha/runtime/api.py.
EffectClass = Literal["read", "store", "external", "exec", "model", "reserved"]

@dataclass(frozen=True)
class RetryPolicy:                                   # counted from the event store (ADR-P14)
    timeout_ms: int = 10_000
    max_retries: int = 2                             # attempts = max_retries + 1
    retry_on: frozenset[str] = frozenset({"timeout", "transient"})
    backoff_ms: tuple[int, ...] = (500, 1_000)       # injected clock in fake model mode

@dataclass(frozen=True)
class ToolSpec:
    name: str
    owner: str                                       # "runtime", "shared", or the agent's name
    effect: EffectClass
    description: str
    schema: Mapping[str, Any]                        # JSON Schema 2020-12; shown to the model as is
    collections: frozenset[str] = frozenset()        # source collections the body may read
    reads_kinds: frozenset[str] = frozenset()        # artifact kinds the body may read
    writes_kinds: frozenset[str] = frozenset()       # artifact kinds its save requests may name
    mints_citation_spans: bool = False               # ADR-P13
    recorded_query: bool = False                     # re-run by invalidation (PR-05)
    retry: RetryPolicy = RetryPolicy()
    result_key: str | None = None                    # S2: JSON pointer to a hit's stable identity
    external: "EffectSpec | None" = None             # S1, ADR-P03: system, key_fields, semantics, on_unknown
    approval: "ApprovalRule | None" = None           # S1, ADR-P17: facts, targets
    egress: str | None = None                        # S3, ADR-P26: a named egress policy
    sandbox: "SandboxNeeds | None" = None            # exec tools only (ADR-P12)
    model_alias: str | None = None                   # model tools only (PR-07)

ToolBody = Callable[["ToolContext", Mapping[str, Any]], "ToolOutcome"]

@dataclass(frozen=True)
class ToolDecl:                                      # what an agent definition lists
    spec: ToolSpec
    body: ToolBody | None                            # None only for model and reserved tools

class ToolRegistry:
    @classmethod
    def build(cls, agent: "AgentDefinition", extensions: Sequence["Extension"]) -> "ToolRegistry": ...
        # refuses duplicates, unknown owners, schemas that do not compile, a body on a model or
        # reserved tool, a missing body on any other class; adds finish, ask, save_artifact
    def get(self, name: str) -> ToolDecl: ...        # raises UnknownTool
    def offered(self, task: "Task", narrowing: "StopPolicyNarrowing") -> tuple["ToolView", ...]: ...
        # what the model is shown this turn: never a tool the gate would refuse by list or narrowing
    def validate(self) -> list["LoadError"]: ...     # the eight loader refusals of 05c 5.4
    def canonical_hash(self) -> str: ...             # part of the agent definition version hash
    def matrix(self, agents: Sequence["AgentDefinition"]) -> "PermissionMatrix": ...

def render_result(outcome: "ToolOutcome", cap_chars: int = 8_000) -> "ToolResultView": ...  # pure
```

`ToolContext`, `ToolOutcome`, `Ok`, `Err` and `SaveRequest` keep 05c section 5.3's shapes. A `ToolContext` has no store handle, no model handle and no adapter handle, so a body cannot write the event store.

Events it writes: none of its own. The registry's hash rides on `run.started`. Every call is recorded by the durable step executor as `step.started` and `step.completed` (with `outcome`, the input hash, the capped view's size and hash), or `step.failed` per attempt; v2's event names stay until the event-store ADR (PR-01) decides on renames.

Projections it reads: none at call time. The registry is built from the agent definition, not from the event store, which is why it cannot be changed by anything the model writes.

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Fits | A search snippet shows a third price for COMP-02 | Nothing new: `search` mints no spans; `fetch_page` does. S2 adds `result_key` |
| Patent | Needs a runtime change | `send_document` has three field sets across ADR-P10, P03 and P17 | S1: one `ToolSpec` with `external` and `approval` |
| Coding | Fits with agent-side code | `shell("git commit && git push")`: who makes the commit `push_branch` pushes? | An agent-side `git_commit` store tool; the split rule (D2) sends the push to `push_branch` |

Verdict after the amendments: fits for all three once S1 lands. S1 is first-milestone work, because the Patent Agent's send tool is declared with it.

Open for the maintainer: none new.

## Sources (public)

1. Model Context Protocol specification, 26 Mar 2025, tool annotations (general usage; not read in our notes).
