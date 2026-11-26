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

### Amendments from the build (26 Nov 2026, Proposed)

Built on 26 Nov 2026 in `runtime/outbox/records.py`, with record facts on `outbox.enqueued`; exported from `aksha.runtime.api` 2.4 as `ExternalRecord` and `external_records`.

1. B1, the record facts ride on the outbox entry (D1, D2). `enqueue` records `record: {key, fields}` and `sources` on `outbox.enqueued` for a tool that declares `record_fields`, both or neither (the catalogue schema's `dependentRequired`); an entry of any other tool is unchanged. So the projection reads only the outbox facts, and the record key is fixed when the request is committed, not recomputed from a definition that may have changed. The key is `sha256` over the tool, the owner and the record-field values as given: record identity is never resolved to an exact artifact version, or two versions of one form would name two records.
2. B2, sources are the artifact versions among the key values (D2, D3). The interface's single `source_artifact` becomes `sources`, every `kind/key@vN` reference found at any depth in the resolved key-field values, in order; `source_artifact` is a property naming the first. Key fields name what an effect means, so a version among them is what the request was written from. `freshness` is the stalest of their states in the invalidation view (`invalidated` over `dirty` over `current_by_cutoff` over `current`), and None when no source names a saved version; `stale` is true for `dirty` and `invalidated`.
3. B3, every record field is a key field. The loader refuses a record field that is not a JSON pointer (`record_field_invalid`) and one outside `key_fields` (`record_field_not_keyed`): two records under one idempotency key would make the second write the first one's replay. `enqueue` still refuses a record field missing from the arguments (`record_field_missing`) for a spec built by hand.
4. B4, revisions and their ends (D2). `ExternalRecord` adds `tool`, `fields`, `pending` and `unknown`. A completion, the worker's or reconciliation's, is a revision with the system's receipt, in completion order. An entry abandoned `outcome_unknown` is listed under `unknown`, since the outside system may hold a revision Aksha never recorded; a refused, stopped or retries-exhausted entry is no revision. A record is listed once it has a revision or an unknown one: a record with only pending entries has not reached the outside system. `system` filters on the entry's system, or the tool's name when it has none.
5. B5, `TransientEffectError` is part of the extension API (ADR-P03, ADR-P20). A contributed system of record raises it, with a code matched to the tool's `retry_on`, to ask the worker for a retry; any other exception abandons the entry `effect_exception`.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. Each adapter tracks its own records | No core change | Every adapter reimplements it; the interface cannot show it uniformly | the first milestone only |
| B. A fact graph that flags prior work as outdated | Same goal | Not shown working | Shape adopted as a projection |
| C. A projection over outbox completions (lean) | No new store; cannot disagree with the event store | Needs `record_fields` declared | Chosen |

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
