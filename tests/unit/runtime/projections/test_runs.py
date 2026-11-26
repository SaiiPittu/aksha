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


def test_ended_runs_lists_each_ended_run_of_the_named_agents() -> None:
    """ADR-P09 D6, D11: the projection episodes are computed from; nothing is stored."""
    from aksha.runtime.projections.runs import RunEnded, ended_runs

    def ev(seq: int, type_: str, run: str, payload: dict[str, object], **kw: object) -> Event:
        return Event(type=type_, payload=payload, run=run, seq=seq, owner="o1", at="", boot=1,
                     schema_version=1, **kw)  # type: ignore[arg-type]  # fmt: skip

    check = {"on": "finish", "check": "c", "message": "", "count": 1, "keys": [], "code": ""}
    events = (
        ev(1, "run.started", "R-1", {"agent": "research", "cause": "request"}),
        ev(2, "completion_check.evaluated", "R-1", {**check, "task": "scope", "key": None,
                                                    "ok": True}),
        ev(3, "artifact.saved", "R-1", {"ref": "research.scope@v1", "kind": "research.scope"}),
        ev(4, "completion_check.evaluated", "R-1", {**check, "task": "collect", "key": "n1",
                                                    "ok": False}),
        ev(5, "run.ended", "R-1", {"outcome": "partial", "reason": "unknowns",
                                   "blockers": ["collect[n1]"]}),
        ev(6, "run.started", "R-2", {"agent": "patent", "cause": "request"}),
        ev(7, "run.ended", "R-2", {"outcome": "complete", "reason": "done"}),
        ev(8, "run.started", "R-3", {"agent": "research", "cause": "request"}),
    )  # fmt: skip
    view = LogView(owner="o1", seq_read=8, events=events, artifacts=ArtifactsView((), 8))
    (one,) = ended_runs(view, frozenset({"research"}))
    assert one == RunEnded(
        run="R-1", agent="research", outcome="partial", reason="unknowns",
        blockers=("collect[n1]",), completed=("scope",), outputs=("research.scope@v1",),
    )  # fmt: skip
    assert [r.run for r in ended_runs(view, frozenset({"research", "patent"}))] == ["R-1", "R-2"]
