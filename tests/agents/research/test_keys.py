"""Company keys are computed at save, and a check reports a missing field instead of raising.

A model that saves its companies and matrix rows without a ``key`` still completes the run:
the save gives each company a key from its canonical name (ADR-P04 K1), the evidence fans
out over those keys, and the report matches each row to its company. A check that indexes
a field the saved work lacks fails with a message naming the field, never ``check_raised``
(ADR-P15 S7).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from aksha.agents.research import checks, computed
from aksha.agents.research.fakes import ResearchModel
from aksha.runtime.canonical import thaw
from aksha.runtime.models.types import GenerationRequest, GenerationResult, ToolCall
from aksha.runtime.tasks.checks import run_check
from tests.agents.research.test_checks import _View
from tests.support.research import ResearchBench


class KeylessModel(ResearchModel):
    """The rule-based policy, saving every company and matrix row without its ``key``."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The policy's calls, with the keys taken out of each competitors and matrix save."""
        result = super().generate(request)
        return replace(result, tool_calls=tuple(self._strip(c) for c in result.tool_calls))

    def _strip(self, call: ToolCall) -> ToolCall:
        if call.name != "save_artifact":
            return call
        args: dict[str, Any] = thaw(call.arguments)
        kind, content = str(args["kind"]).rsplit(".", 1)[-1], args["content"]
        listed = {"competitors": "companies", "matrix": "rows"}.get(kind)
        for item in content.get(listed, []) if listed else []:
            item.pop("key", None)
        return replace(call, arguments=args)


def test_a_model_that_saves_no_company_keys_completes_discover_and_report(
    tmp_path: Path,
) -> None:
    bench = ResearchBench(tmp_path, KeylessModel())
    bench.start()
    assert bench.run_to_end(geography="EU").outcome == "complete"
    found = bench.content("competitors")
    keys = [c["key"] for c in found["companies"]]
    assert keys == [computed.company_key(c) for c in found["companies"]]
    assert len(set(keys)) == len(keys)
    rows = sorted(r["key"] for r in bench.content("matrix")["rows"])
    assert rows == sorted(keys)
    codes = [e.payload.get("code") for e in bench.events("completion_check.evaluated")]
    assert "check_raised" not in codes
    bench.close()


def test_a_supplied_key_is_kept_and_a_missing_one_is_computed_unique() -> None:
    out = computed.competitors({"companies": [
        {"key": "c01-acme", "canonical": {"value": "Acme"}},
        {"canonical": {"value": "Corvane Group", "cite": {}}},
        {"canonical": "Corvane  group"},
        {"pages": [{"record": "web/beta.example/p"}]},
        {"key": "bad/key", "canonical": "Delta"},
        {},
    ]})  # fmt: skip
    assert [c["key"] for c in out["companies"]] == [
        "c01-acme", "corvane-group", "corvane-group-2", "web-beta-example-p", "delta",
        "company-6",
    ]  # fmt: skip
    rows = computed.matrix({"rows": [{"company": {"value": "Acme"}}, {"key": "k"}]})["rows"]
    assert [r["key"] for r in rows] == ["acme", "k"]


def _keyless() -> dict[str, Any]:
    return {"companies": [{"canonical": {"value": "Acme"}, "pages": [{"section": 1}]}],
            "resolved": 1, "pages_found": 1}  # fmt: skip


def test_checks_report_a_missing_field_by_name_and_never_raise() -> None:
    no_key = _View({("competitors", None): _keyless()})
    result, raised = run_check(checks.discover_done, no_key)
    assert not raised and not result.ok
    assert "have no key" in result.message
    with_key = _keyless()
    with_key["companies"][0]["key"] = "acme"
    result, raised = run_check(checks.discover_done, _View({("competitors", None): with_key}))
    assert not raised and "page with no record" in result.message
    # A matrix row with no key is matched to its company by name; a malformed one is named.
    with_key["companies"][0]["pages"] = [{"record": "p/1"}]
    report = _View({
        ("competitors", None): with_key,
        ("matrix", None): {"rows": [{"company": {"value": "acme"}, "prices": [{}]}, "x"]},
        ("scope", None): {"dimensions": []},
        ("findings", None): {"findings": []},
        ("tests", None): {"tests": [{"id": "t1"}, {"id": "t2", "threshold": "high"}]},
    })  # fmt: skip
    result, raised = run_check(checks.report_done, report)
    assert not raised
    assert result.message.startswith("tests ['t1', 't2'] have a threshold")
