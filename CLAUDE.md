# CLAUDE.md

Aksha is a small, offline, pluggable agent harness in Python: one runtime runs many agents as declared data, gates every tool call in code, and resumes after a crash without repeating a write or a send.

Read [REVIEWING.md](REVIEWING.md) before changing or reviewing code; its invariants are project rules, and a change that breaks one is a blocking finding. The self-review procedure is `.claude/skills/aksha-pr-review/SKILL.md`. Contributor conventions (`make test`, `make lint`, `make typecheck`, layout, dependency rule) are in [AGENTS.md](AGENTS.md).

In short:
- One append-only, hash-chained event store; only the `EventStore` adapter writes. Store facts, derive interpretations: no stored status.
- Ports and adapters: the runtime imports only itself, the standard library and `jsonschema`; agents and extensions import only `aksha.runtime.api`.
- Every external effect goes through the outbox with a meaning-based idempotency key; every tool declares `at_least_once`, `reconcile` or `at_most_once`.
- The policy gate denies by default and fails closed; headless never asks, but every deny holds, and a sandbox is required.
- No agent-specific code in the runtime. Tests must fail when the guarantee breaks.
- The repository is public: no private names, no external product named as a design source, synthetic `SYN-` fixtures, no credentials or personal paths.
- Formatting is ruff's job; do not flag it.
