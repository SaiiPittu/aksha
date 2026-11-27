"""The portfolio kit end to end: a supervisor Task over per-item workers (ADR-P15 D7 S5).

Through the real agent loop on SQLite with a rule model (``tests/support/portfolio.py``):
20 synthetic items, a triage tier for every item, a deep tier for the admitted ones and
the audit sample, and one planted wrong drop (``SYN-I-13``: triage scores it 1, deep work
finds it worth keeping).

1. The supervisor's requests hold the work board and its own turns, never a worker's
   transcript, tool result or document.
2. Tiers save model calls against running deep work for every item, measured on two runs.
3. The audit catches the planted wrong drop; the goal re-opens it; the review reports the
   miss rate.
4. The goal holds: a supervisor that never decides cannot finish, and the run does not
   complete; one that ignores the audit cannot finish either.
5. Code refuses a commit past the tier cap, and the refused commit writes nothing.
6. A directive records no read: it is a decision, so nothing marks it stale.
"""

from __future__ import annotations

import json
from pathlib import Path

from aksha.runtime.context.assembly import RECITATION
from tests.support.portfolio import (
    PLANTED,
    FleetBench,
    FleetModel,
    board_of,
    items,
    spec,
)


def _run(tmp_path: Path, model: FleetModel, **changes: object) -> FleetBench:
    bench = FleetBench(tmp_path / "a.sqlite", model, spec(**changes))
    bench.result = bench.run()  # type: ignore[attr-defined]
    return bench


def _lead_requests(bench: FleetBench) -> list[object]:
    return [r for r in bench.model.requests if r.step_id.split("/")[1] == "lead"]


def test_the_supervisor_reads_only_the_board(tmp_path: Path) -> None:
    bench = _run(tmp_path, FleetModel(items()))
    leads = _lead_requests(bench)
    assert leads, "the supervisor took no turn"
    for request in leads:
        texts = [m.text or "" for m in request.messages]  # type: ignore[attr-defined]
        assert any(t.startswith(RECITATION) for t in texts)
        joined = "\n".join(texts)
        assert "a long synthetic document" not in joined  # a worker's tool result
        assert not any(t.startswith("Artifacts this Task reads") for t in texts)
        tool_results = [m for m in request.messages if m.role == "tool"]  # type: ignore[attr-defined]
        assert all(r.call_id.startswith("call-commit_round") or r.call_id.startswith("call-finish")
                   for r in tool_results)  # fmt: skip
        counts, rows, _ = board_of(request)  # type: ignore[arg-type]
        assert counts["items"] == 20 and len(rows) == 20
    bench.close()


def test_tiers_save_model_calls_against_running_everything(tmp_path: Path) -> None:
    tiered = _run(tmp_path / "t", FleetModel(items()))
    everything = _run(tmp_path / "e", FleetModel(items()), supervise_above=1_000)
    assert tiered.result.outcome == everything.result.outcome == "complete"  # type: ignore[attr-defined]
    deep_tiered, deep_all = tiered.turns("deep"), everything.turns("deep")
    assert deep_all == 2 * 20
    assert deep_tiered == 2 * (6 + 3)  # 6 admitted, 3 audited, 2 turns each
    assert everything.turns("lead") == 0  # at or below the threshold no supervisor turn
    saved = everything.turns() - tiered.turns()
    assert saved == (deep_all - deep_tiered) - tiered.turns("lead") > 0
    tiered.close()
    everything.close()


def test_the_audit_catches_the_planted_wrong_drop(tmp_path: Path) -> None:
    bench = _run(tmp_path, FleetModel(items()))
    assert bench.result.outcome == "complete"  # type: ignore[attr-defined]
    review = bench.review()
    assert PLANTED in review["audit"]["sampled"]
    assert review["audit"]["misses"] == [PLANTED]
    assert review["audit"]["miss_rate"] == "1 of 3"
    assert review["audit"]["miss_rate_ppm"] == 333_333
    assert PLANTED in review["admitted"]  # re-opened after the goal said so
    dropped = {d["key"]: d for d in review["dropped"]}
    assert PLANTED not in dropped
    assert all(d["reason"] for d in review["dropped"])
    goals = [board_of(r)[2] for r in _lead_requests(bench)]  # type: ignore[arg-type]
    assert any(g.startswith("[supervisor] re-open drops") for g in goals)
    bench.close()


def test_a_supervisor_that_never_decides_cannot_finish(tmp_path: Path) -> None:
    bench = _run(tmp_path, FleetModel(items(), commits=False))
    assert bench.result.outcome != "complete"  # type: ignore[attr-defined]
    assert bench.turns("deep") == 0 and bench.turns("report") == 0
    assert bench.turns("lead") <= 4  # three vetoed finishes, then FAIL
    bench.close()


def test_a_supervisor_that_ignores_the_audit_cannot_finish(tmp_path: Path) -> None:
    bench = _run(tmp_path, FleetModel(items(), obey_goal=False))
    assert bench.result.outcome != "complete"  # type: ignore[attr-defined]
    assert bench.turns("report") == 0
    bench.close()


def test_a_commit_past_the_tier_cap_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    bench = _run(tmp_path, FleetModel(items(), first_admit=12))
    refused = [
        e
        for e in bench.events("step.completed")
        if "tier_cap" in json.dumps(e.payload, default=dict)
    ]
    rounds = [e for e in bench.events("artifact.saved") if e.payload["kind"] == "fleet.round"]
    first = rounds[0].seq
    assert refused and refused[0].seq < first
    directives = [e for e in bench.events("artifact.saved")
                  if e.payload["kind"] == "fleet.directive"]  # fmt: skip
    assert all(e.seq > refused[0].seq for e in directives)
    assert bench.result.outcome == "complete"  # type: ignore[attr-defined]
    bench.close()


def test_one_round_commits_in_one_transaction_and_records_no_read(tmp_path: Path) -> None:
    bench = _run(tmp_path, FleetModel(items()))
    saved = [e for e in bench.events("artifact.saved")
             if e.payload["kind"] in ("fleet.directive", "fleet.round")]  # fmt: skip
    first_round = [e for e in saved if e.step == saved[0].step]
    assert len(first_round) == 21  # 20 directives and the round, one step
    assert len({e.seq - i for i, e in enumerate(first_round)}) == 1  # one contiguous append
    assert all(not e.payload.get("reads") for e in saved)
    bench.close()
