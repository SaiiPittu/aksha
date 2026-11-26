"""A tool step's result for its outbox entry's end: the model view it records (ADR-P08 D4)."""

from __future__ import annotations

import json

from aksha.runtime.execution.types import StepId
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.types import OutboxEntry
from aksha.runtime.outbox.worker import ended_step_result, step_result
from aksha.runtime.tools.results import VIEW_FIELD, view_from_json

STEP = "R-0001/write/t02/c01"


def _entry(**changes: object) -> OutboxEntry:
    values: dict[str, object] = {
        "key": "k-1", "owner": "o1", "tool": "publish", "system": "board",
        "semantics": "reconcile", "on_unknown": "tell_model", "request": {"text": "noted"},
        "request_hash": "sha256:0", "seq": 4, "run": "R-0001", "step": STEP,
    }  # fmt: skip
    values.update(changes)
    return OutboxEntry(**values)  # type: ignore[arg-type]


def _shown(payload: dict[str, object]) -> dict[str, object]:
    value = payload["value"]
    assert isinstance(value, dict) and VIEW_FIELD in value, "a delivered effect records a view"
    view = view_from_json(value[VIEW_FIELD])
    shown = json.loads(view.text)
    assert view.status == "ok" and shown["status"] == "ok"
    return dict(shown["data"])


def test_a_delivered_effect_is_shown_to_the_model_as_a_success_with_its_receipt() -> None:
    end = facts.completed("k-1", "board-ledger-1", {"message_id": "m-1"}, attempts=1)
    result = step_result(_entry(), end)
    assert result.payload["outcome"] == "ok"
    data = _shown(dict(result.payload))
    assert data == {"key": "k-1", "ledger_id": "board-ledger-1", "receipt": {"message_id": "m-1"}}
    assert result.payload["value"]["receipt"] == {"message_id": "m-1"}  # the fact stays


def test_a_step_ending_on_an_equal_entry_already_delivered_is_shown_a_success() -> None:
    done = _entry(state="completed", receipt={"message_id": "m-1"}, ledger_id="board-ledger-1",
                  attempts=1, step=None)  # fmt: skip
    result = ended_step_result(done, StepId.parse(STEP))
    assert _shown(dict(result.payload))["receipt"] == {"message_id": "m-1"}


def test_an_abandoned_effect_records_no_view_and_keeps_its_code() -> None:
    end = facts.abandoned("k-1", "approval_revoked", attempts=1)
    result = step_result(_entry(), end)
    assert result.payload["outcome"] == "error" and result.payload["code"] == "approval_revoked"
    assert "value" not in result.payload
