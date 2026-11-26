"""Grader self-tests: every grader must pass a known-good bundle and fail a known-bad one.

The bundles are made by agents that read the answer key, so they live here and are never
registered as an agent under test or bound as an agent's fake:

1. Oracle: the outputs, edges, citations and ledger the key describes; must pass all-pass.
2. Perturbed oracles: correct variants (lists reordered, enum casing changed, extra fields,
   amounts written with their currency, an alias used as the name); must grade as the
   oracle does, or a grader is matching shape instead of meaning.
3. Null: nothing saved; must fail the case and pass every invariant.
4. Lister: every page its own company, every matrix cell ``disclosed``; must fail each
   precision criterion.
5. Hedger: abstains on everything; must fail ``precision.known_values``.
6. Mutants: one-defect copies of the oracle bundle; each must fail the criterion it names.

The first line of the self-test report is the mutation coverage: criteria killed by a
mutant over all criteria.
"""
