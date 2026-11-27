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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.canonical import sha256_hex
from aksha.runtime.errors import AkshaError
from aksha.runtime.execution.errors import TransientStepError
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
    ) -> None:
        """Ask only ``allowed`` aliases of ``table``, for ``tool``'s body (of ``agent``).

        ``code_version`` is the agent definition hash a call with the tool's own schema
        depends on (``models.definitions.code_record``); empty, it records no such read.
        """
        self._table = table
        self._allowed = allowed
        self._tool = tool
        self._owner = owner
        self._agent = agent
        self.calls: list[dict[str, Any]] = []
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

        return self._cascade(alias, "extraction", request.request_hash, names, ask)

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

        return self._cascade(alias, "classification", request.request_hash, names, ask)

    def _cascade(
        self, alias: str, model_class: str, request_hash: str, names: list[str], ask: Any
    ) -> tuple[Judgement, ...]:
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
        self.calls.append(
            {
                "alias": alias,
                "model_class": model_class,
                "request_hash": request_hash,
                "candidates": steps,
                "exhausted": exhausted,
                "action": action,
            }
        )
        if exhausted and action == "fail":
            raise ModelCallFailed(message=f"{alias}: no answer accepted for {exhausted}")
        return judged


def _usage(usage: Usage) -> dict[str, int] | None:
    return None if usage == Usage() else usage.as_json()
