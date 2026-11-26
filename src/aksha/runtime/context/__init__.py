"""Context assembly and compaction (ADR-P08): the model's context as a pure projection.

Built: the policy and its hash (``types.py``, ``policy.py``), the instance's recorded turns
(``turns.py``), rendered reads (``reads.py``), observation masking with the supersede rule
of decision 32 (``masking.py``), the runtime's messages (``notices.py``), summary
compaction as a recorded step (``summary.py``), assembly (``assembly.py``) and the turn's
driver (``prepare.py``). Planned: steering input (S4, P27), resume on the instance's own
session (decision 36), and the agent loop that calls ``prepare_context`` (P06).

Amendments from the build (where the code differs from the ADR's first text):

1. An agent that declares no context policy runs under ``DEFAULT_POLICY``, which turns
   masking on: compaction is a core feature (decision 34). ``ContextPolicy.masking`` keeps
   the ADR's default of False, so the frozen extension API does not change.
2. A plan stays monotone through the record, not through recomputation: each model step's
   ``step.started{masked}`` names the results it hid, and later turns hide them again.
3. Stale results (decision 32) are masked first and always, even inside the recent tail,
   with one change notice at the end; ``Masked.reason`` adds ``stale``.
4. A context is rebuilt from the log as it stood before its model step started
   (``turns.as_of``), which is what makes replay byte-identical.
5. The hard-limit value is ``ContextOverLimit`` (``models.errors.ContextOverflow`` is the
   adapter's exception); the summary step's ID is ``<turn>/compact``; a failed summary step
   counts against ``max_per_instance``.
"""
