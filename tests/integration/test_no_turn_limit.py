"""Decision 42: a Task has no turn limit by default, and the run budget is the backstop.

Long-running work stops on completion, the run budget, the stop-veto limit or a person,
as coding agent harnesses do, not on a per-Task turn count sized for scripted fakes. The
research and patent agents declare none; a Task that never finishes is still stopped, by
the run's ``model_turns`` budget. A limit a Task declares itself still applies (the toy
agent and ``tests/unit/runtime/stop_policy/test_limit_then_check.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from aksha.agents.patent.agent import AGENT as PATENT
from aksha.agents.research.agent import AGENT as RESEARCH
from aksha.runtime.definitions.types import Agent
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.tasks.types import Task
from tests.support.loop import Bench, Recorder, call, tools_reply


def _walk(tasks: tuple[Task, ...]) -> Iterator[Task]:
    for t in tasks:
        yield t
        yield from _walk(t.children)


def _declared_turn_limits(agent: Agent) -> dict[str, int]:
    limits = {t.id: t.turn_limit for t in _walk(agent.tasks) if t.turn_limit is not None}
    if agent.limits is not None and agent.limits.turn_limit is not None:
        limits["<agent>"] = agent.limits.turn_limit
    return limits


def test_the_research_and_patent_tasks_have_no_turn_limit() -> None:
    for agent in (RESEARCH, PATENT):
        assert _declared_turn_limits(agent) == {}, agent.name
        assert all(t.max_finish_vetoes == 3 for t in _walk(agent.tasks))  # unchanged


def test_their_run_budgets_stay_as_the_backstop() -> None:
    assert RESEARCH.budget is not None and PATENT.budget is not None
    research, patent = RESEARCH.budget, PATENT.budget
    assert (research.model_turns, research.tool_calls, research.active_ms) == (90, 140, 600_000)
    assert (patent.model_turns, patent.tool_calls) == (160, 900)


def _forever(request: GenerationRequest) -> GenerationResult:
    """A runaway model: it runs the tests every turn and never saves or finishes."""
    return tools_reply(call("run_tests"))


def test_the_run_budget_stops_a_task_with_no_turn_limit(tmp_path: Path) -> None:
    budget = {"model_turns": 12, "tool_calls": 1_000, "active_ms": 10**9}
    b = Bench(tmp_path / "s.sqlite", Recorder(rule=_forever), budget=budget, turn_limit=None)
    try:
        assert b.agent.tasks[0].turn_limit is None
        b.start(turn_factor=3)  # a live run's factor scales nothing here: no limit to scale
        result = b.drive()
        assert result.state == "held" and result.turns[-1].kind == "budget"
        assert not [d for d in b.decisions() if d.payload.get("limit") == "turn_limit"]
        (reached,) = b.events("budget.reached")
        assert [e.payload["kind"] for e in b.events("interrupt.raised")] == ["budget"]
        assert len(b.model.requests) == 12  # every turn the budget allowed, and no more
        assert reached.seq > 0
    finally:
        b.close()
