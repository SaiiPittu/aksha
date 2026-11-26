# Design rationale

*As of 26 Nov 2026. Why the hash chain is in [architecture/overview.md](../architecture/overview.md#why-a-hash-chained-event-store).*

## The rule of three

No abstraction enters the runtime until three different agents fit it without agent-specific code in the runtime. Every ADR except the open P30 was reviewed against all three:

| Agent | Status | What it does | What it presses on |
|---|---|---|---|
| Research agent | built | Researches competitors of a fictional hardware venture from fetched pages and reviews, and writes a competitor matrix with a verified excerpt behind every finding | Grounding, deduplication through typed relations, partial results when a fetch fails |
| Patent agent | built | Extracts invention candidates from engineering notes, compares them with patent families, prepares a counsel-ready packet and asks for approval before a mock send | Invalidation, review of changed documents, approvals bound to the exact action, no duplicate send after a crash |
| Contrast coding agent | designed | Edits a Git repository in a sandbox for hours, runs tests and pushes a branch after approval | Long contexts and compaction, a shell in a sandbox, a completion check that runs the tests, cancellation mid-edit |

An ADR that fits two document agents and fails the coding agent has designed a document pipeline, not a harness. The document agents' world is synthetic and lives in [src/aksha/data/](../../src/aksha/data/README.md).

## Design rationale

Each choice follows a published pattern or standard rather than a new invention.

1. **Event sourcing.** The record of facts is the authority and every view is derived from it, so replay, recovery, invalidation and tracing all read the same record.
2. **Ports and adapters.** The domain owns its interfaces and backends plug in behind them.
3. **Transactional outbox and idempotency keys.** An external effect is committed with the step that wants it, before it is attempted, under a key derived from its meaning, so a retry after a crash finds the first receipt instead of sending twice.
4. **Durable execution.** Steps have deterministic IDs and recorded results, so a resumed run replays recorded steps instead of re-running them (Burckhardt and others, "Durable Functions: Semantics for Stateful Serverless", OOPSLA 2021).
5. **Canonical encoding and SHA-256.** Every hashed record is encoded with a subset of the JSON Canonicalization Scheme (RFC 8785: sorted keys, no whitespace, no floats) and hashed with SHA-256 (FIPS 180-4), so both store adapters produce the same hashes for the same events.
6. **Least privilege, decided in code.** The gate denies by default and nothing the model writes is an input to the decision (Saltzer and Schroeder, "The Protection of Information in Computer Systems", 1975).
7. **Fast and slow models.** System 1 and System 2 are the two modes of dual-process psychology. A fast-class answer is accepted only at or above a declared confidence threshold, compared in integer parts per million so two runs judge it the same way; below it, or inside the abstention band, the model abstains and says why. Fallback never crosses a class.
8. **Context as a rendering.** The model's context is rebuilt from the store each turn; compaction masks old tool results before it summarises, since simple observation masking has been reported to match summarisation (Lindenbauer and others, "The Complexity Trap", arXiv 2508.21433).
