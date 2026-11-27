"""``commit_round``: the supervisor's one write, notes, plan and directives in one step.

Nothing the supervisor decides takes effect until this call: its body checks every bound
in code and returns one save per changed directive plus the round, so the agent loop
commits them in the step's one transaction or, on any refusal, commits none
(super-agent design section 4.1). The model proposes admissions; code refuses a commit:

1. ``plan_required``: the first round carries no plan (goal, tier rule, budget split).
2. ``key_unknown``, ``key_twice``: a key not in the item listing, or named twice.
3. ``not_triaged``: a decision about an item whose triage result does not exist yet.
4. ``tier_not_admissible``: an admission to any tier but the deep one. Triage is every
   item's, by code; a later tier keyed from a person's selection is never the
   supervisor's to admit.
5. ``reason_required``: a drop or deferral with no reason (120 characters at most).
6. ``tier_cap``: more items admitted to the deep tier than its cap.
7. ``batch_cap``: more items newly admitted by one commit than the batch cap.
8. ``over_budget``: (admitted plus audited) x the tier's effort is above the deep budget.

The audit sample is computed here, by code, from the drops the commit leaves (a seeded
hash, ``board.audit_sample``), and recorded on the round; the supervisor cannot choose it.
The commit returns no reads: a decision is not built from a version, it records one
(``basis``), so a changed triage result shows on the board instead of invalidating it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import (
    ArtifactSnapshot,
    Err,
    Ok,
    SaveRequest,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)

from .board import audit_sample, with_state
from .kinds import DIRECTIVE, NOTES_SCHEMA, PLAN_SCHEMA, ROUND, STATES
from .spec import REASON_CHARS, PortfolioSpec

TOOL = "commit_round"


def commit_spec(spec: PortfolioSpec) -> ToolSpec:
    """The tool's spec for ``spec.agent``: it reads the listing, cards, directives, rounds."""
    change = {
        "type": "object",
        "required": ["keys", "state", "reason"],
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "state": {"enum": list(STATES)},
            "tier": {"type": ["string", "null"]},
            "priority": {"type": "integer"},
            "reason": {"type": "string", "maxLength": REASON_CHARS},
        },
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "required": ["notes", "directives"],
        "properties": {
            "notes": NOTES_SCHEMA,
            "plan": PLAN_SCHEMA,
            "directives": {"type": "array", "items": change},
        },
        "additionalProperties": False,
    }
    reads = {spec.full(spec.items[0]), spec.full(spec.basis), spec.full(DIRECTIVE),
             spec.full(ROUND)}  # fmt: skip
    return ToolSpec(
        TOOL,
        spec.agent,
        "store",
        "Commit one round of decisions: your notes, the plan (required on the first round), "
        f"and a directive per item you decide: admit it to the {spec.deep.id} tier or drop "
        "or defer it with a reason. Code enforces the tier cap, the deep budget and the "
        "batch cap, and refuses the whole round when one is broken.",
        schema,
        reads_kinds=frozenset(reads),
        writes_kinds=frozenset({spec.full(DIRECTIVE), spec.full(ROUND)}),
    )


def _err(code: str, message: str, path: str = "") -> ToolOutcome:
    return ToolOutcome(Err(code, message, paths=(path,) if path else ()))


def _latest(ctx: ToolContext, kind: str) -> tuple[ArtifactSnapshot, ...]:
    return () if ctx.artifacts is None else ctx.artifacts.latest(kind)


def _listing(ctx: ToolContext, spec: PortfolioSpec) -> tuple[str, ...]:
    kind, path = spec.items
    found = [s for s in _latest(ctx, spec.full(kind)) if s.ref.key is None]
    if not found:
        return ()
    value: Any = found[0].content
    for part in path.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    listed = value if isinstance(value, list | tuple) else ()
    return tuple(sorted({str(i.get("key", i.get("id")) if isinstance(i, Mapping) else i)
                         for i in listed}))  # fmt: skip


