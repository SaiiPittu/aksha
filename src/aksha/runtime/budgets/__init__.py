"""Budgets, limits, timeouts and retries (ADR-P14): every cap is counted from the event store.

A restart never resets a count, because every count is a projection. Built: ``types.py``
(``Backoff``, ``Budget``, ``Limits``). Planned: the budget accounting reducer and ``Meter``.
"""
