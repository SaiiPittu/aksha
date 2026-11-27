# Aksha invariants: what to check and how

Each rule below has a statement, where it comes from, a mechanical check where one exists, and what makes a finding blocking. Run the greps from the head worktree (`$WORKDIR/tree`). A grep hit is a lead to read, not a finding by itself.

Paths follow [`docs/architecture/codebase-structure.md`](../../../../docs/architecture/codebase-structure.md) and section 10 of [`docs/design/02-architecture.md`](../../../../docs/design/02-architecture.md).

---

## 1. The ADR decision is implemented exactly

**Source:** the ADR in `docs/design/adr/` for the primitive the PR builds; `docs/design/04-decisions-log.md` for what is Accepted.

**Check:**
- The PR names its ADR and issue. Open the ADR's Decision, Interface and Amendments sections side by side with the diff.
- For every numbered decision the diff touches, compare names, check order, event types, payload fields, dedup keys, refusal codes and defaults. A refusal code spelled differently from the ADR is a divergence.
- `git diff origin/main..HEAD -- docs/design/adr/` shows whether the PR amended the ADR. An amendment states what changed and why.

**Blocking:** code that diverges from its ADR with no amendment in the same PR; a status flipped to Accepted with no decisions-log entry; code that implements an Open decision.

---

## 2. The dependency rule

**Source:** decision 18; architecture section 10; `tests/architecture/rules.py`.

| Layer | May import | Never imports |
|---|---|---|
| `src/aksha/runtime/` | itself, the standard library, `jsonschema` | adapters, agents, extensions, interfaces, `aksha.app`, any other third-party package |
| `src/aksha/adapters/<family>/` | the runtime's ports and values, its own family, any library | another adapter family, agents, extensions, interfaces |
| `src/aksha/extensions/<name>/` | `aksha.runtime.api`, itself | any other runtime module, adapters, agents, interfaces |
| `src/aksha/agents/<name>/` | `aksha.runtime.api`, the extensions it names, itself | any other runtime module, adapters, interfaces |
| `src/aksha/interfaces/` | the runtime, `aksha.app`, itself | adapters, agents and extensions directly |
| `src/aksha/app.py` | anything | nothing is refused |

**Check:**
```bash
make check   # runs tests/architecture/test_dependency_rule.py
grep -rnE '^\s*(from|import) aksha\.(adapters|agents|extensions|interfaces|app)\b' src/aksha/runtime/
grep -rnP '^\s*(from|import) aksha\.runtime\.(?!api\b)' src/aksha/agents/ src/aksha/extensions/
grep -rnE 'class \w+\(.*Protocol.*\)' src/aksha/ | grep -v '/ports.py:'
```
Imports under `if TYPE_CHECKING:` count. A new third-party dependency in the runtime (anything beyond `jsonschema`) is a design change.

**Blocking:** any violating import; a `Protocol` outside a `ports.py`; a PR that edits `tests/architecture/rules.py` to allow a new edge without an ADR or decisions-log entry saying so; a change to `api.py` without the extension API version bump.

---

## 3. Store facts, derive interpretations

**Source:** design pattern P-2; architecture section 4.3; ADR-P01.

A stored row is something that happened. A status, a hold, a budget remaining, a retry count, a restart count, a staleness flag or "is approved" is an interpretation, and it is a projection: a pure reducer over events in sequence order. A projection cache is keyed by the last sequence it read and must always equal a fresh rebuild.

**Check:**
```bash
git diff origin/main..HEAD -- src/ | grep -nE '^\+.*\b(status|state|is_[a-z_]+|count|counter|remaining|retries|attempts|stale)\b\s*[:=]' 
git diff origin/main..HEAD -- src/ | grep -nE '^\+.*\b(UPDATE|DELETE FROM|ALTER TABLE)\b'
```
Read each hit: is the value an event payload field recording what happened (fine: `outcome: refused` on `step.completed`), or a field someone will later update to say what is true now (not fine)? Reducers must be pure: no store reads, no clock, no I/O, deterministic over the same events.

**Blocking:** a stored status or counter that a projection could compute; a reducer with side effects; a projection cache without a test that it equals a rebuild; any value read from a cache that is treated as authority over the store.

---

## 4. One writer and an intact hash chain

**Source:** ADR-P01 (and its Amendment H), decision 20, architecture sections 3.2 (R2) and 4.2.

