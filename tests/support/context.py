"""A small log builder for the context tests: runs, entries, turns, tool results and saves.

``Log`` writes the facts the agent loop will write (a model step and its tool steps, the
saves and reads a tool step commits), through a real SQLite event store, so every payload
passes its schema and every boundary its rules. Each test then reads a ``LogView`` back.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from aksha.runtime.canonical import canonical_json, sha256_hex
from aksha.runtime.context.types import AssemblyInputs
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.codec import generation_result_json
from aksha.runtime.models.types import GenerationResult, ToolCall
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.chain import content_hash
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.storage.rows import ArtifactRow
from aksha.runtime.tasks.tree import CompiledTree, compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Read, Task, artifact_keys
from aksha.runtime.tools.results import cap_text, view_json
from aksha.runtime.tools.types import ToolResultView, ToolView
from tests.support.events import START

OWNER = "o"
RUN = "R-0001"
AGENT = "coder"
INPUTS = AssemblyInputs(
    instructions="You edit code.", brief="Fix the parser.", request="Fix bug 7."
)
TOOLS = (
    ToolView("read_file", "Read a file.", {"type": "object"}),
    ToolView("write_file", "Write a file.", {"type": "object"}),
    ToolView("run_tests", "Run the tests.", {"type": "object"}),
)


def _done(view: Any) -> CheckResult:
    return CheckResult(True)


def tree(reads: tuple[str | Read, ...] = ()) -> CompiledTree:
    """An agent with one keyed Task ``edit`` and one unkeyed Task ``plan``."""
    tasks = (
        Task("plan", tools=("read_file",), writes=("coder.plan",), done=_done),
        Task(
            "edit",
            tools=("read_file", "write_file", "run_tests"),
            reads=reads,
            done=_done,
            per=artifact_keys("coder.plan", "files"),
        ),
    )
    return compile_tree(AGENT, tasks)


def result_view(text: str, cap: int = 8_000) -> ToolResultView:
    """The capped view of a result whose canonical text is ``text``."""
    full = canonical_json({"data": {"text": text}, "status": "ok"})
    shown, cut = cap_text(full, cap)
    return ToolResultView("ok", shown, cut, len(full), "sha256:" + sha256_hex(full))


class Log:
    """One owner's SQLite store, with builders for the facts a run writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(path, self.clock, writer=True)

    def close(self) -> None:
        self.store.close()

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def start(
        self, run: str = RUN, *, agent: str = AGENT, cause: str = "request", **extra: Any
    ) -> None:
        payload: dict[str, Any] = {"agent": agent, "cause": cause, **extra}
        if cause == "rebuild":
            payload["rebuild_key"] = f"rebuild:{OWNER}/{agent}/1"
            payload["for_runs"] = ["R-0001"]
        boundary = "U9" if cause == "rebuild" else "U1"
        self.store.append(OWNER, boundary, (NewEvent("run.started", payload, run=run),))

    def enter(self, task: str, key: str | None, run: str = RUN, cause: str = "scheduler") -> None:
        payload = {
            "cause": cause,
            "task": task,
            "key": key,
            "writer": {"agent": AGENT, "task_path": task, "key": key},
        }
        self.store.append(OWNER, "U3", (NewEvent("task.entered", payload, run=run),))

    def save(
        self,
        ref: str,
        content: Mapping[str, Any],
        *,
        step: str,
        run: str = RUN,
        writer: tuple[str, str | None] = ("plan", None),
        reads: tuple[InputRef, ...] = (),
    ) -> None:
        r = ArtifactRef.parse(ref)
        digest = content_hash(content)
        row = ArtifactRow(ref, r.kind, r.key, r.version, content, digest, digest)
        payload: dict[str, Any] = {
            "ref": ref, "kind": r.kind, "key": r.key, "version": r.version,
            "content_hash": digest, "canonical_hash": digest,
            "reads": [i.as_json() for i in reads],
            "writer": {"agent": AGENT, "task_path": writer[0], "key": writer[1]},
        }  # fmt: skip
        sid = StepId.parse(step)
        event = NewEvent("artifact.saved", payload, run=run, instance=sid.instance, step=step)
        self.store.append(OWNER, "U2", (event,), (row,))

    def model_turn(
        self,
        instance: InstanceRef,
        turn: int,
        calls: tuple[tuple[str, Mapping[str, Any]], ...] = (),
        *,
        text: str | None = None,
        masked: tuple[str, ...] = (),
        args_hash: str = "sha256:" + "0" * 64,
    ) -> StepId:
        """A completed model step of ``turn`` that asked for ``calls``."""
        step = StepId(instance.run, instance.instance, turn=turn)
        tool_calls = tuple(ToolCall(f"call-{turn}-{i}", n, a) for i, (n, a) in enumerate(calls, 1))
        result = GenerationResult(text, tool_calls, "tool_calls" if calls else "text")
        started: dict[str, Any] = {
            "kind": "model", "name": "generate", "args_hash": args_hash, "counted": True,
        }  # fmt: skip
        if masked:
            started["masked"] = list(masked)
        env = {"run": step.run, "instance": step.instance, "step": str(step)}
        self.store.append(OWNER, "U2a", (NewEvent("step.started", started, **env),))
        done = {
            "outcome": "ok", "kind": "model", "name": "generate", "attempts": 1,
            "value": {"result": generation_result_json(result)},
        }  # fmt: skip
        self.store.append(OWNER, "U2", (NewEvent("step.completed", done, **env),))
        return step

    def tool_result(
        self,
        instance: InstanceRef,
        turn: int,
        call: int,
        tool: str,
        view: ToolResultView,
        *,
        reads: tuple[InputRef, ...] = (),
    ) -> str:
        """A completed tool step at ``call`` of ``turn``, with its view and the reads it took."""
        step = StepId(instance.run, instance.instance, turn=turn, call=call)
        env = {"run": step.run, "instance": step.instance, "step": str(step)}
        started = {"kind": "tool", "tool": tool, "args_hash": "sha256:" + "1" * 64, "counted": True}
        self.store.append(OWNER, "U2a", (NewEvent("step.started", started, **env),))
        events = [
            NewEvent("read.recorded", {"input": r.as_json(), "via": "tool"}, **env) for r in reads
        ]
        done = {
            "outcome": "ok", "kind": "tool", "tool": tool, "attempts": 1,
            "value": {"view": view_json(view)},
        }  # fmt: skip
        events.append(NewEvent("step.completed", done, **env))
        self.store.append(OWNER, "U2", tuple(events))
        return str(step)

    def turn_with_results(
        self,
        instance: InstanceRef,
        turn: int,
        results: tuple[tuple[str, Mapping[str, Any], str], ...],
    ) -> tuple[str, ...]:
        """A model turn and one tool result per ``(tool, arguments, text)``."""
        self.model_turn(instance, turn, tuple((t, a) for t, a, _ in results))
        return tuple(
            self.tool_result(instance, turn, i, t, result_view(text))
            for i, (t, _, text) in enumerate(results, 1)
        )


