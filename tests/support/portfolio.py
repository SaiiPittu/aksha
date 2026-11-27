"""A bench for the portfolio kit: a generic ``fleet`` agent over N synthetic items.

The agent names no domain. ``list`` saves the item listing; the supervisor ``lead``
(``aksha.extensions.portfolio.supervisor_task``) runs ``triage`` for every item (one turn:
a ``card`` with a score) and ``deep`` for the items it admits plus the audit sample (two
turns: an ``inspect`` read, then a ``verdict``); ``report`` reads the round and saves the
batched review. The rule model answers each Task from the step ID and, for the
supervisor, from the work board recitation in its request, which is the only thing it
reads. Every item is synthetic (``SYN-`` keys).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.extensions.portfolio import (
    PortfolioSpec,
    Tier,
    commit_tool,
    read_board,
    review,
    supervisor_task,
)
from aksha.extensions.portfolio import kinds as portfolio_kinds
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.context.assembly import RECITATION
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleGenerationModel
from aksha.runtime.models.types import GenerationRequest, GenerationResult, ToolCall
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec, ToolUse
from tests.support.events import START

OWNER = "o"
RUN = "R-0001"
AGENT = "fleet"
DEFINITION = "sha256:" + "b" * 64
SAVE = "save_artifact"
INSPECT = "inspect"
OBJECT = {"type": "object"}
BRIEF = "Decide which items get deep work. Read the work board; commit one round."


@dataclass(frozen=True)
class Item:
    """One synthetic item: its triage score and what deep work finds (worth keeping?)."""

    key: str
    score: int
    worth: bool


PLANTED = "SYN-I-13"
"""The planted wrong drop: triage scores it 1, deep work finds it worth keeping."""


def items(n: int = 20, planted: str | None = PLANTED) -> tuple[Item, ...]:
    """``n`` items scored ``(7 i) mod 10``; worth keeping at 7 and up, and ``planted``."""
    out = []
    for i in range(1, n + 1):
        key = f"SYN-I-{i:02d}"
        score = (i * 7) % 10  # 7, 4, 1, 8, 5, 2, 9, 6, 3, 0, ...
        out.append(Item(key, score, score >= 7 or key == planted))
    return tuple(out)


def _has_own(kind: str) -> Any:
    def check(view: TaskView) -> CheckResult:
        return CheckResult(view.own(kind) is not None, f"save {kind}")

    check.__qualname__ = f"has_own_{kind}"
    return check


def _has(kind: str) -> Any:
    def check(view: TaskView) -> CheckResult:
        return CheckResult(view.latest(kind) is not None, f"save {kind}")

    check.__qualname__ = f"has_{kind}"
    return check


def _card(view: Any, key: str) -> Mapping[str, Any]:
    found = view.latest("card", key)
    return {} if found is None else {"score": found.content.get("score")}


def _worth(view: Any, key: str) -> bool | None:
    found = view.latest("verdict", key)
    return None if found is None else bool(found.content.get("keep"))


def spec(**changes: Any) -> PortfolioSpec:
    """The fleet's portfolio: deep effort 2 turns, cap 8, budget 24 turns, audit 3."""
    base = PortfolioSpec(
        agent=AGENT,
        supervisor="lead",
        items=("listing", "items"),
        triage="triage",
        basis="card",
        deep=Tier("deep", "deep", effort=2, cap=8),
        card=_card,
        score="score",
        worth=_worth,
        budget_turns=24,
        audit_sample=3,
        audit_seed=0,
        supervise_above=8,
    )
    return replace(base, **changes)


def _inspect(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    return ToolOutcome(Ok({"key": args.get("key"), "text": "a long synthetic document"}))


def agent(s: PortfolioSpec) -> Agent:
    """The fleet agent (module doc)."""
    lead = supervisor_task(
        s,
        triage=Task("triage", tools=(SAVE,), writes=("card",), done=_has_own("card")),
        deep=Task(
            "deep",
            tools=(INSPECT, SAVE),
            reads=("card",),
            writes=("verdict",),
            done=_has_own("verdict"),
            turn_limit=2,
        ),
        brief="lead.md",
    )
    return Agent(
        name=AGENT,
        purpose="Work a portfolio of synthetic items.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(
            ArtifactKind("listing", OBJECT),
            ArtifactKind("card", OBJECT, keyed=True),
            ArtifactKind("verdict", OBJECT, keyed=True),
            ArtifactKind("report", OBJECT),
            *portfolio_kinds(),
        ),
        tools=(
            ToolDecl(
                ToolSpec(INSPECT, AGENT, "read", "Read one item's document.", OBJECT), _inspect
            ),
            commit_tool(s),
            ToolUse(SAVE),
        ),
        tasks=(
            Task("list", tools=(SAVE,), writes=("listing",), done=_has("listing")),
            lead,
            Task(
                "report",
                needs=("lead",),
                tools=(SAVE,),
                reads=("round",),
                writes=("report",),
                done=_has("report"),
            ),
        ),
    )


def call(name: str, n: int = 1, **args: Any) -> ToolCall:
    return ToolCall(f"call-{name}-{n}", name, args)


def reply(*calls: ToolCall) -> GenerationResult:
    return GenerationResult(None, calls, "tool_calls")


def board_of(request: GenerationRequest) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """The recitation in ``request``: counts, rows and the goal line."""
    text = next(
        (m.text or "" for m in reversed(request.messages)
         if (m.text or "").startswith(RECITATION)),
        "",
    )  # fmt: skip
    board = read_board(text)
    return board["counts"], board["rows"], board["goal"]


