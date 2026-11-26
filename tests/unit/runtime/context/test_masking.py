"""Observation masking (ADR-P08 D5, S2, decision 32), through assembly over a real log."""

import random
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.context.assembly import assemble_context
from aksha.runtime.context.masking import render_stub
from aksha.runtime.context.types import Assembled, ContextOverLimit, ContextPolicy, Masked
from aksha.runtime.stop_policy.types import Prep
from aksha.runtime.tasks.types import InstanceRef
from tests.support.context import INPUTS, RUN, TOOLS, Log, result_view, tree

EDIT = InstanceRef(RUN, "edit", "parser")
SMALL = ContextPolicy(
    masking=True,
    high_water_chars=6_000, low_water_chars=4_500, context_limit_chars=40_000,
    read_block_cap_chars=8_000, read_cap_chars=4_000,
)  # fmt: skip
TREE = tree()


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    yield log
    log.close()


def build(log: Log, turn: int, policy: ContextPolicy = SMALL, **kw: object) -> Assembled:
    out = assemble_context(
        log.view(), TREE, EDIT, turn=turn, inputs=INPUTS, prep=Prep(), policy=policy,
        tools=TOOLS, **kw,  # type: ignore[arg-type]
    )  # fmt: skip
    assert isinstance(out, Assembled)
    return out


def tool_texts(a: Assembled) -> list[str]:
    return [m.text or "" for m in a.request.messages if m.role == "tool"]


def test_unmasked_context_byte_identical(log: Log) -> None:
    log.turn_with_results(EDIT, 1, (("read_file", {"path": "a.py"}, "a" * 500),))
    on = build(log, 2)
    off = build(log, 2, replace(SMALL, masking=False))
    assert on.plan.masked == ()
    assert on.request.as_json() == off.request.as_json()
    assert on.request.context_hash == off.request.context_hash


def test_masking_hides_superseded_reads_first_then_the_oldest(log: Log) -> None:
    # Four reads of the same file, then four distinct test runs, then three recent pairs.
    for t in range(1, 5):
        log.turn_with_results(
            EDIT, t, (("read_file", {"path": "parser.py"}, f"v{t} " + "r" * 900),)
        )
    for t in range(5, 9):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "f" * 900),))
    for t in range(9, 12):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "k" * 900),))
    a = build(log, 12)
    plan = a.plan
    assert plan.raw_chars > SMALL.high_water_chars
    reasons = [(m.step.rsplit("/", 2)[-2], m.reason) for m in plan.masked]
    # the three earlier reads are superseded by the fourth and go first
    assert reasons[:3] == [("t01", "superseded"), ("t02", "superseded"), ("t03", "superseded")]
    assert all(r == "aged" for _, r in reasons[3:])
    assert len(plan.masked) == 8  # every pair but the newest three: still above the low mark
    assert plan.final_chars < plan.raw_chars - 5_000
    masked_turns = {m.since_turn for m in plan.masked}
    assert masked_turns == {12}
    # never the newest three pairs
    kept = [m.step for m in plan.masked]
    assert not any(s.endswith(("t09/c01", "t10/c01", "t11/c01")) for s in kept)
    assert "Recitation for Task edit [parser]" in (a.request.messages[-1].text or "")


def test_mask_never_splits_pair(log: Log) -> None:
    for t in range(1, 10):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "x" * 900),))
    a = build(log, 10)
    msgs = a.request.messages
    calls = [c.call_id for m in msgs if m.role == "assistant" for c in m.tool_calls]
    answered = [m.call_id for m in msgs if m.role == "tool"]
    assert calls == answered
    for m in a.plan.masked:
        assert render_stub(m) in tool_texts(a)
    # the call and its arguments stay for an aged result
    args = [dict(c.arguments) for m in msgs if m.role == "assistant" for c in m.tool_calls]
    assert args == [{"n": t} for t in range(1, 10)]


def test_exempt_tool_results_are_never_masked(log: Log) -> None:
    for t in range(1, 10):
        log.turn_with_results(EDIT, t, (("recall", {"n": t}, "m" * 900),))
    a = build(log, 10, replace(SMALL, exempt_tools=frozenset({"recall"})))
    assert a.plan.masked == ()


def test_masking_is_monotone(log: Log) -> None:
    for t in range(1, 9):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "z" * 900),))
    first = build(log, 9)
    assert first.plan.masked
    # turn 9 runs, recording what its context hid; its own result is small
    log.model_turn(EDIT, 9, (("run_tests", {"n": 9}),), masked=tuple(sorted(first.plan.steps())))
    log.tool_result(EDIT, 9, 1, "run_tests", result_view("ok"))
    second = build(log, 10)
    assert first.plan.steps() <= second.plan.steps()
    assert {m.since_turn for m in second.plan.masked if m.step in first.plan.steps()} == {9}


def test_masking_is_monotone_over_generated_turns(tmp_path: Path) -> None:
    rng = random.Random(7)
    log = Log(tmp_path / "gen.sqlite")
    log.start()
    try:
        seen: frozenset[str] = frozenset()
        for t in range(1, 16):
            if t > 1:
                plan = build(log, t).plan
                assert seen <= plan.steps()
                recent = {f"{RUN}/{EDIT.instance}/t{n:02d}/c01" for n in range(t - 3, t)}
                assert not (plan.steps() & recent)
                seen = plan.steps()
            tool = rng.choice(("read_file", "run_tests"))
            args = {"path": rng.choice(("a.py", "b.py"))} if tool == "read_file" else {"n": t}
            log.model_turn(EDIT, t, ((tool, args),), masked=tuple(sorted(seen)))
            log.tool_result(EDIT, t, 1, tool, result_view("q" * rng.randint(100, 1_500)))
    finally:
        log.close()


