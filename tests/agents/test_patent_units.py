"""Unit tests for the patent agent's reader, rules, tools, checks and model parsing."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest

from aksha import data
from aksha.adapters.sources.folder import FolderSource
from aksha.agents.patent import checks, rules
from aksha.agents.patent.agent import AGENT
from aksha.agents.patent.fakes import ADAPTERS, parse
from aksha.agents.patent.fakes_views import readers, view
from aksha.agents.patent.tools import FORMS, build_tools
from aksha.agents.patent.world import NotInWorld, OneRecord, SourceWorld
from aksha.runtime.api import Err, GenerationRequest, Message, Ok, ToolCall, ToolContext
from aksha.runtime.grounding.parts import part_version
from aksha.runtime.grounding.types import ReturnedText
from tests.support.fast import models_for
from tests.support.patent import AGENT_DIR, TODAY, sources

V1 = sources("stillroom")


def _folder(root: Path) -> FolderSource:
    return FolderSource("inputs", root, "input")


def _models(tool: str = "read_document") -> Any:
    """The fake-mode ``ToolContext.models`` a tool's body gets (``tests.support.fast``)."""
    return models_for(AGENT_DIR, ADAPTERS, tool)


def _world(root: Path = V1, today: date | None = TODAY) -> SourceWorld:
    """The inputs read as the offline model reads them, with the agent's offline readers."""
    return SourceWorld(today).over(_folder(root), models=readers())


@pytest.fixture
def w1() -> SourceWorld:
    return _world()


def _ctx(key: str | None = "C1", root: Path = V1) -> ToolContext:
    return ToolContext("o1", "patent", "prepare", key, "R-0001/prepare[C1]/t01/c01", 1,
                       _folder(root), models=_models())  # fmt: skip


def _tools() -> dict[str, Any]:
    return {t.spec.name: t for t in build_tools()}


def _changed(tmp_path: Path) -> Path:
    """The base world with the demo's change set applied (two edits, one new publication)."""
    from aksha.interfaces.demo.world import apply_change

    root = tmp_path / "sources"
    shutil.copytree(V1, root)
    apply_change(root)
    return root


# --- the reader --------------------------------------------------------------------------


def _rows(record: str, c: str, root: Path = V1) -> Any:
    args = {"ref": record, "where": {"column": "candidate", "values": [c]}}
    return _tools()["read_document"].body(_ctx(c, root), args).result


def test_reads_name_only_the_rows_they_used(tmp_path: Path) -> None:
    """A change to C1's history row moves C1's read of its rows only, never another's."""
    history = "input/disclosures/history.csv"
    before = {c: _rows(history, c) for c in ("C1", "C2", "C3", "C4")}
    root = _changed(tmp_path)
    folder = _folder(root)
    moved = set()
    for c, out in before.items():
        assert isinstance(out, Ok)
        (read,) = out.reads
        assert read.id == f"{history}#where:candidate={c}"
        if folder.record_version("o1", read.id) != read.version:
            moved.add(c)
    assert moved == {"C1"}
    contributors = _rows("input/contributors/records.csv", "C1")
    assert isinstance(contributors, Ok)
    for read in contributors.reads:  # the contributor records did not change
        assert folder.record_version("o1", read.id) == read.version


def test_only_the_c1_search_changes_with_the_new_publication(tmp_path: Path) -> None:
    queries = {
        "C1": [["background"], ["sensor"]],
        "C2": [["mount point", "placement"]],
        "C3": [["overlap"]],
        "C4": [["passive infrared", "PIR"], ["battery"]],
    }
    search = _tools()["search_inputs"].body  # the shared search, over the publications' kind
    root = _changed(tmp_path)

    def versions(c: str, at: Path) -> list[str]:
        out = []
        for terms in queries[c]:
            args = {"terms": terms, "kinds": ["prior_art_publication"]}
            got = search(_ctx(c, at), args).result
            assert isinstance(got, Ok)
            (read,) = got.reads
            assert read.kind == "query" and read.id.startswith("patent.search_inputs?")
            out.append(read.version)
        return out

    assert {c for c in queries if versions(c, V1) != versions(c, root)} == {"C1"}


