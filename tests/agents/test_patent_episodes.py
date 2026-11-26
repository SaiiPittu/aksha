"""End-to-end episodes of the patent agent on both fixture worlds, graded against key.json.

Every run goes through the real agent loop (stop-policy evaluation, the scheduler, the
policy gate, the step executor and the outbox) with the agent's rule-based model, offline.
The graders read the worlds' answer keys; the agent's tools read only ``sources/``.

1. The happy path (scenario 2): every graded fact is found with its exact quote, every
   trap is handled, the matrix, families and recommendations equal the key, the selection
   is saved before any form, the packet opens with the counsel section, and the send runs
   once, only after an approval bound to the packet version's signature.
2. Change and recovery (scenario 4): switching to stillroom-v2 invalidates exactly C1's
   chain, revokes the approval on C1's packet, moves C1's recommendation as the key says,
   raises a diff review for the packet, and sends nothing twice.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aksha.agents.patent import rules
from aksha.runtime.interrupts.approvals import approvals
from aksha.runtime.invalidation.view import invalidation_view
from tests.support.patent import (
    GAP_ANSWER,
    PatentRig,
    key,
    norm,
    patent_facts,
    quotes,
    saved_contents,
    sources,
)

CANDIDATES = ("C1", "C2", "C3", "C4")


@pytest.fixture(scope="module", params=["stillroom", "stillroom-v2"])
def sent(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[PatentRig]:
    """A full episode on one world: assess, select C1, approve, send (read-only for tests)."""
    r = PatentRig(tmp_path_factory.mktemp(request.param) / "aksha.sqlite", request.param)
    a = r.run_to_approval("C1")
    assert r.approve(a.approval_id, a.signature).accepted
    result = r.drive()
    assert (result.state, result.outcome) == ("ended", "complete")
    yield r
    r.close()


def _fresh(tmp_path: Path, world: str = "stillroom") -> PatentRig:
    return PatentRig(tmp_path / "aksha.sqlite", world)


# --- 1. facts, traps, matrix, families, recommendations --------------------------------


def test_every_graded_fact_is_found_with_its_exact_quote(sent: PatentRig) -> None:
    found = [q for c in saved_contents(sent) for q in quotes(c)]
    for file, quote in found:  # every recorded quote is the source's own text
        raw = (sources(sent.world_name) / file).read_text(encoding="utf-8")
        assert norm(quote) in norm(raw), (file, quote)
    for fact in patent_facts(sent.world_name):
        raw = (sources(sent.world_name) / fact["file"]).read_text(encoding="utf-8")
        exact = fact["quote"]["exact"]
        assert raw[fact["position"]["start"] : fact["position"]["end"]] == exact  # key sanity
        hits = [q for f, q in found if f == fact["file"] and norm(exact) in norm(q)]
        assert hits, f"{fact['id']} {exact!r} not quoted from {fact['file']}"


def test_families_are_grouped_by_priority_with_exact_identifiers(sent: PatentRig) -> None:
    k = key(sent.world_name)
    want = {tuple(f["priority_numbers"]): sorted(f["members"]) for f in k["families"].values()}
    got: dict[tuple[str, ...], list[str]] = {}
    for c in CANDIDATES:
        art = sent.content("prior_art", c)
        for fam in art["families"]:
            got[(fam["family"],)] = sorted(fam["members"])
        for p in art["publications"]:
            raw = (sources(sent.world_name) / p["file"]).read_text(encoding="utf-8")
            assert p["id"] in raw and all(d in raw for d in p["dates"].values()), p["id"]
            if isinstance(p["url"], str):
                assert p["url"] in raw
            else:
                assert "gap" in p["url"]  # no URL in the source: a gap, never invented
    assert got == want


def test_status_wordings_are_kept_verbatim_and_the_conflict_stays_visible(sent: PatentRig) -> None:
    art = sent.content("prior_art", "C3")
    (pub6,) = [p for p in art["publications"] if p["id"] == "SYN-PUB-0006"]
    assert [s["wording"] for s in pub6["status"]] == [
        "Status: Granted",
        "Lapsed - renewal fee not paid (2025-11-30).",
    ]
    fam = next(f for f in art["families"] if "SYN-PUB-0006" in f["members"])
    assert "status wordings differ; each is kept as written" in fam["differences"]
    b = next(f for f in art["families"] if "SYN-PUB-0004" in f["members"])
    assert b["status_wordings"] == {
        "SYN-PUB-0003": ["Pending"],
        "SYN-PUB-0004": ["Application deemed to be withdrawn"],
    }


def _family_letter(world: str) -> dict[str, str]:
    return {f["priority_numbers"][0]: letter for letter, f in key(world)["families"].items()}


def test_comparison_matrix_equals_the_key(sent: PatentRig) -> None:
    letters = _family_letter(sent.world_name)
    want = key(sent.world_name)["comparison_matrix"]
    for c in CANDIDATES:
        cmp_ = sent.content("comparison", c)
        got = {(x["element"], letters[x["family"]]): x["state"] for x in cmp_["cells"]}
        expected = {(e, f): s.split()[0] for e, cells in want[c].items() for f, s in cells.items()}
        assert got == expected, c
        for x in cmp_["cells"]:  # a non-empty state carries its excerpt
            assert (x["state"] == "not_found") == (x["evidence"] == []), (c, x["element"])
        assert "not a finding that the element is new" in cmp_["note"]
    c1 = sent.content("comparison", "C1")["closest_families"]
    closest = {"stillroom": ["A"], "stillroom-v2": ["A", "E"]}[sent.world_name]
    assert [letters[f] for f in c1] == closest


def test_recommendations_equal_the_key_for_all_four(sent: PatentRig) -> None:
    want = key(sent.world_name)["recommendations"]
    table = sent.content("table")
    assert [r["candidate"] for r in table["rows"]] == list(CANDIDATES)
    for c in CANDIDATES:
        rec = sent.content("recommendation", c)
        assert rec["next_action"] == want[c]["next_action"], c
        assert rec["why"] and rec["would_change_if"]


def test_grace_period_fact_is_dates_and_days_only(sent: PatentRig) -> None:
    ext = sent.content("extraction", "C1")
    (demo,) = [d for d in ext["disclosures"] if d["event_id"] == "DISC-01"]
    assert (demo["date"], demo["elapsed_days"], demo["as_of"]) == ("2026-03-10", 261, "2026-11-26")
    assert (date(2026, 11, 26) - date(2026, 3, 10)).days == 261
    assert demo["own_disclosure"] and demo["public"] == "yes"
    assert demo["status"] == "needs_counsel_confirmation"
    assert sorted(demo["elements_shown"]) == ["F2", "F3"]
    for c in CANDIDATES:  # the venture's own disclosure is never cited as prior art
        assert all(
            p["id"].startswith("SYN-PUB-") for p in sent.content("prior_art", c)["publications"]
        )
    c4 = sent.content("extraction", "C4")
    (undated,) = [d for d in c4["disclosures"] if d["event_id"] == "DISC-04"]
    assert undated["date"] is None and undated["status"] == "needs_counsel_confirmation"


def test_consultant_suggestion_is_raised_for_counsel(sent: PatentRig) -> None:
    ext = sent.content("extraction", "C1")
    (o,) = ext["outside_contributions"]
    k = key(sent.world_name)
    email = next(f for f in k["facts"] if f["id"] == "KF-06")
    assert o["person_id"] == "P-05" and o["date"] == email["value"]
    assert o["before_conception_record"] is True  # the email predates the 14 Jan note
    assert sorted(o["elements"]) == ["F2", "F3"]
    assert o["before_agreement_effective"] is (sent.world_name == "stillroom-v2")
    assert o["status"] == "needs_counsel_confirmation"
    (cr07,) = [r for r in ext["contributors"] if r["person_id"] == "P-05"]
    assert cr07["contributed"] == "Reviewed the thermal noise model"  # understated, kept as written
    topics = {q["topic"] for q in ext["counsel_questions"]}
    assert {"inventorship", "ownership", "outside contributor"} <= topics
    form = {f["field"]: f for f in sent.content("disclosure", "C1")["fields"]}
    assert form["contributors"]["status"] == "needs_counsel_confirmation"
    assert form["outside_resources"]["status"] == "needs_counsel_confirmation"
    if sent.world_name == "stillroom-v2":
        assert "outside the agreement" in o["counsel_question"]


def test_gaps_are_asked_never_filled(sent: PatentRig) -> None:
    c1 = sent.content("extraction", "C1")
    (why,) = [g for g in c1["missing_details"] if g["quote"] == "Why 20 minutes: to be written up."]
    assert why["value"] is None and why["status"] == "missing" and why["gap"]["question"]
    c2 = sent.content("extraction", "C2")
    (vent,) = [p for p in c2["parameters"] if p["name"] == "Minimum distance from a vent"]
    # Asked, not filled: the value is the person's relayed answer, cited as their statement.
    assert (vent["value"], vent["status"]) == (GAP_ANSWER, "complete")
    (asked,) = [e for e in sent.events("interrupt.raised") if e.payload["kind"] == "input"]
    assert vent["statement"] == {"interrupt": asked.payload["interrupt"], "answer": GAP_ANSWER,
                                 "by": "person"}  # fmt: skip
    (doc,) = c2["missing_documents"]
    assert (doc["name"], doc["status"]) == ("placement-trial-02.csv", "referenced_not_found")
    assert c2["implementation"]["status"] == "idea_only"
    (agreement,) = c1["agreements"]
    assert (agreement["ref"], agreement["document"]) == ("CA-2025-11", "referenced_not_found")


def test_forms_report_every_field_and_refuse_nothing_silently(sent: PatentRig) -> None:
    counts = {
        "disclosure": {"complete": 10, "missing": 0, "needs_counsel_confirmation": 4},
        "worksheet": {"complete": 2, "missing": 3, "needs_counsel_confirmation": 6},
    }
    for kind, n in (("disclosure", 14), ("worksheet", 11)):
        form = sent.content(kind, "C1")
        assert len(form["fields"]) == n
        assert form["counts"] == counts[kind]
        assert rules.required_missing(form) == []  # only optional fields wait "until known"
        for f in form["fields"]:
            assert f["status"] in ("complete", "missing", "needs_counsel_confirmation")
            if f["status"] == "complete":
                assert f["value"] not in (None, "", [], {})
    ws = {f["field"]: f["status"] for f in sent.content("worksheet", "C1")["fields"]}
    for field in (
        "applicant",
        "proposed_inventors",
        "ownership_and_assignments",
        "outside_party_agreements",
        "earlier_public_disclosures",
    ):
        assert ws[field] == "needs_counsel_confirmation"


def test_packet_opens_with_counsel_section_and_scope_note(sent: PatentRig) -> None:
    pkt = sent.content("packet", "C1")
    assert pkt["sections"][0]["title"] == "Needs counsel confirmation"
    assert pkt["sections"][0]["items"]
    note = pkt["scope_note"]
    for phrase in (
        "no patentability or freedom-to-operate opinion",
        "drafted no claims",
        "set no legal deadline",
        "filed nothing",
    ):
        assert phrase in note


FORBIDDEN = [
    r"\bis (likely )?patentable\b",
    r"\bis novel\b",
    r"\bfile by\b",
    r"\bfiling deadline\b",
    r"patentability score",
    r"\bfree to operate\b",
    r"\bwe claim\b",
    r"\bwhat is claimed is\b",
    r"\d+\s?% of (the )?market",
]


def test_no_forbidden_output_anywhere(sent: PatentRig) -> None:
    import json

    for content in saved_contents(sent):
        text = json.dumps(content).casefold()
        for pattern in FORBIDDEN:
            assert not re.search(pattern, text), (pattern, content.get("candidate"))


def test_the_selection_comes_before_any_form_and_one_send_happens(sent: PatentRig) -> None:
    saved = [e.payload["ref"] for e in sent.events("artifact.saved")]
    assert saved.index("patent.selection@v1") < saved.index("patent.disclosure/C1@v1")
    assert sent.content("selection")["selected"] == ["C1"]
    assert not [
        r
        for r in saved
        if r.endswith(("/C2@v1", "/C3@v1", "/C4@v1"))
        and r.split("/")[0] in ("patent.disclosure", "patent.packet")
    ]
    assert len(sent.ledger.rows) == 1
    (row,) = sent.ledger.rows.values()
    assert (row["packet"], row["recipient"]) == ("patent.packet/C1@v1", "counsel-primary")
    (a,) = approvals(sent.view()).values()
    assert a.state == "used" and a.body.target_refs() == ("patent.packet/C1@v1",)
    assert len(sent.events("outbox.enqueued")) == 1


def test_only_extract_c2_waits_for_its_gap_question(tmp_path: Path) -> None:
    r = _fresh(tmp_path)
    try:
        r.start()
        assert r.drive().state == "held"
        (gap,) = r.open_interrupts("input")  # one targeted question, on C2's extraction only
        assert gap.instance == "candidate[C2]/extract"
        assert "Minimum distance from a vent" in gap.payload["questions"][0]["text"]
        for c in ("C1", "C3", "C4"):  # the others went on while C2 waited
            assert r.current("recommendation", c) is not None, c
        assert r.current("extraction", "C2") is None and r.current("table") is None
        assert r.answer_gap().accepted
        assert r.drive().state == "held" and r.open_interrupts("choice")
        assert len([e for e in r.events("interrupt.raised") if e.payload["kind"] == "input"]) == 1
        c1 = r.content("extraction", "C1")  # an open rationale stays a question, unasked
        assert any(g["quote"] == "Why 20 minutes: to be written up." for g in c1["missing_details"])
    finally:
        r.close()


# --- 2. approval binding -----------------------------------------------------------------


def test_no_send_without_an_approval_bound_to_the_packet_signature(tmp_path: Path) -> None:
    r = _fresh(tmp_path)
    try:
        a = r.run_to_approval("C1")
        assert r.drive().state == "held"  # waiting: driving again sends nothing
        assert r.ledger.rows == {}
        wrong = r.approve(a.approval_id, "v1:" + "0" * 64)
        assert not wrong.accepted and wrong.refused is not None
        assert wrong.refused == "signature_mismatch"
        assert r.drive().state == "held" and r.ledger.rows == {}
        assert dict(a.body.facts) == {
            "packet": "patent.packet/C1@v1",
            "recipient": "counsel-primary",
        }
        assert r.approve(a.approval_id, a.signature).accepted
        assert r.drive().outcome == "complete" and len(r.ledger.rows) == 1
    finally:
        r.close()


# --- 3. change and recovery ----------------------------------------------------------------

C1_CHAIN = (
    "extraction",
    "prior_art",
    "comparison",
    "recommendation",
    "disclosure",
    "worksheet",
    "packet",
)


def test_switching_worlds_invalidates_exactly_the_c1_chain(tmp_path: Path) -> None:
    r = _fresh(tmp_path)
    try:
        a = r.run_to_approval("C1")
        assert r.approve(a.approval_id, a.signature).accepted  # approved, not yet sent
        before = {e.payload["ref"] for e in r.events("artifact.saved")}
        assert r.switch("stillroom-v2").accepted
        iv = invalidation_view(r.view())
        touched = set(iv.refs("invalidated")) | set(iv.refs("dirty"))
        expected = {f"patent.{k}/C1@v1" for k in C1_CHAIN} | {
            "patent.table@v1",
            "patent.selection@v1",
        }
        assert touched == expected
        for c in ("C2", "C3", "C4"):  # the key's unchanged set
            for k in ("extraction", "prior_art", "comparison", "recommendation"):
                assert iv.state(_ref(r, k, c)).freshness == "current", (k, c)
        assert iv.state(_ref(r, "portfolio", None)).freshness == "current"

        result = r.drive()  # the open run regenerates its own work in place
        assert result.state == "ended"
        rebuilt = {e.payload["ref"] for e in r.events("artifact.saved")} - before
        assert rebuilt == {f"patent.{k}/C1@v2" for k in C1_CHAIN} | {
            "patent.table@v2",
            "patent.selection@v2",
        }
        # The approval on C1's packet is revoked; nothing was sent.
        (revoked,) = r.events("approval.revoked")
        assert (revoked.payload["approval"], revoked.payload["target"]) == (
            a.approval_id,
            "patent.packet/C1@v1",
        )
        assert r.ledger.rows == {} and r.events("outbox.enqueued") == []
        # C1's recommendation changes as the key says; the others stay as they were.
        k = key("stillroom-v2")["expected_after_change"]["C1_next_action"]
        assert r.content("recommendation", "C1")["next_action"] == k["after"]
        assert r.content("selection")["selected"] == ["C1"]  # the person's choice is kept
        cmp_ = r.content("comparison", "C1")
        assert "SYN-PRI-2023-0215" in cmp_["families"]
        # Family E ties family A at the top (both show four of the five elements, two of
        # them in full): the tie is reported, never broken by sort order.
        assert cmp_["closest_families"] == ["SYN-PRI-2021-0412", "SYN-PRI-2023-0215"]
        # A human diff review appears for the reviewed packet; v1 stays current until accepted.
        (review,) = r.reviews().values()
        assert (str(review.old), str(review.revision)) == (
            "patent.packet/C1@v1",
            "patent.packet/C1@v2",
        )
        assert str(r.current("packet", "C1").ref) == "patent.packet/C1@v1"  # type: ignore[union-attr]
        refused = r.handoff(a.approval_id)
        assert not refused.accepted and r.ledger.rows == {}

        # The person accepts the redline: a fresh approval request, never a grant.
        assert r.accept(review.interrupt, review.card_hash).accepted
        fresh = r.pending_approval()
        assert fresh.body.target_refs() == ("patent.packet/C1@v2",)
        assert fresh.signature != a.signature
        assert [e.payload["approval"] for e in r.events("approval.granted")] == [a.approval_id]
        again_v1 = r.approve(a.approval_id, a.signature)  # the person repeats the old click
        assert again_v1.duplicate  # the first receipt comes back; nothing new is granted
        assert approvals(r.view())[a.approval_id].state == "revoked"
        assert r.approve(fresh.approval_id, fresh.signature).accepted
        assert r.ledger.rows == {}  # approving is not sending
        assert r.rebuild() == ()  # settled: no rebuild run is left to open
    finally:
        r.close()


def _ref(r: PatentRig, kind: str, key_: str | None) -> Any:
    v = r.current(kind, key_)
    assert v is not None, (kind, key_)
    return v.ref


def test_the_fresh_approval_for_the_new_packet_sends_it_once(tmp_path: Path) -> None:
    r = _fresh(tmp_path)
    try:
        a = r.run_to_approval("C1")
        assert r.approve(a.approval_id, a.signature).accepted
        r.switch("stillroom-v2")
        r.drive()
        (review,) = r.reviews().values()
        assert r.accept(review.interrupt, review.card_hash).accepted
        fresh = r.pending_approval()
        # The fresh request names the new version in its call and its facts, not only in
        # its targets, so the person approves what execution will carry out.
        assert dict(fresh.body.facts)["packet"] == "patent.packet/C1@v2"
        assert '"patent.packet/C1@v2"' in fresh.body.canonical_args
        assert r.approve(fresh.approval_id, fresh.signature).accepted
        assert r.handoff(fresh.approval_id).accepted
        (row,) = r.ledger.rows.values()
        assert row["packet"] == "patent.packet/C1@v2"
        again = r.handoff(fresh.approval_id)  # the same command again: its first receipt
        assert again.duplicate and len(r.ledger.rows) == 1
    finally:
        r.close()


# --- 4. restart mid-approval and mid-send (scenario 3) ----------------------------------


def test_restart_mid_approval_and_mid_send_sends_once(tmp_path: Path) -> None:
    from tests.support.steps import Crash

    r = _fresh(tmp_path)
    try:
        a = r.run_to_approval("C1")
        r.restart()  # killed while the approval request is on screen
        assert r.recover() == ()  # suspended: recovery writes nothing and drives nothing
        again = r.pending_approval()
        assert (again.approval_id, again.signature) == (a.approval_id, a.signature)
        assert r.drive().state == "held"
        assert r.approve(a.approval_id, a.signature).accepted
        r.ledger.crash_after_apply = True  # the mailbox records the send, then the process dies
        with pytest.raises(Crash):
            r.drive()
        assert len(r.ledger.rows) == 1
        r.restart()
        r.ledger.crash_after_apply = False
        r.recover()  # reconcile asks the mailbox by key and records the receipt
        assert r.ledger.applied.count(next(iter(r.ledger.rows))) == 1  # never applied again
        assert len(r.ledger.lookups) >= 1
        result = r.drive()
        assert (result.state, result.outcome) == ("ended", "complete")
        assert len(r.ledger.rows) == 1
        assert r.content("handoff", "C1")["status"] == "sent"
        assert approvals(r.view())[a.approval_id].state == "used"
        repeat = r.approve(a.approval_id, a.signature, command_id="second-click")
        assert (repeat.accepted, repeat.refused) == (False, "already_decided")
        assert len(r.ledger.rows) == 1 and len(r.events("approval.granted")) == 1
    finally:
        r.close()
