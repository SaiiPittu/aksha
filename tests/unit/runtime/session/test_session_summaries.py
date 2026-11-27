"""The read-side projections the HTTP reads compose: sessions, documents and citations."""

from __future__ import annotations

from typing import Any

from aksha.runtime.events.envelope import Event
from aksha.runtime.grounding.facts import bound_citations
from aksha.runtime.inputs.reducers import stored_documents
from aksha.runtime.projections.view import ArtifactsView, LogView
from aksha.runtime.session.views import run_sessions, session_summaries

S1, S2 = "S-0001", "S-0002"


def _view(*rows: tuple[str, dict[str, Any], dict[str, Any]]) -> LogView:
    events = tuple(
        Event(type=t, payload=p, seq=i, owner="o1", at=f"2026-11-27T09:00:{i:02d}Z", boot=1,
              schema_version=1, **env)
        for i, (t, p, env) in enumerate(rows, start=1)
    )  # fmt: skip
    return LogView("o1", len(events), events, ArtifactsView((), len(events)))


def _conversation() -> LogView:
    return _view(
        ("user.message", {"message": "M-0001", "text": "Assess the candidates"}, {"session": S1}),
        ("run.started", {"agent": "patent", "cause": "request"}, {"run": "R-0001", "session": S1}),
        ("user.message", {"message": "M-0002", "text": "Who sells sensors?"}, {"session": S2}),
        ("run.started", {"agent": "research", "cause": "request"},
         {"run": "R-0002", "session": S2}),
        ("run.ended", {"outcome": "complete", "reason": "done"}, {"run": "R-0001"}),
        ("source.changed", {"change_set": "c1", "records": {}}, {}),
        ("run.started", {"agent": "patent", "cause": "rebuild", "for_runs": ["R-0001"]},
         {"run": "R-0003"}),
        ("run.started", {"agent": "patent", "cause": "rebuild", "for_runs": ["R-0003"]},
         {"run": "R-0004"}),
        ("run.ended", {"outcome": "partial", "reason": "x"}, {"run": "R-0004"}),
        ("command.accepted", {"command": "answer"}, {"session": "o1"}),
    )  # fmt: skip


def test_a_rebuild_run_belongs_to_the_sessions_of_its_for_runs_and_chains() -> None:
    belongs = run_sessions(_conversation())
    assert belongs["R-0001"] == {S1} and belongs["R-0002"] == {S2}
    assert belongs["R-0003"] == {S1} and belongs["R-0004"] == {S1}


def test_session_summaries_carry_the_first_message_runs_and_times() -> None:
    by_id = {s.session: s for s in session_summaries(_conversation())}
    assert set(by_id) == {S1, S2}  # the owner-named command receipt is no session
    one = by_id[S1]
    assert one.first_message == "Assess the candidates"
    assert one.runs == ("R-0001", "R-0003", "R-0004")
    assert one.latest is not None and one.latest.run == "R-0004"
    assert one.latest.outcome == "partial" and one.waiting_on == ()
    assert one.started_at == "2026-11-27T09:00:01Z" and one.last_at == "2026-11-27T09:00:09Z"
    assert one.last_seq == 9
    two = by_id[S2]
    assert two.runs == ("R-0002",) and two.latest is not None and two.latest.state == "running"


def test_stored_documents_keep_every_version_with_its_facts() -> None:
    stored = {"detected_type": "text/csv", "size_bytes": 4, "origin": "upload"}
    view = _view(
        ("document.stored", {**stored, "doc_key": "D-0001", "version": 1, "path": "a.csv",
                             "blob": "sha256:" + "1" * 64}, {}),
        ("document.stored", {**stored, "doc_key": "D-0001", "version": 2, "path": "a.csv",
                             "blob": "sha256:" + "2" * 64}, {}),
    )  # fmt: skip
    docs = stored_documents(view)
    assert [(d.doc_key, d.version, d.seq) for d in docs] == [("D-0001", 1, 1), ("D-0001", 2, 2)]
    assert docs[1].blob.endswith("2" * 64) and docs[1].facts["detected_type"] == "text/csv"
    assert docs[0].at == "2026-11-27T09:00:01Z"


def test_bound_citations_are_the_carriers_facts_in_order() -> None:
    def cite(carrier: str, n: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
        return ("citation.bound", {"carrier": carrier, "n": n, "pointer": f"/f{n}",
                                   "form": "span", "target": f"EX-000{n}"}, {})  # fmt: skip

    view = _view(cite("k/a@v1", 1), cite("k/b@v1", 0), cite("k/a@v1", 0))
    assert [c["n"] for c in bound_citations(view, "k/a@v1")] == [0, 1]
    assert bound_citations(view, "k/c@v1") == ()
