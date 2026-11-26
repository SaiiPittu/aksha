# ADR-P30: Erasure of personal data in an append-only event store (no lean: The decision is the maintainer's)

*Intermediate design as of 26 Nov 2026. Status: Open. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Open, 25 Nov 2026. Decision for the maintainer; I propose no shape. Found by the Patent stress test (the Patent stress review, missing primitives table), and named for memory only in ADR-P09 MEM-3. It is listed here so the gap has an owner. Nothing here was built as of 25 Nov 2026, and none of it is first-milestone work. On 26 Nov 2026 the build took Option A with salted commitments; it is recorded below as amendments E1 to E10, status Proposed, and the decision stays the maintainer's.

## Terms

**Erasure** here means removing a person's data so that no projection, export or replay can show it again, which an append-only event store refuses by design.

## Context

Contributor names and an outside consultant's employer sit in `excerpts.minted` payloads and in every artifact that cites them, and ADR-P01's triggers refuse `DELETE`. Memory has the same problem: a memory store that keeps a history table still holds deleted text. The product requirements does not ask for erasure.

## Options (from ADR-P09 MEM-3, generalised to the whole store)

| Option | What it is | Cost |
|---|---|---|
| A. An erasable content table | Events hold references; personal text lives in a side table that can be deleted, leaving a tombstone | Changes ADR-P01's rule that the event store is the only authority; replay must tolerate tombstones |
| B. Per-tenant keys (crypto-shredding, general usage) | Personal fields are encrypted under a tenant or subject key; erasure deletes the key | Key management; every reducer that reads those fields needs the key |
| C. No erasure, stated | `forget` hides from recall only, and the README says so (MEM-3 lean C) | Not acceptable for personal data in production |

## Why I propose no shape

Options A and B both change ADR-P01's authority rule, and the choice depends on where Aksha would run and under which data-protection law. That is the maintainer's call.

## Stress test (25 Nov 2026)

Not stress-tested: there is no design to test.


## Amendments from the build (26 Nov 2026, Proposed)

The ADR proposes no shape, so the build took one, Option A (an erasable content table) with the hash chain kept whole, and records it here for the maintainer to accept or drop. Option B (per-tenant keys) is not built; nothing below rules it out later, since a sealed value could be stored encrypted in the same side table.

1. E1, sealed values and a salted commitment. A writer marks a personal value in an event payload as `Personal(subject, value, redacted=...)`. On append the store moves the value into a side table, `erasables (owner, ref, subject, salt, value, event_seq)`, and the stored, hashed payload keeps a placeholder `{"$erasable": ref, "commit": ..., "redacted": ...}`. `commit` is SHA-256 over the canonical bytes of `{"salt", "subject", "value"}` with a 32-byte random salt kept, and deleted, with the value, so the chain covers the value without holding it, and a short value such as a name cannot be guessed back from its commitment (a hiding commitment; the salt is what makes it hiding). The subject is inside the commitment, so a row whose subject is changed in place no longer verifies, and `erase` of the original subject cannot be made to miss it. ADR-P01's authority rule changes in one place only: `erasables` is the one table rows may leave, and only through `EventStore.erase`; its UPDATE (and on PostgreSQL its TRUNCATE) stays refused by trigger. The schema version becomes 3. Salts and refs come from an injected `Entropy` (the runtime core reads no ambient randomness, ADR-P02 D3); both adapters pass `secrets.token_hex`.
2. E2, readers see the value or its stand-in. Every read (`read`, `find`, the event `append` returns, and the event inside `AlreadyRecorded`) resolves each placeholder to its value while the row exists, and to `redacted` after erasure. The payload is validated against its schema twice at append, once with the values and once with the stand-ins, so a replay after erasure still fits every schema and every reducer. A dedup-key template that reads a field holding a personal value is refused (`erasure_in_dedup_key`): the key is a stored column erasure cannot reach.
3. E3, the subject is an opaque id. A subject (`person:p07`) and the erase `reason` stay in the record after erasure, so neither may name the person; the runtime cannot check that and says so in the docs. Subjects and reasons are 1 to 200 characters without NUL, and both are checked before the store looks for values, so a bad reason is refused even when nothing is left to erase (`erasure_reason_invalid`).
4. E4, one boundary for one erasure. `EventStore.erase(owner, subject, *, reason)` deletes the owner's rows for the subject and appends `erasure.performed {subject, refs, reason}` in one transaction, in the new boundary U12, which only `erase` may write (`erasure_boundary_reserved`). Writer only.
5. E5, idempotent. With nothing left to erase, `erase` records nothing and returns `ErasureReceipt(refs=(), seq=None)`.
6. E6, `verify()` proves the chain after an erasure. Beside the hash walk, it holds the side table to the chain: every row must match a placeholder of the same owner and event and hash to its commitment; no row may survive an erasure that names it; every placeholder must have its row unless a later erasure of the same owner names its ref; and an erasure may name only refs sealed before it, each once. A value deleted without a recorded erasure is `erasable_missing`; one edited, moved to another event or subject, or kept after its erasure is `erasable_mismatch`. An erasure that really happened verifies.
7. E7, the hook for memory. `aksha.runtime.erasure.ports.Eraser` is the one call ADR-P09 makes when a `forget` must be a real erasure (MEM-3 options A and B); every `EventStore` implements it. Memory is not changed by this build.
8. E8, artifact content is not erasable. Personal values in artifact content are refused at append (`erasure_in_artifact`): an artifact's content hash and its excerpt evidence (ADR-P04, ADR-P13) would need the same sealing, which is not built. A writer keeps the personal value in an event payload and cites the event.
9. E9, the command line. `aksha erase <subject> --reason <text>` (with `--user` for the owner) opens the store as the writer for its one call through `app.erase_subject`, so it exits 8 while a server holds the lock; a refused subject or reason exits 7.
10. E10, bytes on disk. Deleting a row is not the same as removing its bytes. The SQLite writer sets `secure_delete`, so freed pages are zeroed, and runs a `TRUNCATE` WAL checkpoint after each erase, so the write-ahead log no longer holds the pre-delete page images (a reader mid-read can keep the checkpoint busy; the next one finishes it). On PostgreSQL the deleted tuple stays in its page until vacuum reclaims it, and in the write-ahead log and any backups until they rotate; this build does not vacuum, and states it as a limitation. Backups and anchors are outside the store: an anchor holds only a hash, which holds no personal value.

Open for the maintainer: whether Option A with commitments is the shape (against B, or both); exporting `Personal` through the extension API (`aksha.runtime.api`, an extension API version bump), without which no agent can mark a value; whether erase should also go through the session API so it works beside a running server; a retention or scheduled-erasure policy; PostgreSQL physical removal; and the data-protection regime that decides what counts as erased (for example GDPR Article 17).
