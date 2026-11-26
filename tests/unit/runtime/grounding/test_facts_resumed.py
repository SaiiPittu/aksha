"""The evidence of a resumed instance: the spans shown inside its session's turns (decision 36)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.facts import resumed_spans
from tests.support.context import Log

WRITER = {"agent": "coder", "key": None, "task_path": "voice"}


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    yield log
    log.close()


def _shown(log: Log, turn: int, span: int) -> None:
    """Span ``EX-<span>`` minted and shown to R-0001's voice at its turn ``turn``."""
    step = f"R-0001/voice/t{turn:02d}/c01"
    record = {"span_id": f"EX-{span:04d}", "source": "corpus", "record_id": "reviews/r.json",
              "version": "v1", "start": 0, "end": 3, "text": "abc", "tool": "read"}  # fmt: skip
    payload = {"tool": "read", "spans": [record], "shown": [record["span_id"]]}
    event = NewEvent("excerpts.minted", payload, run="R-0001", instance="voice", step=step)
    log.store.append("o", "U2", (event,))


def _entered(log: Log, mode: str, session: list[dict[str, Any]]) -> None:
    choice = {"as_of_seq": 1, "from_turn": 1, "mode": mode, "new_reads": [],
              "read_runs": ["R-0001"], "session": session, "session_chars": 10,
              "session_turns": 3, "setting": "resume", "stale_chars": 0,
              **({"reason": "premise_changed"} if mode == "fresh" else {})}  # fmt: skip
    payload = {"cause": "rebuild", "key": None, "task": "voice", "writer": WRITER,
               "reentry": choice}  # fmt: skip
    log.store.append("o", "U3", (NewEvent("task.entered", payload, run="R-0002",
                                          instance="voice"),))  # fmt: skip


def test_only_spans_inside_the_resumed_turns_count(log: Log) -> None:
    for turn in (1, 2, 3):
        _shown(log, turn, turn)
    segment = {"run": "R-0001", "instance": "voice", "from_turn": 2, "through_turn": 3}
    _entered(log, "resume", [segment])
    assert resumed_spans(log.view(), "R-0002", "voice") == {"EX-0002", "EX-0003"}
    assert resumed_spans(log.view(), "R-0003", "voice") == frozenset()


def test_a_fresh_latest_entry_grants_nothing(log: Log) -> None:
    _shown(log, 1, 1)
    segment = {"run": "R-0001", "instance": "voice", "from_turn": 1, "through_turn": 1}
    _entered(log, "resume", [segment])
    _entered(log, "fresh", [])
    assert resumed_spans(log.view(), "R-0002", "voice") == frozenset()
