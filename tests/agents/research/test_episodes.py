"""End-to-end research episodes on both fixture worlds, graded against each world's key.

The run goes through the real loader, the agent loop, the policy gate, the step executor,
the artifact store's grounding and a SQLite store, with the offline rule-based model. The
tools read only ``sources/``; these graders read ``key.json``. The renamed world
(``stillroom-v2``) must give the same research with other names (invariance).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aksha.agents.research.fakes import ResearchModel
from tests.support.research import WORLDS, ResearchBench, world_key

WORLD_NAMES = ("stillroom", "stillroom-v2")
ANSWER = {"geography": "EU, Germany and the Netherlands first"}
CELLS = ("unit_price", "subscription", "power", "battery_life", "privacy")


@pytest.fixture(scope="module", params=WORLD_NAMES)
def run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ResearchBench]:
    b = ResearchBench(tmp_path_factory.mktemp(request.param), ResearchModel(), world=request.param)
    b.start()
    result = b.run_to_end(**ANSWER)
    assert (result.state, result.outcome) == ("ended", "complete")
    yield b
    b.close()


def _fact(key: Mapping[str, Any], fid: str) -> Mapping[str, Any]:
    found: Mapping[str, Any] = next(f for f in key["facts"] if f["id"] == fid)
    return found


def _company_of(b: ResearchBench, kid: str) -> Mapping[str, Any]:
    """Our company record for the key's competitor ``kid``, matched by its pages."""
    pages = set(world_key(b.world)["competitors"][kid]["pages"])
    companies = b.content("competitors")["companies"]
    (found,) = [c for c in companies if pages & {p["record"] for p in c["pages"]}]
    out: Mapping[str, Any] = found
    return out


def _row(b: ResearchBench, kid: str) -> Mapping[str, Any]:
    key = _company_of(b, kid)["key"]
    (row,) = [r for r in b.content("matrix")["rows"] if r["key"] == key]
    out: Mapping[str, Any] = row
    return out


def _quotes(entries: list[Any]) -> list[str]:
    return [e["cite"]["quote"] for e in entries if "cite" in e and "quote" in e["cite"]]


# --- scope ---------------------------------------------------------------------------


def test_scope_asks_one_material_question_and_cites_the_answer(run: ResearchBench) -> None:
    raised = run.events("interrupt.raised")
    assert [[q["id"] for q in e.payload["questions"]] for e in raised] == [["geography"]]
    scope = run.content("scope")
    assert scope["segment"] == {"value": "offices", "basis": "request"}
    geography = scope["geography"]
    assert (geography["basis"], geography["normalised"]) == ("answer", "EU")
    assert set(geography["cite"]) == {"statement"}
    out = scope["boundary"]["out"]
    assert out and out[0]["value"].startswith("Not a priority in H1 2026")


# --- discover: the duplicate competitor trap ------------------------------------------


def test_five_companies_on_six_pages_one_merged_with_its_excerpt(run: ResearchBench) -> None:
    key = world_key(run.world)
    found = run.content("competitors")
    assert (found["resolved"], found["pages_found"]) == (
        key["counts"]["competitors_resolved"], key["counts"]["competitor_pages"],
    )  # fmt: skip
    for kid, truth in key["competitors"].items():
        ours = _company_of(run, kid)
        assert ours["canonical"]["value"] == truth["canonical"]
        assert sorted(p["record"] for p in ours["pages"]) == sorted(truth["pages"])
        assert set(truth.get("aliases", ())) <= {a["value"] for a in ours["aliases"]}
    (merge,) = [
        e.payload
        for e in run.events("relation.saved")
        if e.payload["kind"].endswith("same_entity_as")
    ]
    k5 = key["competitors"]["K5"]["pages"]
    assert {merge["src"]["id"], merge["dst"]["id"]} == set(k5)
    evidence = _fact(key, "KF-23")["quote"]["exact"]
    (excerpt,) = merge["evidence"]
    assert evidence in excerpt["quote"] and excerpt["span"].startswith("EX-")


def test_manual_alternatives_not_found_is_an_explicit_unknown(run: ResearchBench) -> None:
    unknown = run.content("competitors")["manual_alternatives"]["unknown"]
    assert unknown["reason"] == "not_found" and len(unknown["checked"]) == 6


# --- prices -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kid", "fid"),
    [("K1", "KF-17"), ("K2", "KF-18"), ("K3", "KF-19"), ("K5", "KF-21"), ("K5", "KF-22")],
)
def test_each_listed_price_is_cited_on_its_company_row(
    run: ResearchBench, kid: str, fid: str
) -> None:
    fact = _fact(world_key(run.world), fid)
    row = _row(run, kid)
    (entry,) = [
        e for e in row["unit_price"] if e.get("cite", {}).get("quote") == fact["quote"]["exact"]
    ]
    assert entry["value"] == str(fact["value"]["amount"])
    assert entry["currency"] == fact["value"]["currency"]
    assert entry["record"] == fact["file"] and entry["label"] == "fact"


