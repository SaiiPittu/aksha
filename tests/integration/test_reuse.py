"""Cross-run reuse end to end on a world, offline (ADR-P04 R1).

A later run of the same agent for the same owner, asked the same thing, takes the earlier
run's versions by writer key when every recorded input still holds, records each reuse as
``artifact.reused`` and takes no model turn for that work; an input that moved makes only
the work built on it recompute; ``AKSHA_REUSE=off`` (or ``reuse = "off"`` on the agent)
makes every run recompute. The research agent runs on the ``tilldew`` world (synthetic).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from aksha import data
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from aksha.runtime.canonical import thaw
from tests.support.events import START
from tests.support.session import env

TILLDEW = data.world_path("tilldew") / "sources"
REQUEST = (data.suites() / "tilldew" / "g-research-happy.toml").read_text("utf-8")
PROMPT = re.search(r'^opening = "(.*)"$', REQUEST, re.M).group(1)  # type: ignore[union-attr]


def aksha(tmp: Path, *argv: str, **extra: str) -> tuple[int, str]:
    out = io.StringIO()
    settings = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
                "AKSHA_SEARCH": "world:tilldew", **extra}  # fmt: skip
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **settings))
    return code, out.getvalue()


def log(tmp: Path) -> list[tuple[str, str | None, dict[str, object]]]:
    store = open_store(tmp / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        return [(e.type, e.run, thaw(e.payload)) for e in store.read("o1")]
    finally:
        store.close()


def steps(tmp: Path, run: str) -> int:
    """How many durable steps (model turns and tool calls) ``run`` completed."""
    return sum(1 for t, r, _ in log(tmp) if t == "step.completed" and r == run)


def reused(tmp: Path, run: str) -> list[str]:
    return [str(p["version"]) for t, r, p in log(tmp) if t == "artifact.reused" and r == run]


EVERY_VERSION = {
    "research.scope@v1", "research.competitors@v1", "research.needs@v1",
    "research.matrix@v1", "research.findings@v1", "research.tests@v1",
    *(f"research.profile/{k}@v1" for k in ("c01-rootline-agro", "c02-aquasense-labs",
                                           "c03-greenfurrow", "c04-dripwise")),
}  # fmt: skip


def first(tmp: Path, **extra: str) -> None:
    code, out = aksha(tmp, "run", PROMPT, "--agent", "research", "--input", str(TILLDEW), **extra)
    assert code == wire.EXIT_OK and "R-0001 research ended complete" in out, out
    assert steps(tmp, "R-0001") > 20


def test_a_second_run_of_the_same_request_reuses_the_first_runs_work(tmp_path: Path) -> None:
    first(tmp_path)
    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research")
    assert code == wire.EXIT_OK and "R-0002 research ended complete" in out, out
    assert set(reused(tmp_path, "R-0002")) == EVERY_VERSION
    assert steps(tmp_path, "R-0002") == 0  # every Task popped DONE at entry: no model turn
    (fact,) = [p for t, r, p in log(tmp_path)
               if t == "artifact.reused" and p["version"] == "research.needs@v1"]  # fmt: skip
    assert fact["from_run"] == "R-0001"
    assert fact["writer"] == {"agent": "research", "task_path": "voice", "key": None}
    # Nothing was saved again: the slots still hold the first run's versions.
    assert not [p for t, r, p in log(tmp_path) if t == "artifact.saved" and r == "R-0002"]


def test_reuse_off_makes_the_second_run_recompute(tmp_path: Path) -> None:
    first(tmp_path)
    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", AKSHA_REUSE="off")
    assert code == wire.EXIT_OK and "R-0002 research ended complete" in out, out
    assert reused(tmp_path, "R-0002") == []
    assert steps(tmp_path, "R-0002") == steps(tmp_path, "R-0001")
    starts = {r: p for t, r, p in log(tmp_path) if t == "run.started"}
    assert starts["R-0002"]["reuse"] == "off" and "reuse" not in starts["R-0001"]
    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", AKSHA_REUSE="maybe")
    assert code == wire.EXIT_ENVIRONMENT and "AKSHA_REUSE" in out, out


def test_another_request_is_not_served_from_the_first_runs_work(tmp_path: Path) -> None:
    first(tmp_path)
    code, out = aksha(tmp_path, "run", PROMPT + " Focus on Kenya.", "--agent", "research")
    assert code == wire.EXIT_OK and "R-0002 research ended complete" in out, out
    assert reused(tmp_path, "R-0002") == [], out
    assert steps(tmp_path, "R-0002") > 0


def test_an_input_that_moved_recomputes_only_the_work_built_on_it(tmp_path: Path) -> None:
    """A changed review export, recorded but not yet rebuilt: the next run asked the same
    thing reuses the scope, discovery and profiles, and recomputes voice and the report."""
    from aksha.app import open_session
    from aksha.runtime.session import named as n
    from tests.support.session import config

    first(tmp_path)
    path = "exports/farmer-reviews.csv"
    edited = (
        (TILLDEW / path)
        .read_text("utf-8")
        .replace("Too expensive for a two-acre plot.", "Too expensive, and the battery died.")
    )
    session = open_session(config(tmp_path, agent_paths=(DEFAULT_AGENTS,),
                                  extension_paths=(DEFAULT_EXTENSIONS,),
                                  search="world:tilldew"))  # fmt: skip
    try:
        session.rebuilds = False  # record the change; open no rebuild run for it
        assert session.dispatch(n.UploadInputs(owner="o1", files={path: edited})).accepted
        receipt = session.dispatch(
            n.SendMessage(owner="o1", session="S-0002", text=PROMPT, agent="research")
        )
        assert receipt.accepted
        session.run_until_yield("o1")
    finally:
        session.store.close()
    took = set(reused(tmp_path, "R-0002"))
    assert took == EVERY_VERSION - {"research.needs@v1", "research.matrix@v1",
                                    "research.findings@v1", "research.tests@v1"}  # fmt: skip
    saved = {str(p["ref"]) for t, r, p in log(tmp_path) if t == "artifact.saved" and r == "R-0002"}
    assert "research.needs@v2" in saved and "research.matrix@v2" in saved
    assert not any(s.startswith(("research.scope", "research.profile")) for s in saved)