def _instance(step_id: str) -> tuple[str, str | None]:
    parts = step_id.split("/")[1:]
    path = []
    for p in parts:
        if p.startswith("t") and p[1:].isdigit():
            break
        path.append(p)
    head = path[0]
    key = head[head.index("[") + 1 : -1] if "[" in head else None
    return path[-1].split("[")[0], key


@dataclass
class FleetModel(RuleGenerationModel):
    """The rule model (module doc). ``admit`` is how many top rows the supervisor admits.

    ``obey_goal`` False makes a supervisor that commits once and then only finishes, to
    show the goal holds; ``commits`` False makes one that never commits at all.
    """

    world: Sequence[Item]
    admit: int = 6
    first_admit: int | None = None
    obey_goal: bool = True
    commits: bool = True
    requests: list[GenerationRequest] = field(default_factory=list)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        task, key = _instance(request.step_id)
        done = [m for m in request.messages if m.role == "tool"]
        by_key = {i.key: i for i in self.world}
        if task == "list":
            listing = {"items": [i.key for i in self.world]}
            return reply(call(SAVE, kind="listing", content=listing), call("finish", summary="ok"))
        if task == "triage":
            card = {"score": by_key[str(key)].score}
            return reply(call(SAVE, kind="card", key=key, content=card), call("finish"))
        if task == "deep":
            if not done:
                return reply(call(INSPECT, key=key))
            verdict = {"keep": by_key[str(key)].worth}
            return reply(call(SAVE, kind="verdict", key=key, content=verdict), call("finish"))
        if task == "lead":
            return self.lead(request)
        if task == "report":
            return reply(call(SAVE, kind="report", content={"ok": True}), call("finish"))
        return reply(call("finish"))

    def lead(self, request: GenerationRequest) -> GenerationResult:
        _counts, rows, goal = board_of(request)
        notes = {"questions": [], "entries": [], "hypotheses": "score predicts worth"}
        plan = {
            "goal": "every item carded or dropped with a reason",
            "tier_rule": f"top {self.admit} by score to deep",
            "budget_split": "deep",
        }
        if not self.commits:
            return reply(call("finish", summary="nothing to do"))
        undecided = [r for r in rows if r["state"] == "undecided"]
        if undecided:
            n = self.admit if self.first_admit is None else self.first_admit
            self.first_admit = None
            top = [r["key"] for r in undecided[:n]]
            rest = [r["key"] for r in undecided[n:]]
            changes = [{"keys": top, "state": "admitted", "tier": "deep", "reason": "top score"}]
            if rest:
                changes.append({"keys": rest, "state": "dropped", "reason": "score below cut"})
            args = {"notes": notes, "plan": plan, "directives": changes}
            return reply(call("commit_round", 1, **args), call("finish", summary="round committed"))
        if self.obey_goal and goal.startswith("[supervisor] re-open drops"):
            misses = [
                r["key"] for r in rows if r["audit"] and r["worth"] and r["state"] == "dropped"
            ]
            changes = [{"keys": misses, "state": "admitted", "tier": "deep",
                        "reason": "the audit found it worth deep work"}]  # fmt: skip
            return reply(call("commit_round", notes=notes, directives=changes),
                         call("finish", summary="drops re-opened"))  # fmt: skip
        return reply(call("finish", summary="done"))


class FleetBench:
    """One SQLite store, the fleet agent and the loop's setup over them."""

    def __init__(self, path: Path, model: FleetModel, s: PortfolioSpec | None = None) -> None:
        self.spec = s or spec()
        self.model = model
        self.clock = FakeClock(START)
        self.agent = agent(self.spec)
        self.store: EventStore = open_store(path, self.clock, writer=True)
        self.tree = compile_tree(AGENT, self.agent.tasks)
        listed = {t.spec.name: t for t in self.agent.tools if isinstance(t, ToolDecl)}
        kinds = {f"{AGENT}.{k.name}": k for k in self.agent.artifacts}
        self.setup = LoopSetup(
            store=self.store,
            owner=OWNER,
            agent=self.agent,
            tree=self.tree,
            registry=ToolRegistry(self.agent, listed, ()),
            table=bind_aliases((), "fake", {"generate": model}),
            instructions="You work a portfolio.",
            request="Work the portfolio.",
            briefs={"lead": BRIEF},
            artifacts=ArtifactStore(kinds),
            kinds=kinds,
            definition_hash=DEFINITION,
        )
        self.executor = StepExecutor(self.store, OWNER, self.clock)

    def run(self, budget: Mapping[str, int] | None = None) -> DriveResult:
        payload: dict[str, Any] = {
            "agent": AGENT, "cause": "request", "definition_hash": DEFINITION,
            "budget": dict(budget or {"model_turns": 400, "tool_calls": 400,
                                      "active_ms": 10**9}),
        }  # fmt: skip
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=RUN),))
        return drive(self.setup, self.executor, RUN)

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def events(self, type_: str | None = None) -> list[Event]:
        return [e for e in self.view().events if type_ is None or e.type == type_]

    def lead_view(self) -> TaskView:
        from aksha.runtime.tasks.types import InstanceRef

        return TaskView(self.view(), self.tree, InstanceRef(RUN, "lead"))

    def review(self) -> Mapping[str, Any]:
        return review(self.lead_view(), self.spec)

    def turns(self, task: str | None = None) -> int:
        """Model requests answered, for one Task id or all."""
        return sum(1 for r in self.model.requests
                   if task is None or _instance(r.step_id)[0] == task)  # fmt: skip

    def close(self) -> None:
        self.store.close()
