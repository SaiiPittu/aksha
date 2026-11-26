import hashlib
import json

import pytest

from aksha.runtime.tools.results import (
    DEFAULT_RESULT_CAP_CHARS,
    cap_text,
    render_result,
    view_from_json,
    view_json,
)
from aksha.runtime.tools.types import Err, Ok, ToolOutcome


def test_result_capped_at_record_time() -> None:
    outcome = ToolOutcome(Ok({"text": "h" * 10_000 + "t" * 10_000}))
    view = render_result(outcome)
    full = json.dumps(
        {"data": {"text": "h" * 10_000 + "t" * 10_000}, "status": "ok"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )  # fmt: skip
    assert DEFAULT_RESULT_CAP_CHARS == 8_000
    assert view.truncated and len(view.text) <= 8_000
    assert view.text.startswith(full[:3_000]) and view.text.endswith(full[-3_000:])
    assert view.full_chars == len(full) > 20_000
    assert view.full_hash == "sha256:" + hashlib.sha256(full.encode("utf-8")).hexdigest()


def test_small_result_is_shown_whole() -> None:
    view = render_result(ToolOutcome(Ok({"n": 1})))
    assert (view.status, view.text, view.truncated) == (
        "ok",
        '{"data":{"n":1},"status":"ok"}',
        False,
    )
    assert view.full_chars == len(view.text)


def test_error_view_carries_code_message_and_paths() -> None:
    view = render_result(ToolOutcome(Err("schema", "/n: bad", paths=("/n",))))
    assert view.status == "error"
    assert json.loads(view.text) == {
        "status": "error", "code": "schema", "message": "/n: bad", "paths": ["/n"], "item": None,
    }  # fmt: skip


def test_render_result_is_pure() -> None:
    outcome = ToolOutcome(Ok({"a": [1, 2]}))
    assert render_result(outcome, cap_chars=100) == render_result(outcome, cap_chars=100)


def test_cap_text_keeps_head_and_tail_within_the_cap() -> None:
    text = "".join(chr(ord("a") + i % 26) for i in range(1_000))
    cut, truncated = cap_text(text, 200)
    assert truncated and len(cut) <= 200
    assert cut.startswith(text[:50]) and cut.endswith(text[-50:])
    assert "characters cut" in cut
    assert cap_text("short", 200) == ("short", False)


def test_cap_text_refuses_a_cap_too_small_for_the_marker() -> None:
    with pytest.raises(ValueError, match="cap_chars"):
        cap_text("x" * 100, 10)


def test_view_round_trips_through_its_recorded_json() -> None:
    view = render_result(ToolOutcome(Ok({"text": "x" * 9_000})))
    recorded = view_json(view)
    assert set(recorded) == {"status", "text", "truncated", "full_chars", "full_hash"}
    assert view_from_json(recorded) == view
    assert view_from_json(json.loads(json.dumps(recorded))) == view
