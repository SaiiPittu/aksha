"""Citation verification (D2, D4, D5, S2, S3): one verifier for every cite, all pure.

:func:`verify_citation` checks one cite under one field's ``CitePolicy``. For a span cite,
in order (D2): the form is allowed (``form_not_allowed``); the span exists for this
tenant (``unknown_span``); it was shown to this Task instance (``span_not_seen``, S1); its
record is at the version the log last recorded for it (``stale_source``, record grain,
S2); the quote, whitespace runs collapsed and nothing else normalised, occurs in the span
text (``quote_not_verbatim``, with the span's text in the message so a small model can
copy it); and the quote is at least ``MIN_QUOTE_CHARS`` long or the whole span
(``quote_too_short``).

:func:`verify_content` walks saved content: every node a policy's field pattern matches
must hold a cite or (where allowed) an explicit unknown, else ``ungrounded_value``; every
cite anywhere is verified, under :data:`ANY_FORM` outside a declared field; and an
unknown outside a ``may_be_unknown`` field is ``unknown_not_allowed``.

Everything the verifier reads is in :class:`Evidence`, built from the owner's event store
by the artifact store's save path, so the verifier itself reads nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.artifacts.hashing import resolve
from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.grounding.types import (
    ANY_FORM,
    MIN_QUOTE_CHARS,
    STATEMENT_SOURCE,
    ArtifactCite,
    CitationSpan,
    CiteError,
    CiteForm,
    CitePolicy,
    SpanCite,
    SpanIndex,
    StatementCite,
    Unknown,
    Verified,
    form_name,
    is_unknown_node,
    parse_cite,
)

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Whitespace runs collapsed to one space and the ends trimmed; nothing else changes."""
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class Evidence:
    """What verification reads, all projected from the owner's event store.

    ``records`` maps a record ID to the version the log last recorded for it
    (``source.changed``; None when it is gone); a record it does not name has not moved
    since it was read. ``reads`` are the instance's lineage inputs as text
    (``record:pages/P-0001@1``), whole. ``artifact`` gives a version's content by ref.
    ``failed_steps`` are the steps the log records as failed.
    """

    spans: SpanIndex
    run: str
    instance: str
    records: Mapping[str, str | None]
    reads: frozenset[str]
    artifact: Callable[[ArtifactRef], Mapping[str, Any] | None]
    failed_steps: frozenset[str] = frozenset()
    resumed_spans: frozenset[str] = frozenset()
    """The spans shown inside the turns of the session this instance resumed, if it did."""


def _err(code: Any, pointer: str, message: str) -> CiteError:
    return CiteError(code, pointer, message)


def verify_citation(
    cite: CiteForm, evidence: Evidence, policy: CitePolicy = ANY_FORM, *, pointer: str = ""
) -> Verified | CiteError:
    """Check one cite under ``policy`` (module doc); ``pointer`` names it in the content."""
    if form_name(cite) not in policy.forms:
        allowed = sorted(policy.forms)
        return _err("form_not_allowed", pointer, f"{policy.field} cites only {allowed}")
    if isinstance(cite, SpanCite):
        return _span(cite, evidence, policy, pointer)
    if isinstance(cite, StatementCite):
        return _statement(cite, evidence, pointer)
    return _artifact(cite, evidence, policy, pointer)


def _seen(span: CitationSpan, evidence: Evidence) -> bool:
    """Shown to this instance in this run, or inside the session it resumed (decision 36)."""
    return (evidence.run, evidence.instance) in span.shown_to or (
        span.span_id in evidence.resumed_spans
    )


def _span(cite: SpanCite, ev: Evidence, policy: CitePolicy, at: str) -> Verified | CiteError:
    span = ev.spans.get(cite.span)
    if span is None:
        return _err(
            "unknown_span", at, f"no span {cite.span!r}: only text a tool returned can be cited"
        )
    if span.source == STATEMENT_SOURCE and "statement" not in policy.forms:
        return _err("form_not_allowed", at, f"{policy.field} may not cite a person's statement")
    if policy.span_sources and span.source not in policy.span_sources:
        allowed = sorted(policy.span_sources)
        return _err("form_not_allowed", at, f"{cite.span} is from {span.source!r}, not {allowed}")
    if not _seen(span, ev):
        return _err("span_not_seen", at, f"{cite.span} was never shown to {ev.instance}")
    if span.source != STATEMENT_SOURCE and span.record_id in ev.records:
        now = ev.records[span.record_id]
        if now != span.version:
            current = "gone" if now is None else f"at version {now}"
            return _err(
                "stale_source", at,
                f"{cite.span} quotes {span.record_id}@{span.version}; the record is {current}: "
                "read it again and cite the new span",
            )  # fmt: skip
    text, quote = normalise(span.text), normalise(cite.quote)
    occurrence = text.find(quote) if quote else -1
    if occurrence < 0:
        return _err(
            "quote_not_verbatim", at,
            f"the quote is not in {cite.span}; copy it exactly from the span's text: {span.text!r}",
        )  # fmt: skip
    if len(quote) < MIN_QUOTE_CHARS and quote != text:
        return _err(
            "quote_too_short", at,
            f"quote at least {MIN_QUOTE_CHARS} characters, or the whole span: {span.text!r}",
        )  # fmt: skip
    return Verified(at, "span", cite.span, quote=quote, occurrence=occurrence)


def _statement(cite: StatementCite, ev: Evidence, at: str) -> Verified | CiteError:
    span = ev.spans.get(cite.statement)
    if span is None:
        return _err("unknown_span", at, f"no statement {cite.statement!r}")
    if span.source != STATEMENT_SOURCE:
        return _err(
            "cite_invalid", at, f"{cite.statement} is a {span.source} span, not a statement"
        )
    if not _seen(span, ev):
        return _err("span_not_seen", at, f"{cite.statement} was never shown to {ev.instance}")
    if cite.statement in ev.spans.superseded():
        return _err("superseded_statement", at, f"{cite.statement} was corrected; cite the new one")
    return Verified(at, "statement", cite.statement)


