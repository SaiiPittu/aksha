"""Span minting (D1, D8, S1) and the span index, a projection over the event store.

Only tools mint spans, and only tools that declare ``mints_citation_spans``. A body returns
``SpanDraft`` values in its ``Ok``; :func:`mint_spans` checks each against a text the
runtime's reader returned in that attempt (``ReturnedText``) and mints it with its full
text into ``excerpts.minted``, which commits with the step's ``step.completed`` (U2). A
model never mints: an ID it invents is in no index, and a search hit has no span.

A span is citable by a Task instance only once its text was in front of that instance's
model (S1): inside the tool's capped view (:func:`shown_in`), or in a later rendering
that writes ``excerpts.shown`` (:func:`spans_shown`, for context assembly, ADR-P08).
A repeated read of the same passage reuses its span ID (v2 05b 5.3 rule 1).

A person's answer to an interrupt is minted the same way by :func:`statement_span`, with
source ``statement``, in the answer's transaction (U4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.canonical import canonical_json
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.errors import MintRefused
from aksha.runtime.grounding.types import (
    SPAN_PREFIX,
    STATEMENT_SOURCE,
    CitationSpan,
    ReturnedText,
    SpanDraft,
    SpanIndex,
)
from aksha.runtime.projections.view import LogView
from aksha.runtime.tools.results import DEFAULT_RESULT_CAP_CHARS, render_result
from aksha.runtime.tools.types import Ok, ToolOutcome, ToolResultView, ToolSpec


def span_index(view: LogView) -> SpanIndex:
    """Every span minted for ``view``'s owner, with who was shown each (a pure reducer).

    Computed once per view (``LogView.derive``): many readers of one view ask for it.
    """
    return view.derive("grounding.span_index", lambda: _span_index(view))


def _span_index(view: LogView) -> SpanIndex:
    minted: dict[str, Mapping[str, Any]] = {}
    seen: dict[str, set[tuple[str, str]]] = {}
    """Who was shown each span since it was last minted."""
    for e in view.events:
        if e.type == "excerpts.minted":
            for s in e.payload["spans"]:
                minted[s["span_id"]] = s
                seen.pop(s["span_id"], None)  # a span minted again starts unshown
            shown = e.payload.get("shown", ())
        elif e.type == "excerpts.shown":
            shown = e.payload["spans"]
        else:
            continue
        if e.run is None or e.instance is None:
            continue
        pair = (e.run, e.instance)
        for span_id in shown:
            if span_id in minted:
                seen.setdefault(span_id, set()).add(pair)
    spans = {
        span_id: CitationSpan(
            span_id=s["span_id"],
            tenant=view.owner,
            source=s["source"],
            record_id=s["record_id"],
            version=s["version"],
            start=s["start"],
            end=s["end"],
            text=s["text"],
            tool=s["tool"],
            shown_to=frozenset(seen.get(span_id, ())),
            supersedes=s.get("supersedes"),
        )
        for span_id, s in minted.items()
    }
    return SpanIndex(view.owner, spans)


def shown_in(view_text: str, span_text: str, *, truncated: bool) -> bool:
    """Whether ``span_text`` lies inside a tool result's model view (S1).

    The view is canonical JSON, so the span's text is looked for in its JSON-escaped form.
    An uncut view shows everything its result held.
    """
    if not truncated:
        return True
    return canonical_json(span_text)[1:-1] in view_text


@dataclass(frozen=True)
class Minted:
    """What minting returns: the spans of the drafts, in order, and the facts to commit."""

    spans: tuple[CitationSpan, ...]
    events: tuple[NewEvent, ...] = ()


def mint_spans(
    drafts: Sequence[SpanDraft],
    returned: Iterable[ReturnedText],
    *,
    spec: ToolSpec,
    index: SpanIndex,
    run: str,
    instance: str,
    step: str,
    view_text: str = "",
    truncated: bool = False,
) -> Minted:
    """Mint ``drafts`` against the texts the reader ``returned`` (module doc).

    Raises :class:`MintRefused` (``class_violation``) when the tool does not mint spans or a
    draft names no returned text or a range outside it. ``view_text`` and ``truncated``
    are the tool result's capped view, which decides who was shown each span (S1).
    """
    if not drafts:
        return Minted(())
    if not spec.mints_citation_spans:
        raise MintRefused(message=f"{spec.name!r} does not declare mints_citation_spans")
    texts = {r.locator(): r.text for r in returned}
    n = index.highest()
    out: list[CitationSpan] = []
    new: list[CitationSpan] = []
    shown: list[str] = []
    for i, d in enumerate(drafts):
        text = texts.get((d.source, d.record_id, d.version))
        if text is None:
            raise MintRefused(
                message=f"draft {i}: {d.source}:{d.record_id}@{d.version} was not returned"
            )
        if not 0 <= d.start < d.end <= len(text):
            raise MintRefused(
                message=f"draft {i}: [{d.start}, {d.end}) is not inside the {len(text)} characters"
            )
        locator = (d.source, d.record_id, d.version, d.start, d.end)
        span = index.find(locator) or next((s for s in new if s.locator() == locator), None)
        if span is None:
            n += 1
            span = CitationSpan(
                span_id=f"{SPAN_PREFIX}-{n:04d}", tenant=index.tenant, source=d.source,
                record_id=d.record_id, version=d.version, start=d.start, end=d.end,
                text=text[d.start : d.end], tool=spec.name,
            )  # fmt: skip
            new.append(span)
        if (
            (run, instance) not in span.shown_to
            and span.span_id not in shown
            and shown_in(view_text, span.text, truncated=truncated)
        ):
            shown.append(span.span_id)
        out.append(span)
    if not new and not shown:
        return Minted(tuple(out))
    payload = {"tool": spec.name, "spans": [s.as_json() for s in new], "shown": shown}
    event = NewEvent("excerpts.minted", payload, run=run, instance=instance, step=step)
    return Minted(tuple(out), (event,))


@dataclass(frozen=True)
class MintedResult:
    """A tool result's model view and the spans its drafts minted."""

    view: ToolResultView
    minted: Minted