def test_a_record_part_is_versioned_by_exactly_its_rows() -> None:
    text = "id,c\n1,a\n2,b\n"
    v = part_version(text, "where:c=a", "whole")
    assert v == part_version("id,c\n1,a\n2,z\n", "where:c=a", "whole")  # row 2 moved: same
    assert v != part_version("id,c\n1,A\n2,b\n", "where:c=a", "whole")
    assert part_version(text, "where:nope=a", "whole") is None
    assert part_version(text, "note", "whole") == "whole"


def test_the_pdf_publication_keeps_both_status_wordings(w1: SourceWorld) -> None:
    p = w1.publication("SYN-PUB-0006")
    assert [s["wording"] for s in p["status"]] == [
        "Status: Granted",
        "Lapsed - renewal fee not paid (2025-11-30).",
    ]
    assert p["url"] is None  # the source states none; nothing is invented
    assert p["priority_claims"] == [{"number": "SYN-PRI-2022-0118", "date": "2022-01-18"}]
    assert [c["depends_on"] for c in p["claims"]] == [None, 1]
    assert p["office"] == "SYN-Office-2"
    assert "applicant" not in w1.publication("SYN-PUB-0001")


def test_the_reader_reads_only_the_inputs(w1: SourceWorld) -> None:
    with pytest.raises(NotInWorld):
        w1.raw("input/../key.json")
    with pytest.raises(NotInWorld):
        w1.raw("../spec.yaml")
    out = _tools()["read_document"].body(_ctx(), {"ref": "input/../key.json"})
    assert isinstance(out.result, Err)
    inside = {"ref": "input/disclosures/../notes/C1-self-calibration.md"}
    out = _tools()["read_document"].body(_ctx(), inside)
    assert isinstance(out.result, Err) and out.result.code == "not_an_input"


def test_a_referenced_agreement_is_looked_for_not_assumed_absent(tmp_path: Path) -> None:
    (row,) = [r for r in _world().contributor_rows("C1")["rows"] if r["person_id"] == "P-05"]
    assert row["agreement_documents"] == [{"ref": "CA-2025-11", "found": False}]
    shutil.copytree(V1, tmp_path / "sources")
    (tmp_path / "sources" / "disclosures" / "CA-2025-11-consulting.md").write_text("x")
    world = _world(tmp_path / "sources")
    (row,) = [r for r in world.contributor_rows("C1")["rows"] if r["person_id"] == "P-05"]
    assert row["agreement_documents"] == [{"ref": "CA-2025-11", "found": True}]


def test_missing_references_are_reported_not_found(w1: SourceWorld) -> None:
    refs = w1.note("C2")["references"]
    assert refs == [{"name": "placement-trial-02.csv", "found": False}]


def test_disclosure_views_count_days_but_the_read_versions_only_the_rows() -> None:
    # the days are counted in the model's turn, to the listing's as_of date; the read of the
    # rows is the same record part whatever the date
    got = view(_world(), "read_disclosures", {"candidate": "C1"})
    assert got["status"] == "ok"
    rows = {r["disclosure_id"]: r for r in got["data"]["rows"]}
    assert rows["DISC-01"]["elapsed_days"] == (date(2026, 11, 26) - date(2026, 3, 10)).days == 261
    assert rows["DISC-01"]["linked_file"] == "input/disclosures/2026-03-10-trade-show-demo.md"
    later = view(_world(today=date(2026, 12, 1)), "read_disclosures", {"candidate": "C1"})
    assert {r["disclosure_id"]: r for r in later["data"]["rows"]}["DISC-01"]["elapsed_days"] == 266
    first = _rows("input/disclosures/history.csv", "C1")
    again = _rows("input/disclosures/history.csv", "C1")
    assert isinstance(first, Ok) and isinstance(again, Ok) and first.reads == again.reads


