"""``save_artifact`` shows the model what it may write, and a refusal lists every fix.

A real model that is not told the kinds a Task writes guesses a kind, then fixes one
schema error per refused call until the Task runs out of turns. These tests pin the two
halves of the fix on the ``coder`` bench: the tool the model is shown names each kind the
Task writes with its content schema, and a save with several schema errors is refused
once with all of them, so one retry suffices.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.canonical import thaw
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.tools.loader_added import LOADER_ADDED_DECLS
from tests.support.loop import AGENT, FINISH, Bench, Recorder, call, tools_reply

NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "lines": {"type": "array", "items": {"type": "string"}},
        "boundary": {
            "type": "object",
            "properties": {"in": {"type": "string"}},
            "required": ["in"],
        },
    },
    "required": ["title", "lines", "boundary"],
    "additionalProperties": False,
}
STRICT_NOTE = ArtifactKind("note", NOTE_SCHEMA)
WRONG = {"lines": "one line", "boundary": "everything", "colour": "red"}
RIGHT = {"title": "Bug 7", "lines": ["fixed"], "boundary": {"in": "the parser"}}
WRONG_PATHS = ("/content/boundary", "/content/colour", "/content/lines", "/content/title")


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Any]:
    made: list[Bench] = []

    def build(model: Recorder) -> Bench:
        b = Bench(tmp_path / "aksha.sqlite", model)
        b.restart(artifacts=ArtifactStore({f"{AGENT}.note": STRICT_NOTE}))
        b.start()
        made.append(b)
        return b

    yield build
    for b in made:
        b.close()


def _save_tool(request: GenerationRequest) -> Any:
    (tool,) = [t for t in request.tools if t.name == "save_artifact"]
    return tool


def _last_result(request: GenerationRequest) -> dict[str, Any]:
    text = request.messages[-1].text or ""
    return dict(json.loads(text.partition("\n")[0]))


def test_the_save_tool_names_each_kind_the_task_writes_with_its_schema(bench: Any) -> None:
    model = Recorder([tools_reply(call("save_artifact", kind="note", content=RIGHT), FINISH)])
    b = bench(model)
    assert b.drive().outcome == "complete"
    tool = _save_tool(model.requests[0])
    assert '"coder.note"' in tool.description
    assert "not keyed: omit `key`" in tool.description
    shown = tool.description.split("content schema: ", 1)[1].split("\n", 1)[0]
    assert json.loads(shown) == NOTE_SCHEMA
    # The parameters stay the schema the gate validates (ADR-P10 D3).
    published = LOADER_ADDED_DECLS["save_artifact"].spec.schema
    assert thaw(tool.schema) == thaw(published)


def test_a_wrong_save_is_refused_with_every_error_and_one_retry_suffices(bench: Any) -> None:
    def fix(request: GenerationRequest) -> GenerationResult:
        refused = _last_result(request)
        assert refused["status"] == "error" and refused["code"] == "schema"
        assert tuple(refused["paths"]) == WRONG_PATHS
        message = refused["message"]
        assert "4 schema errors" in message
        # Each failing path carries what it expected, so the model can fix it unaided.
        assert '\'title\' is required; expected {"minLength":1,"type":"string"}' in message
        assert "/content/boundary: 'everything' is not of type 'object'" in message
        assert '"required":["in"]' in message
        assert "'colour' is not allowed" in message
        return tools_reply(call("save_artifact", 2, kind="note", content=RIGHT), FINISH)

    model = Recorder([tools_reply(call("save_artifact", kind="note", content=WRONG))], rule=fix)
    b = bench(model)
    assert b.drive().outcome == "complete"
    assert len(model.requests) == 2
    (saved,) = b.events("artifact.saved")
    assert saved.payload["kind"] == f"{AGENT}.note"


def test_a_guessed_kind_is_told_the_kinds_the_task_writes(bench: Any) -> None:
    def retry(request: GenerationRequest) -> GenerationResult:
        refused = _last_result(request)
        assert refused["code"] == "kind_unknown"
        assert "this Task writes 'coder.note'" in refused["message"]
        return tools_reply(call("save_artifact", 2, kind="note", content=RIGHT), FINISH)

    guess = tools_reply(call("save_artifact", kind="coder.plan", content=RIGHT))
    model = Recorder([guess], rule=retry)
    assert bench(model).drive().outcome == "complete"
    assert len(model.requests) == 2
