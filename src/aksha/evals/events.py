"""The event types evals write: ``episode.*`` in an episode's store, ``eval.*`` in results.

Both are registered as extension types (:meth:`Catalogue.register_extension`), under the
namespaces ``episode`` and ``eval``, with the core's transaction boundaries: an episode's
start is U1, each tool step's result is U2, an approval is a resolution (U4), a change set
is a source change (U8) and the end is U3. A suite's start, each graded episode and its end
follow the same three boundaries in the results store.

An episode's store holds what the agent under test did, through the episode's tools; the
grader never reads that store directly, only the bundle exported from it. The results store
holds verdicts; the runner never writes into the store under test (ADR-P22, events).
Payloads hold no floats (the hash chain refuses them): rates are recomputed from counts.
"""

from __future__ import annotations

from typing import Any

from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue, EventTypeSpec

_STR = {"type": "string"}
_STR1 = {"type": "string", "minLength": 1}
_INT = {"type": "integer"}
_ANY: dict[str, Any] = {}
CITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": _STR1,
        "exact": _STR1,
        "start": {"type": ["integer", "null"]},
        "end": {"type": ["integer", "null"]},
    },
    "required": ["file", "exact"],
}


def _spec(name: str, boundary: str, props: dict[str, Any], required: list[str]) -> EventTypeSpec:
    return EventTypeSpec(
        name=name,
        schema={"type": "object", "properties": props, "required": required},
        writer=name.split(".", 1)[0],
        boundaries=frozenset({boundary}),
    )


EPISODE_TYPES: tuple[EventTypeSpec, ...] = (
    _spec(
        "episode.started",
        "U1",
        {
            "case": _STR1,
            "world": _STR1,
            "agent": _STR1,
            "rollout": _INT,
            "seed": _INT,
            "opening": _STR,
            "mode": {"enum": ["fake", "scripted", "replay", "live", "selftest"]},
        },
        ["case", "world", "agent", "rollout", "seed", "mode"],
    ),
    _spec(
        "episode.tool_called",
        "U2",
        {
            "tool": _STR1,
            "args": {"type": "object"},
            "ok": {"type": "boolean"},
            "result_hash": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "attempt": _INT,
        },
        ["tool", "args", "ok"],
    ),
    _spec(
        "episode.output_saved",
        "U2",
        {
            "ref": _STR1,
            "kind": _STR1,
            "key": _STR1,
            "version": {"type": "integer", "minimum": 1},
            "content": {"type": "object"},
            "citations": {"type": "array", "items": CITATION_SCHEMA},
        },
        ["ref", "kind", "key", "version", "content", "citations"],
    ),
    _spec(
        "episode.relation_saved",
        "U2",
        {
            "type": _STR1,
            "src": _STR1,
            "dst": _STR1,
            "excerpt": {"anyOf": [CITATION_SCHEMA, {"type": "null"}]},
            "label": {"type": ["string", "null"]},
        },
        ["type", "src", "dst"],
    ),
    _spec(
        "episode.flagged",
        "U2",
        {"kind": _STR1, "text": _STR, "file": {"type": ["string", "null"]}},
        ["kind", "text"],
    ),
    _spec(
        "episode.approval_requested",
        "U2",
        {
            "approval_id": _STR1,
            "target_ref": _STR1,
            "target_version": _INT,
            "action": _STR1,
            "recipient": {"type": ["string", "null"]},
        },
        ["approval_id", "target_ref", "target_version", "action"],
    ),
    _spec(
        "episode.approval_decided",
        "U4",
        {"approval_id": _STR1, "granted": {"type": "boolean"}, "by": _STR1},
        ["approval_id", "granted", "by"],
    ),
    _spec(
        "episode.effect_sent",
        "U2",
        {
            "effect_key": _STR1,
            "recipient": _STR1,
            "action": _STR1,
            "target_ref": {"type": ["string", "null"]},
            "target_version": {"type": ["integer", "null"]},
            "approval_id": {"type": ["string", "null"]},
        },
        ["effect_key", "recipient", "action"],
    ),
    _spec(
        "episode.change_applied",
        "U8",
        {
            "change_set": _STR1,
            "added": {"type": "array", "items": _STR},
            "replaced": {"type": "array", "items": _STR},
            "restart": {"type": "boolean"},
        },
        ["change_set", "added", "replaced", "restart"],
    ),
    _spec(
        "episode.ended",
        "U3",
        {
            "outcome": {"enum": ["finished", "capped", "crashed"]},
            "reason": _STR,
            "restarts": _INT,
        },
        ["outcome", "reason"],
    ),
)