def texts(messages: Any) -> Iterator[str]:
    """Every text and argument string of a request's messages."""
    for m in messages:
        if m.text:
            yield m.text
        for c in m.tool_calls:
            yield canonical_json(dict(c.arguments))


def span_json(span_id: str, text: str, record: str = "pubs/P1") -> dict[str, Any]:
    """One minted span over all of ``text``."""
    return {
        "span_id": span_id, "source": "fixtures", "record_id": record, "version": "1",
        "start": 0, "end": len(text), "text": text, "tool": "fetch",
    }  # fmt: skip


def mint_and_cite(log: Log, carrier: str, spans: tuple[tuple[str, str], ...], step: str) -> None:
    """Mint ``spans`` in a tool step of another instance, and bind them as cites of ``carrier``."""
    sid = StepId.parse(step)
    env = {"run": sid.run, "instance": sid.instance, "step": step}
    minted = NewEvent(
        "excerpts.minted",
        {"tool": "fetch", "spans": [span_json(i, t) for i, t in spans], "shown": [spans[0][0]]},
        **env,
    )
    cites = tuple(
        NewEvent(
            "citation.bound",
            {"carrier": carrier, "n": n, "pointer": f"/c/{n}", "form": "span", "target": i},
            **env,
        )
        for n, (i, _) in enumerate(spans)
    )
    log.store.append(OWNER, "U2", (minted, *cites))
