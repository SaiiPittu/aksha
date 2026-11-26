"""The research agent: who sells what, at what price, and what customers say, every cell cited.

It scopes the question (asking only what changes the result), finds the competitors and
the manual alternatives in its corpus, resolves one company reachable under two names into
one record with the excerpt that proves it, reads each competitor's product and pricing
pages (a search snippet never supports a claim), keeps conflicting prices side by side
with their pages and dates, reads the customer reviews (dates, geographies, cross-posted
duplicates counted once, a vendor's claim kept apart from a measured fact), and saves four
outputs: the competitor matrix as an evidence grid, the customer needs with their
evidence, the findings with their explicit unknowns, and what to test next.

Declarations are in ``agent.py``; tool bodies in ``tools.py`` over the pure readers of
``parsing.py``; the analysis rules (entity resolution, price conflicts, review themes)
in ``analysis.py``; completion checks in ``checks.py``; the offline rule-based model in
``fakes.py``. It imports ``aksha.runtime.api`` only.
"""
