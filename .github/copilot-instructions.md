# Review instructions for Aksha

Aksha is a small, offline, pluggable agent harness in Python. One runtime runs many agents as declared data, gates every tool call in code, pauses durably for approval and resumes after a crash without repeating a write or a send.

Review every pull request against [REVIEWING.md](../REVIEWING.md), which holds the full invariants. A change that breaks one of these is blocking:

- **One event store.** Append-only and hash-chained, written only by the `EventStore` adapter, no update or delete path, dedup rules as unique indexes. A change to canonical bytes or the chain needs a schema version bump and a pinned-hash test; both adapters pass one contract suite unchanged.
- **Store facts, derive interpretations.** No stored status, counter, flag, retry count or remaining budget; those are projections that must equal a full rebuild.
- **Ports and adapters.** `src/aksha/runtime/` imports only itself, the standard library and `jsonschema`. Agents and extensions import only `aksha.runtime.api`, and an agent also the extensions it names. Protocols live in `ports.py`. Only `src/aksha/app.py` wires adapters.
- **Idempotency.** Every external effect is an outbox entry committed before the call, keyed by a hash of its key fields (never a timestamp or random value). Every tool declares `at_least_once`, `reconcile` or `at_most_once`; a dispatched `at_most_once` step is never retried automatically.
- **Determinism.** No wall clock, sleep, randomness or UUID minting in the runtime; use the `Clock` port and store-issued IDs.
- **Policy gate.** One pure `check`, deny by default, fail closed on exceptions, fixed layer order; it checks the proposed call's name and arguments, and no other model-written text (tool results, documents, memory, artifact content) is an input. Headless never asks, but every deny holds; a headless run needs a sandbox (`sandbox_required`).
- **No agent-specific code in the runtime.** No agent, fixture or tool name in logic, no branch on agent identity.
- **Tests fail when the guarantee breaks.** Flag tests that mock what they test, assert only "no exception", or skip one adapter.
- **Public repository.** No private names, no external product named as a design source, synthetic fixtures with `SYN-` identifiers, no credentials or personal paths.
- **The ADR.** Code must match the ADR it implements, or amend it in the same PR.

Focus on write paths, external effects, crash recovery, and changes under `runtime/policy/`, `runtime/storage/`, `runtime/canonical.py` and `runtime/api.py`.

Do not comment on formatting or import order (ruff enforces them), types mypy already checks, design-document wording, fixture prose, or suggestions to add dependencies or frameworks.
