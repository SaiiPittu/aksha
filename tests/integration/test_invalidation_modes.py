"""Invalidation is opt-in per agent (decision 38): ``off``, ``edit_guard``, ``notify``, ``rebuild``.

Each mode is recorded on ``run.started`` and read from the log by the invalidation view,
the masking plan and the agent loop. The rebuild bench (``tests.support.rebuild``) proves
the cascade modes: a source move under ``off`` or ``notify`` invalidates nothing and opens
no rebuild run, where ``rebuild`` (and a run that records no mode, written before the
setting) reopens exactly the work built on it. The masking bench proves that only
``notify`` and ``rebuild`` hide a stale tool result and announce the change. A small
editing agent proves the edit-time check: under ``edit_guard`` an edit of a file that
changed since the instance read it, and an overwrite of an existing file it never read,
are refused with the current text, and the same edits go through under ``off``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, ArtifactRef, InputRef
from aksha.runtime.context.assembly import assemble_context
from aksha.runtime.context.types import Assembled, ContextPolicy
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.invalidation.guard import EDIT_UNCHECKED, STALE_EDIT, UNREAD_OVERWRITE
from aksha.runtime.invalidation.modes import run_mode
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.projections.view import load_view
from aksha.runtime.stop_policy.types import Prep
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import InstanceRef, Task
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import (
    EditSpec,
    Err,
    Ok,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)
from tests.support.context import INPUTS, RUN, TOOLS, Log, result_view, tree
from tests.support.events import START
from tests.support.loop import Recorder, call, tools_reply
from tests.support.rebuild import Rig
from tests.support.tasks import has

# --- the cascade: off and notify open no rebuild run; rebuild does -------------------------


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path / "aksha.sqlite")
    yield r
    r.close()


def _built(rig: Rig, **extra: Any) -> None:
    rig.start(**extra)
    result = rig.drive()
    assert (result.state, result.outcome) == ("ended", "complete")


def _rebuild_runs(rig: Rig) -> list[Event]:
    return [e for e in rig.events("run.started") if e.payload["cause"] == "rebuild"]


@pytest.mark.parametrize("mode", ["off", "edit_guard", "notify"])
def test_a_source_move_opens_no_rebuild_run_unless_the_agent_opted_in(rig: Rig, mode: str) -> None:
    _built(rig, invalidation=mode)
    rig.approve("A-0001", "demo.bundle/C1@v1")
    rig.change("C1", "2")
    iv = invalidation_view(rig.view())
    assert iv.refs("invalidated") == () and iv.refs("dirty") == ()  # no stale marks
    assert rig.rebuild() == ()
    assert _rebuild_runs(rig) == []
    assert "approval.revoked" not in {e.type for e in rig.events()}
    assert open_reviews(rig.view()) == {}
    assert run_mode(rig.view(), "R-0001") == mode


@pytest.mark.parametrize("extra", [{"invalidation": "rebuild"}, {}])
def test_rebuild_and_a_run_that_records_no_mode_get_the_full_cascade(
    rig: Rig, extra: dict[str, str]
) -> None:
    _built(rig, **extra)
    rig.change("C1", "2")
    assert invalidation_view(rig.view()).refs("invalidated") == ("demo.extract/C1@v1",)
    (result,) = rig.rebuild()
    assert (result.state, result.outcome) == ("ended", "complete")
    (started,) = _rebuild_runs(rig)
    assert started.payload["invalidation"] == "rebuild"
    entered = [e.instance for e in rig.events("task.entered", result.run)]
    assert entered == ["extract[C1]", "compare[C1]", "bundle[C1]"]


def test_an_off_run_schedules_no_freshness_recheck(tmp_path: Path) -> None:
    """A recheck only feeds rebuilds: an ``off`` agent's reads schedule none (ADR-P25 D3)."""
    from dataclasses import replace

    counts: dict[str, int] = {}
    for mode in ("off", "rebuild"):
        r = Rig(tmp_path / f"{mode}.sqlite")
        r.setup = replace(r.setup, lifetimes={"refs": 3_600})
        _built(r, invalidation=mode)
        counts[mode] = sum(
            1 for e in r.events("timer.scheduled") if e.payload.get("cause") == "recheck"
        )
        r.close()
    assert counts == {"off": 0, "rebuild": 2}


# --- the context: only notify and rebuild mask a stale result -------------------------------

