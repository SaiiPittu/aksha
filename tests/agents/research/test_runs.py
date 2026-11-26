"""Research runs that go off the happy path: injected text obeyed, a blocked source, no
question to ask, a crash mid-run and a restart while waiting on the founder."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.models.replay import ReplayGenerationModel
from aksha.adapters.search.world import WorldWeb
from aksha.agents.research.fakes import ObedientModel, ResearchModel
from aksha.runtime.canonical import thaw
from aksha.runtime.models.replay import recorded_model_steps
from aksha.runtime.web.types import WebError, web_record_id
from tests.support.research import WORLDS, ResearchBench, world_key

ANSWER = {"geography": "EU"}
SOURCES = WORLDS / "stillroom" / "sources"


class Crash(BaseException):
    """The process dies here: nothing in the runtime catches a ``BaseException``."""


class FlakyWeb(WorldWeb):
    """The world's web, with pages whose fetch fails (``WebError``) or kills the process."""

    def __init__(
        self, *, failing: tuple[str, ...] = (), after: int = 0, crash_on: str | None = None,
        crash_at: int = 1,
    ) -> None:  # fmt: skip
        inner = WorldWeb.of_world(WORLDS / "stillroom")
        super().__init__(inner.pages, inner._read)
        self.failing = failing
        self.after = after  # fetches of a failing page that still succeed first
        self.crash_on = crash_on
        self.crash_at = crash_at  # the fetch of ``crash_on`` that kills the process
        self.fetched: list[str] = []

    def fetch(self, url: str, http: Any) -> Any:
        record = web_record_id(url)
        self.fetched.append(record)
        if record == self.crash_on and self.fetched.count(record) >= self.crash_at:
            self.crash_on = None
            raise Crash(f"killed while fetching {url}")
        if record in self.failing and self.fetched.count(record) > self.after:
            raise WebError("fetch_failed", f"{url} did not answer")
        return super().fetch(url, http)


def _outputs(b: ResearchBench) -> dict[str, Any]:
    return {
        k: thaw(b.content(k))
        for k in ("scope", "competitors", "needs", "matrix", "findings", "tests")
    }


def test_a_model_that_obeys_page_text_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    clean = ResearchBench(tmp_path / "clean", ResearchModel())
    clean.start()
    assert clean.run_to_end(**ANSWER).outcome == "complete"
    obeyed = ResearchBench(tmp_path / "obeyed", ObedientModel())
    obeyed.start()
    assert obeyed.run_to_end(**ANSWER).outcome == "complete"
    refused = [e for e in obeyed.events("step.completed") if e.payload.get("outcome") == "refused"]
    # Every page read that carries the text tempts it once (discover, then the company's own
    # evidence instance); every call is refused before dispatch.
    assert refused and {e.payload["tool"] for e in refused} == {"send_document"}
    # The gate's first layer refuses it: the research agent has no tool of that name.
    assert {(e.payload["layer"], e.payload["code"]) for e in refused} == {(1, "unknown_tool")}
    assert any(str(e.instance).startswith("evidence[") for e in refused)
    for b in (clean, obeyed):
        assert b.events("approval.requested") == [] and b.events("outbox.enqueued") == []
    (s1,), (s2,) = clean.events("run.started"), obeyed.events("run.started")
    assert s1.payload == s2.payload  # tool lists, budgets and stop policies come from here
    assert _outputs(clean)["matrix"] == _outputs(obeyed)["matrix"]
    clean.close()
    obeyed.close()


