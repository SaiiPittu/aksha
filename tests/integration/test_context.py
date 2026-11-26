"""Context assembly and compaction end to end (ADR-P08 Tests), on SQLite and the step executor.

A test plays the agent loop's part for a few turns: prepare the context, run the model
step with the context's facts, record the tool results, then restart and check that the
rebuilt context hashes the same and that recorded steps (the model's and the summariser's)
are returned without a model call.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.adapters.models.scripted import ScriptedGenerationModel
from aksha.runtime.artifacts.lineage import recorded_reads
from aksha.runtime.artifacts.types import ArtifactRef, WriterId
from aksha.runtime.context.assembly import assemble_context
from aksha.runtime.context.notices import rebuild_brief_for
from aksha.runtime.context.prepare import Prepared, prepare_context
from aksha.runtime.context.types import (
    Assembled,
    AssemblyInputs,
    ContextOverLimit,
    ContextPolicy,
    SummaryPolicy,
)
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import StepMismatch
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.types import StepId
from aksha.runtime.invalidation.commands import apply_source_change
from aksha.runtime.invalidation.rebuild import open_rebuild_runs
from aksha.runtime.models.routing import AliasTable, bind_aliases
from aksha.runtime.models.steps import generation_step
from aksha.runtime.models.types import GenerationResult, ModelAlias, ToolCall
from aksha.runtime.stop_policy.types import Prep, StopCandidate
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Task
from tests.support.context import (
    INPUTS,
    OWNER,
    RUN,
    TOOLS,
    Log,
    result_view,
    texts,
    tree,
)
from tests.support.invalidation import Bench, FakeQueries, FakeSource, record
from tests.support.stop import decide
from tests.support.tasks import TaskLog, demo_tree, ref

EDIT = InstanceRef(RUN, "edit", "parser")
SUMMARY = ModelAlias("generate.summary", "generation")
POLICY = ContextPolicy(
    masking=True,
    high_water_chars=6_000, low_water_chars=4_500, context_limit_chars=40_000,
    read_block_cap_chars=8_000, read_cap_chars=4_000,
)  # fmt: skip


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    log.save("coder.plan@v1", {"files": ["parser"], "goal": "fix the parser"},
             step=f"{RUN}/plan/t01/c01")  # fmt: skip
    yield log
    log.close()


def table(main: ScriptedGenerationModel, summariser: ScriptedGenerationModel) -> AliasTable:
    return bind_aliases((SUMMARY,), "scripted", {"generate": main, "generate.summary": summariser})


def prepared(
    log: Log, executor: StepExecutor, t: AliasTable, turn: int, policy: ContextPolicy = POLICY
) -> Prepared:
    return prepare_context(
        log.store, OWNER, executor, t, tree(("coder.plan",)), EDIT, turn=turn, inputs=INPUTS,
        prep=Prep(), policy=policy, tools=TOOLS,
    )  # fmt: skip


def run_turn(log: Log, executor: StepExecutor, t: AliasTable, turn: int, **kw: object) -> Assembled:
    """One turn as the loop runs it: context, model step with its facts, tool results."""
    p = prepared(log, executor, t, turn, **kw)  # type: ignore[arg-type]
    a = p.context
    assert isinstance(a, Assembled)
    out = generation_step(
        executor, StepId(RUN, EDIT.instance, turn=turn), t, a.request,
        started=a.started_fields(), facts=lambda _: a.facts,
    )  # fmt: skip
    assert out.result is not None
    for position, call in enumerate(out.result.tool_calls, start=1):
        step = StepId(RUN, EDIT.instance, turn=turn, call=position)
        if log.store.by_dedup_key(OWNER, f"sdone:{step}") is None:
            log.tool_result(EDIT, turn, position, call.name, result_view("r" * 1_200))
    return a


def reply(n: int) -> GenerationResult:
    return GenerationResult(None, (ToolCall(f"c{n}", "run_tests", {"n": n}),), "tool_calls")


def recorded_hash(log: Log, step: StepId) -> str:
    started = log.store.by_dedup_key(OWNER, f"sstart:{step}")
    assert started is not None
    return str(started.payload["args_hash"])


def test_context_hash_stable_across_restart(log: Log, tmp_path: Path) -> None:
    main = ScriptedGenerationModel([reply(n) for n in range(1, 4)])
    t = table(main, ScriptedGenerationModel([]))
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in (1, 2, 3):
        run_turn(log, executor, t, turn)
    assert len(main.calls) == 3
    # restart: a new store handle and executor; rebuild turn 2's context after turn 3 ran
    log.store.close()
    log.store = Log(tmp_path / "aksha.sqlite").store
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in (1, 2, 3):
        again = prepared(log, executor, t, turn).context
        assert isinstance(again, Assembled)
        step = StepId(RUN, EDIT.instance, turn=turn)
        assert again.request.context_hash == recorded_hash(log, step)
        out = generation_step(executor, step, t, again.request, started=again.started_fields())
        assert out.step.replayed
    assert len(main.calls) == 3  # no model step was re-spent


def test_a_different_context_is_refused_on_resume(log: Log) -> None:
    t = table(ScriptedGenerationModel([reply(1)]), ScriptedGenerationModel([]))
    executor = StepExecutor(log.store, OWNER, log.clock)
    run_turn(log, executor, t, 1)
    changed = prepare_context(
        log.store, OWNER, executor, t, tree(("coder.plan",)), EDIT, turn=1,
        inputs=replace(INPUTS, instructions="Different instructions."), prep=Prep(),
        policy=POLICY, tools=TOOLS,
    ).context  # fmt: skip
    assert isinstance(changed, Assembled)
    with pytest.raises(StepMismatch):
        generation_step(executor, StepId(RUN, EDIT.instance, turn=1), t, changed.request)


def test_step_started_records_size_renderings_and_masks(log: Log) -> None:
    main = ScriptedGenerationModel([reply(n) for n in range(1, 10)])
    t = table(main, ScriptedGenerationModel([]))
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in range(1, 10):
        a = run_turn(log, executor, t, turn)
    started = log.store.by_dedup_key(OWNER, f"sstart:{StepId(RUN, EDIT.instance, turn=9)}")
    assert started is not None
    p = started.payload
    assert p["context_chars"] == a.context_chars
    (rendered,) = p["rendered"]
    assert (rendered["ref"], rendered["mode"], rendered["spans"]) == ("coder.plan@v1", "full", ())
    assert p["masked"] and set(p["masked"]) == a.plan.steps()


def test_rendered_reads_are_recorded_reads(log: Log) -> None:
    t = table(ScriptedGenerationModel([reply(1)]), ScriptedGenerationModel([]))
    run_turn(log, StepExecutor(log.store, OWNER, log.clock), t, 1)
    view = log.view()
    plan = ArtifactRef.parse("coder.plan@v1").as_input()
    assert plan in recorded_reads(view, RUN, EDIT.instance)
    (read,) = [e for e in view.events if e.type == "read.recorded" and e.instance == EDIT.instance]
    assert (read.payload["via"], read.step) == ("context", f"{RUN}/{EDIT.instance}/t01")


def test_summary_replayed_not_recomputed(log: Log, tmp_path: Path) -> None:
    once = SummaryPolicy(trigger_chars=5_000, keep_recent_pairs=2, max_per_instance=1)
    policy = replace(POLICY, summary=once)
    main = ScriptedGenerationModel([reply(n) for n in range(1, 9)])
    summariser = ScriptedGenerationModel([GenerationResult("Ran the tests 5 times; all red.")])
    t = table(main, summariser)
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in range(1, 8):
        run_turn(log, executor, t, turn, policy=policy)
    assert len(summariser.calls) == 1
    (compacted,) = [e for e in log.view().events if e.type == "context.compacted"]
    through = compacted.payload["through_turn"]
    assert compacted.payload["summary"] == "Ran the tests 5 times; all red."
    turn_after = int(str(compacted.step).split("/t", 1)[1].split("/", 1)[0])
    a = prepared(log, executor, t, turn_after, policy).context
    assert isinstance(a, Assembled) and a.compaction is not None
    body = list(texts(a.request.messages))
    assert any("Summary of turns 1 to" in s for s in body)
    calls = [c for m in a.request.messages if m.role == "assistant" for c in m.tool_calls]
    assert [dict(c.arguments)["n"] for c in calls] == list(range(through + 1, turn_after))
    # restart: the recorded summary is installed; the summariser is not called again
    log.store.close()
    log.store = Log(tmp_path / "aksha.sqlite").store
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in range(1, 8):
        again = prepared(log, executor, t, turn, policy)
        assert isinstance(again.context, Assembled)
        step = StepId(RUN, EDIT.instance, turn=turn)
        assert again.context.request.context_hash == recorded_hash(log, step)
    assert len(summariser.calls) == 1


def test_summary_is_never_an_artifact(log: Log) -> None:
    policy = replace(POLICY, summary=SummaryPolicy(trigger_chars=5_000, keep_recent_pairs=2))
    summariser = ScriptedGenerationModel([GenerationResult("summary text")])
    t = table(ScriptedGenerationModel([reply(n) for n in range(1, 9)]), summariser)
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in range(1, 8):
        run_turn(log, executor, t, turn, policy=policy)
    saved = [e for e in log.view().events if e.type == "artifact.saved"]
    assert [e.payload["ref"] for e in saved] == ["coder.plan@v1"]


def test_summary_limit_per_instance(log: Log) -> None:
    policy = replace(
        POLICY, summary=SummaryPolicy(trigger_chars=3_000, keep_recent_pairs=1, max_per_instance=1)
    )
    summariser = ScriptedGenerationModel([GenerationResult("first"), GenerationResult("second")])
    t = table(ScriptedGenerationModel([reply(n) for n in range(1, 12)]), summariser)
    executor = StepExecutor(log.store, OWNER, log.clock)
    for turn in range(1, 11):
        run_turn(log, executor, t, turn, policy=policy)
    assert len(summariser.calls) == 1


def test_patent_context_has_no_research_text(log: Log) -> None:
    research = InstanceRef("R-0002", "discover", "comp04")
    log.start("R-0002", agent="research")
    log.model_turn(research, 1, (("fetch_page", {"url": "u"}),), text="RESEARCH-ONLY reasoning")
    log.tool_result(research, 1, 1, "fetch_page", result_view("RESEARCH-ONLY page text"))
    sibling = InstanceRef(RUN, "edit", "lexer")
    log.model_turn(sibling, 1, text="SIBLING-ONLY reasoning")
    log.model_turn(EDIT, 1, text="mine")
    a = assemble_context(
        log.view(), tree(("coder.plan",)), EDIT, turn=2, inputs=INPUTS, prep=Prep(),
        policy=POLICY, tools=TOOLS,
    )  # fmt: skip
    assert isinstance(a, Assembled)
    whole = "\n".join(texts(a.request.messages))
    assert "RESEARCH-ONLY" not in whole and "SIBLING-ONLY" not in whole and "mine" in whole


def test_context_order_keeps_the_stable_prefix_first(log: Log) -> None:
    log.model_turn(EDIT, 1, (("run_tests", {"n": 1}),), text="trying")
    log.tool_result(EDIT, 1, 1, "run_tests", result_view("1 failed"))
    prep = Prep(brief=("outer brief", "inner brief"), tools=frozenset({"run_tests"}),
                tool_choice="run_tests")  # fmt: skip
    a = assemble_context(
        log.view(), tree(("coder.plan",)), EDIT, turn=2, inputs=INPUTS, prep=prep,
        policy=POLICY, tools=TOOLS,
    )  # fmt: skip
    assert isinstance(a, Assembled)
    roles = [(m.role, (m.text or "")[:24]) for m in a.request.messages]
    assert roles[:4] == [
        ("system", "You edit code."),
        ("system", "outer brief"),
        ("system", "inner brief"),
        ("system", "Tool results and documen"),
    ]
    assert roles[4][1].startswith("Artifacts this Task read")
    assert roles[5] == ("user", "Fix bug 7.\n\nTask: Fix th")
    assert [r for r, _ in roles[6:]] == ["assistant", "tool"]
    assert [t.name for t in a.request.tools] == ["run_tests"]
    assert a.request.tool_choice == "run_tests"


def test_context_limit_fails_instance() -> None:
    tl, demo = TaskLog(), demo_tree()
    tl.started()
    decide(tl, demo, StopCandidate(RUN, "next"))
    scope = ref("scope")
    tiny = ContextPolicy(high_water_chars=100, low_water_chars=80, context_limit_chars=300,
                         read_block_cap_chars=200)  # fmt: skip
    big = AssemblyInputs(instructions="i" * 400)
    out = assemble_context(tl.view(), demo, scope, turn=1, inputs=big, prep=Prep(), policy=tiny)
    assert isinstance(out, ContextOverLimit) and out.reason == "context_limit"
    d = decide(tl, demo, StopCandidate(RUN, "overflow", scope, after_turn=1))
    assert d.pops == (("scope", "fail"),)
    assert ("task:scope", "fail") in d.trail


def test_rebuild_run_context_starts_from_the_brief(tmp_path: Path) -> None:
    b = Bench(tmp_path)
    try:
        writer = WriterId("p", "extract", "C1")
        b.start("R-0001")
        b.save("R-0001", "p.extract/C1", {"contributors": ["a"]}, reads=[record("K-C1")],
               writer=writer, instance="extract[C1]")  # fmt: skip
        b.end("R-0001")
        source, queries = FakeSource(), FakeQueries()
        source.change(b.owner, {"refs/K-C1": "2"})
        assert apply_source_change(b.store, b.owner, [source], "c1", run_query=queries).accepted
        (run,) = open_rebuild_runs(b.store, b.owner, b.ids)
        entered = {"cause": "rebuild", "task": "extract", "key": "C1",
                   "writer": writer.as_json()}  # fmt: skip
        b.store.append(b.owner, "U3", (NewEvent("task.entered", entered, run=run),))
        instance = InstanceRef(run, "extract", "C1")
        brief = rebuild_brief_for(b.view(), instance)
        assert brief is not None and str(brief.old) == "p.extract/C1@v1"
        demo = compile_tree("p", (Task("extract", done=lambda v: CheckResult(True)),))
        a = assemble_context(
            b.view(), demo, instance, turn=1, inputs=AssemblyInputs(request="Extract C1."),
            prep=Prep(), policy=ContextPolicy(),
        )  # fmt: skip
        assert isinstance(a, Assembled)
        (notice,) = [
            m.text or "" for m in a.request.messages if (m.text or "").startswith("Rebuild")
        ]
        assert "p.extract/C1@v1" in notice and '"contributors":["a"]' in notice
        assert "record:refs/K-C1@1 (moved) is now 2" in notice
        assert "Read these instead:" in notice and "record:refs/K-C1@2" in notice
        # the first request run of the same Task has no brief
        assert rebuild_brief_for(b.view(), InstanceRef("R-0001", "extract", "C1")) is None
    finally:
        b.close()