def test_superseded_store_call_arguments_are_stubbed(log: Log) -> None:
    body = "w" * 1_500
    for t, v in ((1, 1), (2, 2), (3, 3)):
        log.model_turn(EDIT, t, (("write_file", {"path": "p.py", "body": body}),))
        step = log.tool_result(EDIT, t, 1, "write_file", result_view(f"saved v{v}"))
        log.save(f"coder.file/parser@v{v}", {"body": body}, step=step, writer=("edit", "parser"))
    for t in range(4, 8):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "e" * 900),))
    a = build(log, 8)
    by_step = {m.step: m for m in a.plan.masked}
    first = by_step[f"{RUN}/{EDIT.instance}/t01/c01"]
    assert (first.reason, first.arguments, first.refs) == (
        "superseded",
        True,
        ("coder.file/parser@v1",),
    )
    assert first.superseded_by == ("coder.file/parser@v3", f"{RUN}/{EDIT.instance}/t02/c01")
    calls = [c for m in a.request.messages if m.role == "assistant" for c in m.tool_calls]
    assert dict(calls[0].arguments) == {
        "masked": "[arguments hidden: saved coder.file/parser@v1, superseded by "
        f"coder.file/parser@v3, {RUN}/{EDIT.instance}/t02/c01]"
    }
    assert calls[0].name == "write_file"  # the call itself stays


def test_stale_read_is_masked_and_the_change_notice_comes_last(log: Log) -> None:
    other = f"{RUN}/plan/t01/c01"
    log.save("coder.plan@v1", {"files": ["parser"], "goal": "old goal"}, step=other)
    old = ArtifactRef.parse("coder.plan@v1").as_input()
    log.model_turn(EDIT, 1, (("read_plan", {}),))
    log.tool_result(EDIT, 1, 1, "read_plan", result_view('{"goal":"old goal"}'), reads=(old,))
    log.turn_with_results(EDIT, 2, (("run_tests", {"n": 2}, "ok"),))
    # another writer replaces the plan: the result read at t01 is stale (decision 32)
    log.save("coder.plan@v2", {"files": ["parser"], "goal": "new goal"}, step=f"{RUN}/plan/t02/c01")
    a = build(log, 3)
    (m,) = a.plan.masked
    assert (m.reason, m.changed) == ("stale", ("coder.plan",))
    texts = tool_texts(a)
    assert texts[0] == (
        "[superseded: coder.plan changed, the current version is in the change notice below]"
    )
    assert "new goal" not in texts[0]  # never written into the old position
    notice = [m.text or "" for m in a.request.messages if "Change notice" in (m.text or "")]
    assert len(notice) == 1 and "coder.plan@v2" in notice[0] and "new goal" in notice[0]
    order = [m.text or "" for m in a.request.messages]
    assert order.index(notice[0]) > max(
        i for i, m in enumerate(a.request.messages) if m.role == "tool"
    )
    assert [s.now for s in a.stale] == [ArtifactRef.parse("coder.plan@v2")]
    reads = [e.payload["input"] for e in a.facts if e.type == "read.recorded"]
    assert {"kind": "artifact", "id": "coder.plan", "version": "v2"} in reads


def test_own_newer_save_supersedes_but_is_not_stale(log: Log) -> None:
    log.save("coder.plan@v1", {"goal": "a"}, step=f"{RUN}/plan/t01/c01")
    old = ArtifactRef.parse("coder.plan@v1").as_input()
    log.model_turn(EDIT, 1, (("read_plan", {}),))
    log.tool_result(EDIT, 1, 1, "read_plan", result_view("plan"), reads=(old,))
    log.model_turn(EDIT, 2, (("save_plan", {"goal": "b"}),))
    step = log.tool_result(EDIT, 2, 1, "save_plan", result_view("saved"))
    log.save("coder.plan@v2", {"goal": "b"}, step=step, writer=("edit", "parser"))
    a = build(log, 3)
    assert a.plan.masked == () and a.stale == ()  # under the watermark, nothing hidden


def test_recovery_masks_harder(log: Log) -> None:
    for t in range(1, 6):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "r" * 600),))
    normal = build(log, 6)
    harder = build(log, 6, recovery=True)
    assert normal.plan.masked == ()
    assert len(harder.plan.masked) == 4 and harder.plan.recovery
    assert not any(m.step.endswith("t05/c01") for m in harder.plan.masked)


def test_over_the_limit_nothing_is_sent(log: Log) -> None:
    for t in range(1, 5):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, "o" * 7_000),))
    policy = replace(SMALL, context_limit_chars=20_000, read_block_cap_chars=5_000)
    out = assemble_context(
        log.view(), TREE, EDIT, turn=5, inputs=INPUTS, prep=Prep(), policy=policy, tools=TOOLS
    )
    assert isinstance(out, ContextOverLimit)
    assert (out.reason, out.limit) == ("context_limit", 20_000)
    assert out.chars > 20_000 and out.plan is not None and out.plan.over_limit


def test_stub_names_step_size_refs_and_spans() -> None:
    m = Masked("R-0001/edit[a]/t01/c01", "fetch", 9_000, ("k/a@v1",), ("EX-0001",), "aged", 4)
    text = render_stub(m)
    for part in ("fetch", "R-0001/edit[a]/t01/c01", "9000 characters", "k/a@v1", "EX-0001",
                 "repeat the call"):  # fmt: skip
        assert part in text