def test_inputs_are_found_by_content_in_a_world_with_another_layout() -> None:
    w = _world(sources("tilldew"))
    assert [c["candidate"] for c in w.candidates()["candidates"]] == ["TD-1", "TD-2", "TD-3"]
    note = w.note("TD-1")
    assert note["mechanism_heading"] == "How it works" and len(note["mechanism_steps"]) == 4
    assert note["parameters"][0]["Parameter"] == "Low excitation frequency"
    assert note["date"] == "2026-01-19" and note["alternatives"]
    (row,) = [r for r in w.contributor_rows("TD-1")["rows"] if r["person_id"] == "SYN-P-03"]
    assert row["agreement_documents"] == [{"ref": "SYN-AA-2025-04", "found": False}]
    events = {r["disclosure_id"]: r for r in w.disclosure_rows("TD-3")["rows"]}
    assert events["SYN-EV-04"]["date"] is None
    folder = _folder(sources("tilldew"))
    ctx = ToolContext("o1", "patent", "prepare", None, "R-0001/prepare/t01/c01", 1, folder,
                      models=_models("read_form"))  # fmt: skip
    listed = _tools()["list_inputs"].body(ctx, {"kinds": ["form_template"]}).result
    assert isinstance(listed, Ok)
    forms = set()
    for item in listed.data["inputs"]:
        got = _tools()["read_form"].body(ctx, {"template": item["ref"]}).result
        assert isinstance(got, Ok)
        forms.add(got.data["template"])
    assert forms == {"tilldew-idf", "tilldew-filing-sheet"}
    assert [i["relates_to"] for i in w.priorities()["items"]] == ["TD-1", "TD-3", "TD-2"]
    # The publications are on the world's web, not among the inputs: each is read by
    # content from its page as fetch_url reads it (OneRecord), whatever its format.
    assert w.publication_ids() == []
    pages = {}
    for n in range(1, 6):
        rel = f"web/patents.synthetic.example/SYN-PUB-010{n}"
        text = (data.worlds() / "tilldew" / rel).read_text(encoding="utf-8")
        pages[n] = SourceWorld(TODAY).over(
            OneRecord(ReturnedText("web", rel, "v1", text)), None, w.models
        )
    assert [p.publication_ids() for p in pages.values()] == [
        [f"SYN-PUB-010{n}"] for n in range(1, 6)
    ]
    assert pages[4].publication("SYN-PUB-0104")["claims"][1]["depends_on"] == 1


def test_a_disclosure_row_is_quoted_by_its_own_cells_whatever_the_column_order(
    tmp_path: Path,
) -> None:
    """The row's quote ends at its ID, date and candidate cells, wherever those columns sit."""
    (tmp_path / "log.csv").write_text(
        "shown,public,candidate,when,event\n"
        '"A demo, with a poster",yes,INV-12,2026-02-01,EV-7\n'
        "A call,no,INV-3,2026-02-02,EV-8\n",
        encoding="utf-8",
    )
    w = _world(tmp_path)
    (row,) = w.disclosure_rows("INV-12")["rows"]
    assert (row["disclosure_id"], row["date"]) == ("EV-7", "2026-02-01")
    assert row["row_start"] == '"A demo, with a poster",yes,INV-12,2026-02-01,EV-7'
    (other,) = w.disclosure_rows("INV-3")["rows"]
    assert other["row_start"] == "A call,no,INV-3,2026-02-02,EV-8"


def test_st9_claims_at_the_end_of_the_text_are_read(tmp_path: Path) -> None:
    (tmp_path / "pub.pdf.txt").write_text(
        "SYN-Office-9  (11) SYN-PUB-9001 A1\n(54) A probe\n\nClaims\n\n"
        "1. A probe with two electrodes.\n\n2. The probe of claim 1, cleaned by reversal.\n",
        encoding="utf-8",
    )
    claims = _world(tmp_path).publication("SYN-PUB-9001")["claims"]
    assert [(c["number"], c["depends_on"]) for c in claims] == [(1, None), (2, 1)]


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


