# ADR-P10: Tool registry and schemas (every tool is declared data with one effect class, and the schema the model sees is the schema the gate checks)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. Amended 27 Nov 2026 with T1 to T4 (shared input tools and common tools), Proposed. Two calls in the decisions log shape it and are the maintainer's: agents are defined in Python with their structure as declared data (decision 2), and the runtime core must hold no agent-specific code (decision 1, the design patterns note P-1). Neither chooses the registry's fields, the effect classes or the rule that splits argument-dependent tools, so those stay Proposed. Amended 25 Nov 2026 after the three-agent stress test: S1 to S3 below are new and Proposed.

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
| Policy gate (PR-11) | Name, schema, effect class, which agent and Task list it | Permission checks become string matching inside each tool body, and a tool that forgets to check runs unchecked |
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
3. **The published schema is the validated schema.** The model is shown the exact JSON Schema (Draft 2020-12) the gate validates against, so the two cannot drift: the dict that is published is the dict that is validated. Authors write the schema by hand in the first milestone. A helper that derives it from a Python signature, using only the standard library's `inspect` and `typing`, is later, because the runtime core imports only the standard library and `jsonschema`.
4. **The registry is frozen per run and part of the agent definition version hash.** The registry is built once by the agent loader, and its canonical form goes into the definition hash stored on `run.started`. A run that resumes under a changed tool set ends through the stop policy with `definition_changed`, never with silently different tools.
5. **The loader refuses an unsafe registry before anything runs.** The eight refusals of 05c section 5.4 stay, among them: an `external` tool with neither a receiver nor `recovery="report"` (FR-RE-04), an `exec` tool with no sandbox needs, a citation-minting tool on a Task that does not extract, and a child Task whose tools are not a subset of its parent's.
6. **Tool errors are results, not exceptions.** An unknown tool, bad arguments, a refusal or a raising body each become a recorded `step.completed` whose result the model reads in its next turn, with every failing schema path listed. A raising body is `tool_exception` and is not retried, because a bug repeats.
7. **Large results are capped where they are recorded.** The model view of a result is cut at 8,000 characters, head and tail kept, with the full size and hash on `step.completed`, so a rebuilt context recomputes nothing. Storing every read result as an input artifact, with the full value kept beside the capped view, is the general design (RQ-8), not the first milestone's.
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

### Amendments: shared input tools and common tools (27 Nov 2026, Proposed)

Until this amendment each document agent wrote its own readers. The research agent had `list_inputs`, `search`, `fetch_page`, `read_table` and `read_note`, and its own `fetch_url` body; the patent agent had nine readers shaped by its fixtures (`list_candidates`, `read_note`, `read_contributors`, `read_people`, `read_disclosures`, `read_document`, `read_priorities`, and the template pair). The same jobs (list what was given, read one input in pages, search across inputs, cite what was read) were written twice, with different limits, different error codes and different shapes. A survey of common agent tool sets on 27 Nov 2026 found paged reads with a continue-at notice, lexical search whose hits are pointers, a plan list and a scratchpad in most of them. These amendments move those jobs into the harness. They do not change the registry, the effect classes or the gate.

