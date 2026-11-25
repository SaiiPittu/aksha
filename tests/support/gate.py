"""A gate test agent with one tool of every effect class, and a ``GateInput`` builder.

The agent is built straight into a ``ToolRegistry`` without the loader, so the gate tests
need no system of record, sandbox backend or model adapter to exist. Every tool body is a
spy: it records that it ran, so a test can show a refused call ran no body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from aksha.runtime.api import (
    Agent,
    ApprovalRule,
    ArtifactKind,
    DeclaredInterrupt,
    EffectSpec,
    Ok,
    SandboxNeeds,
    Task,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)
from aksha.runtime.events.envelope import Event
from aksha.runtime.policy.events import boundary_of, decision_event
from aksha.runtime.policy.types import (
    Allow,
    ArgumentRule,
    GateInput,
    MeterReading,
    Permissions,
    Refuse,
    SourceVersion,
    StopPolicyNarrowing,
)
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolCall

OBJECT = {"type": "object"}


@dataclass
class Spy:
    """Records every body call by tool name."""

    calls: list[str] = field(default_factory=list)

    def body(self, name: str) -> Any:
        def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
            self.calls.append(name)
            return ToolOutcome(Ok({"tool": name}))

        return run


def _spec(name: str, effect: Any, schema: Mapping[str, Any] = OBJECT, **kw: Any) -> ToolSpec:
    return ToolSpec(name=name, owner="probe", effect=effect, description=name, schema=schema, **kw)


FETCH_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "minLength": 1}},
    "required": ["url"],
    "additionalProperties": False,
}
SEND_SCHEMA = {
    "type": "object",
    "properties": {"document": {"type": "string"}, "recipient_id": {"type": "string"}},
    "required": ["document", "recipient_id"],
    "additionalProperties": False,
}
WRITE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
    "required": ["path", "text"],
}


def specs() -> tuple[ToolSpec, ...]:
    """One tool per effect class the gate routes differently."""
    return (
        _spec("fetch", "read", FETCH_SCHEMA),
        _spec("write_file", "store", WRITE_SCHEMA),
        _spec(
            "send",
            "external",
            SEND_SCHEMA,
            external=EffectSpec("mail", ("/document", "/recipient_id"), "reconcile"),
            approval=ApprovalRule(facts=lambda view, args: {"to": str(args["recipient_id"])}),
        ),
        _spec("post", "external", external=EffectSpec(None, (), "at_most_once")),
        _spec("run", "exec", sandbox=SandboxNeeds(min_strength="container")),
        _spec("classify", "model", model_alias="classify.kind"),
    )


SEND_TO_LISTED = ArgumentRule(
    tool="send",
    path="/recipient_id",
    matcher="id_in_source",
    value="people",
    reason="send only to a listed person",
    role="recipient",
)
NO_GIT = ArgumentRule(
    tool="write_file",
    path="/path",
    matcher="path_not_under",
    value=".git",
    reason="never write inside .git",
)


def agent(spy: Spy) -> Agent:
    """The gate test agent: every tool listed, four Tasks with different lists."""
    decls = tuple(ToolDecl(s, None if s.effect == "model" else spy.body(s.name)) for s in specs())
    return Agent(
        name="probe",
        purpose="Exercise every layer of the policy gate.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(ArtifactKind("note", OBJECT),),
        tools=(*decls, ToolUse("save_artifact")),
        tasks=(
            Task("gather", tools=("fetch", "save_artifact"), writes=("note",)),
            Task(
                "act",
                tools=("fetch", "write_file", "send", "post"),
                interrupt=DeclaredInterrupt("input", "question"),
            ),
            Task("build", tools=("run", "classify")),
            Task("idle"),
        ),
    )


def registry(a: Agent) -> ToolRegistry:
    """``a``'s registry, built without the loader's refusals."""
    listed = {t.spec.name: t for t in a.tools if isinstance(t, ToolDecl)}
    return ToolRegistry(a, listed, ())


def task(a: Agent, task_id: str) -> Task:
    """The top-level Task ``task_id`` of ``a``."""
    return next(t for t in a.tasks if t.id == task_id)


PEOPLE = SourceVersion(name="people", version="v3", ids=frozenset({"P-1", "P-2"}))
CONTAINER = Availability(backend="docker", strength="container", available=True)

GOOD_ARGS: Mapping[str, Mapping[str, Any]] = {
    "fetch": {"url": "https://example.com/a"},
    "write_file": {"path": "src/app.py", "text": "x"},
    "send": {"document": "doc:1@v1", "recipient_id": "P-1"},
    "post": {},
    "run": {},
    "classify": {},
    "finish": {"summary": "done"},
    "ask": {"question": "which one?"},
    "save_artifact": {"kind": "note", "content": {}},
}
"""Arguments that pass every schema and every rule, by tool."""


def gate_input(
    a: Agent,
    task_id: str,
    name: str,
    args: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> GateInput:
    """A ``GateInput`` that allows ``name`` on ``task_id`` unless ``overrides`` say otherwise."""
    from aksha.runtime.policy.permissions import ceiling_of

    base = GateInput(
        call=ToolCall(name, GOOD_ARGS.get(name, {}) if args is None else args),
        agent=a,
        task=task(a, task_id),
        ceiling=ceiling_of(a, rules=(SEND_TO_LISTED, NO_GIT)),
        narrowing=StopPolicyNarrowing(),
        meter=MeterReading(),
        sources={"people": PEOPLE},
        sandbox=CONTAINER,
    )
    return replace(base, **overrides)


def permissions(*tools: str, rules: tuple[ArgumentRule, ...] = ()) -> Permissions:
    """A ``Permissions`` value from tool names."""
    return Permissions(frozenset(tools), rules)


def append_decision(
    store: EventStore,
    owner: str,
    decision: Allow | Refuse,
    call: ToolCall,
    *,
    run: str,
    instance: str,
    step: str,
) -> Event:
    """What the step executor (ADR-P02) will do with a decision: append its one event."""
    event = decision_event(decision, call, run=run, instance=instance, step=step)
    (stored,) = store.append(owner, boundary_of(decision), [event])
    return stored
