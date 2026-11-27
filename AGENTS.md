# AGENTS.md

Aksha is a small, offline, pluggable agent harness in Python. One runtime runs many agents, each a module of declared data; the runtime gates every tool call in code, pauses durably for approval, and resumes after a crash without repeating a write or a send.

The full rules are in [REVIEWING.md](REVIEWING.md). This file holds the short version for coding agents and automated reviewers.

## Contributor conventions

- `make venv` once, then `make test`, `make lint` (ruff check and ruff format --check) and `make typecheck` (mypy). `make check` runs all three.
- Layout: `src/aksha/runtime/` is the domain; `src/aksha/adapters/` implements its ports; `src/aksha/agents/<name>/` and `src/aksha/extensions/<name>/` are agents and extensions; `src/aksha/interfaces/` is the outer surface; `src/aksha/app.py` is the only module that wires adapters to ports.
- Dependency rule: the runtime imports only itself, the standard library and `jsonschema`. Agents and extensions import only `aksha.runtime.api`, and an agent also the extensions it names (`aksha.extensions.<name>`). Every `typing.Protocol` lives in a `ports.py`.
- A pull request implements a decision recorded in `docs/design/adr/`; it does not invent one. If the code must differ, amend the ADR in the same PR.

## Code Review Rules

Review against REVIEWING.md. Breaking any of these is a blocking finding:

### Event store
- One append-only, hash-chained event store is the only source of truth. Only the `EventStore` adapter writes; no update or delete path. Dedup rules are unique indexes.
- A change to canonical JSON bytes or the chain needs a schema version bump and a test pinned to a recorded hash. SQLite and PostgreSQL pass one contract suite unchanged.
- Store facts, derive interpretations: no stored status, counter, flag, retry count or remaining budget. Those are projections, and a projection cache must equal a full rebuild.

### Effects and determinism
- Every external effect is an outbox entry committed before the call, keyed by a hash of its declared key fields, never a timestamp or random value. Commands carry a client-generated command ID.
- Every tool declares `at_least_once`, `reconcile` or `at_most_once`; a dispatched `at_most_once` step is never retried automatically.
- No wall clock, sleep, randomness or UUID minting in `src/aksha/runtime/`. Time comes from the `Clock` port, IDs from the event store.

### Policy gate
- One pure `check` decides every tool call, denies by default, fails closed on any exception, runs its layers in the ADR order, and checks the proposed call's name and arguments; no other model-written text (tool results, documents, memory, artifact content) is an input, and model output never grants a permission.
- Headless never asks, but every deny holds: only an approval route changes. A headless start with no sandbox backend is refused (`sandbox_required`).

### Generality and tests
- No agent-specific code in the runtime: no agent, fixture or tool name in logic and no branch on agent identity. A new runtime abstraction must serve all three agents.
- Each guarantee has a test that fails when the enforcing code is removed. No mocking the thing under test, no adapter-specific skips.

### Public repository
- No private names of people, clients, employers or projects. No external harness or product named as the source of a design.
- Fixtures are synthetic, say so, and use `SYN-` identifiers. No credentials, tokens or personal paths.

### Focus and ignore
- Focus on: new write paths, new external effects, recovery if the process dies between two lines, changes under `runtime/policy/`, `runtime/storage/`, `runtime/canonical.py` and `runtime/api.py`, and code versus its ADR.
- Ignore: formatting and import order (ruff enforces them in CI), types mypy already checks, wording of design documents, fixture prose, suggestions to add dependencies or frameworks, and pre-existing issues a PR only touches (note them, do not block).