The five store rules: append-only (triggers refuse update and delete), one writer (a single-writer lock), transaction boundaries from a closed list, dedup keys as unique indexes, tenant first (every read names the owner). Each event carries `prev_hash` and `hash`, a SHA-256 over canonical JSON (RFC 8785 bytes, `src/aksha/runtime/canonical.py`). The genesis hash derives from a random store ID.

**Check:**
- Only the `EventStore` adapters open a write connection: `grep -rnE 'connect\(|INSERT INTO' src/aksha | grep -v 'adapters/storage/'`.
- Any change to `canonical.py`, `runtime/storage/chain.py` or the event envelope changes the bytes that are hashed. That breaks every existing store. It needs an ADR amendment, a schema version bump in `runtime/storage/schema.py` and a test with a fixed, recorded hash that would change if the bytes changed.
- A new boundary is added to `runtime/events/boundaries.py`, not invented at a call site. A new dedup rule is a unique index, not a Python `if exists` check.
- Both adapters (SQLite and PostgreSQL) pass the same contract suite unchanged.

**Blocking:** any write outside the single writer; any update or delete path; a dedup check done in Python instead of by the database; a change to hashed bytes without a version bump and a pinned-hash test; an adapter-specific branch in the contract suite.

---

## 5. Idempotency and step semantics

**Source:** ADR-P02 (D2, D4, D5, D9), ADR-P03 (D1 to D8).

- Every external effect runs only after `outbox.enqueued` commits, in the same transaction as the decision that wants it, and the drainer re-checks authority (approval signature recomputed, target current) before every attempt.
- The idempotency key is a SHA-256 over the tool name, the owner and the canonical JSON of the tool's declared `key_fields`, with every artifact reference resolved to its exact version. Never a random value, a timestamp or a counter.
- Every session API command carries a client-generated `command_id`; its transaction starts with `command.accepted` under the dedup key `cmd:{owner}/{command_id}`. A retry returns the stored receipt.
- Every tool declares `semantics`: `at_least_once` for reads, saves and model steps; `reconcile` for a tool whose system of record has `lookup`; `at_most_once` for one that cannot be asked. The loader refuses an effectful tool with neither a receiver nor `at_most_once`.
- A dispatched `at_most_once` step that times out or crashes is closed `outcome_unknown` and never retried automatically.
- Step IDs follow the ADR-P02 D2 grammar; a restart reaching a recorded step ID with different argument hashes raises `StepMismatch`.
- Retry and restart counts are counted from events (rule 3), so a restart never resets them.

**Check:** for each new tool, read its `ToolSpec`: effect class, `semantics`, `external.key_fields`, `approval` facts. For each new command, find its `command.accepted` and dedup key. For each new external call, find the outbox entry that precedes it.

**Blocking:** an effect called outside the outbox; a key built from anything but meaning; an effectful tool marked `at_least_once`; an automatic retry of an `at_most_once` step; a command with no command ID; a tool body that touches the store directly (tool bodies return values only, ADR-P02 D9).

---

## 6. A deterministic runtime

**Source:** ADR-P02 D3.

No module in `src/aksha/runtime/` calls the wall clock, sleeps, draws randomness or mints a UUID. Time comes from the `Clock` port (`now()`, `sleep()`, and `mono_ms()` for deadlines only, which every comparison excludes by name); IDs come from the `IdSource`, counted from the event store per owner. Two fake-model runs write byte-identical logs.

**Check:**
```bash
grep -rnE '\b(datetime\.(now|utcnow|today)|time\.(time|sleep|monotonic)|uuid\.uuid[14]|uuid4\(|random\.|secrets\.)' src/aksha/runtime/
```
The genesis store ID is the one sanctioned random value, and it lives in the adapter that creates the store, not in the runtime.

**Blocking:** any hit in the runtime without an ADR reason; iteration over a `set` or an unordered source where the order reaches an event or a hash.

---

## 7. The policy gate denies by default

**Source:** ADR-P11 (D1 to D12), decision 26, architecture sections 4.6 and 7.

