"""Memory through the agent loop (ADR-P09): the sequence diagram's three runs on one store.

Run 1: the person says "only EU vendors from now on"; the intake remembers it, citing the
statement. Run 2: recall returns it first, and the scope cites it through lineage. Run 3:
"EU and UK now" supersedes it, and run 2's scope is invalidated by the ordinary
invalidation view, with no memory code in it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.extensions.memory import RecallResult, episodes, used_by
from aksha.runtime.models.types import GenerationRequest, ToolCall
from tests.support.memory import GEO, Bench, ask, call, statement


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path / "aksha.sqlite")
    yield b
    b.close()


def remember_and_scope(text: str, region: str):  # type: ignore[no-untyped-def]
    def script(n: int, r: GenerationRequest) -> list[ToolCall]:
        if n == 1:
            return [ask()]
        return [
            call("remember", kind="preference", subject="research.geography", text=text,
                 source={"statement": statement(r)}),
            call("save_artifact", kind="scope", content={"region": region}),
            call("finish", summary="scoped"),
        ]  # fmt: skip

    return script


def scope_only(region: str):  # type: ignore[no-untyped-def]
    def script(n: int, r: GenerationRequest) -> list[ToolCall]:
        return [call("save_artifact", kind="scope", content={"region": region}),
                call("finish", summary="scoped")]  # fmt: skip

    return script


def three_runs(b: Bench) -> None:
    one = b.run("R-0001", remember_and_scope("Only EU vendors.", "EU"),
                says="Only EU vendors from now on.")  # fmt: skip
    assert one.outcome == "complete", one
    assert b.run("R-0002", scope_only("EU, run 2")).outcome == "complete"
    three = b.run("R-0003", remember_and_scope("EU and UK vendors.", "EU+UK"),
                  says="EU and UK now.")  # fmt: skip
    assert three.outcome == "complete", three


def test_recall_runs_first_in_the_intake_and_its_hits_become_lineage(bench: Bench) -> None:
    three_runs(bench)
    first = next(r for r in bench.model.requests if r.step_id.startswith("R-0002"))
    offered = {t.name for t in first.tools}
    assert first.tool_choice == "recall" and "recall" in offered
    assert not offered & {"remember", "forget", "save_artifact"}
    (empty,) = bench.recalls("R-0001")
    assert RecallResult.from_json(empty).hits == ()
    (two,) = bench.recalls("R-0002")
    hits = RecallResult.from_json(two).hits
    assert [(h.ref, h.veracity) for h in hits] == [(f"{GEO}@v1", "stated")]
    saved = {e.payload["ref"]: e.payload for e in bench.events("artifact.saved")}
    reads = [r["id"] + "@" + r["version"] for r in saved["scout.scope@v2"]["reads"]]
    assert f"{GEO}@v1" in reads
    assert not any(r["id"] == GEO for r in saved["scout.scope@v1"]["reads"])


def test_a_superseded_preference_reopens_exactly_the_outputs_that_used_it(bench: Bench) -> None:
    three_runs(bench)
    (rel,) = [
        e.payload for e in bench.events("relation.saved") if e.payload["kind"] == "supersedes"
    ]
    assert (rel["carrier"], rel["dst"]["version"]) == (f"{GEO}@v2", "v1")
    iv = bench.iv()
    assert iv.state("scout.scope@v2").freshness == "invalidated"
    assert iv.state("scout.scope@v1").freshness == "current"
    assert iv.state("scout.scope@v3").freshness == "current"
    assert iv.state(f"{GEO}@v2").freshness == "current"
    assert not iv.state(f"{GEO}@v1").is_latest
    # Lineage, not freshness: run 3 recalled v1 before replacing it, so v2 and run 3's scope
    # read it too; only run 2's scope is out of date.
    assert used_by(bench.view(), [f"{GEO}@v1"]) == ("scout.scope@v2", f"{GEO}@v2", "scout.scope@v3")


def test_a_preference_cites_the_persons_statement_or_is_refused(bench: Bench) -> None:
    def uncited(n: int, r: GenerationRequest) -> list[ToolCall]:
        if n == 1:
            return [call("remember", kind="preference", subject="research.geography",
                         text="Only EU.", source={"statement": "EX-0042"}),
                    call("save_artifact", kind="scope", content={"region": "EU"}),
                    call("finish", summary="scoped")]  # fmt: skip
        return [call("finish", summary="scoped")]

    assert bench.run("R-0001", uncited).outcome == "complete"
    (remember,) = bench.results("R-0001", "remember")
    assert remember["status"] == "error" and remember["code"] == "unknown_span", remember
    assert not [
        e for e in bench.events("artifact.saved") if e.payload["kind"] == "memory.preference"
    ]


def test_forget_hides_a_record_from_the_next_recall(bench: Bench) -> None:
    bench.run("R-0001", remember_and_scope("Only EU vendors.", "EU"),
              says="Only EU vendors from now on.")  # fmt: skip

    def forget(n: int, r: GenerationRequest) -> list[ToolCall]:
        return [call("forget", kind="preference", subject="research.geography"),
                call("save_artifact", kind="scope", content={"region": "any"}),
                call("finish", summary="scoped")]  # fmt: skip

    assert bench.run("R-0002", forget).outcome == "complete"
    assert bench.run("R-0003", scope_only("any, run 3")).outcome == "complete"
    (three,) = bench.recalls("R-0003")
    assert RecallResult.from_json(three).hits == ()
    saved = {e.payload["ref"]: e for e in bench.events("artifact.saved")}
    assert saved[f"{GEO}@v2"].run == "R-0002"
    assert bench.view().artifacts.get(f"{GEO}@v2").row.content == {  # type: ignore[union-attr]
        "subject": "research.geography", "forgotten": True,
    }  # fmt: skip
    (rel,) = [
        e.payload for e in bench.events("relation.saved") if e.payload["kind"] == "supersedes"
    ]
    assert (rel["carrier"], rel["dst"]["version"]) == (f"{GEO}@v2", "v1")


def test_episodes_are_the_runs_read_back(bench: Bench) -> None:
    three_runs(bench)
    found = episodes(bench.view(), frozenset({"scout"}))
    assert [(e.run_id, e.outcome) for e in found] == [
        ("R-0001", "done"), ("R-0002", "done"), ("R-0003", "done"),
    ]  # fmt: skip
    assert "scout.scope@v2" in found[1].outputs and "scope" in found[1].completed