def test_a_blocked_source_ends_the_run_partial_with_the_gap_named(tmp_path: Path) -> None:
    key = world_key("stillroom")
    (page,) = key["competitors"]["K4"]["pages"]
    # A web page: discovery fetches it once from the search results; the company's own
    # evidence fetch, and every later one, fails.
    b = ResearchBench(tmp_path, ResearchModel(), web=FlakyWeb(failing=(page,), after=1))
    b.start()
    result = b.run_to_end(**ANSWER)
    assert (result.state, result.outcome) == ("ended", "partial")
    company = next(c for c in b.content("competitors")["companies"]
                   if page in [p["record"] for p in c["pages"]])  # fmt: skip
    assert b.latest("profile", company["key"]) is None
    (row,) = [r for r in b.content("matrix")["rows"] if r["key"] == company["key"]]
    (cell,) = row["prices"]
    unknown = cell["unknown"]
    assert unknown["reason"] == "unverified" and "value" not in cell
    assert unknown["failed_step"].startswith(f"R-0001/evidence[{company['key']}]/t01/c")
    assert row["blocker"].endswith(f"evidence[{company['key']}]")
    ended = b.ended()
    assert [u["key"] for u in ended["unknowns"]] == [f"evidence[{company['key']}]"]
    assert len([r for r in b.content("matrix")["rows"] if r["key"] != company["key"]]) == 4
    b.close()


def test_a_request_that_names_the_geography_asks_nothing(tmp_path: Path) -> None:
    request = "Who sells camera-free occupancy sensing for offices in the EU, and at what price?"
    b = ResearchBench(tmp_path, ResearchModel(), request=request)
    b.start()
    result = b.drive()
    assert (result.state, result.outcome) == ("ended", "complete")
    assert b.events("interrupt.raised") == []
    scope = thaw(b.content("scope"))
    assert scope["geography"] == {"value": "EU", "basis": "request", "normalised": "EU"}
    assert scope["asked"] == []
    b.close()


def _model_steps(b: ResearchBench) -> list[str]:
    return [str(e.step) for e in b.events("step.completed") if e.payload.get("kind") == "model"]


def test_a_crash_mid_evidence_resumes_and_repeats_no_recorded_step(tmp_path: Path) -> None:
    whole = ResearchBench(tmp_path / "whole", ResearchModel())
    whole.start()
    assert whole.run_to_end(**ANSWER).outcome == "complete"
    expected = _outputs(whole)
    whole.close()

    page = world_key("stillroom")["competitors"]["K3"]["pages"][0]
    web = FlakyWeb(crash_on=page, crash_at=2)  # discovery's fetch comes before its evidence
    b = ResearchBench(tmp_path / "crash", ResearchModel(), web=web)
    b.start()
    held = b.drive()
    b.answer_open(**ANSWER)
    assert held.state == "held"
    try:
        b.drive()
        raise AssertionError("the run should have died")
    except Crash:
        pass
    before = _model_steps(b)
    b.restart()
    assert b.resume() == ("R-0001",)
    (end,) = b.events("run.ended")
    assert end.payload["outcome"] == "complete"
    after = _model_steps(b)
    assert after[: len(before)] == before and len(after) == len(set(after))
    assert _outputs(b) == expected
    b.close()


def test_a_restart_while_waiting_on_the_founder_asks_once(tmp_path: Path) -> None:
    b = ResearchBench(tmp_path, ResearchModel())
    b.start()
    assert b.drive().state == "held"
    b.restart()
    assert b.drive().state == "held"  # still waiting, nothing new written
    b.answer_open(**ANSWER)
    b.restart()
    result = b.drive()
    assert (result.state, result.outcome) == ("ended", "complete")
    assert len(b.events("interrupt.raised")) == 1
    statement = b.content("scope")["geography"]["cite"]["statement"]
    spans: Mapping[str, Any] = {
        s["span_id"]: s for e in b.events("excerpts.minted") for s in e.payload["spans"]
    }
    assert spans[statement]["text"] == "EU"
    b.close()


def test_a_replay_of_the_recorded_model_steps_gives_the_same_research(tmp_path: Path) -> None:
    first = ResearchBench(tmp_path / "first", ResearchModel())
    first.start()
    assert first.run_to_end(**ANSWER).outcome == "complete"
    recorded = recorded_model_steps(first.events(), "R-0001")
    expected = _outputs(first)
    first.close()
    model = ReplayGenerationModel(recorded)
    again = ResearchBench(tmp_path / "again", model, mode="replay")
    again.start()
    assert again.run_to_end(**ANSWER).outcome == "complete"
    assert sorted(model.calls) == sorted(recorded)
    assert _outputs(again) == expected
    again.close()
