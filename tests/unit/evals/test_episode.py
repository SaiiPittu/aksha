import os
from pathlib import Path

import pytest

from aksha.evals.agents.scripted import ScriptedAgent
from aksha.evals.bundle import read_bundle
from aksha.evals.environment import Environment, rollout_seed
from aksha.evals.episode import guard_path
from aksha.evals.errors import EvalError, PathRefused
from aksha.evals.faults import FaultProfile, FaultRule, decide
from aksha.evals.state import fold
from tests.support.evals import OPEN, case, run_episode, world

KF17 = {
    "file": "web/kelvra.example/product",
    "exact": "EUR 249 per unit",
    "start": 671,
    "end": 687,
}


@pytest.mark.parametrize(
    "rel",
    [
        "../key.json",
        "../spec.yaml",
        "../manifest.json",
        "../world.lock.json",
        "key.json",
        "notes/../../key.json",
        "/etc/passwd",
        "C:/x",
        "notes\\C1.md",
        "",
    ],
)
def test_no_tool_path_reaches_a_key(tmp_path: Path, rel: str) -> None:
    (tmp_path / "sources").mkdir()
    with pytest.raises(PathRefused):
        guard_path(tmp_path / "sources", rel)


def test_a_symlink_out_of_the_tool_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "sources").mkdir()
    (tmp_path / "secret.txt").write_text("answer")
    os.symlink(tmp_path / "secret.txt", tmp_path / "sources" / "link.txt")
    with pytest.raises(PathRefused):
        guard_path(tmp_path / "sources", "link.txt")


def test_the_episode_world_copy_holds_only_sources_and_web_pages(tmp_path: Path) -> None:
    rec = run_episode(ScriptedAgent([("list", "")]), tmp_path)
    root = rec.sources.parent
    assert sorted(p.name for p in root.iterdir()) == ["sources", "web"]
    held = ("key.json", "spec.yaml", "manifest.json", "web.json")
    assert not any(p.name in held for p in root.rglob("*"))
    # the web declaration (which pages are noise) sits beside the copy, out of reach
    assert (root.parent / "web.json").is_file()


def test_an_agent_asking_for_the_key_is_refused_and_recorded(tmp_path: Path) -> None:
    rec = run_episode(ScriptedAgent([("read", "../key.json")]), tmp_path)
    state = fold(rec.events)
    assert state.tool_calls[0]["error"] == "path_refused"
    assert state.ended is not None and state.ended["outcome"] == "crashed"


def test_saves_version_and_skip_unchanged_content(tmp_path: Path) -> None:
    rec = run_episode(
        ScriptedAgent(
            [
                ("save", "note", "a", {"x": 1}, [KF17]),
                ("save", "note", "a", {"x": 1}, [KF17]),
                ("save", "note", "a", {"x": 2}),
            ]
        ),
        tmp_path,
    )
    state = fold(rec.events)
    assert [o.version for o in state.history["note/a"]] == [1, 2]
    assert state.outputs["note/a"].content == {"x": 2}


def test_approvals_bind_the_current_version_and_sends_reach_the_ledger(tmp_path: Path) -> None:
    rec = run_episode(
        ScriptedAgent(
            [
                ("save", "packet", "C1", {"v": 1}),
                ("approve", "packet/C1", "send", "counsel@fixture.example"),
                ("send", "counsel@fixture.example", "send", "packet/C1"),
            ]
        ),
        tmp_path,
    )
    state = fold(rec.events)
    (approval,) = state.approvals.values()
    assert (approval.target_ref, approval.target_version, approval.granted) == (
        "packet/C1",
        1,
        True,
    )
    assert rec.ledger == (
        {
            "effect_key": "send:counsel@fixture.example:packet/C1@v1",
            "recipient": "counsel@fixture.example",
            "action": "send",
            "target_ref": "packet/C1",
            "target_version": 1,
            "approval_id": "A-0001",
        },
    )


def test_an_approval_needs_a_saved_target(tmp_path: Path) -> None:
    rec = run_episode(ScriptedAgent([("approve", "packet/C9", "send")]), tmp_path)
    ended = fold(rec.events).ended
    assert ended is not None and "target_missing" in ended["reason"]


