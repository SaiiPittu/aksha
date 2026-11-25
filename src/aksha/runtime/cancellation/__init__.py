"""Cancellation (ADR-P18): a person's stop is a durable request, honoured at the next candidate.

A stop is a fact, ``run.stop_requested``, that the ``stop`` command commits with its
receipt, so it holds across a crash. Built:

1. ``types.py``: the stop scope, the request view, the holds and the kill policy.
2. ``facts.py``: the stop request event.
3. ``requests.py``: the requests of a run, the pending one, the runs recovery must end,
   and the check before each new step (D1 to D3, D5).
4. ``hold.py``: the stop's part of the hold predicate, the standing hold after a person's
   stop (D4), and the audit ``inv.no_auto_start_while_held`` (TC-4).
5. ``ending.py``: the stop ending at a stop candidate: open interrupts voided,
   ``run.ended{stopped}`` and ``stop.decided`` in one transaction (D2).
6. ``command.py``: the ``stop`` command (D1).
7. ``ports.py`` and ``kill.py``: the killable unit and the kill boundary (D6).

The durable step executor skips each call not yet started once a stop is requested (D5)
and runs the kill boundary for a running call that declares a unit (D6); recovery ends a
stopped run with no model turn (D3) and holds the agent's other work (D4).

Planned: the ``turn`` scope (D7, refused ``scope_not_built``), ``aksha stop`` and exit code
6, the first Ctrl-C as a stop, and the web Stop button (interfaces, ADR-P19); reconciling
an ``effects_unknown`` step through its idempotency key (ADR-P03).
"""
