"""Evals (ADR-P22, the eval design): each fixture world is an environment, graded by code.

An eval runs an agent under test over eval cases and grades what it left behind. The parts
follow the reinforcement-learning environment shape: a task (:class:`~aksha.evals.case.EvalCase`),
a world state copied fresh per episode (:mod:`aksha.evals.world`), tools that read and
write that state behind a path guard (:mod:`aksha.evals.episode`), and a grader the agent
cannot reach (:mod:`aksha.evals.grade` over the answer key in :mod:`aksha.evals.key`).

Layout:

1. ``world``, ``key``, ``case``: worlds and their locks, answer keys with ``resolve`` and
   ``key_check``, case files and suites with the pairing rules.
2. ``episode``, ``environment``, ``bundle``, ``state``: one episode's event store and
   fixture tools, its exported bundle, and the graded state folded from the bundle.
3. ``grade`` and ``criteria/``: the grader cascade (gate, invariants, state, citations,
   traps and forbidden outputs, then the optional judge in ``judge``).
4. ``relations``: invariance and directional runs across a base world and its variant.
5. ``selftest/``: the oracle, perturbed oracles, null, lister and hedger bundles and the
   mutants that prove each grader can fail.
6. ``metrics``, ``results``, ``report``, ``runner``: pass@k, pass^k and the other numbers,
   results recorded as events in a results store with a projection over them, the text
   and JSON reports, and the suite runner.
7. ``registry`` and ``agents/``: the agent-under-test registry (the hook the real agents
   register through) and the scripted agents the runner is tested with.

The package imports the runtime and itself; the composition root (:mod:`aksha.app`) hands
it the stores, clocks and agents.
"""
