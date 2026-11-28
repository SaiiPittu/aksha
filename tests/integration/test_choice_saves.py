"""A person's choice, saved by the runtime (ADR-P04 choice binding, ADR-P17 D2), on a toy agent.

The agent ``picker`` lists a menu (``menu``), puts the choice to a person (``pick``: the
options are the menu's items, built by code, any subset of them; the runtime saves the
``pick`` artifact from the answer) and then works once per chosen code (``work``, keyed
by ``artifact_keys("pick", "chosen")``). The model never writes the pick: it asks, with a
summary, and finishes once the person answered.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.api import artifact_keys
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.artifacts.versions import current
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.interrupts.commands import answer
from aksha.runtime.interrupts.reducers import interrupts
from aksha.runtime.interrupts.types import AnswerSave, DeclaredInterrupt
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.tasks.choices import chosen_by_person
from aksha.runtime.tasks.tree import CompiledTree, compile_tree
from aksha.runtime.tasks.types import CheckResult, Task
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolUse
from tests.support.events import START
from tests.support.loop import Recorder, call, tools_reply

OWNER = "o"
RUN = "R-0001"
AGENT = "picker"
DEFINITION = "sha256:" + "b" * 64
BUDGET = {"model_turns": 1_000, "tool_calls": 1_000, "active_ms": 10**9}
MENU = ArtifactKind("menu", {"type": "object", "required": ["items"]})
PICK = ArtifactKind("pick", {"type": "object", "required": ["chosen"]}, reviewable=True)
WORK = ArtifactKind("work", {"type": "object"}, keyed=True)


def has(kind: str) -> Any:
    def check(view: TaskView) -> CheckResult:
        return CheckResult(view.own(kind) is not None, f"save the {kind}")

    check.__qualname__ = f"has_{kind}"
    return check


def options(view: TaskView) -> tuple[tuple[str, str], ...]:
    menu = view.latest("menu")
    items = [] if menu is None else menu.content["items"]
    return tuple((str(i["code"]), str(i["label"])) for i in items)


def build(view: TaskView, chosen: tuple[str, ...]) -> dict[str, Any]:
    menu = view.latest("menu")
    return {"chosen": list(chosen), "from": "" if menu is None else str(menu.ref)}


def picker(*, pick_tools: tuple[str, ...] = ()) -> Agent:
    return Agent(
        name=AGENT,
        purpose="Let a person pick from a menu, then work on each pick.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(MENU, PICK, WORK),
        tools=(ToolUse("save_artifact"),),
        tasks=(
            Task("menu", tools=("save_artifact",), writes=("menu",), done=has("menu")),
            Task(
                "pick",
                needs=("menu",),
                reads=("menu",),
                tools=pick_tools,
                writes=("pick",),
                interrupt=DeclaredInterrupt(
                    "choice",
                    "dish",
                    options=options,
                    multiple=True,
                    saves=AnswerSave("pick", "chosen", build),
                ),
                done=chosen_by_person,
            ),
            Task(
                "work",
                per=artifact_keys("pick", "chosen"),
                needs=("pick",),
                tools=("save_artifact",),
                writes=("work",),
                done=has("work"),
            ),
        ),
    )


def _answered(request: GenerationRequest) -> bool:
    return any((m.text or "").startswith("Answers from the person:") for m in request.messages)


class Toy:
    """The model: list the menu, ask (or, ``self_select``, try to pick itself), then work."""

    def __init__(self, items: list[str], *, self_select: bool = False) -> None:
        self.items = items
        self.self_select = self_select

    def __call__(self, request: GenerationRequest) -> GenerationResult:
        instance = request.step_id.split("/")[1]
        if instance == "menu":
            listed = [{"code": c, "label": f"dish {c}"} for c in self.items]
            return tools_reply(
                call("save_artifact", kind="menu", content={"items": listed}),
                call("finish", summary="menu saved"),
            )
        if instance == "pick":
            if self.self_select and not _answered(request):
                taken = {"chosen": self.items[:1], "from": "the model"}
                return tools_reply(
                    call("save_artifact", kind="pick", content=taken),
                    call("finish", summary="picked"),
                )
            if _answered(request) or not self.items:
                return tools_reply(call("finish", summary="the person picked"))
            return tools_reply(call("ask", question="Which dishes? " + ", ".join(self.items)))
        key = instance.split("[", 1)[1].rstrip("]")
        return tools_reply(
            call("save_artifact", kind="work", content={"done": key}),
            call("finish", summary=f"worked on {key}"),
        )


class Bench:
    def __init__(self, path: Path, toy: Toy, agent: Agent | None = None) -> None:
        self.clock = FakeClock(START)
        self.store = open_store(path, self.clock, writer=True)
        self.agent = agent or picker()
        self.tree: CompiledTree = compile_tree(AGENT, self.agent.tasks)
        self.model = Recorder(rule=toy)
        kinds = {f"{AGENT}.{k.name}": k for k in self.agent.artifacts}
        self.setup = LoopSetup(
            store=self.store, owner=OWNER, agent=self.agent, tree=self.tree,
            registry=ToolRegistry(self.agent, {}, ()),
            table=bind_aliases((), "fake", {"generate": self.model}),
            instructions="You help a person pick.", request="Pick dishes.",
            artifacts=ArtifactStore(kinds), definition_hash=DEFINITION,
        )  # fmt: skip
        self.executor = StepExecutor(self.store, OWNER, self.clock)
        payload = {"agent": AGENT, "cause": "request", "definition_hash": DEFINITION,
                   "budget": BUDGET}  # fmt: skip
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=RUN),))

    def drive(self) -> DriveResult:
        return drive(self.setup, self.executor, RUN)

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def open_choice(self) -> Any:
        found = [i for i in interrupts(self.view()).values() if i.is_open]
        assert len(found) == 1, found
        return found[0]

    def answer(self, text: str) -> None:
        receipt = answer(self.store, OWNER, self.open_choice().id, by="person", text=text,
                         tree=self.tree)  # fmt: skip
        assert receipt.accepted, receipt

    def pick(self) -> Any:
        return current(self.view(), f"{AGENT}.pick", None)

    def work_keys(self) -> list[str]:
        return sorted(
            str(e.payload["key"])
            for e in self.view().events
            if e.type == "artifact.saved" and e.payload["kind"] == f"{AGENT}.work"
        )

    def ended(self) -> dict[str, Any]:
        (end,) = [e.payload for e in self.view().events if e.type == "run.ended"]
        return dict(end)


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path / "s.sqlite", Toy(["A", "B", "C"]))
    yield b
    b.store.close()


def test_the_choice_lists_the_code_built_options_and_the_run_waits(bench: Bench) -> None:
    result = bench.drive()
    assert result.state == "held"
    i = bench.open_choice()
    assert i.kind == "choice" and list(i.payload["options"]) == ["A", "B", "C"]
    assert list(i.payload["option_labels"]) == ["dish A", "dish B", "dish C"]
    assert i.payload["multiple"] is True
    assert bench.pick() is None  # nothing is chosen until the person answers


def test_the_answer_is_saved_by_the_runtime_and_fans_out(bench: Bench) -> None:
    bench.drive()
    bench.answer("C, A")
    result = bench.drive()
    assert result.state == "ended" and result.outcome == "complete"
    pick = bench.pick()
    assert pick.origin == "choice" and pick.answer == bench_answer_id(bench)
    assert list(pick.content["chosen"]) == ["A", "C"]  # option order, the person's codes
    assert pick.step.endswith("/derived")  # the runtime's reserved step, not a model call
    assert bench.work_keys() == ["A", "C"]
    resolved = [e for e in bench.view().events if e.type == "interrupt.resolved"]
    assert list(resolved[-1].payload["chosen"]) == ["A", "C"]
    statements = [r for r in pick.reads if r.kind == "statement"]
    assert statements, "the answer is in the pick's lineage"


def bench_answer_id(bench: Bench) -> str:
    raised = [e for e in bench.view().events if e.type == "interrupt.raised"]
    return str(raised[-1].payload["interrupt"])


def test_an_answer_of_none_is_recorded_and_the_run_ends_with_no_work(bench: Bench) -> None:
    bench.drive()
    bench.answer("none")
    result = bench.drive()
    assert result.outcome == "complete"
    assert list(bench.pick().content["chosen"]) == []
    assert bench.pick().origin == "choice"
    assert bench.work_keys() == []


def test_an_answer_outside_the_options_is_refused(bench: Bench) -> None:
    bench.drive()
    receipt = answer(bench.store, OWNER, bench.open_choice().id, by="person", text="A, Z",
                     tree=bench.tree)  # fmt: skip
    assert not receipt.accepted and receipt.refused == "not_an_option"


def test_a_model_written_pick_is_refused_and_the_run_still_waits_for_the_person(
    tmp_path: Path,
) -> None:
    b = Bench(tmp_path / "s.sqlite", Toy(["A", "B"], self_select=True),
              picker(pick_tools=("save_artifact",)))  # fmt: skip
    try:
        first = b.drive()
        refused = [
            e.payload for e in b.view().events
            if e.type == "step.completed" and e.payload.get("tool") == "save_artifact"
            and e.step and "/pick/" in e.step
        ]  # fmt: skip
        assert refused and all(p["code"] == "answer_only" for p in refused)
        assert b.pick() is None
        # The model's finish is vetoed (nothing was chosen); it never decides for the person.
        assert first.state == "ended" and first.outcome != "complete"
        assert b.work_keys() == []
    finally:
        b.store.close()


def test_finish_before_asking_is_vetoed(tmp_path: Path) -> None:
    def finishes(request: GenerationRequest) -> GenerationResult:
        if request.step_id.split("/")[1] == "pick":
            return tools_reply(call("finish", summary="done"))
        return Toy(["A"])(request)

    b = Bench(tmp_path / "s.sqlite", Toy(["A"]))
    b.model.rule = finishes
    try:
        b.drive()
        vetoes = [
            e.payload for e in b.view().events
            if e.type == "stop.decided" and e.payload.get("candidate_instance") == "pick"
            and e.payload.get("verdict") == "continue"
        ]  # fmt: skip
        assert vetoes and "call ask" in str(vetoes[0].get("reason"))
        assert b.pick() is None and b.work_keys() == []
    finally:
        b.store.close()


def test_with_no_options_nothing_is_asked_and_the_empty_pick_is_recorded(tmp_path: Path) -> None:
    b = Bench(tmp_path / "s.sqlite", Toy([]))
    try:
        result = b.drive()
        assert result.outcome == "complete"
        assert not interrupts(b.view())
        pick = b.pick()
        assert list(pick.content["chosen"]) == [] and pick.answer == ""
        assert b.work_keys() == []
    finally:
        b.store.close()


def test_a_model_options_argument_is_refused_on_a_declared_choice(tmp_path: Path) -> None:
    def own_options(request: GenerationRequest) -> GenerationResult:
        if request.step_id.split("/")[1] == "pick" and not _answered(request):
            return tools_reply(call("ask", question="which?", options=["A"]))
        return Toy(["A", "B"])(request)

    b = Bench(tmp_path / "s.sqlite", Toy(["A", "B"]))
    b.model.rule = own_options
    try:
        b.drive()
        asked = [
            e.payload for e in b.view().events
            if e.type == "step.completed" and e.payload.get("tool") == "ask"
        ]  # fmt: skip
        assert asked and asked[0]["outcome"] == "error" and asked[0]["code"] == "schema"
    finally:
        b.store.close()
