from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, RunSummary, open_runs
from aksha.runtime.projections.view import ArtifactsView, LogView


def _event(seq: int, type_: str, run: str, payload: dict[str, object]) -> Event:
    return Event(
        type=type_, payload=payload, run=run, seq=seq, owner="o1", at="", boot=1,
        schema_version=1,
    )  # fmt: skip


def test_runs_projection_tracks_start_and_end() -> None:
    events = (
        _event(1, "run.started", "R-1", {"agent": "research", "cause": "request"}),
        _event(2, "probe.noted", "R-1", {"n": 1}),
        _event(3, "run.started", "R-2", {"agent": "patent", "cause": "rebuild"}),
        _event(4, "run.ended", "R-1", {"outcome": "partial", "reason": "budget"}),
    )
    view = LogView(owner="o1", seq_read=4, events=events, artifacts=ArtifactsView((), 4))
    state = rebuild(RUNS, view)
    assert state["R-1"] == RunSummary("R-1", "research", "request", "partial")
    assert state["R-2"].outcome is None
    assert open_runs(state) == ("R-2",)
    assert RUNS.initial(()) == {}


def test_runs_projection_keeps_the_recorded_definition_hash() -> None:
    started: dict[str, object] = {
        "agent": "a",
        "cause": "request",
        "definition_hash": "sha256:" + "0" * 64,
    }
    view = LogView(
        owner="o1", seq_read=1, events=(_event(1, "run.started", "R-1", started),),
        artifacts=ArtifactsView((), 1),
    )  # fmt: skip
    assert rebuild(RUNS, view)["R-1"].definition_hash == "sha256:" + "0" * 64
