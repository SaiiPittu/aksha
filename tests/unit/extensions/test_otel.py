"""ADR-P21: the trace is a projection of the event store, named in one module, exported offline."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.extensions.otel import semconv as sc
from aksha.extensions.otel.export import export_run
from aksha.extensions.otel.trace import build_trace
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES
from aksha.runtime.events.envelope import Event
from aksha.runtime.session import named as n
from tests.support.session import OWNER, SESSION, SessionBench

SRC = Path(__file__).resolve().parents[3] / "src"


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[SessionBench]:
    b = SessionBench(tmp_path)
    yield b
    b.close()


def _run(b: SessionBench, text: str) -> str:
    b.session.dispatch(n.SendMessage(owner=OWNER, session=SESSION, text=text, command_id="c-1"))
    b.session.run_until_yield(OWNER)
    status = b.session.status(OWNER, SESSION)
    assert status.run is not None
    return status.run.run


def _store_id(b: SessionBench) -> str:
    return b.session.store.identity().store_id


def test_every_core_event_type_has_a_mapping_row() -> None:
    assert sorted(s.name for s in CORE_EVENT_TYPES) == sorted(sc.EVENT_MAP)


def test_a_run_is_one_trace_with_the_span_tree(bench: SessionBench) -> None:
    run = _run(bench, "Write a note")
    (segment,) = build_trace(bench.events(), run, store_id=_store_id(bench))
    assert segment.outcome == "ended"
    by_key = {s.key: s for s in segment.spans}
    root = by_key["run"]
    assert root.name == "invoke_workflow scribe" and root.parent_id is None
    assert root.attributes[sc.GEN_AI_CONVERSATION] == SESSION
    assert root.attributes[sc.AKSHA_RUN_OUTCOME] == "complete"
    task = by_key["task:write"]
    assert task.name == "invoke_agent scribe" and task.parent_id == root.span_id
    turn = by_key["turn:write/t01"]
    assert turn.parent_id == task.span_id
    model = by_key[f"step:{run}/write/t01"]
    assert model.name.startswith("chat ") and model.parent_id == turn.span_id
    assert model.attributes[sc.GEN_AI_PROVIDER] == sc.PROVIDER_FAKE
    tools = [s for s in segment.spans if s.name.startswith("execute_tool ")]
    assert {s.attributes[sc.GEN_AI_TOOL_NAME] for s in tools} >= {"save_artifact", "finish"}
    assert all(s.parent_id == turn.span_id for s in tools)
    assert any(s.name.startswith("aksha.stop_policy.evaluate") for s in segment.spans)
    assert all(not s.name.startswith("aksha.unmapped") for s in segment.spans)
    unmapped = [e for s in segment.spans for e in s.events if e.name.startswith("aksha.unmapped")]
    assert unmapped == []


def test_ids_are_derived_so_two_builds_are_equal(bench: SessionBench) -> None:
    run = _run(bench, "Write a note")
    one = build_trace(bench.events(), run, store_id=_store_id(bench))
    two = build_trace(bench.events(), run, store_id=_store_id(bench))
    assert one == two
    assert build_trace(bench.events(), run, store_id="other")[0].trace_id != one[0].trace_id


def test_a_suspension_splits_the_run_into_linked_segments(bench: SessionBench) -> None:
    run = _run(bench, "Ask first")
    (waiting,) = bench.session.status(OWNER, SESSION).waiting_on
    bench.session.dispatch(n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                                             answers={"q1": "blue"}, command_id="c-2"))  # fmt: skip
    bench.session.run_until_yield(OWNER)
    first, second = build_trace(bench.events(), run, store_id=_store_id(bench))
    assert (first.outcome, second.outcome) == ("suspended", "ended")
    (asked,) = [s for s in first.spans if s.key.startswith("interrupt:")]
    assert asked.attributes[sc.AKSHA_INTERRUPT_STATE] == "open"
    root2 = next(s for s in second.spans if s.key == "run")
    assert root2.links[0].trace_id == first.trace_id
    assert root2.attributes[sc.GEN_AI_CONVERSATION] == SESSION
    assert any(e.name == sc.EV_INTERRUPT_RESOLVED for e in root2.events)


def test_an_unmapped_event_type_is_visible_not_lost(bench: SessionBench) -> None:
    run = _run(bench, "Write a note")
    events = bench.events()
    planted = Event("future.thing", {}, run=run, seq=events[-1].seq + 1, owner=OWNER,
                    at=events[-1].at, boot=events[-1].boot, schema_version=1)  # fmt: skip
    (segment,) = build_trace([*events, planted], run, store_id="s")
    names = [e.name for s in segment.spans for e in s.events]
    assert "aksha.unmapped future.thing" in names


def test_export_is_offline_byte_identical_and_withholds_content(
    bench: SessionBench, tmp_path: Path
) -> None:
    run = _run(bench, "Write a note")
    (a,) = export_run(bench.events(), run, tmp_path / "a", store_id=_store_id(bench),
                      capture=None)  # fmt: skip
    (b,) = export_run(bench.events(), run, tmp_path / "b", store_id=_store_id(bench),
                      capture="no_content")  # fmt: skip
    assert a.read_bytes() == b.read_bytes()
    (c,) = export_run(bench.events(), run, tmp_path / "a", store_id=_store_id(bench),
                      capture="")  # fmt: skip
    assert c == a and len(a.read_text().splitlines()) == 1  # re-export overwrites
    data = json.loads(a.read_text())
    (resource_spans,) = data["resourceSpans"]
    attrs = {x["key"]: x["value"] for x in resource_spans["resource"]["attributes"]}
    assert attrs[sc.AKSHA_SEMCONV] == {"stringValue": sc.SEMCONV}
    spans = resource_spans["scopeSpans"][0]["spans"]
    assert any(s["name"] == "invoke_workflow scribe" for s in spans)
    assert sc.GEN_AI_TOOL_RESULT not in a.read_text()
    (d,) = export_run(bench.events(), run, tmp_path / "d", store_id=_store_id(bench),
                      capture="SPAN_ONLY")  # fmt: skip
    assert sc.GEN_AI_TOOL_RESULT in d.read_text()


def test_capture_mode_reads_the_standard_values() -> None:
    assert sc.capture_mode(None) == "NO_CONTENT"
    assert sc.capture_mode("span_only") == "SPAN_ONLY"
    assert sc.capture_mode("everything") == "NO_CONTENT"


def _genai_literals(root: Path) -> list[str]:
    """``gen_ai.*`` string literals in any module but the one mapping module (D4)."""
    out = []
    for f in sorted((root / "aksha").rglob("*.py")):
        if f.name == "semconv.py" and f.parent.name == "otel":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value.startswith("gen_ai."):
                out.append(f"{f.relative_to(root)}:{node.lineno}")
    return out


def test_only_the_mapping_module_spells_gen_ai_names(tmp_path: Path) -> None:
    assert _genai_literals(SRC) == []
    import shutil

    shutil.copytree(SRC / "aksha", tmp_path / "aksha",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    planted = tmp_path / "aksha/extensions/otel/trace.py"
    planted.write_text(planted.read_text() + '\nLEAK = "gen_ai.agent.name"\n')
    assert _genai_literals(tmp_path) == [f"aksha/extensions/otel/trace.py:{_last_line(planted)}"]


def _last_line(path: Path) -> int:
    return len(path.read_text().splitlines())
