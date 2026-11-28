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


def _rows_with(r: PatentRig, edit: Any) -> None:
    """Rewrite the rows of the model's table save with ``edit(rows) -> rows``."""
    fake = r.model.generate

    def edited(request: GenerationRequest) -> GenerationResult:
        reply = fake(request)
        if request.step_id.split("/")[1] != "table":
            return reply
        calls = []
        for c in reply.tool_calls:
            args = dict(c.arguments)
            if c.name == "save_artifact" and "rows" in dict(args.get("content") or {}):
                content = dict(args["content"])
                content["rows"] = edit([dict(row) for row in content["rows"]])
                args["content"] = content
            calls.append(ToolCall(c.call_id, c.name, args))
        return GenerationResult(reply.text, tuple(calls), reply.finish)

    r.model.generate = edited  # type: ignore[method-assign]


def test_the_labels_come_from_the_recommendations_not_the_table_text(tmp_path: Path) -> None:
    """A live model saved rows holding only the candidate; the labels were empty (I-0001)."""
    r = PatentRig(tmp_path / "aksha.sqlite")
    _rows_with(r, lambda rows: [{"candidate": row["candidate"]} for row in rows])
    try:
        r.run_to_selection()
        (choice,) = r.open_interrupts("choice")
        codes = list(choice.payload["options"])
        actions = {c: r.content("recommendation", c)["next_action"] for c in codes}
        labels = dict(zip(codes, choice.payload["option_labels"], strict=True))
        assert all(labels[c].startswith(f"{actions[c]}: ") for c in codes), labels
        assert r.choose("C1, C4").accepted
        r.drive()
        content: Any = r.content("selection")
        assert dict(content["recommendations"]) == {c: actions[c] for c in ("C1", "C4")}
    finally:
        r.close()


def test_an_invented_or_mislabelled_row_does_not_reach_the_person(tmp_path: Path) -> None:
    r = PatentRig(tmp_path / "aksha.sqlite")
    edits: list[int] = []

    def once(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if edits:
            return rows
        edits.append(1)
        for row in rows:
            row["next_action"] = "prioritize_counsel_review"  # the model steers the choice
        return [*rows, {"candidate": "C9", "next_action": "prioritize_counsel_review"}]

    _rows_with(r, once)
    try:
        r.run_to_selection()
        failed = [e.payload for e in r.events("completion_check.evaluated")
                  if e.instance == "table" and e.payload.get("check") == "table_saved"
                  and e.payload.get("code")]  # fmt: skip
        assert failed, "a row for a candidate with no recommendation fails the table check"
        (choice,) = r.open_interrupts("choice")
        codes = list(choice.payload["options"])
        assert "C9" not in codes
        for code, label in zip(codes, choice.payload["option_labels"], strict=True):
            assert label.startswith(r.content("recommendation", code)["next_action"])
    finally:
        r.close()


def test_a_rejected_copy_is_chosen_again_and_the_fresh_answer_is_saved(tmp_path: Path) -> None:
    from aksha.runtime.invalidation.commands import reject_review

    r = PatentRig(tmp_path / "aksha.sqlite")
    try:
        a = r.run_to_approval("C1")
        assert r.approve(a.approval_id, a.signature).accepted
        assert r.change(tmp_path / "changed").accepted
        r.drive()  # the open run rebuilds in place; the copied selection waits on the person
        (copied,) = [v for v in r.reviews().values() if v.old.kind == "patent.selection"]
        receipt = reject_review(r.store, r.setup.owner, copied.interrupt, copied.card_hash,
                                r.ids, tree=r.setup.tree)  # fmt: skip
        assert receipt.accepted
        (fresh,) = r.open_interrupts("choice")
        assert fresh.payload["reason"] == "review_rejected"
        assert r.choose("C1, C2").accepted
        r.rebuild()  # the writer is entered again and saves from the fresh answer
        selection = r.current("selection")
        assert selection is not None and selection.origin == "choice"
        assert selection.answer == fresh.id
        assert list(selection.content["selected"]) == ["C1", "C2"]
        assert not [v for v in r.reviews().values() if v.old.kind == "patent.selection"]
    finally:
        r.close()
