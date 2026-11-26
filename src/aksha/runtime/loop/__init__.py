"""The agent loop (ADR-P06): one turn of one Task instance, judged at every turn end.

The loop calls the model, runs the tool calls it asked for, records the results and hands
every turn end to stop-policy evaluation. It keeps nothing between turns: each turn starts
from projections of the event store, and every write goes through the durable step
executor, so a restarted process returns the recorded steps and runs only the rest.

Built: the values (``types.py``), turn-end classification with its fixed priority and the
turn end rebuilt from the log (``classify.py``), call positions through the policy gate
and the executor in listed order, with the skip rule (``calls.py``), ``run_turn``
(``turn.py``) and the driver that alternates evaluation and turns, with the recovery
driver (``drive.py``).

Amendments from the build (where the code differs from the ADR's first text):

1. ``TurnEnd`` keeps the ADR's fields; ``TurnEnd.candidate`` maps each kind to the stop
   candidate kind ADR-P16 names (``tool_results`` is ``tools``, ``final_answer`` is
   ``finish``, ``interrupt`` is ``interrupted``, ``context_overflow`` is ``overflow``). A
   ``step_mismatch`` is a run-level fault, so the run ends ``failed``; any other fault
   fails the instance only.
2. A reply refused by the provider, or with neither text nor calls, is ``malformed`` too.
3. The gate checks a position against the log as it stood before the position started,
   so a resumed position is judged as it was; a recorded position is never re-checked.
4. The executor gained ``recorded``, ``refuse`` (the gate's refusal as the step's one fact)
   and ``skip`` (a position after a signal, ``skipped{after_<signal>}``).
5. A granted approval whose signature matches the call's rebuilt body runs the action
   through the outbox; otherwise the call requests one (ADR-P17 D5, D6).
6. The driver reads where a run stands from the log (``control_point``): a CONTINUE whose
   turn has no recorded end runs that turn (the resume mid-turn), and a recorded turn end
   no decision has judged is judged once.

Planned: the ``model`` route (a tool that runs a routed model call), the runtime's reader
for span drafts (a tool's drafts are minted against no returned text today, so a draft is
refused ``class_violation``), exec calls inside a ``KillableUnit``, read-only positions in
parallel (committed in position order), and a replaceable loop (ADR-P06 "Where this
stops").
"""
