# Aksha review style guide

Aksha is a small, offline, pluggable agent harness in Python. One runtime runs many agents as declared data, gates every tool call in code, pauses durably for approval and resumes after a crash without repeating a write or a send.

The full rules are in `REVIEWING.md` at the repository root. Review against them; a change that breaks one is a blocking finding.

## Invariants

1. **One event store, single writer, hash chain.** The append-only, hash-chained event store is the only source of truth. Only the `EventStore` adapter writes; there is no update or delete path; dedup rules are unique indexes. Changing canonical JSON bytes or the chain needs a schema version bump and a test pinned to a recorded hash. SQLite and PostgreSQL pass the same contract suite unchanged.
2. **Store facts, derive interpretations.** No stored status, counter, flag, retry count or remaining budget. Those are projections from pure reducers, and a cache must equal a full rebuild.
3. **Ports and adapters, with the dependency rule.** `src/aksha/runtime/` imports only itself, the standard library and `jsonschema`. Adapters implement `typing.Protocol` ports declared in `ports.py`. Agents and extensions import only `aksha.runtime.api`. Only `src/aksha/app.py` wires adapters to ports.
4. **Idempotency keys and step semantics.** Every external effect is an outbox entry committed before the call, keyed by a hash of its declared key fields, never a timestamp or random value. Every tool declares `at_least_once`, `reconcile` or `at_most_once`; a dispatched `at_most_once` step is never retried automatically.
5. **Determinism.** No `datetime.now`, `time.time`, `time.sleep`, `uuid4`, `random` or `secrets` in the runtime. Time comes from the `Clock` port; IDs from the event store.
6. **Deny-by-default policy gate.** One pure `check` decides every tool call, fails closed on any exception, runs its layers in the ADR order, and takes no model-written input. Headless never asks, but every deny holds. A headless start without a sandbox backend is refused with `sandbox_required`.
7. **No agent-specific code in the runtime.** No agent, fixture or tool name in runtime logic, no branch on agent identity. A runtime abstraction must serve all three agents.
8. **Tests fail when the guarantee breaks.** Each guarantee has a test that fails if the enforcing code is removed. Storage rules run against both adapters; crash rules restart at the crash point; gate refusals assert layer and code.
9. **Public-repository rules.** No private names of people, clients or projects; no external harness or product named as a design source; fixtures synthetic and labelled, with `SYN-` identifiers; no credentials or personal paths.
10. **The ADR is implemented exactly**, or the same PR amends it.

## Focus on

- New write paths to the event store and new external effects.
- What happens if the process dies between two lines.
- Changes under `src/aksha/runtime/policy/`, `runtime/storage/`, `runtime/canonical.py` and `runtime/api.py`.
- Whether each test would fail if the code it covers were wrong.

## Ignore

- Formatting and import order: `ruff format` and `ruff check` enforce them in CI.
- Type annotations mypy already checks, unless one hides a real bug.
- Wording and length of design documents and fixture prose.
- Suggestions to add a dependency, framework or workflow engine.
- Pre-existing issues a PR merely touches: note them, do not block.
