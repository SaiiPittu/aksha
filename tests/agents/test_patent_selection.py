"""The person chooses which candidates go to counsel; the patent agent never does.

A live run (gpt-6.2-sol, main b2dacd9) ended complete with no question asked: the select
Task's model saved ``patent.selection`` itself, as a list of objects, and the fan-out over
it found no keys, so nothing was prepared. These tests hold the fixed behaviour:

1. ``select`` always puts the choice to the person, with the table's candidates and their
   recommendations as the options, built by code; the run waits.
2. A model-written selection (the live reply, replayed) is refused, and the run still waits.
3. The selection is the codes the person chose, saved by the runtime; ``prepare``,
   ``packet`` and ``send`` fan out over them.
4. Choosing none is recorded, and the run ends with nothing prepared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aksha.runtime.models.types import GenerationRequest, GenerationResult, ToolCall
from tests.support.patent import PatentRig

LIVE_REPLY = GenerationResult(
    None,
    (
        ToolCall(
            "call-live-1",
            "save_artifact",
            {
                "kind": "patent.selection",
                "content": {
                    "based_on": "patent.table@v1",
                    "selected": [
                        {"candidate": "C1", "documents": ["invention-disclosure-form"]},
                        {"candidate": "C4", "documents": ["invention-disclosure-form"]},
                    ],
                },
            },
        ),
    ),
    "tool_calls",
)
"""What the live model replied in the select Task (event 911 of the recorded store)."""


def _keys(r: PatentRig, kind: str) -> list[str]:
    return sorted({str(e.payload["key"]) for e in r.events("artifact.saved")
                   if e.payload["kind"] == f"patent.{kind}"})  # fmt: skip


def test_select_puts_the_tables_candidates_to_the_person_and_waits(tmp_path: Path) -> None:
    r = PatentRig(tmp_path / "aksha.sqlite")
    try:
        r.run_to_selection()
        (choice,) = r.open_interrupts("choice")
        rows = r.content("table")["rows"]
        assert list(choice.payload["options"]) == [row["candidate"] for row in rows]
        labels = list(choice.payload["option_labels"])
        assert all(label.split(":")[0] in {row["next_action"] for row in rows} for label in labels)
        assert choice.payload["multiple"] is True
        assert r.current("selection") is None and _keys(r, "form") == []
    finally:
        r.close()


def test_the_live_self_selection_is_refused_and_the_run_waits_for_the_person(
    tmp_path: Path,
) -> None:
    r = PatentRig(tmp_path / "aksha.sqlite")
    fake = r.model.generate
    replayed: list[str] = []

    def live_first(request: GenerationRequest) -> GenerationResult:
        if request.step_id.split("/")[1] == "select" and not replayed:
            replayed.append(request.step_id)
            return LIVE_REPLY
        return fake(request)

    r.model.generate = live_first  # type: ignore[method-assign]
    try:
        r.run_to_selection()
        assert replayed, "the select Task never took a turn"
        refused = [
            e.payload for e in r.events("step.completed")
            if e.step and e.step.startswith(replayed[0])
            and e.payload.get("tool") == "save_artifact"
        ]  # fmt: skip
        # select holds no tool, so the gate refuses it; were it offered, the save path would
        # refuse it answer_only (tests/integration/test_choice_saves.py).
        assert [p.get("code") for p in refused] == ["not_in_task"], refused
        assert r.current("selection") is None
        assert r.open_interrupts("choice"), "the run must hold on the person's choice"
    finally:
        r.close()


def test_two_chosen_candidates_each_get_forms_and_a_packet(tmp_path: Path) -> None:
    r = PatentRig(tmp_path / "aksha.sqlite")
    try:
        r.run_to_selection()
        assert r.choose("C1, C4").accepted
        r.drive()
        selection = r.current("selection")
        assert selection is not None and selection.origin == "choice"
        assert list(selection.content["selected"]) == ["C1", "C4"]
        assert {str(k).split("~")[0] for k in _keys(r, "form")} == {"C1", "C4"}
        assert _keys(r, "packet") == ["C1", "C4"]
    finally:
        r.close()


def test_choosing_none_is_recorded_and_the_run_ends_with_nothing_prepared(tmp_path: Path) -> None:
    r = PatentRig(tmp_path / "aksha.sqlite")
    try:
        r.run_to_selection()
        assert r.choose("none").accepted
        result = r.drive()
        assert (result.state, result.outcome) == ("ended", "complete")
        content: Any = r.content("selection")
        assert list(content["selected"]) == [] and "chose none" in content["note"]
        assert _keys(r, "form") == [] and _keys(r, "packet") == []
    finally:
        r.close()
