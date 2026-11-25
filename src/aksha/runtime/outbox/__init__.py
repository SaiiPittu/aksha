"""The transactional outbox and idempotency keys (ADR-P03): an external effect happens once.

Every external effect is an outbox entry committed before the call, keyed by what it
means, and reconciled against the system of record after a crash. Built: ``types.py``
(``SystemOfRecordSpec``, ``EffectSpec``). Planned: the outbox, idempotency keys and
reconciliation on startup.
"""
