"""Stop policy and stop-policy evaluation (ADR-P16).

A stack of pure stop policies, derived from the event store, decides each stop
candidate; the run stop policy is the outermost. Built: ``types.py`` (the opt-ins an agent
declares). Planned: the built-in stop policies, evaluation and the hold predicate.
"""