1. T1, the harness declares the input tools (`runtime/inputs/tools.py`, exported through `aksha.runtime.api` 2.14). `input_tools(agent, reading)` returns three `ToolDecl`s that any agent lists. `reading` is an `InputReading`, which is declared data: the agent's versioned source and collection, its decide alias and `DocumentKind`s (ADR-P07 G3), and the window caps. All three are `read` tools over that one collection.
   - `list_inputs(kinds?, formats?, offset?, limit?)` lists each input with its format, its size in characters and lines, its version, a table's columns and, when the agent declares kinds, the decided kind of the input and of each of its sections (G4). It is a recorded query (ADR-P05 D6). Its version hashes the ref, the kinds and the columns of each hit, so a new input of another kind leaves the listing unchanged. With `kinds`, an input whose record kind the decision abstained on is kept, with `decided: false` and a note that the record's kind is undecided (28 Nov 2026: a fast classifier is an optimisation, never a gatekeeper). It is not kept when the record's kind was decided, nor when one of its sections was confidently decided as a declared kind outside `kinds`, so an abstaining preamble never defeats the filter. It shows `as_of`, the date the run counts to; reading it records the date as a time read (ADR-P25 B2). It mints nothing.
   - `read_document(ref, offset?, limit?, section?, cells?, where?, extract?)` reads one input through its representation, which is what the ingest extension serves at the input's path (ADR-P31). The window is numbered lines, `limit` lines from `offset`. It is capped at `page_chars` characters and `line_chars` characters per line, and cut shorter when its whole result would pass the agent's result cap (`result_chars`, ADR-P08), so the cap never cuts a result in the middle. A window that was cut says which offset to continue at, and an offset past the end is refused with the input's line range (`offset_past_end`). Each structural block in the window is a unit, under its anchor: the nearest heading, or a `[p.N]`, `[slide N]` or `[sheet X]` marker. In a line-oriented format a block's whole lines are also a unit, so a heading can be quoted with its `#`. With `cells`, a table's cells are units too. Units are minted as citation spans in unit order, so unit `n` is cited by the `n`-th span ID. `where` reads a comma-separated table's header and only the rows whose column holds one of the given values, and records the read as that record part (`where_part`, at `part_version`). `extract` runs decide-then-extract (G3): each typed value comes back with the units that state it, and each value's own range is minted as a unit. Whatever the fast models leave is listed under `unresolved`.
   - `search_inputs(query | terms, ref?, formats?, match?, limit?)` searches every unit of every input, or of one input. It returns ranked passages with their ref, anchor, lines and a snippet. It is a recorded query whose version hashes the set of inputs it hit, and it mints nothing.
   The generic `fetch_url` (`fetch_url_tool(agent, reading)`) shows a fetched page through the same view (`read_view`), so a web page and an uploaded page read alike.
2. T2, two common tools (`runtime/tools/common.py`).
   - `todo(todos)` is a `read` tool. It takes the whole plan on every call and refuses two items in progress, or one item named twice, naming the paths at fault. The plan is the latest call on the log; nothing else stores it.
   - `think(thought)` is a `read` tool that only acknowledges. It is off by default: no shipped agent lists it.
3. T3, the agents keep only their domain actions. The research agent now lists only harness tools: `web_search`, `fetch_url`, `list_inputs`, `read_document`, `search_inputs` and `todo`. The patent agent lists the same six plus its domain tools: `search_publications`, `fetch_publication`, `fill_form`, `send_packet`, and `list_templates` and `read_template`. The template pair stays because it shows exactly the fields `fill_form` checks, which are required and which only counsel completes, so it is part of the form tool's contract. (Superseded on 28 Nov 2026 by F1 below: the patent agent's only tool of its own is now `send_packet`.) A join across inputs is now the generation model's reading, not a tool's: which rows are one candidate's, who a person ID names, how many days have passed since a disclosure (counted to `as_of`), which column of an export holds the customer's words, and which theme a review speaks to. The classification aliases that answered those joins inside the old readers (`table_roles`, `candidate_of`, `theme_match`) are no longer declared.
4. T4, rule-based models read with the agent's offline readers. In `fake` mode the generation model only sees what its request carries. It puts the windows a tool returned back at their `start` offsets and reads them with the agent's own rule readers, run in process (`OfflineReaders`, `runtime/models/offline.py`, judged by the declared native thresholds and recording nothing). It cites each passage by the unit with exactly that range, or else by the smallest unit that holds it. Each Task reads only the inputs its rules use: by kind, a candidate's table rows by `where`, and an email only when a row links it. So the demo's change set still reopens exactly C1's chain.

Measured on 27 Nov 2026 in fake mode, every bundled eval set passes before and after (research, scripted, change, patent, tilldew, cindral). The six held-out worlds give the same results as before, eval case by eval case and failure cluster by failure cluster: research passes 25 of its 26 eval cases and patent 3 of its 23, the gaps that `docs/limitations.md` item 19 lists. Steps per passing episode, against main as of 27 Nov 2026: research +3% on the stillroom worlds, +4% on tilldew and +16% on cindral (a cut window is read on in a turn of its own); patent -16% on the stillroom worlds, -19% on tilldew and -15% on cindral. Patent steps drop because the joins happen in the model's turn and are no longer tool calls. The demo reaches the same 14 outputs from the same change and keeps the other 13 patent outputs.

