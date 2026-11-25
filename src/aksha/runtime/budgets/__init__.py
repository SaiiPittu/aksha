"""Budgets, limits, timeouts and retries (ADR-P14): every cap is counted from the event store.

A restart never resets a count, because every count is a projection. Built: the values
(``types.py``), the fixed backoff (``backoff.py``), what each recorded fact counts for
(``records.py``), budget accounting and ``Meter`` (``meter.py``), the turn, stop-veto and
restart limits, the attempt number and the automatic-start count (``limits.py``), and the
two budget facts (``facts.py``). Timeouts by abandonment and bounded retries run in the
durable step executor (``execution/executor.py``), which numbers attempts with
``limits.next_attempt``. Planned: budget narrowing down the Task tree beyond
``Budget.narrow``, and the budget interrupt itself, which the stop policy raises (P16).
"""
