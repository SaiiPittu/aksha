"""The environment: reset a world for one episode, run the agent, apply changes, finalize.

``reset`` gives each episode its own folder with a private copy of ``sources/`` and a fresh
event store, so one episode cannot leave state for the next. ``run`` hands the agent its
:class:`~aksha.evals.episode.Episode` and catches what the agent leaves unhandled: an
exception ends the episode ``crashed`` with its type, and the episode is still graded,
because a crash with its work kept is something the criteria judge, not the runner.

A change step (``after = "episode.output_saved:recommendation/C1"``) applies its change set
to the world copy once a matching event is recorded, records ``episode.change_applied``,
and, with ``restart``, stops the agent and runs it again on the same store, which is the
change-and-recovery demo's restart. The restart is raised once the tool call that recorded
the matching event has recorded everything it records (:func:`aksha.evals.episode.tool`),
so no send is left without its ``episode.effect_sent`` and no approval without its
decision. A change set's targets pass the same path guard as a tool path.

``finalize`` ends the episode, checks the store's hash chain, exports the chain beside the
events so the gate can check one against the other, and writes the bundle before any
grader runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from aksha.evals.bundle import EpisodeRecord, chain_rows, identity_json, write_bundle
from aksha.evals.case import ChangeStep, EvalCase
from aksha.evals.episode import Episode, guard_path
from aksha.evals.errors import EvalError, PathRefused, Restart
from aksha.evals.events import episode_catalogue
from aksha.evals.faults import FaultProfile
from aksha.evals.ports import AgentUnderTest, StoreOpener
from aksha.evals.world import (
    World,
    load_change_set,
    load_world,
    lock_digest,
    renaming,
    verify_lock,
)
from aksha.runtime.canonical import thaw
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.storage.ports import EventStore

MAX_RESTARTS = 3
"""A change step restarts the agent at most this many times per episode."""


def rollout_seed(seed: int, case: str, rollout: int) -> int:
    """The seed of one rollout: the same in every world.

    With the episode naming a fault step by the base world's path (``fault_path``), a
    variant world's renamed files draw the same fault decisions as their base, so a
    faulted invariance case compares like with like.
    """
    digest = hashlib.sha256(f"{seed}|{case}|{rollout}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


@dataclass
class EpisodeHandle:
    """One episode in flight: its folder, store, tools and timing."""

    case: EvalCase
    world: World
    rollout: int
    folder: Path
    store: EventStore
    episode: Episode
    agent: str
    mode: str
    lock_problems: list[str]
    started_ms: int
    pending: list[ChangeStep] = field(default_factory=list)


class Environment:
    """Runs episodes of cases in one world. ``out`` is the suite's output folder."""

    def __init__(
        self,
        world: World,
        *,
        open_store: StoreOpener,
        out: Path,
        seed: int = 0,
        faults: dict[str, FaultProfile] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        """Bind the world, the store opener and the output folder."""
        self.world = world
        self.open_store = open_store
        self.out = out
        self.seed = seed
        self.faults = faults or {}
        self._ms = clock_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self._to_base = _base_paths(world)

    def reset(
        self, case: EvalCase, rollout: int, *, agent: str, mode: str = "fake"
    ) -> EpisodeHandle:
        """A fresh folder, a private copy of ``sources/`` and a new store, then start."""
        folder = self.out / case.id / self.world.name / f"r{rollout}"
        if folder.exists():
            shutil.rmtree(folder)
        (folder / "world").mkdir(parents=True)
        shutil.copytree(self.world.sources, folder / "world" / "sources")
        store = self.open_store(folder / "store.sqlite", episode_catalogue())
        seed = rollout_seed(self.seed, case.id, rollout)
        handle_box: list[EpisodeHandle] = []

        def on_event(event: Event) -> None:
            if handle_box:
                self._maybe_change(handle_box[0], event)

        episode = Episode(
            store=store,
            owner=f"episode:{case.id}",
            root=folder / "world",
            case_id=case.id,
            world=self.world.name,
            rollout=rollout,
            seed=seed,
            opening=case.opening,
            faults=self.faults.get(case.faults) if case.faults else None,
            on_event=on_event,
            fault_path=self._fault_path,
        )
        store.append(
            episode.owner,
            "U1",
            [
                _new(
                    "episode.started",
                    {
                        "case": case.id,
                        "world": self.world.name,
                        "agent": agent,
                        "rollout": rollout,
                        "seed": seed,
                        "opening": case.opening,
                        "mode": mode,
                    },
                )
            ],
        )
        handle = EpisodeHandle(
            case=case,
            world=self.world,
            rollout=rollout,
            folder=folder,
            store=store,
            episode=episode,
            agent=agent,
            mode=mode,
            lock_problems=verify_lock(self.world),
            started_ms=self._ms(),
            pending=list(case.change_steps),
        )
        handle_box.append(handle)
        return handle

    def run(self, h: EpisodeHandle, agent: AgentUnderTest) -> EpisodeHandle:
        """Run the agent until it returns, restarting it on a change step's ``restart``."""
        while True:
            try:
                agent.run(h.episode)
            except Restart:
                h.episode.restarts += 1
                if h.episode.restarts > MAX_RESTARTS:
                    h.episode.end("crashed", "restart_limit")
                    return h
                continue
            except Exception as e:  # the agent's failure is graded, not raised
                h.episode.end("crashed", f"{type(e).__name__}: {e}"[:500])
                return h
            return h

    def _fault_path(self, path: str) -> str:
        """A tool path as the base world names it (a renamed file maps back to its base)."""
        return self._to_base.get(path, path)

    def _maybe_change(self, h: EpisodeHandle, event: Event) -> None:
        if not h.pending or event.type == "episode.change_applied":
            return
        payload = thaw(event.payload)
        ref = str(payload.get("ref") or payload.get("target_ref") or "")
        for step in list(h.pending):
            etype, _, want = step.after.partition(":")
            if event.type == etype and (not want or ref.startswith(want)):
                h.pending.remove(step)
                self.apply_change(h, step)
                if step.restart:
                    raise Restart(step.change_set)

    def apply_change(self, h: EpisodeHandle, step: ChangeStep) -> None:
        """Copy the change set's files into the world copy and record the change."""
        cs = load_change_set(self.world, step.change_set)
        for rel, src in {**cs.added, **cs.replaced}.items():
            try:
                target = guard_path(h.episode.sources, rel)
            except PathRefused as e:
                raise EvalError(
                    "change_set_broken", f"{cs.name}: target {rel!r} refused: {e.message}"
                ) from None
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        h.store.append(
            h.episode.owner,
            "U8",
            [
                _new(
                    "episode.change_applied",
                    {
                        "change_set": cs.name,
                        "added": sorted(cs.added),
                        "replaced": sorted(cs.replaced),
                        "restart": step.restart,
                    },
                )
            ],
        )

    def finalize(self, h: EpisodeHandle) -> EpisodeRecord:
        """End the episode, check the chain, export the bundle and close the store."""
        ep = h.episode
        with contextlib.suppress(EvalError):  # already ended: crashed or restart limit
            ep.end("finished")
        broken = h.store.verify()
        stored = list(h.store.read(ep.owner))
        events = [
            {"seq": e.seq, "type": e.type, "payload": thaw(e.payload)}
            for e in stored
            if e.type.startswith("episode.")
        ]
        ident = h.store.identity()
        chain = chain_rows(stored, ident.genesis_hash)
        head = h.store.head()
        h.store.close()
        meta = {
            "case": h.case.id,
            "world": h.world.name,
            "cluster": h.world.cluster,
            "rollout": h.rollout,
            "agent": h.agent,
            "mode": h.mode,
            "seed": ep.seed,
            "store_verified": broken is None,
            "store_break": None if broken is None else str(broken),
            "chain_head": head[1],
            "store_identity": identity_json(
                ident.store_id, ident.created_at, ident.chain_version, ident.genesis_hash
            ),
            "world_lock": lock_digest(h.world),
            "world_lock_problems": h.lock_problems,
            "steps": ep.steps,
            "elapsed_ms": max(0, self._ms() - h.started_ms),
            "restarts": ep.restarts,
        }
        rec = EpisodeRecord(
            meta=meta,
            events=tuple(_renumber(events)),
            ledger=tuple(ep.receiver.rows),
            reads=dict(ep.reads),
            sources=ep.sources,
            chain=tuple(chain),
        )
        bundle = write_bundle(rec, h.folder / "bundle")
        return replace(rec, path=bundle)


def _renumber(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{**e, "seq": i + 1} for i, e in enumerate(events)]


def _new(type_: str, payload: dict[str, object]) -> NewEvent:
    return NewEvent(type_, payload)


def _base_paths(world: World) -> dict[str, str]:
    """Variant path to base path for a variant world's renamed files; empty for a base."""
    if not world.variant_of:
        return {}
    try:
        base = load_world(world.variant_of, world.root.parent.parent)
        paths = renaming(base, world).paths
    except EvalError:
        return {}
    return {variant: base_path for base_path, variant in paths.items()}