def test_families_group_by_any_shared_priority_number(w1: SourceWorld) -> None:
    pubs = [w1.publication(p) for p in ("SYN-PUB-0007", "SYN-PUB-0008", "SYN-PUB-0009")]
    assert rules.family_of(pubs) == {
        "SYN-PRI-2019-0630": ["SYN-PUB-0009"],
        "SYN-PRI-2021-1103": ["SYN-PUB-0007", "SYN-PUB-0008"],  # a continuation joins its parent
    }


WORKSHEET = "input/templates/filing-information-worksheet.md"


def _worksheet() -> dict[str, Any]:
    """The worksheet as the shared form tool reads it (``read_form``)."""
    got = _tools()["read_form"].body(_ctx(), {"template": WORKSHEET}).result
    assert isinstance(got, Ok)
    return dict(got.data)


def _ws_fields(**over: Any) -> list[dict[str, Any]]:
    t = _worksheet()
    out = [{"field": f["field"], "value": None, "status": "missing"} for f in t["fields"]]
    for f in out:
        if f["field"] in over:
            f.update(over[f["field"]])
    return out


def test_the_patent_forms_are_declared_as_data_for_the_shared_form_tool() -> None:
    """Which fields only counsel completes is the worksheet's, read by the shared tool."""
    t = _worksheet()
    assert t["template"] == "filing-information-worksheet" and t["ref"] == WORKSHEET
    counsel = [f["field"] for f in t["fields"] if f["default_status"]]
    assert "proposed_inventors" in counsel and "applicant" in counsel
    assert t["statuses"] == ["complete", "missing", "needs_counsel_confirmation"]
    assert FORMS.confirm_status == "needs_counsel_confirmation"


# --- the tools ---------------------------------------------------------------------------


def test_fill_form_refuses_marking_a_counsel_field_complete() -> None:
    fill = _tools()["fill_form"].body
    args = {
        "template": WORKSHEET,
        "key": "C1",
        "fields": _ws_fields(applicant={"value": "Stillroom", "status": "complete"}),
    }
    out = fill(_ctx(), args)
    assert isinstance(out.result, Err) and out.result.code == "confirm_only"
    assert "counsel" in out.result.message
    assert out.saves == ()  # "mark it complete" changes nothing
    asked = {"value": "x", "status": "complete", "counsel_question": "Is this the title?"}
    out = fill(_ctx(), {**args, "fields": _ws_fields(working_title=asked)})
    assert isinstance(out.result, Err) and out.result.code == "confirm_only"
    ok = fill(_ctx(), {**args, "fields": _ws_fields()})
    assert isinstance(ok.result, Ok) and len(ok.saves) == 1
    (save,) = ok.saves
    assert (save.kind, save.key, save.content["counts"]["missing"]) == (
        "form", "C1~filing-information-worksheet", 11,
    )  # fmt: skip
    assert save.content["candidate"] == "C1"
    other = fill(_ctx("C2"), {**args, "fields": _ws_fields()})
    assert isinstance(other.result, Err) and other.result.code == "wrong_key"


def test_send_needs_an_approval_and_goes_through_the_outbox() -> None:
    send = _tools()["send_packet"]
    spec = send.spec
    assert spec.effect == "external" and spec.approval is not None
    assert spec.external is not None and spec.external.semantics == "reconcile"
    assert spec.external.key_fields == ("/packet", "/recipient")
    assert spec.approval.targets is not None
    assert spec.approval.targets({"packet": "patent.packet/C1@v1"}) == ("patent.packet/C1@v1",)
    # the person names their own counsel; the approval binds the exact recipient
    recipient = spec.schema["properties"]["recipient"]
    assert recipient["type"] == "string" and "pattern" in recipient
    out = send.body(_ctx(), {"packet": "patent.packet/C1@v1", "recipient": "counsel-primary"})
    assert isinstance(out.result, Err) and out.result.code == "outbox_only"


