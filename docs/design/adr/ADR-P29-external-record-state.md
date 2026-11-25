# ADR-P29: External record state (lean: a projection over outbox completions that tracks each outside record an effect created or revised, and the freshness of the artifact version it was written from)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the Patent stress test (the Patent stress review, missing primitive "external record state", and the P03 row). The outbox (ADR-P03) makes each write happen at most once; nothing tracks the life of the record a write created. No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026.

## Terms

The terms:

- An **external record** is a record in an outside system that one or more of Aksha's effects created or revised, such as a disclosure form in the forms service.
- **Request identity** (`key_fields`, ADR-P03 D2) says which request this is; **record identity** (`record_fields`, ADR-P03 S2) says which outside record it revises.

## Context: what breaks without it

After the product requirements' change set, Aksha knows `disclosure/C1@v1` is invalidated, but the forms service still shows v1 as the only record, unmarked, until the rebuild writes v2, and the rebuild waits behind the selection review. Then `write_form` for v2 has a new idempotency key (it covers the content hash), so the ledger holds two disclosure records for C1.

## Decision

1. D1, an effect declares its record identity. `EffectSpec.record_fields` names the argument fields that identify the outside record (for `write_form`: form kind and candidate). Requests with equal record identity are revisions of one record.
2. D2, the record is a projection. `external_records(view)` reduces `outbox.completed` events into one `ExternalRecord` per (owner, tool, record identity), listing its revisions oldest first and the artifact version the latest was written from.
3. D3, freshness is joined, not stored. An external record's freshness is its source version's state in ADR-P05's view, so the interface can show "the forms service holds v1, which Aksha has invalidated" without writing a mark.
4. D4, marking the outside copy stale is an effect. If an agent wants the outside system to show the record as superseded, that is its own declared effect through the outbox, never an automatic write.

The first milestone's agent-side answer, with no core change: The Patent Agent's forms adapter keeps one record per form kind and candidate and stores each keyed request as a revision, which meets the product requirements.

## Alternatives considered

| Option | Prior art | For | Against | Verdict |
|---|---|---|---|---|
| A. Each adapter tracks its own records | The first milestone agent-side answer | No core change | Every adapter reimplements it; the interface cannot show it uniformly | the first milestone only |
| B. A fact graph that flags prior work as outdated | Harvey's engineering posts describe one, announced and not shown in use (notes on Harvey's engineering posts, 22 Nov 2026) | Same goal | Not shown working | Shape adopted as a projection |
| C. A projection over outbox completions (lean) | ADR-P03's outbox | No new store; cannot disagree with the event store | Needs `record_fields` declared | Chosen |

## Interface

```python
# aksha/runtime/outbox.py (proposal; design only)
@dataclass(frozen=True)
class ExternalRecord:                              # a projection, never stored
    system: str
    record_key: str                                # sha256 over owner, tool, record_fields
    revisions: tuple["SystemReceipt", ...]         # oldest first
    source_artifact: "ArtifactRef"                 # the version the latest revision was written from
    freshness: "Freshness"                         # that version's state in ADR-P05's view

def external_records(view: "LogView", system: str | None = None) -> tuple[ExternalRecord, ...]: ...
```

## Stress test (25 Nov 2026)

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits (unused) | No external effect |
| Patent | Fits | Two writes for C1 are two revisions of one record; the interface shows v1 invalidated |
| Coding | Fits | A force push is a revision of one branch record |