def commit_round(spec: PortfolioSpec, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """The body (module doc): every refusal in order, then the saves of one round."""
    keys = set(_listing(ctx, spec))
    basis = {str(s.ref.key): str(s.ref) for s in _latest(ctx, spec.full(spec.basis))
             if s.ref.key is not None}  # fmt: skip
    before = {str(s.ref.key): s.content for s in _latest(ctx, spec.full(DIRECTIVE))
              if s.ref.key is not None}  # fmt: skip
    rounds = [s for s in _latest(ctx, spec.full(ROUND)) if s.ref.key is None]
    last = rounds[0].content if rounds else None
    number = 1 if last is None else int(last["round"]) + 1
    plan = args.get("plan") or (last["plan"] if last is not None else None)
    if plan is None:
        return _err("plan_required", "the first round carries the plan: goal, tier rule and "
                    "budget split", "/plan")  # fmt: skip
    after: dict[str, dict[str, Any]] = {k: dict(v) for k, v in before.items()}
    named: set[str] = set()
    for i, change in enumerate(args.get("directives", ())):
        state = str(change["state"])
        tier = change.get("tier")
        reason = str(change.get("reason") or "").strip()
        decided = [str(key) for key in change["keys"]]
        for key in decided:
            if key not in keys:
                return _err("key_unknown", f"{key!r} is not an item", f"/directives/{i}/keys")
            if key in named:
                return _err("key_twice", f"{key!r} is decided twice in one round",
                            f"/directives/{i}/keys")  # fmt: skip
            if key not in basis:
                return _err("not_triaged", f"{key!r} has no triage result to decide on",
                            f"/directives/{i}/keys")  # fmt: skip
            named.add(key)
        if state == "admitted" and tier != spec.deep.id:
            return _err("tier_not_admissible", f"only the {spec.deep.id} tier is admitted by a "
                        f"directive, not {tier!r}", f"/directives/{i}/tier")  # fmt: skip
        if state != "admitted" and not reason:
            return _err("reason_required", f"a {state} item needs a reason",
                        f"/directives/{i}/reason")  # fmt: skip
        for key in decided:
            after[key] = {
                "key": key, "state": state, "tier": spec.deep.id if state == "admitted" else None,
                "priority": int(change.get("priority", 0)), "reason": reason, "basis": basis[key],
                "round": number,
            }  # fmt: skip
    admitted = with_state(after, "admitted")
    if len(admitted) > spec.deep.cap:
        return _err("tier_cap", f"{len(admitted)} items admitted to the {spec.deep.id} tier; "
                    f"its cap is {spec.deep.cap}", "/directives")  # fmt: skip
    newly = [k for k in admitted if before.get(k, {}).get("state") != "admitted"]
    if len(newly) > spec.batch_cap:
        return _err("batch_cap", f"{len(newly)} items newly admitted; one round admits at "
                    f"most {spec.batch_cap}", "/directives")  # fmt: skip
    sample = audit_sample(with_state(after, "dropped"), last.get("audit", ()) if last else (), spec)
    deep = set(admitted) | set(sample)
    need = len(deep) * spec.deep.effort
    if need > spec.budget_turns:
        extra = len(set(sample) - set(admitted))
        return _err("over_budget", f"{len(admitted)} admitted and {extra}"
                    f" audited items at {spec.deep.effort} turns each need {need} turns; the "
                    f"{spec.deep.id} budget is {spec.budget_turns}", "/directives")  # fmt: skip
    changed = sorted(k for k in named if dict(before.get(k, {})) != after[k])
    saves = [SaveRequest(spec.full(DIRECTIVE), k, after[k]) for k in changed]
    content = {
        "round": number,
        "notes": args["notes"],
        "plan": plan,
        "admitted": list(admitted),
        "dropped": list(with_state(after, "dropped")),
        "deferred": list(with_state(after, "deferred")),
        "audit": sample,
        "changed": changed,
    }
    saves.append(SaveRequest(spec.full(ROUND), None, content))
    receipt = {
        "round": number, "changed": len(changed), "admitted": len(admitted),
        "dropped": len(content["dropped"]), "audit": sample, "deep_turns_committed": need,
        "deep_budget_turns": spec.budget_turns,
    }  # fmt: skip
    return ToolOutcome(Ok(receipt), saves=tuple(saves))


def commit_tool(spec: PortfolioSpec) -> ToolDecl:
    """``commit_round`` bound to ``spec``, for the agent's tool list."""

    def body(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return commit_round(spec, ctx, args)

    return ToolDecl(commit_spec(spec), body)
