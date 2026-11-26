"""Erasure in the append-only, hash-chained event store (ADR-P30, amendments E1 to E10).

A personal value is appended inside a :class:`~aksha.runtime.erasure.types.Personal`
marker. The store keeps the value in the ``erasables`` side table and hashes only a
salted commitment to it (``seal.py``), so erasing the value deletes its row, records
``erasure.performed`` and leaves every hashed byte, and so the chain, unchanged.
``verify.py`` holds the side table to the chain; ``ports.py`` is the hook memory
(ADR-P09) calls.

Built: sealing and resolving payloads, the side table in both adapters, ``erase`` on
the event store, the verify checks and ``aksha erase``. Not built: personal values in
artifact content (refused, E8), per-tenant keys (option B) and a retention schedule.
"""