Not done, and open for the maintainer:
- `delegate` is not built.
- Questions to the person stay `ask`; there is no `ask_user` with options.
- The coding agent's file tools (`bash`, `read_file`, `edit`, `write_file`, `glob`, `grep`) are not built.
- A fetched page is not stored once with a handle for later pages: a later window fetches the page again.
- Content tags for read-before-cite are not built.
- `where` reads only comma-separated tables. A table whose columns name no candidate is read whole.

### Amendment F1: tools two agents need are the harness's, the form tool included (28 Nov 2026, Proposed)

The maintainer asked on 28 Nov 2026 that a tool every agent could use live in the runtime as one shared tool, never as an agent's own copy. After T3 the patent agent still carried five such tools. `search_publications` and `fetch_publication` searched and read uploaded inputs of one kind, which `search_inputs` and `read_document` already do; `fetch_publication` also decided the kind of every input on each call (about 21 classification calls, 122 to 226 seconds live, [06-evals-and-live-runs.md](../../assignment/06-evals-and-live-runs.md)). `list_templates` and `read_template` listed and read inputs of another kind. `fill_form` checked a form's field statuses with rules that hold for any form.

1. F1a, `search_inputs` takes `kinds` (`runtime/inputs/tools.py`) for an agent that sets `InputReading.search_kinds` (off by default, so an agent that does not search by kind keeps the tool list its recordings were made with; the bundled research recordings still replay). It then searches only the inputs whose kind, or a section's kind, is one of them, decided as `list_inputs` decides it. The query ID names the kinds, and the version hashes the inputs hit, so a new input of another kind leaves the search unchanged. The tool declares the agent's decide alias, so the decision is a fast-class call from the body (ADR-P07 G1) and is shared with the listing's for the run.
2. F1b, the form tools (`runtime/forms/tools.py`, exported through `aksha.runtime.api` 3.2). `form_tools(agent, reading, spec)` returns `read_form` (`read`, mints spans) and `fill_form` (`store`, writes the agent's form kind). `spec` is a `FormSpec`, declared data: which document kind is a blank form, which list fields of that kind's extraction hold the form's fields, the required ones and the ones only a confirmer completes, the header field a form names itself by, the confirm status and the confirmer's name, the field key of an open question, and the artifact kind and the content field a filled form is saved under. `read_form(template)` shows a blank form's fields exactly as `fill_form` checks them, read over the form's passages as whole lines, and refuses an input the kind decision confidently read as another kind (`not_a_form`) or one with no field (`no_fields`). The decision is an optimisation, never a gatekeeper: when it abstains, the form is recognised by its structure. It reuses the row `list_inputs` decides (the same requests, so memo hits), refuses `not_a_form` an undecided input with a section confidently decided as another declared kind, and otherwise accepts it when the form kind's extraction yields at least one form field, and both results say `recognised_by: "structure"`; an undecided input with no form field is `not_a_form`. `list_inputs` with `kinds` keeps an input whose record kind is undecided, under the same section rule, with a note saying so, rather than hiding it. `fill_form(template, key, fields)` refuses a field list that is not the form's (`fields_mismatch`, naming the form's fields), a status outside complete, missing and the confirm status (`status_invalid`), a complete field with no value (`complete_needs_value`), and a complete field that only the confirmer completes or that carries an open question (`confirm_only`). It takes `required` from the form, never from the model, computes the counts, and saves `<key>~<form name>` recording the form's version as a read. A key other than the Task instance's is refused (`wrong_key`). `template` takes the form's input ref or its name as `read_form` shows it (the name the plan uses too); a name two forms share is refused (`ambiguous_ref`), and every refusal points at `/template`. The kind `fill_form` saves into comes from the harness too: `form_kind(spec)` returns it (keyed, requiring the form's name, the key field, the fields and the counts), and its `compute` recomputes the counts from the fields on every save with the spec's statuses, so the agent declares `form_kind(spec)` among its artifacts and keeps no status list or count rule of its own.
3. F1c, the patent agent keeps one tool of its own, its one external effect `send_packet`. It lists `search_inputs` and `read_document` in `prior_art` and `read_form` and `fill_form` in `scope` and `prepare`, and declares `tools.FORMS`: the `form_template` kind, its `template_fields` extraction, counsel as the confirmer, `needs_counsel_confirmation` as the confirm status and `counsel_question` as the open question. Nothing patent-specific is in the runtime; `tests/unit/runtime/forms/test_tools.py` tests the form tools with a checklist agent.

What this costs. The search's term groups (a hit matches every group) are now one `search_inputs` per group, intersected by the model, so a new publication that matches one group of a candidate's search moves that search's version even when it matches no other group. The offline model reads every input the intersection names in full and works the hits and families out itself, as a live model reads `read_document` and its extraction. A form is named by its header field, else its file name.

### Amendment L1: `save_artifact` shows what the Task writes (27 Nov 2026, Proposed)

A live run of the research agent on a hosted model made 68 model calls with no provider error and still ran out of turns on its first Task. Every `save_artifact` call was refused: first a guessed kind, then one schema error per call, because the model was never shown the kinds it may write or their schemas, and each refusal named only one error.

1. L1, the save tool is described per Task (`offered_for_task` in `runtime/tools/loader_added.py`, applied by `run_turn`). Its description names each kind the Task writes by full name, whether it takes a `key`, the fields that must cite their evidence (each kind's `CitePolicy` patterns), and the kind's content schema as compact JSON (a dataclass kind is shown as a JSON Schema of its fields). The parameters schema is unchanged, so D3 holds: what the gate validates is what the model is shown, and the per-Task part is the description, made from kinds that are already in the definition hash. A union over kinds in the parameters was considered and left out: it would publish a schema the gate does not validate, and not every provider takes a union.
2. L2, a schema refusal lists every error (ADR-P04 D7 check 2). The store reports every failing path of the content in one `schema` refusal, each with its message and the schema part it expected (a missing property with that property's schema, cut at 400 characters), and the step's error carries every path. A `kind_unknown` or `rights` refusal names the kinds the Task writes. So a model can fix a save in one retry.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. Schema from the function signature | Introspect a Python signature and docstring into a Pydantic model | Least typing for authors | Pulls Pydantic into the runtime core; effect class, recovery policy and rights still need declaring somewhere | Rejected for the core; a stdlib-only helper is later |
| B. Declared `ToolSpec` data plus a body, schema hand-written (my lean) | A frozen dataclass per tool, JSON Schema as a dict | The gate, loader, step executor and matrix all read one record; no dependency added | Authors type a schema; drift between schema and body is caught only by tests | Chosen |
| C. One `shell` tool, effect decided by parsing the command | Classify `git push` as external, `pytest` as read | One tool the model already knows | A parser guesses at shell syntax (pipes, `sh -c`, aliases); a wrong guess turns an external effect into an unrecorded one | Rejected for effect class; pattern rules on arguments survive as narrowing in PR-11 |
| D. MCP as the only tool format | Every tool is an MCP server | Cross-vendor; tools reusable outside Aksha | A process boundary for every fixture tool; annotations are self-reported, so the effect class still has to be declared | Rejected as the only format; MCP comes in through an extension |
| E. Tools without effect metadata, safety in prompts | The model is told which tools are dangerous | Simplest | Fails the product requirements directly | Rejected |

## Interface

```python
# aksha/runtime/tools.py (proposal; design only). Exported through aksha/runtime/api.py.
EffectClass = Literal["read", "store", "external", "exec", "model", "reserved"]

@dataclass(frozen=True)
class RetryPolicy:                                   # counted from the event store (ADR-P14)
    timeout_ms: int = 10_000
    max_retries: int = 2                             # attempts = max_retries + 1
    retry_on: frozenset[str] = frozenset({"timeout", "transient"})   # never "timeout" after dispatch on at_most_once (ADR-P14 D4)
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
