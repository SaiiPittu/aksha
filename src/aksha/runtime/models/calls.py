"""Fast-class model calls made from inside a tool body (ADR-P07 D6 as amended G1).

A read tool understands an input by asking an extraction or classification alias, never
by matching one layout: :class:`ToolModelCalls` is the ``ToolContext.models`` handle the
agent loop gives a dispatched body. For each call it:

1. refuses an alias the tool's spec does not list in ``model_aliases`` (the loader has
   checked that the agent declares each with its class), raising ``AliasNotDeclared``;
2. builds the request: an extraction's state is the passages the body drafted over the
   text its reader returned, as provisional spans ``p0``, ``p1``, ... in draft order, so
   an answer's span is an offset inside one passage and :func:`span_draft` turns it into a
   draft over the record that the runtime mints after the body (ADR-P13 D1);
3. runs the alias's cascade (``routing.cascade_for``) candidate by candidate, judging every
   answer in code by the declared thresholds and asking the next candidate only what is
   still unaccepted, exactly as :func:`~aksha.runtime.models.steps.run_cascade` does;
4. keeps a record of every call, which the loop writes on the tool step's
   ``step.completed`` under ``model_calls``, one entry per alias: how many calls, the
   adapters that answered, how many answers were accepted, abstained, exhausted or failed,
   what the calls spent (``usage``, which the run's token and money budgets count, ADR-P14
   D9), and one hash over every request and answer in order. The accepted values are in the
   tool's result, which the step records too; the summary keeps a step that asks a model
   once per row of a large export under the payload cap.
5. records, for a call made under the alias's declared fields or questions, a read of
   the definition it was answered under (``reads``, ``models.definitions``, ADR-P05
   amendment M2), which the loop records with the tool's own reads, so a value derived
   under an old schema reopens when the definition changes.

The tool step is the durable unit: a completed step returns its recorded result without
running the body, so a resume never asks again, and a fake-mode run writes the same bytes.
A body that dies mid-call is ``at_least_once`` and asks again on its next attempt. A
transient model error is raised as ``TransientStepError("transient")``, which the tool's
retry policy retries; a permanent one moves to the next candidate. An alias whose
cascade is used up follows its ``on_exhausted``: ``unknown`` (the default) leaves the
abstention for the body to report as unknown, ``fail`` raises ``ModelCallFailed``.

A run asks each fast-class request once (:class:`FastAnswers`). The loop gives every tool
step of a run the same memo, so a retried attempt, a second listing or a read of a document
the listing already decided gets the answer the run already had, without asking the adapter
again. The key is the alias and the request hash, which covers the state the model is shown
(the preview text, the passages) and the questions or fields with their options, so a
changed record or a changed definition is a new request. Only an answer is remembered: a
transient error is raised to the step as before and leaves nothing behind, so its retry asks
again. The key also hashes the thresholds each field or question is judged by, which the
request hash leaves out: the memo holds judged answers, so a call under other thresholds is
judged by its own and asked again. A remembered answer is recorded on the step like an asked
one. Its usage is recorded on the first completed step that used it and nowhere else: an
answer a failed attempt paid for (a failed step records no calls) is booked, usage and all,
by the attempt that completes, and every later use records no usage. So the run's spend
counts each answer once, the step's ``model_calls`` summary counts every decision the body
used, and a fake-mode run, whose answers spend nothing, writes the same bytes with or without
the memo. An attempt abandoned at its deadline (``execution.executor`` item 4) still runs on
its worker thread beside the retry, so only the attempt the executor keeps writes to the memo:
past its deadline an attempt remembers no answer and books nothing, the first answer stored
for a request wins, and the memo is read and written under :attr:`FastAnswers.lock`. Live and
replay ask each request once, so a new recording replays exactly. The memo
is held in memory: a restarted process asks again, and the session forgets an ended run's
memo.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, MutableMapping, Sequence
from dataclasses import replace
from typing import Any

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.canonical import sha256_hex
from aksha.runtime.errors import AkshaError
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.execution.types import Deadline
from aksha.runtime.grounding.types import CitationSpan, ReturnedText, SpanDraft
from aksha.runtime.models.codec import (
    classification_result_json,
    extraction_result_json,
    judgement_json,
)
from aksha.runtime.models.definitions import alias_definition, code_record, definition_record
from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.routing import AliasTable, adapter_name, cascade_for
from aksha.runtime.models.thresholds import threshold_classification, threshold_extraction
from aksha.runtime.models.types import (
    ClassificationRequest,
    ExtractionRequest,
    FieldSpec,
    Judgement,
    QuestionSpec,
    SpanRef,
    Usage,
)

MODEL_CALLS_FIELD = "model_calls"
"""Where a tool step records the fast-class calls its body made."""


class AliasNotDeclared(AkshaError):
    """A body asked an alias its tool does not declare, or one of another class."""

    default_code = "alias_not_declared"


class ModelCallFailed(AkshaError):
    """An alias with ``on_exhausted="fail"`` had no candidate accept an answer."""

    default_code = "model_call_failed"


def passage_id(n: int) -> str:
    """The provisional span ID of the ``n``-th passage of a request (``p0``, ``p1``, ...)."""
    return f"p{n}"


def span_draft(ref: SpanRef, passages: Sequence[SpanDraft]) -> SpanDraft | None:
    """The record-level draft of ``ref`` (an offset inside passage ``pN``); None if invalid."""
    if not ref.span_id.startswith("p") or not ref.span_id[1:].isdigit():
        return None
    n = int(ref.span_id[1:])
    if n >= len(passages):
        return None
    p = passages[n]
    if not 0 <= ref.start < ref.end <= p.end - p.start:
        return None
    return SpanDraft(p.source, p.record_id, p.version, p.start + ref.start, p.start + ref.end)


MemoKey = tuple[str, str, str]
"""A remembered answer's key: the alias, the request hash and the thresholds' hash."""

Remembered = tuple[tuple[Judgement, ...], dict[str, Any], bool]
"""One remembered answer: the judgements the body got, the call entry with its usage, and
whether a completed step has recorded that usage yet (module doc)."""


class FastAnswers:
    """The fast-class answers each run has had in this process (module doc).

    One instance is shared by every tool step a process drives; :meth:`of_run` is the memo of
    one owner's run, keyed by ``(alias, request_hash)``. Runs never share answers, so what a
    run asks and records does not depend on what another run asked before it.
    """

    def __init__(self) -> None:
        """No run has been answered yet."""
        self._runs: dict[tuple[str, str], dict[MemoKey, Remembered]] = {}
        self.lock = threading.Lock()
        """Held around every read and write of a memo: an attempt abandoned at its deadline
        (``execution.executor`` item 4) still runs on its worker thread beside the retry."""

    def of_run(self, owner: str, run: str) -> MutableMapping[MemoKey, Remembered]:
        """The memo of ``owner``'s ``run``: created empty on first use."""
        with self.lock:
            return self._runs.setdefault((owner, run), {})

    def book(self, owner: str, run: str, keys: Iterable[MemoKey]) -> None:
        """Mark ``keys`` of ``run`` as booked: a completed step has recorded their usage."""
        with self.lock:
            memo = self._runs.get((owner, run), {})
            for k in keys:
                if k in memo:
                    judged, entry, _ = memo[k]
                    memo[k] = (judged, entry, True)

    def forget(self, owner: str, run: str) -> None:
        """Drop the memo of ``owner``'s ``run`` (it ended, so nothing will read it again)."""
        with self.lock:
            self._runs.pop((owner, run), None)

    def retain(self, owner: str, runs: Iterable[str]) -> None:
        """Drop every memo of ``owner``'s runs that are not in ``runs`` (the open ones)."""
        keep = set(runs)
        with self.lock:
            for k in [k for k in self._runs if k[0] == owner and k[1] not in keep]:
                del self._runs[k]


