"""Span minting, the span index and S1's shown set (ADR-P13 D1, D8, S1)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.grounding.errors import MintRefused
from aksha.runtime.grounding.spans import (
    mint_for_result,
    mint_spans,
    shown_in,
    span_index,
    spans_shown,
    statement_span,
)
from aksha.runtime.grounding.types import ReturnedText, SpanDraft, SpanIndex
from aksha.runtime.tools.types import Ok, ToolOutcome
from tests.support.artifacts import FETCH, OWNER, RUN, World

PAGE = ReturnedText(
    "fixtures", "pages/P-0001", "1", "Acme Sensors GmbH. Pricing: 279 EUR per sensor."
)
PRICE = SpanDraft("fixtures", "pages/P-0001", "1", 28, 47)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.close()


def _mint(
    world: World, *drafts: SpanDraft, instance: str = "gather", step: str = "s1"
) -> list[str]:
    minted = mint_spans(
        drafts, (PAGE,), spec=FETCH, index=span_index(world.view()), run=RUN,
        instance=instance, step=f"{RUN}/{instance}/t01/{step}",
    )  # fmt: skip
    if minted.events:
        world.store.append(OWNER, "U2", minted.events)
    return [s.span_id for s in minted.spans]


def test_minted_span_carries_its_text_and_is_shown_to_the_instance(world: World) -> None:
    (span_id,) = _mint(world, PRICE)
    span = span_index(world.view()).get(span_id)
    assert span_id == "EX-0001" and span is not None
    assert span.text == "279 EUR per sensor." and (span.start, span.end) == (28, 47)
    assert span.shown_to == frozenset({(RUN, "gather")}) and span.tool == "fetch_page"
    (event,) = [e for e in world.view().events if e.type == "excerpts.minted"]
    assert event.payload["spans"][0]["text"] == "279 EUR per sensor."


def test_a_repeated_read_reuses_the_span_and_ids_count_from_the_store(world: World) -> None:
    assert _mint(world, PRICE) == ["EX-0001"]
    assert _mint(world, PRICE, SpanDraft("fixtures", "pages/P-0001", "1", 0, 17), step="s2") == [
        "EX-0001",
        "EX-0002",
    ]
    before = world.store.last_seq(OWNER)
    assert _mint(world, PRICE, step="s3") == ["EX-0001"]
    assert world.store.last_seq(OWNER) == before  # nothing new, already shown: no event
    assert _mint(world, PRICE, instance="compare", step="s4") == ["EX-0001"]
    span = span_index(world.view()).get("EX-0001")
    assert span is not None and span.shown_to == {(RUN, "gather"), (RUN, "compare")}


def test_only_a_minting_tool_mints(world: World) -> None:
    index = span_index(world.view())
    with pytest.raises(MintRefused) as e:
        mint_spans((PRICE,), (PAGE,), spec=replace(FETCH, mints_citation_spans=False),
                   index=index, run=RUN, instance="gather", step="s")  # fmt: skip
    assert e.value.code == "class_violation"
    assert mint_spans((), (), spec=replace(FETCH, mints_citation_spans=False), index=index,
                      run=RUN, instance="gather", step="s").spans == ()  # fmt: skip


@pytest.mark.parametrize(
    "draft",
    [
        SpanDraft("fixtures", "pages/P-0002", "1", 0, 5),  # never returned
        SpanDraft("fixtures", "pages/P-0001", "2", 0, 5),  # another version
        SpanDraft("search", "pages/P-0001", "1", 0, 5),  # another source
        SpanDraft("fixtures", "pages/P-0001", "1", 5, 5),  # empty
        SpanDraft("fixtures", "pages/P-0001", "1", 40, 99),  # past the end
        SpanDraft("fixtures", "pages/P-0001", "1", -1, 3),
    ],
)
def test_a_draft_over_text_nobody_returned_is_a_class_violation(
    world: World, draft: SpanDraft
) -> None:
    with pytest.raises(MintRefused) as e:
        _mint(world, draft)
    assert e.value.code == "class_violation"


def test_shown_in_reads_the_json_escaped_view() -> None:
    assert shown_in("", "anything", truncated=False)
    assert shown_in('{"t":"say \\"hi\\"\\n"}', 'say "hi"\n', truncated=True)
    assert not shown_in('{"t":"head [... cut ...] tail"}', "middle", truncated=True)


def test_a_span_hidden_by_truncation_is_not_shown_until_rendered(world: World) -> None:
    """S1: the tool minted the span, but the cap cut its text out of the model's view."""
    body = "A" * 400 + " Pricing table: 279 EUR per sensor. " + "Z" * 400
    page = ReturnedText("fixtures", "pages/P-0009", "1", body)
    start = body.index("Pricing")
    hidden = SpanDraft("fixtures", "pages/P-0009", "1", start, start + 34)
    head = SpanDraft("fixtures", "pages/P-0009", "1", 0, 20)
    outcome = ToolOutcome(Ok({"text": body}, spans=(hidden, head)))
    result = mint_for_result(
        outcome, (page,), spec=FETCH, index=span_index(world.view()), run=RUN,
        instance="gather", step=f"{RUN}/gather/t01/c01", cap_chars=200,
    )  # fmt: skip
    assert result.view.truncated
    world.store.append(OWNER, "U2", result.minted.events)
    index = span_index(world.view())
    hidden_id, head_id = (s.span_id for s in result.minted.spans)
    assert index.spans[hidden_id].shown_to == frozenset()
    assert index.spans[head_id].shown_to == {(RUN, "gather")}
    shown = spans_shown(index, [hidden_id, head_id, "EX-0404"], run=RUN, instance="gather")
    (event,) = shown
    assert event.payload == {"spans": [hidden_id], "via": "context"}
    world.store.append(OWNER, "U2", shown)
    assert span_index(world.view()).spans[hidden_id].shown_to == {(RUN, "gather")}
    assert spans_shown(span_index(world.view()), [hidden_id], run=RUN, instance="gather") == ()


def test_an_error_result_mints_nothing(world: World) -> None:
    from aksha.runtime.tools.types import Err

    result = mint_for_result(
        ToolOutcome(Err("timeout", "too slow")), (), spec=FETCH,
        index=span_index(world.view()), run=RUN, instance="gather", step="s",
    )  # fmt: skip
    assert result.minted.spans == () and result.minted.events == ()


def test_statement_span_is_a_citable_span_of_its_own(world: World) -> None:
    index = span_index(world.view())
    minted = statement_span(index, "We file in March.", interrupt="I-0001", answer="C-0001",
                            run=RUN, instance="intake")  # fmt: skip
    world.store.append(OWNER, "U4", minted.events)
    index = span_index(world.view())
    (span,) = minted.spans
    stored = index.get(span.span_id)
    assert stored is not None and stored.source == "statement" and stored.tool == "ask"
    assert stored.shown_to == {(RUN, "intake")}
    again = statement_span(index, "We file in March.", interrupt="I-0001", answer="C-0001",
                           run=RUN, instance="intake")  # fmt: skip
    assert again.events == () and again.spans == (stored,)
    fix = statement_span(index, "We file in May.", interrupt="I-0001", answer="C-0002",
                         run=RUN, instance="intake", supersedes=span.span_id)  # fmt: skip
    world.store.append(OWNER, "U4", fix.events)
    assert span_index(world.view()).superseded() == {span.span_id}
    with pytest.raises(MintRefused):
        statement_span(index, "", interrupt="I-0001", answer="C-3", run=RUN, instance="intake")


def test_the_span_index_is_per_owner() -> None:
    assert SpanIndex("o2").get("EX-0001") is None and SpanIndex("o2").highest() == 0