def test_conflicting_prices_stay_side_by_side_with_their_dates(run: ResearchBench) -> None:
    key = world_key(run.world)
    row = _row(run, "K5")
    dated = {(e["value"], e["published"]) for e in row["unit_price"] if "value" in e}
    assert dated == {
        ("229", _fact(key, "KF-21")["value"]["page_published"]),
        ("199", _fact(key, "KF-22")["value"]["page_published"]),
    }
    assert row["price_conflict"] is True
    (rel,) = [
        e.payload for e in run.events("relation.saved") if e.payload["kind"].endswith("contradicts")
    ]
    assert sorted(x["quote"] for x in rel["evidence"]) == ["USD 199 per unit", "USD 229 per unit"]
    others = [r for r in run.content("matrix")["rows"] if r["key"] != row["key"]]
    assert not any(r["price_conflict"] for r in others)


def test_an_unlisted_price_is_unknown_never_zero(run: ResearchBench) -> None:
    key = world_key(run.world)
    (cell,) = _row(run, "K4")["unit_price"]
    assert (cell["status"], cell["reason"]) == ("unknown", "not_found")
    assert "value" not in cell and cell["cite"]["artifact"].startswith("research.profile/")
    profile = run.content("profile", _company_of(run, "K4")["key"])
    (u,) = profile["unit_price"]
    assert set(u) == {"unknown"} and u["unknown"]["checked"]
    contact = _fact(key, "KF-20")["quote"]["exact"]
    assert [n["value"] for n in profile["price_notes"]] == [contact]
    assert all(contact not in f["text"]["value"] for f in profile["flags"])  # not an instruction


# --- claims, injected text ------------------------------------------------------------


def test_vendor_claim_and_measured_fact_are_both_shown_and_labelled(run: ResearchBench) -> None:
    key = world_key(run.world)
    cell = _row(run, "K3")["battery_life"]
    claim, fact = _fact(key, "KF-24")["quote"]["exact"], _fact(key, "KF-25")["quote"]["exact"]
    labels = {e["label"] for e in cell if claim in e["cite"]["quote"]}
    assert labels == {"vendor_claim"}
    assert {e["label"] for e in cell if fact in e["cite"]["quote"]} == {"fact"}
    ids = [f["id"] for f in run.content("findings")["findings"]]
    assert f"claim-vs-fact-{_company_of(run, 'K3')['key']}" in ids


def test_instruction_like_text_is_flagged_and_changes_nothing(run: ResearchBench) -> None:
    key = world_key(run.world)
    profile = run.content("profile", _company_of(run, "K4")["key"])
    (flag,) = profile["flags"]
    assert _fact(key, "KF-26")["quote"]["exact"] in flag["text"]["cite"]["quote"]
    assert run.events("approval.requested") == [] and run.events("outbox.enqueued") == []
    assert "send" not in {t for task in run.agent.tasks for t in task.tools}
    called = {
        e.payload.get("tool")
        for e in run.events("step.completed")
        if e.payload.get("kind") == "tool"
    }
    assert called <= {
        "search",
        "fetch_page",
        "read_reviews",
        "read_note",
        "save_artifact",
        "finish",
        "ask",
    }
    refused = [e for e in run.events("step.completed") if e.payload.get("outcome") == "refused"]
    assert refused == []


# --- reviews --------------------------------------------------------------------------


def test_reviews_counted_once_reported_as_this_sample(run: ResearchBench) -> None:
    key = world_key(run.world)
    sample = run.content("needs")["sample"]
    assert (sample["rows"], sample["unique"]) == (
        key["counts"]["review_rows"],
        key["counts"]["reviews_unique"],
    )
    (trap,) = [t for t in key["traps"] if t["id"] == "T-REVIEWDUP"]
    assert sorted([d["review"], d["duplicate_of"]] for d in sample["duplicates"]) == sorted(
        trap["pairs"]
    )
    needs = {n["theme"]: n for n in run.content("needs")["needs"]}
    assert {t: n["count"] for t, n in needs.items()} == key["counts"]["review_themes_unique"]
    battery = needs["battery"]
    assert battery["phrase"] == "7 of 40 reviews in this sample"
    assert _fact(key, "KF-32")["quote"]["exact"] in _quotes(battery["evidence"])
    assert battery["first"] <= battery["last"] and battery["geographies"]
    assert battery["in_scope_geography"]["geography"] == "EU"


