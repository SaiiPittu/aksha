"""Episodes (ADR-P09 D6): a tenant's past runs of an agent, as a projection.

Nothing is stored for an episode, so there is nothing to keep in sync and nothing extra to
erase: each is read back from the run's own facts (``run.ended``, the completion checks
and the saves) by the core's ``ended_runs`` projection. The tenant is the log view's owner.
"""

from __future__ import annotations

from aksha.runtime.api import LogView, ended_runs

from .types import Episode, Outcome

_OUTCOMES: dict[str, Outcome] = {
    "complete": "done",
    "partial": "partial",
    "blocked": "failed",
    "failed": "failed",
    "stopped": "stopped",
}


def episodes(view: LogView, agents: frozenset[str]) -> tuple[Episode, ...]:
    """Every ended run of ``agents`` in ``view``, oldest end first; pure."""
    return tuple(
        Episode(
            run_id=r.run,
            agent=r.agent,
            outcome=_OUTCOMES.get(r.outcome, "failed"),
            completed=r.completed,
            blockers=r.blockers,
            outputs=r.outputs,
        )
        for r in ended_runs(view, agents)
    )