- One pure function, `check(GateInput) -> Allow | Refuse`, for every tool call from every path. It reads no clock, store or network.
- Deny by default; any exception inside `check` is `Refuse("gate_error")`. No mode, flag, environment variable or sub-agent setting skips it.
- The layer order is fixed: 0 budget, 1 registered, 2 agent list and run ceiling, 3 Task list, 4 stop-policy narrowing, 5 schema, 6 argument rules, 7 route by effect class. Refusal codes are the ADR's.
- `GateInput` holds the proposed call's name and arguments (which the model writes, and which the gate checks against declared policy) and nothing else the model can write: no tool result, document, memory text, artifact content or model free text.
- Narrowing only removes. Argument rules use the closed matcher set (`path_under`, `path_not_under`, `enum`, `id_in_source`, `max_length`); no lambdas.
- A model never allows.
- The gate never appends. An allow becomes the tool step's `step.started`; a refusal becomes one `step.completed{outcome: refused, code, layer}`.
- **Headless never asks, but every deny holds.** `headless` is fixed on `run.started`. Every layer runs unchanged in headless mode. Only an allow routed to `approval` changes, in `resolve_ask`, to the route the tool would take without approval. A headless start with no sandbox backend is refused with `sandbox_required`.

**Check:**
```bash
grep -rnE '\bheadless\b' src/aksha/ | grep -v 'runtime/policy/'
grep -rnE '(import|from) .*(storage|adapters|socket|urllib|http)' src/aksha/runtime/policy/
grep -rnE 'except\b' src/aksha/runtime/policy/
```
Every `headless` read outside the gate is a lead: a second place that changes behaviour in headless mode is a bypass. Every `except` in the gate must end in a refusal, never a pass.

**Blocking:** any path that reaches a tool body without `check`; a headless branch before or inside layers 0 to 7; a new `GateInput` field carrying model-written content; a swallowed exception that allows; a layer reordered without an ADR amendment.

---

## 8. No agent-specific code in the runtime

**Source:** decisions 1, 10 and 16 (the rule of three); design pattern P-1.

The runtime holds no agent-specific code. Agent behaviour arrives as declared data and named pure functions in an agent module; an extension contributes declared data (tools, artifact kinds, relations, projections, event types), never hooks. An abstraction enters the runtime only when the research agent, the patent agent and the contrast coding agent all need it without agent-specific code. An agent opts into built-in stop policies by configuration and never subclasses one.

**Check:**
```bash
grep -rniE '\b(patent|research|disclosure|competitor|stillroom|coding_agent|counsel|prior_art)\b' src/aksha/runtime/
grep -rnE 'if .*(agent|agent_id|agent\.name)\s*==' src/aksha/runtime/
```
Read the PR description for the rule-of-three argument when the PR adds a runtime abstraction: how would each of the three agents use it?

**Blocking:** an agent, fixture or tool name in runtime logic (a docstring example is a `COMMENT`); a branch on agent identity; a runtime hook an extension can run code through; a new runtime concept justified by one agent only.

---

## 9. Tests fail when the guarantee breaks

**Source:** ADR-P20 decision 8 (guards are seen to fail before they are trusted); architecture section 3.2 ("each rule with the test that breaks when it is broken").

**Check:** for each guarantee the PR claims, find the test and ask: if I deleted the line that enforces this, would the test fail? Concretely:
- A store rule is tested against both adapters through the contract suite, not against a mock.
- A crash guarantee is tested by stopping at the named crash point and restarting, then asserting the effect landed once.
- A gate rule is tested by a call that must refuse, asserting the layer and the code, not just "not allowed".
- A projection is tested by comparing the incremental answer with a full rebuild.
- A new guard in `tests/architecture/` plants a violation in a temporary copy and asserts the guard reports it.
- A test that asserts only "no exception" or only a truthy result is weak; a test that mocks the thing it claims to test proves nothing.

Mutation by hand is fine and often fastest: revert the enforcing line in the worktree, run the one test, confirm it fails, restore.

**Blocking:** a claimed guarantee with no test; a test that still passes with the enforcing code removed; a contract test that one adapter skips.

---

## 10. The public-repository rules

**Source:** the repository is public by design; Phase 0.5d of the skill.

- No private names: no person, client, employer, project or event from the maintainer's private context. The maintainer's denylist (`AKSHA_REVIEW_DENYLIST`) catches the known ones; reading catches the rest. Never quote the denylist.
- No external harness, framework or product named as the source of a design. Sources are this design's own records, public papers, standards and public posts by their authors.
- Every fixture is synthetic and says so: each source carries a "synthetic" label, in the file or in its world's `manifest.json`. Patent and publication identifiers use the `SYN-` prefix; never a real-looking patent number.
- No credentials, tokens, personal paths or personal email addresses anywhere in the diff, the commit messages or the PR description.

**Blocking:** every violation of this rule blocks, and it leads the review body.
