"""The research agent: who sells what, at what price, and what customers say, every cell cited.

It works for anyone: the request names the market, the segment and what to compare, and
the inputs are whatever the person uploaded (pages, notes, exports, emails, PDF text). It
scopes the question (asking only what changes the result), lists its inputs, reads every
page that sells something and resolves one company reachable under two names into one
record with the excerpt that proves it (a search snippet never supports a claim), keeps
conflicting prices side by side with their pages and dates, reads the review exports
(dates, geographies, cross-posted duplicates counted once, a vendor's claim kept apart
from a measured fact), and saves four outputs: the competitor matrix as an evidence grid
over the request's dimensions, the customer needs with their evidence, the findings with
their explicit unknowns, and what to test next.

Declarations are in ``agent.py``; tool bodies in ``tools.py`` over the pure readers of
``parsing.py``; the analysis rules (entity resolution, price conflicts, term matching,
review samples) in ``analysis.py``; completion checks in ``checks.py``; the offline
rule-based model, a test double, in ``fakes.py``. No module but ``fakes.py`` knows a
fixture. It imports ``aksha.runtime.api`` only.
"""