def _artifact(
    cite: ArtifactCite, ev: Evidence, policy: CitePolicy, at: str
) -> Verified | CiteError:
    try:
        ref = ArtifactRef.parse(cite.artifact)
    except ValueError:
        return _err("cite_invalid", at, f"not an artifact version: {cite.artifact!r}")
    if policy.artifact_kinds and ref.kind not in policy.artifact_kinds:
        allowed = sorted(policy.artifact_kinds)
        return _err("form_not_allowed", at, f"{policy.field} cites only kinds {allowed}")
    if str(ref.as_input()) not in ev.reads:
        return _err("span_not_seen", at, f"{ref} was never read here")
    content = ev.artifact(ref)
    try:
        found = content is not None and resolve(content, cite.path)[0]
    except ValueError:
        found = False
    if not found:
        return _err("stale_artifact_cite", at, f"{ref} has no {cite.path or '/'!r}")
    return Verified(at, "artifact", str(ref), path=cite.path)


# --- content ------------------------------------------------------------------------------


def _pointer(parts: Sequence[str | int]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _children(value: Any) -> Iterator[tuple[str | int, Any]]:
    if isinstance(value, Mapping):
        yield from value.items()
    elif isinstance(value, list | tuple):
        yield from enumerate(value)


def _nodes(value: Any, parts: tuple[str | int, ...] = ()) -> Iterator[tuple[str, Any]]:
    """Every cite holder and unknown in ``value``, as ``(pointer, node)``; not inside them."""
    if is_unknown_node(value) or (isinstance(value, Mapping) and "cite" in value):
        yield _pointer(parts), value
        return
    for key, child in _children(value):
        yield from _nodes(child, (*parts, key))


def _matches(
    value: Any, pattern: list[str], parts: tuple[str | int, ...] = ()
) -> Iterator[tuple[str, Any]]:
    """The nodes a policy's field pattern matches, as ``(pointer, node)``."""
    if not pattern:
        yield _pointer(parts), value
        return
    head, rest = pattern[0], pattern[1:]
    for key, child in _children(value):
        if head == "*" or str(key) == head:
            yield from _matches(child, rest, (*parts, key))


def _value_in_quote(node: Mapping[str, Any], quote: str) -> bool:
    value = node.get("value")
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return False
    text = normalise(value if isinstance(value, str) else str(value))
    return bool(text) and text in quote


def _unknown(node: Mapping[str, Any], ev: Evidence, at: str) -> CiteError | None:
    try:
        unknown = Unknown.from_json(node)
    except ValueError as e:
        return _err("unknown_invalid", at, str(e))
    if unknown.reason == "not_found":
        if not unknown.checked:
            return _err("unknown_invalid", at, "not_found lists the records read in full")
        for read in unknown.checked:
            if read not in ev.reads:
                return _err("unknown_invalid", at, f"{read} was never read here")
            if read.startswith("record:"):
                record, _, version = read[len("record:") :].rpartition("@")
                if record in ev.records and ev.records[record] != version:
                    return _err("unknown_invalid", at, f"{read} has moved; read it again")
    if unknown.reason == "unverified" and unknown.failed_step not in ev.failed_steps:
        return _err("unknown_invalid", at, "unverified names the step that failed")
    return None


def _cite_node(
    node: Mapping[str, Any], ev: Evidence, policy: CitePolicy, at: str
) -> Verified | CiteError:
    cite = parse_cite(node["cite"])
    if cite is None:
        return _err(
            "cite_invalid", f"{at}/cite",
            "a cite is {span, quote}, {statement} or {artifact, path}",
        )  # fmt: skip
    result = verify_citation(cite, ev, policy, pointer=f"{at}/cite")
    if (
        isinstance(result, Verified)
        and policy.value_in_quote
        and result.form == "span"
        and not _value_in_quote(node, result.quote or "")
    ):
        return _err(
            "value_not_in_quote", f"{at}/value",
            f"record the value exactly as the quote states it: {result.quote!r}",
        )  # fmt: skip
    return result


def verify_content(
    content: Mapping[str, Any], policies: Sequence[CitePolicy], evidence: Evidence
) -> tuple[Verified | CiteError, ...]:
    """Every citation and every policy field in ``content``, checked (module doc).

    Results come in the order of the policies, then of the remaining cites; pointers are
    into the content. The first policy whose pattern matches a node governs it. A valid
    explicit unknown adds no result: it cites nothing.
    """
    out: list[Verified | CiteError] = []
    governed: set[str] = set()
    for policy in policies:
        for at, node in _matches(content, policy.parts()):
            if at in governed:
                continue
            governed.add(at)
            if is_unknown_node(node):
                if not policy.may_be_unknown:
                    out.append(_err("unknown_not_allowed", at, f"{policy.field} needs a value"))
                elif (problem := _unknown(node, evidence, at)) is not None:
                    out.append(problem)
            elif isinstance(node, Mapping) and "cite" in node:
                out.append(_cite_node(node, evidence, policy, at))
            else:
                why = f"{policy.field} cites its evidence or, if allowed, is an explicit unknown"
                out.append(_err("ungrounded_value", at, why))
    for at, node in _nodes(content):
        if at in governed:
            continue
        if is_unknown_node(node):
            out.append(_err("unknown_not_allowed", at, "no field here may be unknown"))
        else:
            out.append(_cite_node(node, evidence, ANY_FORM, at))
    return tuple(out)
