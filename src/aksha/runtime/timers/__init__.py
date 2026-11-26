"""ADR-P25: durable timers and time as a recorded input.

A timer is a fact on the injected clock (``timer.scheduled``, closed once by
``timer.fired`` or ``timer.cancelled``), so it survives a restart and fires the same on
every fake run. Firing is an automatic start: a firing that starts work asks the hold
predicate and counts toward the automatic-start limit; a firing that only voids or
expires starts nothing. A read of the current date is a lineage input (``time:clock``),
and the runtime records the date as a versioned source, so a later day invalidates what
was built from an earlier one before any reuse decision.

Modules: ``types.py`` (the values), ``reads.py`` (one attempt's time reads), ``facts.py``
(the three timer facts), ``view.py`` (the projection and ``due``), ``clock.py`` (the date
as a read and as a source), ``freshness.py`` (lifetimes and the recheck timers a fetch or
save schedules), ``firing.py`` (firing due timers, and the tick the session runs).
"""