def test_the_definition_loads_and_declares_its_source() -> None:
    from aksha.runtime.definitions.loader import load_agents

    root = Path(__file__).resolve().parents[2] / "src" / "aksha" / "agents"
    (loaded,) = load_agents([root / "patent"], [root.parent / "extensions"])
    assert loaded.agent.name == AGENT.name == "patent"
    assert [(s.name, s.collections) for s in AGENT.sources] == [("inputs", ("input",))]
    assert "patent.packet" in loaded.kinds and loaded.kinds["patent.packet"].reviewable


# --- the completion checks -----------------------------------------------------------------


@dataclass
class _V:
    content: dict[str, Any]
    ref: str = ""


@dataclass
class _Log:
    events: tuple[Any, ...] = ()


class _View:
    """The parts of a ``TaskView`` the checks read; ``forms`` are this candidate's, by template."""

    def __init__(self, forms: dict[str, dict[str, Any]] | None = None,
                 **own: dict[str, Any]) -> None:  # fmt: skip
        self.mine = {k: _V(v) for k, v in own.items()}
        self.forms = {f"C1~{t}": _V(v) for t, v in (forms or {}).items()}
        self.key = "C1"
        self.log = _Log()

    def own(self, kind: str) -> _V | None:
        return self.mine.get(kind)

    def latest(self, kind: str, key: str | None = None) -> _V | None:
        return self.mine.get(kind)

    def all_latest(self, kind: str) -> dict[str | None, _V]:
        found: dict[str | None, _V] = {}
        if kind == "form":
            found.update(self.forms)
        return found


def _business(*refs: str) -> dict[str, Any]:
    return {
        "from_research": "read" if refs else "unknown",
        "research": [{"value": r, "cite": {"artifact": r, "path": ""}} for r in refs],
        "summary": "business priority 1",
        "note": rules.RESEARCH_NOTE,
    }


def _rec(action: str = "prioritize_counsel_review", **extra: Any) -> dict[str, Any]:
    return {"next_action": action, "business_importance": _business(), **extra}


def test_the_recommendation_check_vetoes_an_action_the_rules_do_not_give() -> None:
    ext, cmp_ = _ext(), _cmp("not_found")
    good = _View(recommendation=_rec(), extraction=ext, comparison=cmp_)
    assert checks.recommendation_follows_rules(good).ok
    bad = _View(recommendation=_rec("deprioritize_for_now"), extraction=ext, comparison=cmp_)
    result = checks.recommendation_follows_rules(bad)
    assert not result.ok and "prioritize_counsel_review" in result.message


class _ResearchView(_View):
    """A view where the research agent has saved ``research`` (kind to ref)."""

    def __init__(self, research: dict[str, str], **own: dict[str, Any]) -> None:
        super().__init__(**own)
        self.research = {k: _V({"needs": []}, ref) for k, ref in research.items()}

    def latest(self, kind: str, key: str | None = None) -> _V | None:
        return self.research.get(kind) or self.mine.get(kind)


READ = {"research.needs": "research.needs@v2", "research.findings": "research.findings@v1"}


def test_business_importance_cites_the_exact_research_versions_read() -> None:
    """R-36: each research output read is cited by its version; an older version is refused."""
    ext, cmp_ = _ext(), _cmp("not_found")
    cited = {"business_importance": _business("research.needs@v2", "research.findings@v1")}
    good = _ResearchView(READ, recommendation={**_rec(), **cited}, extraction=ext,
                         comparison=cmp_)  # fmt: skip
    assert checks.recommendation_follows_rules(good).ok
    old = {"business_importance": _business("research.needs@v1", "research.findings@v1")}
    stale = _ResearchView(READ, recommendation={**_rec(), **old}, extraction=ext,
                          comparison=cmp_)  # fmt: skip
    result = checks.recommendation_follows_rules(stale)
    assert not result.ok and "research.needs@v2" in result.message
    silent = _ResearchView(READ, recommendation=_rec(), extraction=ext, comparison=cmp_)
    assert not checks.recommendation_follows_rules(silent).ok  # read, so "unknown" is wrong