EDIT = InstanceRef(RUN, "edit", "parser")
SMALL = ContextPolicy(
    masking=True,
    high_water_chars=6_000, low_water_chars=4_500, context_limit_chars=40_000,
    read_block_cap_chars=8_000, read_cap_chars=4_000,
)  # fmt: skip


def _stale_plan_context(tmp_path: Path, **extra: Any) -> Assembled:
    log = Log(tmp_path / "ctx.sqlite")
    try:
        log.start(**extra)
        log.save("coder.plan@v1", {"goal": "old goal"}, step=f"{RUN}/plan/t01/c01")
        old = ArtifactRef.parse("coder.plan@v1").as_input()
        log.model_turn(EDIT, 1, (("read_plan", {}),))
        log.tool_result(EDIT, 1, 1, "read_plan", result_view('{"goal":"old goal"}'), reads=(old,))
        log.save("coder.plan@v2", {"goal": "new goal"}, step=f"{RUN}/plan/t02/c01")
        out = assemble_context(
            log.view(), tree(), EDIT, turn=2, inputs=INPUTS, prep=Prep(), policy=SMALL,
            tools=TOOLS,
        )  # fmt: skip
    finally:
        log.close()
    assert isinstance(out, Assembled)
    return out


@pytest.mark.parametrize("mode", ["notify", "rebuild"])
def test_notify_and_rebuild_mask_a_stale_result_and_announce_it(tmp_path: Path, mode: str) -> None:
    a = _stale_plan_context(tmp_path, invalidation=mode)
    (m,) = a.plan.masked
    assert m.reason == "stale"
    assert any("Change notice" in (x.text or "") for x in a.request.messages)


@pytest.mark.parametrize("mode", ["off", "edit_guard"])
def test_off_and_edit_guard_leave_old_results_and_announce_nothing(
    tmp_path: Path, mode: str
) -> None:
    a = _stale_plan_context(tmp_path, invalidation=mode)
    assert a.plan.masked == () and a.stale == ()
    assert not any("Change notice" in (x.text or "") for x in a.request.messages)
    texts = [x.text or "" for x in a.request.messages if x.role == "tool"]
    assert any("old goal" in t for t in texts)  # left in place, as a coding harness would


# --- the edit-time check -------------------------------------------------------------------

OWNER = "o1"
AGENT = "coder"
SOURCE = "files"
PATH_ARGS: Mapping[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": False,
}
EDIT_ARGS: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "old": {"type": "string"},
        "new": {"type": "string"},
    },
    "required": ["path", "old", "new"],
    "additionalProperties": False,
}
WRITE_ARGS: Mapping[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}, "text": {"type": "string"}},
    "required": ["path", "text"],
    "additionalProperties": False,
}


