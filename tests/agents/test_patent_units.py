"""Unit tests for the patent agent's reader, rules, tools, checks and model parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aksha.agents.patent import checks, rules
from aksha.agents.patent.agent import AGENT
from aksha.agents.patent.fakes import parse
from aksha.agents.patent.tools import build_tools
from aksha.agents.patent.world import NotInWorld, SourceWorld, query_version, view_version
from aksha.runtime.api import Err, GenerationRequest, Message, Ok, ToolCall, ToolContext
from tests.support.patent import TODAY, sources

V1, V2 = sources("stillroom"), sources("stillroom-v2")


@pytest.fixture
def w1() -> SourceWorld:
    return SourceWorld(V1, TODAY)


@pytest.fixture
def w2() -> SourceWorld:
    return SourceWorld(V2, TODAY)


def _ctx(key: str | None = "C1") -> ToolContext:
    return ToolContext("o1", "patent", "prepare", key, "R-0001/prepare[C1]/t01/c01", 1)


def _tools(world: SourceWorld) -> dict[str, Any]:
    return {t.spec.name: t for t in build_tools(world)}


# --- the reader --------------------------------------------------------------------------


def test_views_ignore_renamed_fields_and_see_changed_ones(w1: SourceWorld, w2: SourceWorld) -> None:
    for c in ("C2", "C3", "C4"):
        for view in ("note", "contributor_rows", "disclosure_rows"):
            assert view_version(getattr(w1, view)(c)) == view_version(getattr(w2, view)(c)), (
                c,
                view,
            )
    assert view_version(w1.disclosure_rows("C1")) != view_version(w2.disclosure_rows("C1"))
    assert view_version(w1.priorities()) == view_version(w2.priorities())
    for pub in w1.publication_ids():  # the applicant is renamed; it is not in the view
        assert view_version(w1.publication(pub)) == view_version(w2.publication(pub)), pub
    assert "applicant" not in w1.publication("SYN-PUB-0001")


def test_only_the_c1_search_changes_between_worlds(w1: SourceWorld, w2: SourceWorld) -> None:
    queries = {
        "C1": [["background"], ["sensor"]],
        "C2": [["mount point", "placement"]],
        "C3": [["overlap"]],
        "C4": [["passive infrared", "PIR"], ["battery"]],
    }
    moved = {
        c for c, q in queries.items() if query_version(w1.search(q)) != query_version(w2.search(q))
    }
    assert moved == {"C1"}
    assert "SYN-PUB-0011" in {h["id"] for h in w2.search(queries["C1"])}


def test_the_pdf_publication_keeps_both_status_wordings(w1: SourceWorld) -> None:
    p = w1.publication("SYN-PUB-0006")
    assert [s["wording"] for s in p["status"]] == [
        "Status: Granted",
        "Lapsed - renewal fee not paid (2025-11-30).",
    ]
    assert p["url"] is None  # the source states none; nothing is invented
    assert p["priority_claims"] == [{"number": "SYN-PRI-2022-0118", "date": "2022-01-18"}]
    assert [c["depends_on"] for c in p["claims"]] == [None, 1]


def test_the_reader_reads_only_below_sources(w1: SourceWorld) -> None:
    with pytest.raises(NotInWorld):
        w1.raw("../key.json")
    with pytest.raises(NotInWorld):
        w1.raw("../spec.yaml")
    out = _tools(w1)["read_document"].body(_ctx(), {"path": "../key.json"})
    assert isinstance(out.result, Err)
    inside = {"path": "disclosures/../notes/C1-self-calibration.md"}
    out = _tools(w1)["read_document"].body(_ctx(), inside)
    assert isinstance(out.result, Err) and out.result.code == "not_allowed"


def test_a_referenced_agreement_is_looked_for_not_assumed_absent(tmp_path: Path) -> None:
    import shutil

    world = SourceWorld(V1, TODAY)
    (row,) = [r for r in world.contributor_rows("C1")["rows"] if r["person_id"] == "P-05"]
    assert row["agreement_documents"] == [{"ref": "CA-2025-11", "found": False}]
    shutil.copytree(V1, tmp_path / "sources")
    (tmp_path / "sources" / "disclosures" / "CA-2025-11-consulting.md").write_text("x")
    world.use(tmp_path / "sources")
    (row,) = [r for r in world.contributor_rows("C1")["rows"] if r["person_id"] == "P-05"]
    assert row["agreement_documents"] == [{"ref": "CA-2025-11", "found": True}]


def test_missing_references_are_reported_not_found(w1: SourceWorld) -> None:
    refs = w1.note("C2")["references"]
    assert refs == [{"name": "placement-trial-02.csv", "found": False}]


def test_disclosure_reads_count_days_but_version_only_the_rows(w1: SourceWorld) -> None:
    tool = _tools(w1)["read_disclosures"]
    out = tool.body(_ctx(), {"candidate": "C1"})
    assert isinstance(out.result, Ok)
    (read,) = out.result.reads
    assert read.version == view_version(w1.disclosure_rows("C1"))
    rows = {r["event_id"]: r for r in out.result.data["rows"]}
    assert rows["DISC-01"]["elapsed_days"] == (date(2026, 11, 26) - date(2026, 3, 10)).days == 261
    later = SourceWorld(V1, date(2026, 12, 1))
    again = _tools(later)["read_disclosures"].body(_ctx(), {"candidate": "C1"})
    assert isinstance(again.result, Ok) and again.result.reads == out.result.reads


# --- the rules -----------------------------------------------------------------------------


def _ext(
    status: str = "built_and_tested", rank: int = 1, docs: bool = False, full: bool = False
) -> dict[str, Any]:
    return {
        "implementation": {"status": status},
        "business_priority": {"rank": rank},
        "missing_documents": [{"name": "x.csv"}] if docs else [],
        "disclosures": [{"public": "yes", "describes_fully": full}],
    }


def _cmp(*states: str) -> dict[str, Any]:
    return {
        "cells": [{"element": "E1", "family": f"F{i}", "state": s} for i, s in enumerate(states)]
    }


@pytest.mark.parametrize(
    ("ext", "cmp_", "want"),
    [
        (_ext(), _cmp("not_found", "not_found"), ("prioritize_counsel_review", "R1")),
        (_ext(status="prototype"), _cmp("not_found"), ("collect_more_evidence", "R4")),
        (_ext(docs=True), _cmp("not_found"), ("collect_more_evidence", "R2")),
        (_ext(status="idea_only", rank=4), _cmp("disclosed"), ("collect_more_evidence", "R2")),
        (
            _ext(status="prototype", rank=2, full=True),
            _cmp("not_found"),
            ("collect_more_evidence", "R3"),
        ),
        (_ext(), _cmp("not_found", "partial"), ("collect_more_evidence", "R4")),
        (_ext(status="in_use_under_nda", rank=3), _cmp("partial"), ("deprioritize_for_now", "R5")),
    ],
)
def test_the_rule_table(ext: dict[str, Any], cmp_: dict[str, Any], want: tuple[str, str]) -> None:
    assert rules.next_action(ext, cmp_) == want


def test_families_group_by_shared_priority_number(w1: SourceWorld) -> None:
    pubs = [w1.publication(p) for p in ("SYN-PUB-0007", "SYN-PUB-0008", "SYN-PUB-0009")]
    assert rules.family_of(pubs) == {
        "SYN-PRI-2019-0630": ["SYN-PUB-0009"],
        "SYN-PRI-2021-1103": ["SYN-PUB-0007", "SYN-PUB-0008"],  # a continuation joins its parent
    }


def _ws_fields(**over: Any) -> list[dict[str, Any]]:
    t = SourceWorld(V1).template("filing-information-worksheet")
    out = [{"field": f["field"], "value": None, "status": "missing"} for f in t["fields"]]
    for f in out:
        if f["field"] in over:
            f.update(over[f["field"]])
    return out


def test_form_refusals() -> None:
    t = SourceWorld(V1).template("filing-information-worksheet")
    assert rules.form_refusal(t, _ws_fields()) is None
    done = {"proposed_inventors": {"value": ["A"], "status": "complete"}}
    assert rules.form_refusal(t, _ws_fields(**done))[0] == "counsel_decides"  # type: ignore[index]
    empty = {"working_title": {"value": "", "status": "complete"}}
    assert rules.form_refusal(t, _ws_fields(**empty))[0] == "complete_needs_value"  # type: ignore[index]
    assert rules.form_refusal(t, _ws_fields()[1:])[0] == "fields_mismatch"  # type: ignore[index]
    bad = {"representative": {"status": "done"}}
    assert rules.form_refusal(t, _ws_fields(**bad))[0] == "status_invalid"  # type: ignore[index]


# --- the tools ---------------------------------------------------------------------------


def test_fill_form_refuses_marking_a_counsel_field_complete(w1: SourceWorld) -> None:
    fill = _tools(w1)["fill_form"].body
    args = {
        "template": "filing-information-worksheet",
        "candidate": "C1",
        "fields": _ws_fields(applicant={"value": "Stillroom", "status": "complete"}),
    }
    out = fill(_ctx(), args)
    assert isinstance(out.result, Err) and out.result.code == "counsel_decides"
    assert out.saves == ()  # "mark it complete" changes nothing
    ok = fill(_ctx(), {**args, "fields": _ws_fields()})
    assert isinstance(ok.result, Ok) and len(ok.saves) == 1
    (save,) = ok.saves
    assert (save.kind, save.key, save.content["counts"]["missing"]) == ("worksheet", "C1", 11)
    other = fill(_ctx("C2"), {**args, "fields": _ws_fields()})
    assert isinstance(other.result, Err) and other.result.code == "wrong_candidate"


def test_send_needs_an_approval_and_goes_through_the_outbox(w1: SourceWorld) -> None:
    send = _tools(w1)["send_packet"]
    spec = send.spec
    assert spec.effect == "external" and spec.approval is not None
    assert spec.external is not None and spec.external.semantics == "reconcile"
    assert spec.external.key_fields == ("/packet", "/recipient")
    assert spec.approval.targets is not None
    assert spec.approval.targets({"packet": "patent.packet/C1@v1"}) == ("patent.packet/C1@v1",)
    assert spec.schema["properties"]["recipient"] == {"enum": ["counsel-primary"]}
    out = send.body(_ctx(), {"packet": "patent.packet/C1@v1", "recipient": "counsel-primary"})
    assert isinstance(out.result, Err) and out.result.code == "outbox_only"


def test_the_definition_loads_and_declares_its_source() -> None:
    from aksha.runtime.definitions.loader import load_agents

    root = Path(__file__).resolve().parents[2] / "src" / "aksha" / "agents"
    (loaded,) = load_agents([root / "patent"], [])
    assert loaded.agent.name == AGENT.name == "patent"
    assert [s.name for s in AGENT.sources] == ["patent_sources"]
    assert "patent.packet" in loaded.kinds and loaded.kinds["patent.packet"].reviewable


# --- the completion checks -----------------------------------------------------------------


@dataclass
class _V:
    content: dict[str, Any]


class _View:
    def __init__(self, **own: dict[str, Any]) -> None:
        self.mine = {k: _V(v) for k, v in own.items()}
        self.key = "C1"

    def own(self, kind: str) -> _V | None:
        return self.mine.get(kind)


def test_the_recommendation_check_vetoes_an_action_the_rules_do_not_give() -> None:
    ext, cmp_ = _ext(), _cmp("not_found")
    good = _View(
        recommendation={"next_action": "prioritize_counsel_review"}, extraction=ext, comparison=cmp_
    )
    assert checks.recommendation_follows_rules(good).ok
    bad = _View(
        recommendation={"next_action": "deprioritize_for_now"}, extraction=ext, comparison=cmp_
    )
    result = checks.recommendation_follows_rules(bad)
    assert not result.ok and "prioritize_counsel_review" in result.message


def _form(*fields: tuple[str, bool, str]) -> dict[str, Any]:
    return {"fields": [{"field": n, "required": r, "status": s} for n, r, s in fields]}


READY = _form(("title", True, "complete"), ("figure_list", True, "needs_counsel_confirmation"))
LATER = _form(("representative", False, "missing"))


def test_the_packet_check_needs_the_counsel_section_first() -> None:
    ok = {"scope_note": "no opinion", "sections": [{"title": "Needs counsel confirmation"}]}
    assert checks.packet_saved(_View(packet=ok, disclosure=READY, worksheet=LATER)).ok
    late = {
        "scope_note": "x",
        "sections": [{"title": "Scope"}, {"title": "Needs counsel confirmation"}],
    }
    assert not checks.packet_saved(_View(packet=late, disclosure=READY, worksheet=LATER)).ok
    bare = {**ok, "scope_note": ""}
    assert not checks.packet_saved(_View(packet=bare, disclosure=READY, worksheet=LATER)).ok


def test_a_missing_required_field_keeps_the_packet_from_ready() -> None:
    ok = {"scope_note": "no opinion", "sections": [{"title": "Needs counsel confirmation"}]}
    gap = _form(("title", True, "complete"), ("figure_list", True, "missing"))
    result = checks.packet_saved(_View(packet=ok, disclosure=gap, worksheet=LATER))
    assert not result.ok and "figure_list" in result.message and result.count == 1
    assert not checks.packet_saved(_View(packet=ok, disclosure=READY)).ok  # no worksheet yet


def test_fill_form_records_required_from_the_template_not_the_model(w1: SourceWorld) -> None:
    fields = _ws_fields()
    for f in fields:
        f["required"] = False  # the model claims nothing is required
    out = _tools(w1)["fill_form"].body(
        _ctx(), {"template": "filing-information-worksheet", "candidate": "C1", "fields": fields}
    )
    (save,) = out.saves
    required = {f["field"]: f["required"] for f in save.content["fields"]}
    assert required["applicant"] is True and required["representative"] is False
    assert rules.required_missing(save.content) == [
        "working_title",
        "applicant",
        "proposed_inventors",
        "ownership_and_assignments",
        "outside_party_agreements",
        "earlier_public_disclosures",
        "attachments",
    ]


# --- the model's parsing ---------------------------------------------------------------


def test_parse_reads_the_instance_reads_answers_and_starts_over_after_finish() -> None:
    reads = (
        "Artifacts this Task reads (current versions):\n\n"
        '[artifact patent.table@v2, full]\n{"rows":[]}'
    )
    calls = (ToolCall("c1", "read_note", {"candidate": "C1"}), ToolCall("c2", "finish", {}))
    req = GenerationRequest(
        "R-0001/candidate[C1]/extract/t03/m",
        "generate",
        (
            Message("user", reads),
            Message("assistant", None, calls),
            Message("tool", '{"data":{"x":1},"status":"ok"}', call_id="c1"),
            Message("tool", '{"data":{},"status":"ok"}', call_id="c2"),
            Message("user", "Answers from the person:\nQ: Which?\nA: C1"),
        ),
    )
    ctx = parse(req)
    assert (ctx.instance, ctx.task, ctx.key) == ("candidate[C1]/extract", "extract", "C1")
    assert ctx.read("table") == ("patent.table@v2", {"rows": []})
    assert ctx.answers == ["C1"]
    assert ctx.results == []  # a finish ends the attempt: a re-entry reads again


def test_a_world_with_no_folder_reads_the_same_views_through_the_record_reader() -> None:
    from aksha.adapters.sources.folder import FolderSources
    from aksha.agents.patent.world import SOURCE_NAME, SourceWorld
    from tests.support.patent import sources

    folder = SourceWorld(sources("stillroom"))
    read = SourceWorld().over(FolderSources({SOURCE_NAME: sources("stillroom")}))
    assert read is not folder and folder.over(None) is folder
    assert read.candidates() == folder.candidates()
    for c in ("C1", "C2"):
        assert read.note(c) == folder.note(c)
        assert read.disclosure_rows(c) == folder.disclosure_rows(c)
    assert read.priorities() == folder.priorities()
    assert read.search([["vent"]]) == folder.search([["vent"]])
    note = "notes/C1-self-calibration.md#note"
    assert read.record_version("o1", note) == folder.record_version("o1", note)
    assert SourceWorld().over(None).root is None