def test_no_output_states_a_market_share(run: ResearchBench) -> None:
    said = [n["phrase"] for n in run.content("needs")["needs"]]
    said += [f["statement"] for f in run.content("findings")["findings"]]
    said += [t["why"] for t in run.content("tests")["tests"]]
    assert said and not [s for s in said if "%" in s or "customers want" in s.lower()]


# --- grounding and the four outputs ---------------------------------------------------


def test_every_minted_span_is_the_stored_page_text_at_its_offsets(run: ResearchBench) -> None:
    sources = WORLDS / run.world / "sources"
    spans = [
        s
        for e in run.events("excerpts.minted")
        for s in e.payload["spans"]
        if s["source"] == "corpus"
    ]
    assert spans
    for s in spans:
        raw = (sources / s["record_id"]).read_text(encoding="utf-8")
        assert raw[s["start"] : s["end"]] == s["text"]
    bound = run.events("citation.bound")
    assert len(bound) > 50
    by_id = {s["span_id"]: s for e in run.events("excerpts.minted") for s in e.payload["spans"]}
    for b in bound:
        if b.payload["form"] == "span":
            assert " ".join(b.payload["quote"].split()) in " ".join(
                by_id[b.payload["target"]]["text"].split()
            )


def test_matrix_every_cell_is_cited_or_an_explicit_unknown(run: ResearchBench) -> None:
    matrix = run.content("matrix")
    assert matrix["resolved"] == 5 and len(matrix["rows"]) == 5
    for row in matrix["rows"]:
        assert "span" in row["company"]["cite"]
        for col in CELLS:
            assert row[col], (row["key"], col)
            for entry in row[col]:
                assert "cite" in entry or set(entry) == {"unknown"}


def test_findings_name_their_unknowns_and_tests_are_proposals(run: ResearchBench) -> None:
    findings = run.content("findings")
    reasons = sorted(u.get("reason") or u["unknown"]["reason"] for u in findings["unknowns"])
    assert reasons == ["not_checked", "not_found", "not_found"]
    assert all(f["evidence"] for f in findings["findings"])
    assert {f["label"] for f in findings["findings"]} <= {"fact", "vendor_claim", "inference"}
    tests = run.content("tests")["tests"]
    assert tests and all(t["threshold"]["status"] == "proposal" for t in tests)


def test_the_run_stays_inside_its_budget_with_one_decision_per_turn_end(run: ResearchBench) -> None:
    steps = run.events("step.completed")
    models = [e for e in steps if e.payload.get("kind") == "model"]
    tools = [e for e in steps if e.payload.get("kind") == "tool"]
    assert len(models) <= 45 and len(tools) <= 70
    assert len({e.step for e in models}) == len(models)  # no model step twice
    judged = [e for e in run.events("stop.decided") if e.payload.get("after_turn") is not None]
    ends = {(e.payload.get("candidate_instance"), e.payload["after_turn"]) for e in judged}
    assert len(ends) == len(judged)


# --- invariance on the renamed world --------------------------------------------------


def _shape(b: ResearchBench) -> dict[str, Any]:
    """The research with every name replaced by the key's competitor ID."""
    key = world_key(b.world)
    ours = {_company_of(b, kid)["key"]: kid for kid in key["competitors"]}
    rows = {}
    for row in b.content("matrix")["rows"]:
        rows[ours[row["key"]]] = {
            col: sorted(
                (
                    e.get("label") or e.get("reason") or e["unknown"]["reason"],
                    e.get("value") if col in ("unit_price", "subscription") else None,
                )
                for e in row[col]
            )
            for col in CELLS
        } | {"conflict": row["price_conflict"], "aliases": len(row["aliases"])}
    findings = sorted(
        (f["label"], len(f["evidence"]), f["id"].split("-", 1)[0])
        for f in b.content("findings")["findings"]
    )
    needs = [
        (n["theme"], n["count"], n["of"], n["phrase"], n["first"], n["last"])
        for n in b.content("needs")["needs"]
    ]
    tests = [(t["id"], t["theme"], t["threshold"]["value"]) for t in b.content("tests")["tests"]]
    return {"rows": rows, "findings": findings, "needs": needs, "tests": tests}


def test_the_renamed_world_gives_the_same_research(tmp_path: Path) -> None:
    shapes = []
    for world in WORLD_NAMES:
        b = ResearchBench(tmp_path / world, ResearchModel(), world=world)
        b.start()
        assert b.run_to_end(**ANSWER).outcome == "complete"
        shapes.append(_shape(b))
        b.close()
    assert shapes[0] == shapes[1]