@dataclass
class Tree:
    """A working tree of text files, versioned by a counter per file; also the record reader."""

    files: dict[str, str] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    served: bool = True

    def put(self, path: str, text: str) -> str:
        self.files[path] = text
        self.versions[path] = self.versions.get(path, 0) + 1
        return self.version(path)

    def version(self, path: str) -> str:
        return f"v{self.versions[path]}"

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        path = record_id.removeprefix("file/")
        if source != SOURCE or path not in self.files:
            return None
        return ReturnedText(SOURCE, record_id, self.version(path), self.files[path])

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        return tuple(f"file/{p}" for p in sorted(self.files))

    def serves(self, source: str) -> bool:
        return self.served and source == SOURCE

    # --- the tool bodies ---

    def read_file(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        path = str(args["path"])
        found = self.read(SOURCE, f"file/{path}")
        if found is None:
            return ToolOutcome(Err("not_found", f"{path} does not exist"))
        return ToolOutcome(
            Ok({"path": path, "text": found.text}, reads=(_read(path, found.version),))
        )

    def edit_file(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        path, old, new = str(args["path"]), str(args["old"]), str(args["new"])
        text = self.files.get(path, "")
        if old not in text:
            return ToolOutcome(Err("no_match", f"{old!r} is not in {path}"))
        version = self.put(path, text.replace(old, new, 1))
        return ToolOutcome(Ok({"path": path, "edited": True}, reads=(_read(path, version),)))

    def write_file(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        path = str(args["path"])
        version = self.put(path, str(args["text"]))
        return ToolOutcome(Ok({"path": path, "written": True}, reads=(_read(path, version),)))


def _read(path: str, version: str) -> InputRef:
    return InputRef("record", f"file/{path}", version)


NOTE = ArtifactKind("note", {"type": "object"})
KINDS = {f"{AGENT}.note": NOTE}


def _coder(tree_: Tree) -> Agent:
    edits = EditSpec(SOURCE, "/path", collection="file")
    whole = EditSpec(SOURCE, "/path", collection="file", overwrite=True)
    specs = (
        ToolDecl(ToolSpec("read_file", AGENT, "read", "Read a file.", PATH_ARGS), tree_.read_file),
        ToolDecl(
            ToolSpec("edit_file", AGENT, "store", "Replace text in a file.", EDIT_ARGS,
                     edits=edits),
            tree_.edit_file,
        ),
        ToolDecl(
            ToolSpec("write_file", AGENT, "store", "Write a whole file.", WRITE_ARGS,
                     edits=whole),
            tree_.write_file,
        ),
    )  # fmt: skip
    return Agent(
        name=AGENT,
        purpose="Edit files.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(NOTE,),
        tools=(*specs, ToolUse("save_artifact")),
        tasks=(
            Task(
                "edit",
                tools=("read_file", "edit_file", "write_file", "save_artifact"),
                writes=("note",),
                done=has("note"),
            ),
        ),
        invalidation="edit_guard",
    )


class EditBench:
    """One run of the coding agent over ``Tree``; ``script`` is its reply per turn."""

    def __init__(self, path: Path, mode: str, script: Mapping[int, GenerationResult]) -> None:
        self.tree = Tree()
        self.tree.put("a.py", "x = 1\n")
        self.tree.put("b.py", "keep me\n")
        self.hooks: dict[int, Any] = {}
        self.script = script
        self.model = Recorder(rule=self.rule)
        clock = FakeClock(START)
        self.store = open_store(path, clock, writer=True)
        agent = _coder(self.tree)
        listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
        self.setup = LoopSetup(
            store=self.store, owner=OWNER, agent=agent, tree=compile_tree(AGENT, agent.tasks),
            registry=ToolRegistry(agent, listed, ()),
            table=bind_aliases((), "fake", {"generate": self.model}),
            instructions="Edit the files.", request="Change x to 2 in a.py.", reader=self.tree,
            artifacts=ArtifactStore(KINDS), kinds=KINDS,
        )  # fmt: skip
        self.executor = StepExecutor(self.store, OWNER, clock)
        payload = {"agent": AGENT, "cause": "request", "invalidation": mode}
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run="R-0001"),))

    def rule(self, request: GenerationRequest) -> GenerationResult:
        turn = StepId.parse(request.step_id).turn
        assert turn is not None
        hook = self.hooks.pop(turn, None)
        if hook is not None:
            hook()
        done = tools_reply(
            call("save_artifact", kind="note", content={"done": True}),
            call("finish", summary="done"),
        )
        return self.script.get(turn, done)

    def run(self) -> None:
        result = drive(self.setup, self.executor, "R-0001")
        assert result.state == "ended", result

    def code(self, turn: int) -> str | None:
        step = f"R-0001/edit/t{turn:02d}/c01"
        for e in load_view(self.store, OWNER).events:
            if e.type == "step.completed" and e.step == step:
                code = e.payload.get("code")
                return None if code is None else str(code)
        raise AssertionError(f"no step {step}")

    def shown(self, turn: int) -> str:
        """The text of the last tool result the model was shown on ``turn``."""
        for request in self.model.requests:
            if StepId.parse(request.step_id).turn == turn:
                return [m.text or "" for m in request.messages if m.role == "tool"][-1]
        raise AssertionError(f"no request on turn {turn}")


@pytest.fixture
def benches() -> Iterator[list[EditBench]]:
    """Every bench a test opens, closed after it so no store is left to the collector."""
    opened: list[EditBench] = []
    yield opened
    for bench in opened:
        bench.store.close()


def _bench(
    opened: list[EditBench], path: Path, mode: str, script: Mapping[int, GenerationResult]
) -> EditBench:
    bench = EditBench(path, mode, script)
    opened.append(bench)
    return bench


READ_A = tools_reply(call("read_file", path="a.py"))
EDIT_A = tools_reply(call("edit_file", path="a.py", old="x = 1", new="x = 2"))


