"""The counts the research checks assert are computed by code, never asked of the model.

A model that saves wrong counts (companies resolved, pages found, review rows, each theme's
"N of M") still finishes every Task first time: the save writes the computed values over
its own (ADR-P04 K1). A model that leaves a theme without a need is told, in one veto, which
themes have one, which do not and what to save, and passes on its next turn. Both runs end
with the same outputs as the rule-based model's.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from aksha.agents.research import computed
from aksha.agents.research.fakes import ResearchModel
from aksha.runtime.canonical import thaw
from aksha.runtime.models.types import GenerationRequest, GenerationResult, ToolCall
from tests.support.research import ResearchBench

ANSWER = {"geography": "EU"}
OUTPUTS = ("scope", "competitors", "needs", "matrix", "findings", "tests")
VETO = "missing a need for themes"


class MiscountingModel(ResearchModel):
    """The rule-based policy, with every count it saves wrong or left out.

    ``drop_theme`` also leaves the last theme's need out of its first save of the needs,
    until a finish veto has told it which theme is missing.
    """

    drop_theme = False

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The policy's calls, with the counts in each ``save_artifact`` spoiled."""
        result = super().generate(request)
        told = VETO in json.dumps(request.as_json())
        calls = tuple(self._spoil(c, told) for c in result.tool_calls)
        return replace(result, tool_calls=calls)

    def _spoil(self, call: ToolCall, told: bool) -> ToolCall:
        if call.name != "save_artifact":
            return call
        args: dict[str, Any] = thaw(call.arguments)
        kind, content = str(args["kind"]).rsplit(".", 1)[-1], args["content"]
        if kind == "competitors":
            content["resolved"] = len(content["companies"]) + 4
            del content["pages_found"]
        elif kind == "needs":
            content["sample"]["rows"] = 999
            for need in content["needs"]:
                need["phrase"] = "about a fifth of buyers"
                need["count"] = need.get("count", 0) + 3
            if self.drop_theme and not told:
                content["needs"] = content["needs"][:-1]
        return replace(call, arguments=args)


class DroppingModel(MiscountingModel):
    drop_theme = True


def _run(tmp: Path, model: ResearchModel) -> ResearchBench:
    bench = ResearchBench(tmp, model)
    bench.start()
    assert bench.run_to_end(**ANSWER).outcome == "complete"
    return bench


def _vetoes(bench: ResearchBench) -> list[dict[str, Any]]:
    return [
        thaw(e.payload)
        for e in bench.events("stop.decided")
        if e.payload.get("verdict") == "continue" and e.payload.get("candidate") == "finish"
        and "pops" not in e.payload  # a finish that passed pops its Task
    ]  # fmt: skip


def test_wrong_counts_are_computed_at_save_and_no_finish_is_vetoed(tmp_path: Path) -> None:
    clean = _run(tmp_path / "clean", ResearchModel())
    wrong = _run(tmp_path / "wrong", MiscountingModel())
    assert _vetoes(wrong) == []
    for kind in OUTPUTS:
        assert thaw(wrong.content(kind)) == thaw(clean.content(kind)), kind
    found = wrong.content("competitors")
    assert found["resolved"] == len(found["companies"])
    assert found["pages_found"] == sum(len(c["pages"]) for c in found["companies"])
    sample = wrong.content("needs")["sample"]
    assert sample["rows"] == sample["unique"] + len(sample["duplicates"])
    clean.close()
    wrong.close()


def test_a_missing_theme_is_named_in_one_veto_and_the_next_turn_passes(tmp_path: Path) -> None:
    clean = _run(tmp_path / "clean", ResearchModel())
    dropping = _run(tmp_path / "dropping", DroppingModel())
    (veto,) = _vetoes(dropping)
    themes = sorted(str(t["id"]) for t in clean.content("scope")["themes"])
    last = str(clean.content("needs")["needs"][-1]["theme"])
    saved = sorted(set(themes) - {last})
    assert veto["candidate_instance"] == "voice"
    assert veto["reason"].startswith(f"needs saved for themes {saved}; missing a need for "
                                     f"themes {[last]}: save the needs again")  # fmt: skip
    # The needs are on their second version, which the findings cite; nothing else differs.
    assert dropping.latest("needs").ref.version == 2  # type: ignore[union-attr]
    for kind in OUTPUTS:
        then = json.dumps(thaw(dropping.content(kind))).replace(
            "research.needs@v2", "research.needs@v1"
        )
        assert json.loads(then) == thaw(clean.content(kind)), kind
    clean.close()
    dropping.close()


def test_the_needs_counts_follow_from_the_unique_reviews_and_each_needs_evidence() -> None:
    content = {
        "sample": {"unique": 40, "rows": 7, "duplicates": [{}, {}]},
        "needs": [{"theme": "a", "phrase": "most", "evidence": [{}] * 6, "count": 1, "of": 2},
                  {"theme": "b", "evidence": [{}] * 3}],
    }  # fmt: skip
    out = computed.needs(thaw(content))
    assert out["sample"]["rows"] == 42
    assert [n["phrase"] for n in out["needs"]] == [
        "6 of 40 reviews in this sample",
        "3 of 40 reviews in this sample",
    ]
    assert (out["needs"][0]["count"], out["needs"][0]["of"]) == (6, 40)
    assert "count" not in out["needs"][1]  # only a field the need carries is kept in step
    # Only rows given: unique is rows less the duplicates.
    only_rows = computed.needs({"sample": {"rows": 12, "duplicates": [{}]}, "needs": []})
    assert only_rows["sample"]["unique"] == 11
    # Nothing to count from: left as it was, for the schema and the check to report.
    assert computed.needs({"sample": {"duplicates": []}, "needs": []}) == {
        "sample": {"duplicates": []},
        "needs": [],
    }
    assert computed.competitors({"companies": "x"}) == {"companies": "x"}
    counted = computed.competitors({"companies": [{"pages": [1, 2]}, {"pages": [3]}, {}]})
    assert (counted["resolved"], counted["pages_found"]) == (3, 3)
