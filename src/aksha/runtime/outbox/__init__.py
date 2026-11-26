"""The transactional outbox and idempotency keys (ADR-P03): an external effect happens once.

Every external effect is an outbox entry committed before the call, in the transaction of
the command that decided it or of the step intent of an effectful tool call, keyed by what
it means, and reconciled against the system of record after a crash. Built: the values and
ports (``types.py``, ``ports.py``), keys from ``key_fields`` and the owner (``keys.py``),
the outbox facts (``facts.py``), the outbox projection and ``enqueue`` (``outbox.py``), and
the worker that claims and runs entries, retries under the tool's step policy, records
``outcome_unknown`` for an entry nothing can ask, ends a tool step's entry together with the
step's result, and recovers by key in two phases (``worker.py``), which recovery on startup
runs in ADR-P02 D5's order, and external record state (ADR-P29, ``records.py``): one
record per outside record the effects created or revised, with its freshness joined.
The step side is ``StepExecutor.effect_step``. Command receipts are in
:mod:`aksha.runtime.session.commands`. Planned: the receipt artifact (S2,
``EffectSpec.receipt_kind`` is declared but never read), the ledger check (a replaced ledger
is not refused yet: ``ledger_id`` is recorded, never compared), artifact-version resolution
of key values (``resolve`` is optional until the artifact store, P04, so a key does not yet
pin an artifact reference to its exact version as D2 requires), the approval and
invalidation checks behind ``Authority`` (P17, P05), and one drainer lane per session.
"""
