"""The Task model and scheduler (ADR-P15): a declared Task tree with pure completion checks.

Built:

1. ``types.py``: ``Task``, ``Read``, ``KeySource``, ``Check``, ``CheckResult``, and what the
   scheduler computes: ``InstanceRef``, ``InstanceStatus``, ``Evaluation``, ``SupervisorMove``.
2. ``tree.py``: ``compile_tree`` and ``state_table`` (D1).
3. ``keys.py`` and ``ports.py``: key sources read from the log, ``keys.listed`` and the
   ``KeyLister`` port (D3).
4. ``status.py``: instance status as a projection, the Task tree read view (D6).
5. ``view.py``: ``TaskView``, all a completion check may read (D2).
6. ``checks.py`` and ``completion.py``: checks run safely, the parent rule, partial
   parents (S1), vetoes and the stop-veto limit (D2).
7. ``scheduler.py``: the pick order, withdrawal, entry and re-entry by writer key (D5).
8. ``supervisor.py``: the supervisor rule and its two limits (D7).

Planned: the work board projection for supervisors (an extension), concurrency above 1,
keyed children below the top level.
"""
