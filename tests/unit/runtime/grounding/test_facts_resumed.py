"""The evidence of a resumed instance names the session it resumed (decision 36)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.facts import resumed_session
from tests.support.context import Log

WRITER = {"agent": "coder", "key": None, "task_path": "voice"}


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    yield log
    log.close()


def _entered(log: Log, run: str, reentry: dict[str, Any] | None) -> None:
    payload: dict[str, Any] = {"cause": "rebuild", "key": None, "task": "voice", "writer": WRITER}
    if reentry is not None:
        payload["reentry"] = reentry
    log.store.append("o", "U3", (NewEvent("task.entered", payload, run=run, instance="voice"),))


def _choice(mode: str, session: list[dict[str, Any]]) -> dict[str, Any]:
    return {"as_of_seq": 1, "from_turn": 1, "mode": mode, "new_reads": [],
            "read_runs": ["R-0001"], "session": session, "session_chars": 10,
            "session_turns": 3, "setting": "resume", "stale_chars": 0,
            **({"reason": "premise_changed"} if mode == "fresh" else {})}  # fmt: skip


def test_the_latest_entry_decides(log: Log) -> None:
    segment = {"run": "R-0001", "instance": "voice", "from_turn": 1, "through_turn": 3}
    _entered(log, "R-0002", _choice("resume", [segment]))
    assert resumed_session(log.view(), "R-0002", "voice") == {("R-0001", "voice")}
    assert resumed_session(log.view(), "R-0003", "voice") == frozenset()
    _entered(log, "R-0002", _choice("fresh", []))
    assert resumed_session(log.view(), "R-0002", "voice") == frozenset()