def mint_for_result(
    outcome: ToolOutcome,
    returned: Iterable[ReturnedText],
    *,
    spec: ToolSpec,
    index: SpanIndex,
    run: str,
    instance: str,
    step: str,
    cap_chars: int = DEFAULT_RESULT_CAP_CHARS,
) -> MintedResult:
    """Render ``outcome`` for the model and mint its drafts against that view (D1, S1).

    The one call the step executor makes for a read or fetch tool's result: the view the
    model will see decides which minted spans this instance may cite.
    """
    view = render_result(outcome, cap_chars)
    drafts = outcome.result.spans if isinstance(outcome.result, Ok) else ()
    minted = mint_spans(
        drafts, returned, spec=spec, index=index, run=run, instance=instance, step=step,
        view_text=view.text, truncated=view.truncated,
    )  # fmt: skip
    return MintedResult(view, minted)


def statement_span(
    index: SpanIndex,
    text: str,
    *,
    interrupt: str,
    answer: str,
    run: str,
    instance: str,
    supersedes: str | None = None,
) -> Minted:
    """A person's answer to ``interrupt`` minted as a citable span, shown to ``instance``.

    ``answer`` names the answer (its command ID) and is the span's version. ``supersedes``
    names an earlier statement span this answer corrects; a cite of that one is then
    ``superseded_statement``. The event commits with the answer, in U4.
    """
    if not text:
        raise MintRefused(message="a statement span quotes a non-empty answer")
    locator = (STATEMENT_SOURCE, interrupt, answer, 0, len(text))
    found = index.find(locator)
    if found is not None:
        return Minted((found,))
    span = CitationSpan(
        span_id=f"{SPAN_PREFIX}-{index.highest() + 1:04d}", tenant=index.tenant,
        source=STATEMENT_SOURCE, record_id=interrupt, version=answer, start=0, end=len(text),
        text=text, tool="ask", supersedes=supersedes,
    )  # fmt: skip
    payload = {"tool": "ask", "spans": [span.as_json()], "shown": [span.span_id]}
    return Minted((span,), (NewEvent("excerpts.minted", payload, run=run, instance=instance),))


def spans_shown(
    index: SpanIndex,
    span_ids: Iterable[str],
    *,
    run: str,
    instance: str,
    step: str | None = None,
) -> tuple[NewEvent, ...]:
    """The ``excerpts.shown`` fact for spans a rendering put in front of ``instance`` (S1).

    Context assembly calls it when it renders a span's text (``Rendered.spans``, ADR-P08
    S1); spans already shown to the instance and unknown IDs are left out.
    """
    fresh: list[str] = []
    for span_id in span_ids:
        span = index.get(span_id)
        if span is None or (run, instance) in span.shown_to or span_id in fresh:
            continue
        fresh.append(span_id)
    if not fresh:
        return ()
    payload = {"spans": fresh, "via": "context"}
    return (NewEvent("excerpts.shown", payload, run=run, instance=instance, step=step),)
