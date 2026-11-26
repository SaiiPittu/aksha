"""The criteria the grader cascade runs, by ID, in one registry.

``REGISTRY`` maps each criterion ID to its :class:`~aksha.evals.grade.Criterion`: the
invariants, the state checks over outputs, the citation checks, the ten trap checks and the
six forbidden-output checks. A case names criteria by ID; an unknown ID is refused when the
suite loads.
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.evals.criteria import citations, forbidden, invariants, outputs, traps
from aksha.evals.grade import Criterion

REGISTRY: Mapping[str, Criterion] = {
    c.id: c
    for c in (
        *invariants.CRITERIA,
        *outputs.CRITERIA,
        *citations.CRITERIA,
        *traps.CRITERIA,
        *forbidden.CRITERIA,
    )
}
