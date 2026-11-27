# Reviewing Aksha

This page is for anyone reviewing a pull request here, person or bot. It says what Aksha is, how it is built, which guarantees every change must keep, what to look at closely and what to leave alone. The full self-review procedure, with the mechanical checks, is the project skill at [`.claude/skills/aksha-pr-review/SKILL.md`](.claude/skills/aksha-pr-review/SKILL.md); its rules are the same ones listed here.

## What Aksha is

Aksha is a small, offline, pluggable agent harness in Python. One runtime runs many agents, each defined as a Python module of declared data. The runtime gates every tool call in code, pauses durably for approval or missing information, and resumes after a crash without repeating a write or a send. Two example agents (a research agent and a patent-disclosure agent, both for a fictional venture) and one contrast coding agent, designed but not built, test that the runtime stays general.

The design comes first. Every primitive has one architecture decision record (ADR) in [`docs/design/adr/`](docs/design/adr/), and the maintainer's numbered decisions are in [`docs/design/04-decisions-log.md`](docs/design/04-decisions-log.md). A pull request implements a decision; it does not invent one.

## The architecture in short

- **One source of truth.** An append-only, hash-chained event store (SQLite locally, PostgreSQL in production, behind one `EventStore` port). Every status, hold, budget or trace is a projection: a pure reducer over events.
- **Ports and adapters.** `src/aksha/runtime/` is the domain and imports only itself, the standard library and `jsonschema`. Backends live in `src/aksha/adapters/` and implement `typing.Protocol` ports. `src/aksha/app.py` is the only module that wires adapters to ports.
- **Agents and extensions as data.** An agent in `src/aksha/agents/<name>/` imports only `aksha.runtime.api` and the extensions it names. An extension contributes declared data, never hooks.
- **Three tiers.** Tier 1 holds the record and computes nothing. Tier 2, the runtime, holds the logic and every credential, and is the only writer of tier 1. Tier 3, the sandbox, holds nothing, has no network and only answers requests.

The longer version is [`docs/design/02-architecture.md`](docs/design/02-architecture.md); the module map is `docs/architecture/codebase-structure.md`.

## The invariants

A change that breaks one of these is a blocking finding.

1. **The ADR decision is implemented exactly.** Names, check order, event types, payload fields, dedup keys and refusal codes match the ADR. If the code has to differ, the same PR amends the ADR and says why. Only the decisions log makes a decision Accepted.
2. **The dependency rule.** The runtime never imports adapters, agents, extensions, interfaces or `aksha.app`. Agents and extensions import only `aksha.runtime.api`, and an agent also the extensions it names (`aksha.extensions.<name>`, which the loader allows). Every `Protocol` lives in a `ports.py`. A change to `api.py` bumps the extension API version.
3. **Store facts, derive interpretations.** No stored status, counter, flag, retry count or remaining budget. Those are projections, and a projection cache must always equal a full rebuild.
4. **One writer and an intact hash chain.** Only the `EventStore` adapter writes. No update or delete path exists. Dedup rules are database unique indexes. Any change to the canonical JSON bytes or the chain needs a schema version bump and a test pinned to a recorded hash. Both adapters pass one contract suite unchanged.
5. **Idempotency and step semantics.** Every external effect is an outbox entry committed before the call, keyed by a hash of what it means (never a timestamp or a random value). Every command carries a client-generated command ID. Every tool declares `at_least_once`, `reconcile` or `at_most_once`, and a dispatched `at_most_once` step is never retried automatically.
6. **A deterministic runtime.** No wall clock, sleep, randomness or UUID minting in `src/aksha/runtime/`. Time comes from the `Clock` port and IDs from the event store.
7. **The policy gate denies by default.** One pure `check` decides every tool call, fails closed on any exception, runs its layers in the fixed order, and checks the proposed call's name and arguments against declared policy; no other model-written text (tool results, documents, memory text, artifact content) is an input, and model output never grants a permission. In headless mode every layer still runs: asks become allows, but every deny holds.
8. **No agent-specific code in the runtime.** No agent, fixture or tool name in runtime logic, and no branch on which agent is running. A new runtime abstraction must serve all three agents (the rule of three).
9. **Tests fail when the guarantee breaks.** Each guarantee has a test that would fail if the enforcing code were removed. Storage rules are tested against both adapters, crash rules by restarting at the crash point, gate rules by asserting the refusal's layer and code. A new guard is shown failing on a planted violation.
10. **The public-repository rules.** This repository is public. No private names of people, clients or projects. No external harness or product named as the source of a design. Fixtures are synthetic, say so, and use `SYN-` identifiers, never real-looking patent numbers. No credentials or personal paths.

## What to focus on

- Every new write path to the event store, and whether it goes through the single writer inside a declared transaction boundary.
- Every new external effect, and whether the outbox entry, the key and the step semantics are right.
- Every change under `src/aksha/runtime/policy/`, `src/aksha/runtime/storage/`, `src/aksha/runtime/canonical.py` and `src/aksha/runtime/api.py`.
- Recovery paths: what happens if the process dies between two lines.
- Whether each test would fail if the code it covers were wrong.
- Whether the code matches its ADR, decision by decision.

## What to ignore

- Formatting and import order. `ruff format` and `ruff check` decide them in CI.
- Type annotations `mypy` already checks, unless one hides a real bug.
- Wording and length of the design documents, unless a statement contradicts the code or an ADR.
- The fixture worlds' prose content. Check only that it is labelled synthetic and uses `SYN-` identifiers.
- Suggestions to add a dependency, a framework or a workflow engine. The runtime's dependency set is a design decision.
- Pre-existing issues a PR merely touches. Raise them as a note, not a blocker.
- Status labels inside the design documents (Proposed, Accepted). They are the maintainer's call.

## How a review ends

A review ends in one verdict: approve only when there is no blocking finding and no open question, otherwise request changes. Reviews and squash-merges are delegated to a coding agent under stated conditions (decision 35): every check green, the branch rebased on main, and a self-review against these rules.
