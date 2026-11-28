"""A keyed Task's instances are told their key, and a refused text-only finish is answered.

A toy agent, not one of the bundled ones: ``plan`` saves a list of items, then ``work``
runs once per item (``artifact_keys``) and must save a ``result`` under its own key. Every
instance of ``work`` shares the Task's brief and reads, so the only way its model can know
which item it is for is the key notice the runtime adds to its context (ADR-P08 D1).

The model here knows nothing the context does not say: it reads the key from the request
and answers its first turn with text only. That finish is refused by the completion check,
whose message is the next turn's runtime message (ADR-P15 D2), and the instance then passes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.agents.patent.agent import AGENT as PATENT
from aksha.agents.research.agent import AGENT as RESEARCH
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.projections.view import load_view
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task, artifact_keys
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolUse
from tests.support.events import START
from tests.support.loop import Recorder, call, tools_reply

OWNER = "o"
RUN = "R-0001"
NAME = "toy"
DEFINITION = "sha256:" + "b" * 64
BUDGET = {"model_turns": 1_000, "tool_calls": 1_000, "active_ms": 10**9}
ITEMS = [{"key": "alpha", "title": "The first item"}, {"key": "beta", "title": "The second"}]
PLAN = ArtifactKind("plan", {"type": "object"})
RESULT = ArtifactKind("result", {"type": "object"}, keyed=True)
WORK_BRIEF = "For the one item whose key you are given, save its result."


def result_saved(view: TaskView) -> CheckResult:
    """``work``'s check: a result saved under the instance's own key."""
    return CheckResult(view.own("result") is not None, f"save the result of {view.key}")


def plan_saved(view: TaskView) -> CheckResult:
    return CheckResult(view.latest("plan") is not None, "save the plan")


def toy() -> Agent:
    return Agent(
        name=NAME,
        purpose="Plan a few items, then work on each one.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(PLAN, RESULT),
        tools=(ToolUse("save_artifact"),),
        tasks=(
            Task("plan", tools=("save_artifact",), writes=("plan",), done=plan_saved),
            Task(
                "work",
                per=artifact_keys("plan", "items"),
                needs=("plan",),
                reads=("plan",),
                tools=("save_artifact",),
                writes=("result",),
                done=result_saved,
                brief="prompts/work.md",
            ),
        ),
    )


KEY_LINE = re.compile(r"works on one key only: `([^`]+)`")


def text_of(request: GenerationRequest) -> str:
    return "\n".join(m.text or "" for m in request.messages)


def model_rule(request: GenerationRequest) -> GenerationResult:
    """Answer from what the context says, as a real model has to.

    ``plan`` saves the items. A ``work`` instance with no runtime message yet answers in
    text only (a finish); once told why its finish was refused, it saves its result under
    the key its context names, or asks which item it is for when none is named.
    """
    if "/plan/" in request.step_id:
        save = call("save_artifact", kind="plan", content={"items": ITEMS})
        return tools_reply(save, call("finish", summary="planned"))
    text = text_of(request)
    found = KEY_LINE.search(text)
    if "The runtime did not accept your finish" not in text:
        return GenerationResult("I have looked at the items.", (), "text")
    if found is None:
        return GenerationResult("Which item should I work on?", (), "text")
    save = call("save_artifact", kind="result", key=found.group(1), content={"ok": True})
    return tools_reply(save, call("finish", summary="saved"))


@pytest.fixture
def ran(tmp_path: Path) -> Iterator[tuple[Recorder, list[Event]]]:
    clock = FakeClock(START)
    store = open_store(tmp_path / "aksha.sqlite", clock, writer=True)
    agent = toy()
    model = Recorder(rule=model_rule)
    setup = LoopSetup(
        store=store,
        owner=OWNER,
        agent=agent,
        tree=compile_tree(NAME, agent.tasks),
        registry=ToolRegistry(agent, {}, ()),
        table=bind_aliases((), "fake", {"generate": model}),
        instructions="You work through a plan.",
        request="Work through the plan.",
        artifacts=ArtifactStore({f"{NAME}.plan": PLAN, f"{NAME}.result": RESULT}),
        definition_hash=DEFINITION,
        briefs={"work": WORK_BRIEF},
    )
    started = {"agent": NAME, "cause": "request", "definition_hash": DEFINITION, "budget": BUDGET}
    store.append(OWNER, "U1", (NewEvent("run.started", started, run=RUN),))
    result = drive(setup, StepExecutor(store, OWNER, clock), RUN)
    assert result.state == "ended"
    yield model, list(load_view(store, OWNER).events)
    store.close()


def _first_request(model: Recorder, instance: str) -> GenerationRequest:
    return next(r for r in model.requests if r.step_id == f"{RUN}/{instance}/t01")


def test_each_keyed_instance_s_context_names_its_own_key_and_entry(
    ran: tuple[Recorder, list[Event]],
) -> None:
    model, _ = ran
    alpha, beta = _first_request(model, "work[alpha]"), _first_request(model, "work[beta]")
    assert alpha.context_hash != beta.context_hash
    assert "works on one key only: `alpha`" in text_of(alpha)
    assert "works on one key only: `beta`" in text_of(beta)
    assert "`items` of `toy.plan`" in text_of(alpha)
    assert '"title":"The first item"' in text_of(alpha)
    assert "The first item" not in text_of(beta).split("works on one key only")[1]
    # The shared part comes first: the instructions, reads and request are the same.
    shared = [m for m in alpha.messages if "works on one key only" not in (m.text or "")]
    assert [m.text for m in shared] == [
        m.text for m in beta.messages if "works on one key only" not in (m.text or "")
    ]
    # The Task's brief is its text, once, as the goal: never the prompt file's path.
    assert "prompts/work.md" not in text_of(alpha)
    assert text_of(alpha).count(WORK_BRIEF) == 1


def test_an_unkeyed_task_gets_no_key_notice(ran: tuple[Recorder, list[Event]]) -> None:
    model, _ = ran
    assert "works on one key only" not in text_of(_first_request(model, "plan"))


def test_a_refused_text_only_finish_is_answered_with_the_check_s_message_then_passes(
    ran: tuple[Recorder, list[Event]],
) -> None:
    model, events = ran
    second = next(r for r in model.requests if r.step_id == f"{RUN}/work[alpha]/t02")
    assert "The runtime did not accept your finish: save the result of alpha" in text_of(second)
    saved = sorted(
        (str(e.payload["key"]), str(e.payload["writer"]["key"]))
        for e in events
        if e.type == "artifact.saved" and e.payload["kind"] == "toy.result"
    )
    assert saved == [("alpha", "alpha"), ("beta", "beta")]  # each under its own key
    (ended,) = (e for e in events if e.type == "run.ended")
    assert ended.payload["outcome"] == "complete"


def _leaves(tasks: tuple[Task, ...]) -> Iterator[Task]:
    for t in tasks:
        yield from _leaves(t.children)
        if not t.children:
            yield t


@pytest.mark.parametrize("agent", [RESEARCH, PATENT], ids=lambda a: a.name)
def test_every_bundled_leaf_task_gives_a_refused_finish_back_to_its_model(agent: Agent) -> None:
    """A ``max_finish_vetoes`` of 1 fails the first refused finish: its message is never read."""
    low: Mapping[str, Any] = {
        t.id: t.max_finish_vetoes for t in _leaves(agent.tasks) if t.max_finish_vetoes < 2
    }
    assert low == {}