def test_without_research_business_importance_is_unknown_not_a_failure() -> None:
    """R-36: the patent agent alone records business importance from research as unknown."""
    ext, cmp_ = _ext(), _cmp("not_found")
    alone = _View(recommendation=_rec(), extraction=ext, comparison=cmp_)
    assert checks.recommendation_follows_rules(alone).ok
    claimed = {"business_importance": _business("research.needs@v1")}
    invented = _View(recommendation={**_rec(), **claimed}, extraction=ext, comparison=cmp_)
    result = checks.recommendation_follows_rules(invented)
    assert not result.ok and "has not run" in result.message


@pytest.mark.parametrize(
    "misuse",
    [
        {"novelty": {"value": "new", "cite": {"artifact": "research.needs@v2", "path": ""}}},
        {"why": "no family shows F2, and research.findings@v1 confirms it is new"},
        {"prior_art": ["research.needs@v2"]},
    ],
)
def test_research_cited_as_novelty_or_prior_art_evidence_is_refused(
    misuse: dict[str, Any],
) -> None:
    """R-36: research is not evidence of technical novelty; naming it outside business
    importance is refused, however the rest of the recommendation reads."""
    ext, cmp_ = _ext(), _cmp("not_found")
    cited = {"business_importance": _business("research.needs@v2", "research.findings@v1")}
    rec = {**_rec(), **cited, **misuse}
    result = checks.recommendation_follows_rules(
        _ResearchView(READ, recommendation=rec, extraction=ext, comparison=cmp_)
    )
    assert not result.ok and "not evidence of novelty" in result.message
    assert rules.research_misuse(rec) and not rules.research_misuse({**_rec(), **cited})


def test_the_note_says_research_is_no_novelty_evidence() -> None:
    ext, cmp_ = _ext(), _cmp("not_found")
    part = {**_business(), "note": "research shows demand"}
    rec = {**_rec(), "business_importance": part}
    result = checks.recommendation_follows_rules(
        _View(recommendation=rec, extraction=ext, comparison=cmp_)
    )
    assert not result.ok and rules.RESEARCH_NOTE in result.message


def test_the_recommend_task_declares_the_research_reads_by_kind() -> None:
    """R-55: the reads are declared (so lineage records them) and granted on the agent."""
    from aksha.agents.patent.agent import DEEP

    (recommend,) = [t for t in DEEP.children if t.id == "recommend"]
    kinds = {r.kind if not isinstance(r, str) else r for r in recommend.reads}
    assert set(rules.RESEARCH_KINDS) <= kinds
    assert set(rules.RESEARCH_KINDS) <= set(AGENT.reads)


def _form(*fields: tuple[str, bool, str]) -> dict[str, Any]:
    return {"fields": [{"field": n, "required": r, "status": s} for n, r, s in fields]}


READY = _form(("title", True, "complete"), ("figure_list", True, "needs_counsel_confirmation"))
LATER = _form(("representative", False, "missing"))


def test_the_packet_check_needs_the_counsel_section_first() -> None:
    ok = {"scope_note": "no opinion", "sections": [{"title": "Needs counsel confirmation"}]}
    both = {"disclosure": READY, "worksheet": LATER}
    assert checks.packet_saved(_View(both, packet=ok)).ok
    late = {
        "scope_note": "x",
        "sections": [{"title": "Scope"}, {"title": "Needs counsel confirmation"}],
    }
    assert not checks.packet_saved(_View(both, packet=late)).ok
    bare = {**ok, "scope_note": ""}
    assert not checks.packet_saved(_View(both, packet=bare)).ok


