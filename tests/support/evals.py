"""Helpers for the eval tests: the repository's fixtures, a store opener, one-episode runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aksha.app import eval_store_opener
from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.criteria import REGISTRY
from aksha.evals.environment import Environment
from aksha.evals.faults import FaultProfile
from aksha.evals.grade import Verdict, grade
from aksha.evals.key import AnswerKey, load_key
from aksha.evals.ports import AgentUnderTest
from aksha.evals.world import World, load_world

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures"
SUITES = REPO / "evals" / "suites"
OPEN = eval_store_opener()


def world(name: str = "stillroom") -> World:
    return load_world(name, FIXTURES)


def key(name: str = "stillroom") -> AnswerKey:
    return load_key(world(name))


def case(**kw: Any) -> EvalCase:
    base: dict[str, Any] = {
        "id": "t-case",
        "kind": "guard",
        "origin": "hand_written",
        "source": "requirement: tests",
        "category": "capability",
        "agents": ("scripted",),
        "worlds": ("stillroom",),
    }
    base.update(kw)
    return EvalCase(**base)


def run_episode(
    agent: AgentUnderTest,
    out: Path,
    *,
    w: World | None = None,
    c: EvalCase | None = None,
    rollout: int = 0,
    seed: int = 0,
    faults: dict[str, FaultProfile] | None = None,
) -> EpisodeRecord:
    w = w or world()
    c = c or case(worlds=(w.name,))
    env = Environment(w, open_store=OPEN, out=out, seed=seed, faults=faults)
    h = env.reset(c, rollout, agent=agent.name, mode=agent.mode)
    env.run(h, agent)
    return env.finalize(h)


def graded(rec: EpisodeRecord, c: EvalCase, w: World | None = None) -> Verdict:
    w = w or world(str(rec.meta["world"]))
    return grade(rec, c, load_key(w), w, REGISTRY)
