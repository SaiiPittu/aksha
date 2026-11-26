"""``check(gate_input, registry)``: the one checker every tool call passes (ADR-P11 D1 to D5).

Layers in the fixed order of D3, the first refusing layer winning:

0. budget left (not counted, so the stop policy can raise the budget interrupt cleanly);
1. the tool is registered;
2. it is in the agent's list (implicit tools included) and inside the run ceiling;
3. it is in the Task's list;
4. it survives stop-policy narrowing (only ``reserved`` tools, ``finish`` and ``ask``, are
   never narrowed away);
5. its arguments validate against the published schema;
6. every argument rule on it holds;
7. route checks: the rules declared with ``role="recipient"`` (the recipient check) and,
   for an exec tool, a sandbox backend strong enough.

``check`` is pure and never raises: any exception is ``Refuse("gate_error")`` at the layer
it happened in. There is no mode, flag or setting that skips a layer, and every layer
only removes: none can allow what an earlier one refused.

Headless (D12, decision 26, option B): after every layer has run, :func:`resolve_ask`
turns an allow whose route is ``approval`` into the route the tool takes without approval
(``outbox`` for an external effect, ``dispatch`` otherwise). It is the one place the flag
is read; it never touches a refusal, so every deny holds headless exactly as attended.
"""

from __future__ import annotations

from collections.abc import Callable

from aksha.runtime.definitions.types import Agent
from aksha.runtime.policy.permissions import agent_tools, route_of, task_tools
from aksha.runtime.policy.rules import broken_rule
from aksha.runtime.policy.types import (
    Allow,
    ArgumentRule,
    GateInput,
    Permissions,
    RefusalCode,
    Refuse,
    StopPolicyNarrowing,
)
from aksha.runtime.sandbox.types import strong_enough
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.loader_added import IMPLICIT_TOOLS
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolSpec


def check(g: GateInput, registry: ToolRegistry) -> Allow | Refuse:
    """Allow the call with a route, or refuse it with a code, a layer and a reason."""
    layer = 0
    try:
        for number, step in _LAYERS:
            layer = number
            refused = step(g, registry)
            if refused is not None:
                return refused
        spec = registry.get(g.call.name).spec
        return resolve_ask(Allow(route_of(spec)), g.headless, spec)
    except Exception as exc:  # fail closed: a bug in a helper is a refusal, never an allow
        msg = f"the policy gate failed at layer {layer}: {type(exc).__name__}"
        return Refuse("gate_error", layer, msg, counted=True, detail=(type(exc).__name__,))


def resolve_ask(a: Allow, headless: bool, tool: ToolSpec) -> Allow:
    """Headless never asks: route ``approval`` becomes ``outbox`` or ``dispatch`` (D12).

    Every other allow, and every attended allow, is returned unchanged.
    """
    if not headless or a.route != "approval":
        return a
    return Allow("outbox" if tool.effect == "external" else "dispatch", a.counted)


def _budget(g: GateInput, registry: ToolRegistry) -> Refuse | None:
    """Layer 0: refuse, uncounted, once any budget dimension is spent."""
    if not g.meter.spent:
        return None
    spent = tuple(sorted(g.meter.exhausted))
    msg = f"the budget is spent ({', '.join(spent)}); no more tool calls this turn"
    return Refuse("budget_exhausted", 0, msg, counted=False, detail=spent)


def _listed(g: GateInput, registry: ToolRegistry) -> Refuse | None:
    """Layers 1 to 4, through :func:`permitted`."""
    return permitted(g.call.name, g.agent, g.task, g.ceiling, g.narrowing, registry)


def _schema(g: GateInput, registry: ToolRegistry) -> Refuse | None:
    """Layer 5: the arguments match the published schema; the refusal names every path."""
    error = registry.check_arguments(g.call.name, g.call.arguments)
    if error is None:
        return None
    return Refuse("schema", 5, error.message, counted=True, detail=error.paths)


def _argument_rules(g: GateInput, registry: ToolRegistry) -> Refuse | None:
    """Layer 6: every rule with role ``argument`` on the tool holds."""
    rules = tuple(r for r in g.ceiling.rules_for(g.call.name) if r.role == "argument")
    return _first_broken(rules, g, "argument_rule", 6)


def _route_checks(g: GateInput, registry: ToolRegistry) -> Refuse | None:
    """Layer 7: every ``recipient`` rule holds; an exec tool has a strong enough sandbox."""
    spec = registry.get(g.call.name).spec
    rules = tuple(r for r in g.ceiling.rules_for(g.call.name) if r.role == "recipient")
    refused = _first_broken(rules, g, "recipient_not_listed", 7)
    if refused is not None or spec.effect != "exec":
        return refused
    need = spec.sandbox.min_strength if spec.sandbox is not None else "os-jail"
    box = g.sandbox
    if box is not None and box.available and strong_enough(box.strength, need):
        return None
    found = "none" if box is None else f"{box.backend} ({box.strength})"
    why = "" if box is None or box.available else f", unavailable: {box.reason}"
    msg = f"{g.call.name} needs a {need} sandbox; found {found}{why}"
    return Refuse("sandbox_unavailable", 7, msg, counted=True, detail=(need,))


_LAYERS: tuple[tuple[int, Callable[[GateInput, ToolRegistry], Refuse | None]], ...] = (
    (0, _budget),
    (1, _listed),
    (5, _schema),
    (6, _argument_rules),
    (7, _route_checks),
)
"""The layers in their fixed order, each with the number a ``gate_error`` reports."""


def permitted(
    name: str,
    agent: Agent,
    task: Task,
    ceiling: Permissions,
    narrowing: StopPolicyNarrowing,
    registry: ToolRegistry,
) -> Refuse | None:
    """Layers 1 to 4, which need no arguments: None when ``name`` may be called on ``task``.

    The permission matrix uses this too, so the matrix and the gate cannot disagree.
    """
    if name not in registry.names():
        return Refuse("unknown_tool", 1, f"no tool named {name!r}", counted=True)
    if name not in agent_tools(agent) or not (ceiling.allows(name) or name in IMPLICIT_TOOLS):
        msg = f"{name} is not in this agent's tool list or this run's ceiling"
        return Refuse("not_allowed", 2, msg, counted=True)
    if name not in task_tools(task):
        return Refuse("not_in_task", 3, f"{name} is not a tool of Task {task.id!r}", counted=True)
    if not narrowing.keeps(name) and not registry.is_reserved(name):
        by = narrowing.by or "a stop policy"
        msg = f"{name} is not available this turn: narrowed by {by}"
        return Refuse("narrowed", 4, msg, counted=True, detail=(by,))
    return None


def _first_broken(
    rules: tuple[ArgumentRule, ...], g: GateInput, code: RefusalCode, layer: int
) -> Refuse | None:
    """A ``code`` refusal at ``layer`` naming the first of ``rules`` that fails, or None."""
    broken = broken_rule(rules, g.call.arguments, g.sources)
    if broken is None:
        return None
    rule, why = broken
    return Refuse(code, layer, f"{rule.reason}: {why}", counted=True, detail=(rule.label,))
