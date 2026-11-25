# ADR-P30: Erasure of personal data in an append-only event store (no lean: The decision is the maintainer's)

*Intermediate design as of 26 Nov 2026. Status: Open. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Open, 25 Nov 2026. Decision for the maintainer; I propose no shape. Found by the Patent stress test (the Patent stress review, missing primitives table), and named for memory only in ADR-P09 MEM-3. It is listed here so the gap has an owner. Nothing here was built as of 25 Nov 2026, and none of it is first-milestone work.

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

