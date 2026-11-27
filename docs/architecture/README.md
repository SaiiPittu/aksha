# Architecture, in layers

*As of 28 Nov 2026.* Read these in order. Each layer zooms one step into the one before it, and each links to the code and tests that hold it true. The decision records (ADRs) in [../design/adr/](../design/adr/) carry the reasoning and the rejected options; these pages do not repeat them.

| Layer | Page | Question it answers |
|---|---|---|
| 0 | [Context](0-context.md) | Who uses Aksha, and which outside systems does it talk to? |
| 1 | [Tiers](1-tiers.md) | What runs where, and which part may hold state or credentials? |
| 2 | [Runtime](2-runtime.md) | What are the runtime's parts, and what happens in one model turn? |
| 3 | [Lifecycles](3-lifecycles.md) | What states do runs, Task instances, approvals, outbox effects, artifact versions and documents pass through? |
| 4 | [Change and recovery](4-change-and-recovery.md) | What reruns when an input changes, and what happens after a crash or a restart? |
| Reference | [Data model](data-model.md) | What is actually stored, table by table? |
| Reference | [Trust boundary](trust-boundary.md) | Why can a document or a tool result not change permissions or approvals? |

Two older pages stay as they are. [overview.md](overview.md) is the one-page summary with the primitive catalogue, and [codebase-structure.md](codebase-structure.md) maps every package and its import rules.

## Conventions on these pages

- Every state, event and command name is the one in the code. Event types are declared in [`runtime/events/catalogue.py`](../../src/aksha/runtime/events/catalogue.py); state names are the `Literal` types named under each diagram.
- Every diagram is Mermaid, has a title that states a claim, and has a two-sentence caption saying what to notice.
- Where a part is designed but not built, the page says so.