def _someone_else_edits_a(bench: EditBench) -> None:
    bench.tree.put("a.py", "x = 1\ny = 3\n")  # a person adds a line between the turns


def test_edit_guard_refuses_a_stale_edit_with_the_current_text(
    tmp_path: Path, benches: list[EditBench]
) -> None:
    script = {1: READ_A, 2: EDIT_A, 3: READ_A, 4: EDIT_A}
    bench = _bench(benches, tmp_path / "e.sqlite", "edit_guard", script)
    bench.hooks[2] = lambda: _someone_else_edits_a(bench)
    bench.run()
    assert bench.code(2) == STALE_EDIT
    refused = bench.shown(3)
    assert "changed since this task read it" in refused
    assert "read at version v1, now v2" in refused and "Read it again" in refused
    assert "y = 3" in refused  # the current text, so the model need not guess
    assert bench.code(4) is None  # after the re-read, the same edit goes through
    assert bench.tree.files["a.py"] == "x = 2\ny = 3\n"  # the person's line survived


def test_edit_guard_lets_consecutive_edits_through_after_its_own_write(
    tmp_path: Path, benches: list[EditBench]
) -> None:
    second = tools_reply(call("edit_file", path="a.py", old="x = 2", new="x = 3"))
    bench = _bench(benches, tmp_path / "e.sqlite", "edit_guard", {1: READ_A, 2: EDIT_A, 3: second})
    bench.run()
    assert (bench.code(2), bench.code(3)) == (None, None)
    assert bench.tree.files["a.py"] == "x = 3\n"


def test_edit_guard_refuses_overwriting_an_existing_file_never_read(
    tmp_path: Path, benches: list[EditBench]
) -> None:
    write_b = tools_reply(call("write_file", path="b.py", text="replaced\n"))
    new_c = tools_reply(call("write_file", path="c.py", text="new\n"))
    bench = _bench(benches, tmp_path / "e.sqlite", "edit_guard", {1: write_b, 2: new_c})
    bench.run()
    assert bench.code(1) == UNREAD_OVERWRITE
    refused = bench.shown(2)
    assert "has not been read" in refused and "keep me" in refused
    assert bench.tree.files["b.py"] == "keep me\n"
    assert bench.code(2) is None and bench.tree.files["c.py"] == "new\n"  # creating is free


def test_off_runs_the_same_edits_unchecked(tmp_path: Path, benches: list[EditBench]) -> None:
    write_b = tools_reply(call("write_file", path="b.py", text="replaced\n"))
    bench = _bench(benches, tmp_path / "e.sqlite", "off", {1: READ_A, 2: EDIT_A, 3: write_b})
    bench.hooks[2] = lambda: _someone_else_edits_a(bench)
    bench.run()
    assert (bench.code(2), bench.code(3)) == (None, None)
    assert bench.tree.files["b.py"] == "replaced\n"


def test_a_resumed_step_keeps_the_recorded_refusal(
    tmp_path: Path, benches: list[EditBench]
) -> None:
    """The check runs inside the durable step: replaying the run never checks again."""
    bench = _bench(benches, tmp_path / "e.sqlite", "edit_guard", {1: READ_A, 2: EDIT_A})
    bench.hooks[2] = lambda: _someone_else_edits_a(bench)
    bench.run()
    before = [e.seq for e in load_view(bench.store, OWNER).events]
    bench.run()  # the run has ended: driving it again appends nothing
    assert [e.seq for e in load_view(bench.store, OWNER).events] == before
    assert bench.code(2) == STALE_EDIT


def test_edit_guard_refuses_a_write_into_a_source_the_reader_does_not_serve(
    tmp_path: Path, benches: list[EditBench]
) -> None:
    """An unserved source reads every record as absent; that is no licence to create one."""
    write_b = tools_reply(call("write_file", path="b.py", text="replaced\n"))
    new_c = tools_reply(call("write_file", path="c.py", text="new\n"))
    bench = _bench(benches, tmp_path / "e.sqlite", "edit_guard", {1: write_b, 2: new_c})
    bench.tree.served = False
    bench.run()
    assert (bench.code(1), bench.code(2)) == (EDIT_UNCHECKED, EDIT_UNCHECKED)
    assert "is not served" in bench.shown(2)
    assert bench.tree.files["b.py"] == "keep me\n" and "c.py" not in bench.tree.files