def test_a_missing_required_field_keeps_the_packet_from_ready() -> None:
    ok = {"scope_note": "no opinion", "sections": [{"title": "Needs counsel confirmation"}]}
    gap = _form(("title", True, "complete"), ("figure_list", True, "missing"))
    result = checks.packet_saved(_View({"disclosure": gap, "worksheet": LATER}, packet=ok))
    assert not result.ok and "figure_list" in result.message and result.count == 1
    assert not checks.packet_saved(_View(packet=ok)).ok  # no form filled yet
    # any number of forms: one form, filled, is a ready packet
    assert checks.packet_saved(_View({"own-counsel-form": READY}, packet=ok)).ok


def test_fill_form_records_required_from_the_template_not_the_model() -> None:
    fields = _ws_fields()
    for f in fields:
        f["required"] = False  # the model claims nothing is required
    out = _tools()["fill_form"].body(_ctx(), {"template": WORKSHEET, "key": "C1", "fields": fields})
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


def test_fill_form_takes_the_forms_name_as_read_form_shows_it() -> None:
    """A live model copies ``template`` from read_form's result: the name fills the form too."""
    name = _worksheet()["template"]
    assert name == "filing-information-worksheet"
    args = {"template": name, "key": "C1", "fields": _ws_fields()}
    out = _tools()["fill_form"].body(_ctx(), args)
    assert isinstance(out.result, Ok)
    (save,) = out.saves
    assert save.key == "C1~filing-information-worksheet" and save.content["file"] == WORKSHEET
    gone = _tools()["fill_form"].body(_ctx(), {"template": "no-such-form", "key": "C1",
                                               "fields": []})  # fmt: skip
    assert isinstance(gone.result, Err) and gone.result.paths == ("/template",)


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


def test_the_tools_read_only_through_the_call_reader() -> None:
    out = _tools()["read_form"].body(
        ToolContext("o1", "patent", "scope", None, "R-0001/scope/t01/c01", 1), {"template": "x"}
    )
    assert isinstance(out.result, Err) and out.result.code == "not_found"
    listed = _tools()["list_inputs"].body(
        ToolContext("o1", "patent", "scope", None, "R-0001/scope/t01/c01", 1), {}
    )
    assert isinstance(listed.result, Ok) and listed.result.data["inputs"] == []
    dated = SourceWorld().over(_folder(V1), "2026-11-26", readers())
    assert dated.today == TODAY
    assert dated.disclosure_rows("C1") == _world().disclosure_rows("C1")


def test_the_agent_keeps_only_its_one_external_effect_of_its_own() -> None:
    """Every read and the form tool are the harness's; the agent's own tool is the send."""
    tools = build_tools()
    names = {t.spec.name for t in tools}
    shared = {"web_search", "fetch_url", "list_inputs", "read_document", "search_inputs", "todo",
              "read_form", "fill_form"}  # fmt: skip
    assert names == shared | {"send_packet"}
    own = [t.spec.name for t in tools if t.body.__module__ == "aksha.agents.patent.tools"]
    assert own == ["send_packet"]


def test_a_forms_counts_are_computed_from_its_fields_at_every_save() -> None:
    """ADR-P04 K1: the harness's form kind recomputes ``counts``, so a corrected field moves
    them too; the agent declares it with ``form_kind`` and keeps no status list of its own."""
    (form,) = [k for k in AGENT.artifacts if k.name == "form"]
    assert form.compute is not None and form.keyed
    assert isinstance(form.schema, dict)
    assert form.schema["required"] == ["template", "candidate", "fields", "counts"]
    assert not hasattr(rules, "FIELD_STATUSES") and not hasattr(rules, "computed_form")
    fields = [{"field": "a", "status": "complete"}, {"field": "b", "status": "missing"},
              {"field": "c", "status": "missing"}]  # fmt: skip
    wrong = {"fields": fields, "counts": {"complete": 3, "missing": 0}}
    assert form.compute(wrong)["counts"] == {
        "complete": 1,
        "missing": 2,
        "needs_counsel_confirmation": 0,
    }