def test_the_bundle_round_trips(tmp_path: Path) -> None:
    rec = run_episode(
        ScriptedAgent([("read", KF17["file"]), ("save", "note", "a", {"x": 1}, [KF17])]), tmp_path
    )
    assert rec.path is not None
    back = read_bundle(rec.path)
    assert back.events == rec.events and back.ledger == rec.ledger
    assert back.reads == rec.reads and back.sources == rec.sources.resolve()
    assert back.trace_hash == rec.trace_hash
    assert back.meta["store_verified"] is True and back.meta["world_lock_problems"] == []


def test_a_crash_is_kept_and_graded_not_raised(tmp_path: Path) -> None:
    rec = run_episode(ScriptedAgent([("save", "note", "a", {"x": 1}), ("fail", "boom")]), tmp_path)
    state = fold(rec.events)
    assert state.ended == {"outcome": "crashed", "reason": "RuntimeError: boom", "restarts": 0}
    assert "note/a" in state.outputs


def test_floats_are_refused_at_save(tmp_path: Path) -> None:
    rec = run_episode(ScriptedAgent([("save", "note", "a", {"x": 0.5})]), tmp_path)
    ended = fold(rec.events).ended
    assert ended is not None and ended["outcome"] == "crashed"


def test_fault_decisions_are_pure_and_seeded() -> None:
    rule = FaultRule("read_source", probability_ppm=500_000)
    first = [decide(rule, 7, f"s{i}", 1) for i in range(200)]
    assert first == [decide(rule, 7, f"s{i}", 1) for i in range(200)]
    assert 60 < sum(first) < 140
    assert first != [decide(rule, 8, f"s{i}", 1) for i in range(200)]
    assert decide(FaultRule("x", probability_ppm=1_000_000), 0, "s", 1)
    assert not decide(FaultRule("x", probability_ppm=0), 0, "s", 1)


def test_a_fault_switch_fails_the_named_tool(tmp_path: Path) -> None:
    profile = FaultProfile("all-reads", (FaultRule("read_source", code="timeout"),))
    c = case(faults="all-reads")
    rec = run_episode(
        ScriptedAgent([("read", KF17["file"])]), tmp_path, c=c, faults={"all-reads": profile}
    )
    call = fold(rec.events).tool_calls[0]
    assert (call["ok"], call["error"]) == (False, "timeout")


def test_rollout_seeds_differ_by_rollout_not_by_world() -> None:
    assert rollout_seed(0, "c", 0) == rollout_seed(0, "c", 0)
    assert rollout_seed(0, "c", 0) != rollout_seed(0, "c", 1)


def test_a_change_step_applies_its_set_and_restarts_the_agent(tmp_path: Path) -> None:
    import json
    import shutil

    from aksha.evals.case import ChangeStep
    from aksha.evals.world import load_world, write_lock
    from tests.support.evals import FIXTURES

    root = tmp_path / "fx"
    shutil.copytree(FIXTURES / "worlds" / "stillroom", root / "worlds" / "stillroom")
    folder = root / "worlds/stillroom/changes/demo"
    folder.mkdir(parents=True)
    (folder / "note.md").write_text("new note")
    (folder / "changeset.json").write_text(json.dumps({"added": {"notes/new.md": "note.md"}}))
    write_lock(root / "worlds/stillroom")
    w = load_world("stillroom", root)
    c = case(change_steps=(ChangeStep("episode.output_saved:packet/C1", "demo", restart=True),))

    class Resumer:
        name, mode = "resumer", "scripted"
        runs = 0

        def run(self, episode):  # type: ignore[no-untyped-def]
            Resumer.runs += 1
            episode.save("packet", "C1", {"v": 1})
            if Resumer.runs > 1:
                episode.read_source("notes/new.md")

    env = Environment(w, open_store=OPEN, out=tmp_path / "out")
    h = env.reset(c, 0, agent="resumer")
    env.run(h, Resumer())
    rec = env.finalize(h)
    state = fold(rec.events)
    assert Resumer.runs == 2
    assert state.changes == [
        {"change_set": "demo", "added": ["notes/new.md"], "replaced": [], "restart": True}
    ]
    assert [o.version for o in state.history["packet/C1"]] == [1]
    assert state.tool_calls[-1]["ok"] is True
    assert rec.meta["restarts"] == 1


def test_an_episode_cannot_record_after_its_end(tmp_path: Path) -> None:
    w = world()
    env = Environment(w, open_store=OPEN, out=tmp_path)
    h = env.reset(case(), 0, agent="x")
    env.finalize(h)
    with pytest.raises(EvalError):
        h.episode.flag("x", "y")
