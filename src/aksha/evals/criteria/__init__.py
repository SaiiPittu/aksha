"""The criteria the grader cascade runs, by ID, in one registry.

``REGISTRY`` maps each criterion ID to its :class:`~aksha.evals.grade.Criterion`: the
invariants, the state checks over outputs, the change checks, the citation checks, the ten
trap checks, the six forbidden-output checks and the web checks (``web.*``). A case names
criteria by ID; an unknown ID is refused when the suite loads.
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.evals.criteria import change, citations, forbidden, invariants, outputs, traps, web
from aksha.evals.grade import Criterion

REGISTRY: Mapping[str, Criterion] = {
    c.id: c
    for c in (
        *invariants.CRITERIA,
        *outputs.CRITERIA,
        *change.CRITERIA,
        *citations.CRITERIA,
        *traps.CRITERIA,
        *forbidden.CRITERIA,
        *web.CRITERIA,
    )
}