class ToolModelCalls:
    """``ToolContext.models`` for one dispatched call (module doc)."""

    def __init__(
        self,
        table: AliasTable,
        allowed: frozenset[str],
        tool: str,
        owner: str = "",
        agent: str = "",
        code_version: str = "",
        answers: MutableMapping[MemoKey, Remembered] | None = None,
        lock: threading.Lock | None = None,
        deadline: Deadline | None = None,
    ) -> None:
        """Ask only ``allowed`` aliases of ``table``, for ``tool``'s body (of ``agent``).

        ``code_version`` is the agent definition hash a call with the tool's own schema
        depends on (``models.definitions.code_record``); empty, it records no such read.
        ``answers`` is the run's memo (:meth:`FastAnswers.of_run`), read and written under
        ``lock`` (:attr:`FastAnswers.lock`); None asks every time. ``deadline`` is the
        attempt's: once it has passed the executor drops this attempt's output, so an answer
        it gets after that is not remembered.
        """
        self._answers = answers
        self._lock = lock or threading.Lock()
        self._deadline = deadline
        self._table = table
        self._allowed = allowed
        self._tool = tool
        self._owner = owner
        self._agent = agent
        self.calls: list[dict[str, Any]] = []
        self.booked_here: set[MemoKey] = set()
        """The memo keys whose usage this call's step records (module doc); the loop books
        them once the step completes."""
        self._code_version = code_version
        self._definitions: dict[str, str] = {}
        self._own_schema = False

    @property
    def reads(self) -> tuple[InputRef, ...]:
        """The definition reads of the calls (item 5): declared ones, then the tool's code."""
        declared = tuple(
            InputRef("record", definition_record(self._agent, alias), version)
            for alias, version in sorted(self._definitions.items())
        )
        if not self._own_schema:
            return declared
        return (*declared, InputRef("record", code_record(self._agent), self._code_version))

    def _defined(self, alias: str, declared: bool) -> None:
        if not self._agent:
            return
        if declared:
            self._definitions[alias] = alias_definition(self._table.aliases[alias])
        elif self._code_version:
            self._own_schema = True

    @property
    def records(self) -> list[dict[str, Any]]:
        """The calls as the step records them: one entry per alias (module doc, item 4)."""
        out: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            r = out.setdefault(c["alias"], {
                "alias": c["alias"], "model_class": c["model_class"], "calls": 0,
                "adapters": [], "accepted": 0, "abstained": 0, "exhausted": 0,
                "failures": 0, "hashes": [],
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_micro_usd": 0},
            })  # fmt: skip
            r["calls"] += 1
            r["exhausted"] += len(c["exhausted"])
            r["hashes"].append(c["request_hash"])
            for cand in c["candidates"]:
                if cand["adapter"] not in r["adapters"]:
                    r["adapters"].append(cand["adapter"])
                if "error" in cand:
                    r["failures"] += 1
                    continue
                r["hashes"].append(cand["answers_hash"])
                for k, n in (cand.get("usage") or {}).items():
                    r["usage"][k] += int(n)
                for j in cand["judgements"]:
                    r["accepted" if j["outcome"] == "accept" else "abstained"] += 1
        for r in out.values():
            r["calls_hash"] = "sha256:" + sha256_hex(r.pop("hashes"))
        return [out[k] for k in sorted(out)]

    def _alias(self, alias: str, model_class: str) -> None:
        declared = self._table.aliases.get(alias)
        if alias not in self._allowed or declared is None or declared.model_class != model_class:
            raise AliasNotDeclared(
                message=f"tool {self._tool!r} does not declare the {model_class} alias {alias!r}"
            )

    def extract(
        self,
        alias: str,
        text: ReturnedText,
        passages: Sequence[SpanDraft],
        fields: Sequence[FieldSpec] | None = None,
    ) -> tuple[Judgement, ...]:
        """Fill ``fields`` from ``passages`` of ``text`` (module doc, items 2 and 3)."""
        self._alias(alias, "extraction")
        declared = self._table.aliases[alias].fields
        asked = tuple(fields) if fields is not None else declared
        self._defined(alias, bool(asked) and asked == declared)
        state = tuple(
            CitationSpan(
                passage_id(n), self._owner, p.source, p.record_id, p.version, p.start, p.end,
                text.text[p.start : p.end], self._tool,
            )
            for n, p in enumerate(passages)
        )  # fmt: skip
        request = ExtractionRequest(f"{self._tool}/{alias}", alias, state, asked)
        by_id = {s.span_id: s for s in state}
        names = [f.name for f in asked]

        def ask(candidate: Any, pending: set[str]) -> tuple[dict[str, Any], list[Judgement]]:
            narrowed = replace(request, fields=tuple(f for f in asked if f.name in pending))
            result = candidate.adapter.extract(narrowed)
            found = [
                threshold_extraction(result.answer(f.name), f, by_id, candidate.native)
                for f in narrowed.fields
            ]
            return {"answers": extraction_result_json(result), "usage": _usage(result.usage)}, found

        return self._cascade(
            alias, "extraction", request.request_hash, names, ask, _thresholds_hash(asked)
        )

    def classify(
        self,
        alias: str,
        state: str,
        questions: Sequence[QuestionSpec] | None = None,
        state_inputs: Sequence[str] = (),
    ) -> tuple[Judgement, ...]:
        """Answer ``questions`` about ``state`` (module doc, item 3)."""
        self._alias(alias, "classification")
        declared_q = self._table.aliases[alias].questions
        asked = tuple(questions) if questions is not None else declared_q
        self._defined(alias, bool(asked) and asked == declared_q)
        request = ClassificationRequest(
            f"{self._tool}/{alias}", alias, state, asked, tuple(state_inputs)
        )
        names = [q.name for q in asked]

        def ask(candidate: Any, pending: set[str]) -> tuple[dict[str, Any], list[Judgement]]:
            narrowed = replace(request, questions=tuple(q for q in asked if q.name in pending))
            result = candidate.adapter.classify(narrowed)
            found = [
                threshold_classification(result.answer(q.name), q, candidate.native)
                for q in narrowed.questions
            ]
            data = {"answers": classification_result_json(result), "usage": _usage(result.usage)}
            return data, found

        return self._cascade(
            alias, "classification", request.request_hash, names, ask, _thresholds_hash(asked)
        )

    def _cascade(
        self,
        alias: str,
        model_class: str,
        request_hash: str,
        names: list[str],
        ask: Any,
        thresholds: str = "",
    ) -> tuple[Judgement, ...]:
        key = (alias, request_hash, thresholds)
        remembered: Remembered | None = None
        if self._answers is not None:
            with self._lock:
                remembered = self._answers.get(key)
        if remembered is not None:
            judged, held, booked = remembered
            if booked or key in self.booked_here:
                self.calls.append(_spent_nothing(held))
            else:
                self.calls.append(held)
                self.booked_here.add(key)
            if held["action"] == "fail":
                raise ModelCallFailed(
                    message=f"{alias}: no answer accepted for {held['exhausted']}"
                )
            return judged
        pending = set(names)
        last: dict[str, Judgement] = {}
        steps: list[dict[str, Any]] = []
        for candidate in cascade_for(alias, self._table):
            if not pending:
                break
            entry: dict[str, Any] = {
                "alias": candidate.alias,
                "adapter": adapter_name(candidate.adapter),
                "native": candidate.native,
            }
            try:
                data, found = ask(candidate, pending)
            except TransientModelError as e:
                raise TransientStepError("transient", e.message, detail=e.detail) from e
            except (PermanentModelError, ContextOverflow) as e:
                steps.append({**entry, "error": e.code})
                continue
            steps.append(
                {
                    **entry,
                    "answers_hash": "sha256:" + sha256_hex(data["answers"]),
                    "usage": data["usage"],
                    "judgements": [
                        {"name": j.name, "outcome": j.outcome, "reason": j.reason,
                         "value_hash": "sha256:" + sha256_hex(judgement_json(j)["value"])}
                        for j in found
                    ],
                }
            )  # fmt: skip
            for j in found:
                if j.name in pending:
                    last[j.name] = j
                    if j.accepted:
                        pending.discard(j.name)
        judged = tuple(last.get(n) or Judgement(n, "abstain", None, "not_asked") for n in names)
        exhausted = [n for n in names if n in pending]
        action = (self._table.aliases[alias].on_exhausted or "unknown") if exhausted else None
        made: dict[str, Any] = {
            "alias": alias,
            "model_class": model_class,
            "request_hash": request_hash,
            "candidates": steps,
            "exhausted": exhausted,
            "action": action,
        }
        if self._answers is not None and not (self._deadline and self._deadline.expired()):
            # The first answer stored wins: an abandoned attempt that finishes a call after
            # the retry stored it neither replaces the retry's answer nor unbooks it.
            with self._lock:
                prior = self._answers.setdefault(key, (judged, made, False))
            if prior[1] is not made:  # another attempt stored it first: use its answer
                judged, held, booked = prior
                made = _spent_nothing(held) if booked else held
                exhausted, action = held["exhausted"], held["action"]
            if prior[1] is made or not prior[2]:
                self.booked_here.add(key)
        self.calls.append(made)
        if exhausted and action == "fail":
            raise ModelCallFailed(message=f"{alias}: no answer accepted for {exhausted}")
        return judged


def _spent_nothing(entry: dict[str, Any]) -> dict[str, Any]:
    """A remembered call's entry: the same answers, with no usage (module doc)."""
    return {**entry, "candidates": [{**c, "usage": None} if "usage" in c else c
                                    for c in entry["candidates"]]}  # fmt: skip


def _thresholds_hash(asked: Sequence[FieldSpec] | Sequence[QuestionSpec]) -> str:
    """One hash over the thresholds each asked field or question is judged by (module doc)."""
    return sha256_hex(
        [[s.name, repr(s.thresholds_for(True)), repr(s.thresholds_for(False))] for s in asked]
    )


def _usage(usage: Usage) -> dict[str, int] | None:
    return None if usage == Usage() else usage.as_json()