_CRITERION = {
    "type": "object",
    "properties": {
        "id": _STR1,
        "layer": _STR1,
        "must": {"type": "boolean"},
        "outcome": {"enum": ["pass", "fail", "ungradeable"]},
        "cause": {"type": ["string", "null"]},
        "reading": {"type": ["string", "null"]},
        "fabricated": {"type": "boolean"},
        "evidence": {"type": "array", "items": _STR},
    },
    "required": ["id", "layer", "must", "outcome"],
}

EVAL_TYPES: tuple[EventTypeSpec, ...] = (
    _spec(
        "eval.suite_started",
        "U1",
        {
            "suite": _STR1,
            "seed": _INT,
            "mode": _STR1,
            "modes": {"type": "object"},
            "agents": {"type": "object"},
            "conditions": {"type": "object"},
        },
        ["suite", "seed", "mode"],
    ),
    _spec(
        "eval.selftest_recorded",
        "U2",
        {
            "world": _STR1,
            "bundle": _STR1,
            "ok": {"type": "boolean"},
            "problems": {"type": "array", "items": _STR},
            "killed": {"type": "array", "items": _STR},
            "must_criteria": {"type": "array", "items": _STR},
        },
        ["world", "bundle", "ok", "problems"],
    ),
    _spec(
        "eval.episode_graded",
        "U2",
        {
            "case": _STR1,
            "world": _STR1,
            "cluster": _STR1,
            "rollout": _INT,
            "agent": _STR1,
            "mode": _STR1,
            "kind": _STR1,
            "origin": _STR1,
            "source": _STR1,
            "category": _STR1,
            "verdict": {"enum": ["pass", "fail", "ungradeable"]},
            "xfail": {"type": "boolean"},
            "criteria": {"type": "array", "items": _CRITERION},
            "steps": _INT,
            "elapsed_ms": _INT,
            "trace_hash": _STR1,
            "bundle_path": _STR,
            "trace_path": _STR,
            "conditions": {"type": "object"},
            "arm": {"type": "object"},
            "metrics": {"type": "object"},
        },
        ["case", "world", "rollout", "agent", "verdict", "criteria", "steps", "trace_hash"],
    ),
    _spec(
        "eval.relation_checked",
        "U2",
        {
            "case": _STR1,
            "relation": {"enum": ["invariance", "directional"]},
            "base": _STR1,
            "variant": _STR1,
            "rollout": _INT,
            "criteria": {"type": "array", "items": _CRITERION},
        },
        ["case", "relation", "base", "variant", "criteria"],
    ),
    _spec(
        "eval.suite_ended",
        "U3",
        {"episodes": _INT, "verdict": {"enum": ["pass", "fail", "ungradeable"]}},
        ["episodes", "verdict"],
    ),
)


def episode_catalogue() -> Catalogue:
    """The core types plus ``episode.*``, for one episode's store."""
    cat = Catalogue(CORE_EVENT_TYPES)
    for spec in EPISODE_TYPES:
        cat.register_extension(spec, extension="episode")
    return cat


def results_catalogue() -> Catalogue:
    """The core types plus ``eval.*``, for a suite's results store."""
    cat = Catalogue(CORE_EVENT_TYPES)
    for spec in EVAL_TYPES:
        cat.register_extension(spec, extension="eval")
    return cat
