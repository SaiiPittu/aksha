"""Durable execution (ADR-P02): record every step, return recorded results on a restart.

Each step is recorded in the event store before it starts and when it ends, under a
deterministic step ID, so a restarted process returns recorded results instead of running
steps again and continues at the first step with no recorded outcome. Built: the step-ID
grammar and step values (``types.py``), the step facts (``facts.py``), the durable step
executor with retries, timeouts by abandonment and late-result discard, and effectful tool
calls through the outbox (``executor.py``), IDs counted from the store (``ids.py``), and
recovery on startup with the outbox reconciliation in ADR-P02 D5's order, the
definition-changed check, lazy resume, resume records and the restart limit
(``recovery.py``). With cancellation (ADR-P18) the executor skips a new step once a stop is
requested and runs the kill boundary for a step that declares a unit, and recovery ends a
stopped run and holds the agent's other work. Planned: the source-change step of recovery
(ADR-P05), rebuild runs, ``recovery.completed``, the prior-exit report, the automatic-start
limit on resume records and the replay check (D11); ``recovery.py`` lists them.
"""
