"""Stop policy and stop-policy evaluation (ADR-P16).

One owner decides every stop. A stack of pure stop policies, derived from the event store
at every evaluation (no push or pop event), answers each stop candidate; evaluation
combines the answers with one fixed precedence and records one ``stop.decided`` per
candidate. Human state comes first: the hold predicate is asked before any policy may
propose work, and it holds only the held work (S1).

Built: ``types.py`` (opt-ins, candidates, verdicts, decisions, holds), ``ports.py`` (the
``StopPolicy`` Protocol), ``core.py`` (the Run, Task and interrupt policies with the
budget, turn and stop-veto limits), ``builtins.py`` (goal, plan, forced tool and todo
reminder), ``stack.py`` (the derived stack and ``compose_prep``), ``hold.py`` (the hold
predicate, ``rebuild_blocked`` and the TC-4 audit), ``evaluation.py`` (``evaluate_stop``,
pure) and ``commit.py`` (the one transaction). Not built: the request router's session
scope (ADR-P23), and follow-up input as a finish veto (S2, waits on ADR-P27). The agent loop
(``runtime/loop/``, ADR-P06) calls this at every turn end.
"""