def test_the_research_note_is_written_by_code_at_every_save() -> None:
    """ADR-P04 K1: a sentence fixed by code is the kind's, never asked of the model."""
    (rec,) = [k for k in AGENT.artifacts if k.name == "recommendation"]
    assert rec.compute is rules.computed_recommendation
    for given in (None, "research shows demand", rules.RESEARCH_NOTE):
        part = {**_business(), "note": given}
        out = rec.compute({**_rec(), "business_importance": part})
        assert out["business_importance"]["note"] == rules.RESEARCH_NOTE
    missing = {k: v for k, v in _business().items() if k != "note"}
    out = rec.compute({**_rec(), "business_importance": missing})
    assert out["business_importance"]["note"] == rules.RESEARCH_NOTE


def test_a_recommendation_saved_without_the_note_passes_its_check() -> None:
    """The save path's ``computed`` writes the note, so the model never words it."""
    from aksha.runtime.artifacts.store import computed

    (kind,) = [k for k in AGENT.artifacts if k.name == "recommendation"]
    ext, cmp_ = _ext(), _cmp("not_found")
    for note in (None, "research shows demand"):
        saved = computed(kind, {**_rec(), "business_importance": {**_business(), "note": note}})
        view = _View(recommendation=dict(saved), extraction=ext, comparison=cmp_)
        assert checks.recommendation_follows_rules(view).ok
    # Without the part at all, nothing is written and the check still asks for it.
    bare = computed(kind, {"next_action": "prioritize_counsel_review"})
    result = checks.recommendation_follows_rules(
        _View(recommendation=dict(bare), extraction=ext, comparison=cmp_)
    )
    assert not result.ok and rules.BUSINESS_PART in result.message


def test_an_option_label_is_the_next_action_when_the_why_is_structured() -> None:
    """A live model saved a recommendation whose why was an object: the label showed the
    object's Python text. The label is the next action, with the why only when it is text."""

    class Version:
        def __init__(self, content: dict[str, Any]) -> None:
            self.content, self.ref = content, "patent.table@v1"

    class View:
        recs: ClassVar[dict[str, dict[str, Any]]] = {
            "C1": {"next_action": "collect_more_evidence", "why": {"gaps": ["bench log"]}},
            "C2": {"next_action": "prioritize_counsel_review", "why": "Built and  tested."},
        }

        def latest(self, kind: str, key: str | None = None) -> Version | None:
            if kind == "table":
                return Version({"rows": [{"candidate": "C1"}, {"candidate": "C2"}]})
            return Version(self.recs[key]) if key in self.recs else None

    assert rules.selection_options(View()) == (
        ("C1", "collect_more_evidence"),
        ("C2", "prioritize_counsel_review: Built and tested."),
    )
    saved = rules.selection_from_answer(View(), ("C1",))
    assert saved["recommendations"] == {"C1": "collect_more_evidence"}


def test_forms_named_by_file_in_the_plan_match_the_filled_forms() -> None:
    """A plan may name its forms by file (as a live model did); the filled forms are by name."""
    from types import SimpleNamespace

    from aksha.agents.patent import checks

    plan = {
        "forms": ["input/templates/invention-disclosure-form.md", "filing-information-worksheet"]
    }
    filled = {"C1~invention-disclosure-form": object(), "C1~filing-information-worksheet": object()}

    class FormsView:
        key = "C1"

        def latest(self, kind: str) -> Any:
            return SimpleNamespace(content={"plan": plan}) if kind == "portfolio" else None

        def all_latest(self, kind: str) -> dict[str, Any]:
            return filled if kind == "form" else {}

    assert checks.forms_filled(FormsView()).ok
    filled.pop("C1~invention-disclosure-form")
    result = checks.forms_filled(FormsView())
    assert not result.ok and "invention-disclosure-form" in result.message
