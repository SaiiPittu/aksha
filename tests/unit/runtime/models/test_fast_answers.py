"""The run's fast-class memo (``models.calls.FastAnswers``): what keys an answer, who books it.

1. The memo holds judged answers, so its key covers the thresholds: the same state and
   question asked twice in a run under other thresholds is judged by its own.
2. A remembered answer's usage is recorded by the first step that completes with it, once.
3. An ended run's memo can be forgotten, and ``retain`` keeps only the open runs'.
4. An attempt past its deadline remembers and books nothing, so the retry books it once.
5. The first answer stored for a request wins.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from aksha.runtime.execution.types import Deadline
from aksha.runtime.models.calls import FastAnswers, MemoKey, Remembered, ToolModelCalls
from aksha.runtime.models.fixed import distribution_ppm
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleClassificationModel, RuleGenerationModel
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ConfidenceThresholds,
    ModelAlias,
    QuestionSpec,
    Usage,
)

ALIAS = ModelAlias("pick", "classification", on_exhausted="unknown")


class NinetyPercent(RuleClassificationModel):
    """Answers ``a`` at 0.9 and counts how often it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        self.asked += 1
        q = request.questions[0]
        answer = ClassAnswer("a", distribution_ppm({"a": 9, "b": 1}))
        return ClassificationResult(((q.name, answer),), Usage(input_tokens=10))


def _calls(model: NinetyPercent, memo: FastAnswers) -> ToolModelCalls:
    table = bind_aliases((ALIAS,), "fake", {"generate": RuleGenerationModel(), "pick": model})
    return ToolModelCalls(table, frozenset({"pick"}), "read", "o", answers=memo.of_run("o", "R"))


def _question(accept_at: float) -> QuestionSpec:
    return QuestionSpec("which", "Which one?", options=("a", "b"),
                        thresholds=ConfidenceThresholds(accept_at=accept_at))  # fmt: skip


def test_the_same_request_under_other_thresholds_is_judged_by_its_own() -> None:
    model, memo = NinetyPercent(), FastAnswers()
    calls = _calls(model, memo)
    (low,) = calls.classify("pick", "SYN state", questions=[_question(0.5)])
    (high,) = calls.classify("pick", "SYN state", questions=[_question(0.99)])
    assert (low.outcome, high.outcome) == ("accept", "abstain")
    assert model.asked == 2
    (again,) = calls.classify("pick", "SYN state", questions=[_question(0.5)])
    assert again.outcome == "accept" and model.asked == 2, "same thresholds: remembered"


def test_an_unbooked_answer_is_booked_by_the_first_step_that_completes() -> None:
    model, memo = NinetyPercent(), FastAnswers()
    failed = _calls(model, memo)
    failed.classify("pick", "SYN state", questions=[_question(0.5)])  # its step then fails
    retried = _calls(model, memo)
    retried.classify("pick", "SYN state", questions=[_question(0.5)])
    retried.classify("pick", "SYN state", questions=[_question(0.5)])
    assert model.asked == 1
    (summary,) = retried.records
    assert summary["calls"] == 2 and summary["usage"]["input_tokens"] == 10
    memo.book("o", "R", retried.booked_here)
    later = _calls(model, memo)
    later.classify("pick", "SYN state", questions=[_question(0.5)])
    assert later.records[0]["usage"]["input_tokens"] == 0


def test_forget_and_retain_drop_ended_runs() -> None:
    memo = FastAnswers()
    memo.of_run("o", "R1")[("pick", "h", "t")] = ((), {}, False)
    memo.of_run("o", "R2")[("pick", "h", "t")] = ((), {}, False)
    memo.of_run("p", "R1")[("pick", "h", "t")] = ((), {}, False)
    memo.retain("o", ["R2"])
    assert not memo.of_run("o", "R1") and memo.of_run("o", "R2") and memo.of_run("p", "R1")
    memo.forget("o", "R2")
    assert not memo.of_run("o", "R2")


def test_an_attempt_past_its_deadline_leaves_its_answers_to_the_retry() -> None:
    """4. An attempt abandoned at its deadline (``execution.executor`` item 4) still runs on
    its worker thread: what it gets after the deadline is not remembered and it books nothing,
    so the attempt the executor keeps records the answer's usage exactly once."""
    model, memo = NinetyPercent(), FastAnswers()
    table = bind_aliases((ALIAS,), "fake", {"generate": RuleGenerationModel(), "pick": model})
    past = Deadline(at_ms=0, mono_ms=lambda: 1)
    late = ToolModelCalls(table, frozenset({"pick"}), "read", "o",
                          answers=memo.of_run("o", "R"), lock=memo.lock, deadline=past)  # fmt: skip
    late.classify("pick", "SYN state", questions=[_question(0.5)])
    if not past.expired():  # what the loop does: an expired attempt books nothing
        memo.book("o", "R", late.booked_here)
    assert not memo.of_run("o", "R"), "an answer got past the deadline is not remembered"
    retry = _calls(model, memo)
    retry.classify("pick", "SYN state", questions=[_question(0.5)])
    assert retry.records[0]["usage"]["input_tokens"] == 10
    memo.book("o", "R", retry.booked_here)
    later = _calls(model, memo)
    later.classify("pick", "SYN state", questions=[_question(0.5)])
    assert later.records[0]["usage"]["input_tokens"] == 0, "booked once"


class _MissedBeforeStore(dict):  # type: ignore[type-arg]
    """A run's memo as an abandoned attempt saw it: empty when it looked, filled by the
    retry while the abandoned attempt was still asking."""

    def __init__(self, memo: MutableMapping[MemoKey, Remembered]) -> None:
        super().__init__()
        self.memo = memo

    def get(self, key: Any, default: Any = None) -> Any:
        return default

    def setdefault(self, key: Any, default: Any = None) -> Any:
        return self.memo.setdefault(key, default)


def test_the_first_answer_stored_wins() -> None:
    """5. An abandoned attempt that finishes a call after the retry stored the same request
    neither replaces the retry's answer nor unbooks it."""
    model, memo = NinetyPercent(), FastAnswers()
    retry = _calls(model, memo)
    (first,) = retry.classify("pick", "SYN state", questions=[_question(0.5)])
    memo.book("o", "R", retry.booked_here)
    (stored,) = memo.of_run("o", "R").values()
    table = bind_aliases((ALIAS,), "fake", {"generate": RuleGenerationModel(), "pick": model})
    late = ToolModelCalls(table, frozenset({"pick"}), "read", "o", lock=memo.lock,
                          answers=_MissedBeforeStore(memo.of_run("o", "R")))  # fmt: skip
    (got,) = late.classify("pick", "SYN state", questions=[_question(0.5)])
    assert model.asked == 2, "it asked: the memo was empty when it looked"
    assert got == first and memo.of_run("o", "R") == {next(iter(memo.of_run("o", "R"))): stored}
    assert stored[2], "still booked"
    assert late.records[0]["usage"]["input_tokens"] == 0 and not late.booked_here
