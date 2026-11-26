"""Grounding through the artifact store's save path, on SQLite (ADR-P13 D2, D5, decision 6, 9).

Each test mints spans the way a fetch tool's step commits them, then saves through the one
save path and reads what was committed.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.artifacts.errors import SaveRefused
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, Excerpt, InputRef, RelationRequest
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.types import CitePolicy, Unknown, maybe_unknown
from tests.support.artifacts import KINDS, OWNER, RELATIONS, RUN, World, page

PRICE_PAGE = "Pricing for COMP-02: 279 EUR per sensor, billed yearly."
CELL = {"type": "object"}
MATRIX_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"price": maybe_unknown(CELL)},
                "required": ["price"],
            },
        }
    },
    "required": ["cells"],
}
GROUNDED = {
    **KINDS,
    "notes.matrix": ArtifactKind(
        "matrix", MATRIX_SCHEMA, cites=(CitePolicy("/cells/*/price", may_be_unknown=True),)
    ),
}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    w.artifacts = ArtifactStore(GROUNDED, RELATIONS, blobs=w.blobs)
    w.save = functools.partial(w.save, writes=frozenset(GROUNDED))  # type: ignore[method-assign]
    yield w
    w.close()


def _refused(code: str, world: World, content: Mapping[str, Any], **kw: Any) -> SaveRefused:
    with pytest.raises(SaveRefused) as e:
        world.save("matrix", None, content, **kw)
    assert e.value.code == code, e.value
    return e.value


def _cell(price: Any) -> dict[str, Any]:
    return {"cells": [{"price": price}]}


def _cited(span: str, quote: str = "279 EUR per sensor", value: Any = 279) -> dict[str, Any]:
    return _cell({"value": value, "cite": {"span": span, "quote": quote}})


def test_a_grounded_save_binds_each_citation_in_its_transaction(world: World) -> None:
    span = world.fetch("gather", page(1), PRICE_PAGE)
    saved = world.save("matrix", None, _cited(span))
    bound = [e for e in saved.events if e.type == "citation.bound"]
    assert [e.payload for e in bound] == [
        {
            "carrier": "notes.matrix@v1", "n": 0, "pointer": "/cells/0/price/cite",
            "form": "span", "target": span, "quote": "279 EUR per sensor", "occurrence": 21,
        }
    ]  # fmt: skip
    committed = [e for e in world.view().events if e.type == "citation.bound"]
    assert [e.payload["target"] for e in committed] == [span]
    assert world.save("matrix", None, _cited(span)).replayed


def test_search_hit_cannot_be_cited(world: World) -> None:
    """A hit ID, or a query read used as evidence, fails ``unknown_span``."""
    hit = InputRef("query", "Q-0007", "h1")
    world.read("gather", hit, page(1), page(2))
    e = _refused("unknown_span", world, _cited("Q-0007"))
    assert e.path == "/content/cells/0/price/cite"
    rel = RelationRequest("same_as", page(1), page(2), (Excerpt(hit, "Acme Sensors GmbH"),))
    with pytest.raises(SaveRefused) as r:
        world.save("merge", "m1", {}, relations=(rel,))
    assert (r.value.code, r.value.path) == ("unknown_span", "/relations/0/evidence/0/span")


def test_model_cannot_mint_span(world: World) -> None:
    """A fake model that writes an invented ``EX-`` ID fails ``unknown_span``."""
    world.fetch("gather", page(1), PRICE_PAGE)
    _refused("unknown_span", world, _cited("EX-0042"))


def test_span_not_seen_by_instance(world: World) -> None:
    """A span shown only to compare[C2] cannot be cited by compare[C1]."""
    span = world.fetch("compare[C2]", page(1), PRICE_PAGE)
    world.read("compare[C1]", page(1))
    _refused("span_not_seen", world, _cited(span), instance="compare[C1]")
    world.save("matrix", None, _cited(span), instance="compare[C2]")


def test_quote_must_be_verbatim_and_long_enough(world: World) -> None:
    span = world.fetch("gather", page(1), PRICE_PAGE)
    e = _refused("quote_not_verbatim", world, _cited(span, "279 euros per sensor"))
    assert PRICE_PAGE in str(e)
    _refused("quote_too_short", world, _cited(span, "279 EUR"))
    world.save("matrix", None, _cited(span, "279   EUR per\nsensor"))


def _change(world: World, records: Mapping[str, str | None]) -> None:
    payload = {"source_version": 1, "cause": "command", "sources": {"fixtures": "2"},
               "records": dict(records)}  # fmt: skip
    world.store.append(OWNER, "U8", (NewEvent("source.changed", payload),))


def test_stale_source_refused(world: World) -> None:
    """After a source change, an old span fails with the current version named."""
    old = world.fetch("gather", page(1), PRICE_PAGE)
    _change(world, {"pages/P-0001": "2"})
    e = _refused("stale_source", world, _cited(old))
    assert "version 2" in str(e)
    span_v2 = world.fetch("gather", page(1, "2"), "Pricing for COMP-02: 279 EUR per sensor.")
    world.save("matrix", None, _cited(span_v2))


def test_stale_source_is_record_grain(world: World) -> None:
    """S2: one new record in the source leaves an unchanged record's span citable."""
    span = world.fetch("gather", page(1), PRICE_PAGE)
    _change(world, {"pages/P-0007": "1"})
    world.save("matrix", None, _cited(span))


def test_missing_is_unknown_not_zero(world: World) -> None:
    """A price never found saves as an explicit unknown with a question; 0 is refused."""
    world.read("gather", page(1))
    _refused("ungrounded_value", world, _cell(0))
    _refused("ungrounded_value", world, _cell({"value": 0}))
    gap = Unknown("What does COMP-02 charge per sensor?", "not_found", ("record:pages/P-0001@1",))
    saved = world.save("matrix", None, _cell(gap.as_json()))
    stored = world.artifacts.version(world.view(), saved.version.ref)
    assert stored is not None
    assert Unknown.from_json(stored.content["cells"][0]["price"]) == gap


def test_failed_fetch_is_unverified(world: World) -> None:
    """A fetch that ran out of retries leaves the cell ``unverified`` with the step named."""
    failed = f"{RUN}/gather/t02/c01"
    gap = Unknown("Is 279 EUR still the price?", "unverified", failed_step=failed)
    world.read("gather", page(1))
    _refused("unknown_invalid", world, _cell(gap.as_json()))
    outcome = {"outcome": "error", "code": "timeout", "kind": "tool", "tool": "fetch_page"}
    done = NewEvent("step.completed", outcome, run=RUN, instance="gather", step=failed)
    world.store.append(OWNER, "U2", (done,))
    world.save("matrix", None, _cell(gap.as_json()))


def test_an_unknown_where_none_may_be_is_refused(world: World) -> None:
    world.read("gather", page(1))
    gap = Unknown("Which text?", "not_checked").as_json()
    with pytest.raises(SaveRefused) as e:
        world.save("line", "n1", {"text": "x", "rows": [gap]})
    assert (e.value.code, e.value.path) == ("unknown_not_allowed", "/content/rows/0")


def test_every_save_verifies_cites_even_without_a_policy(world: World) -> None:
    world.read("gather", page(1))
    content = {"text": "x", "rows": [{"cite": {"span": "EX-0404", "quote": "made up text"}}]}
    with pytest.raises(SaveRefused) as e:
        world.save("line", "n1", content)
    assert e.value.code == "unknown_span"


def test_edge_evidence_verified(world: World) -> None:
    """A typed relation's record excerpt passes the same verifier (decision 9)."""
    world.read("gather", page(1), page(2))
    span = world.fetch("gather", page(1), "Acme Sensors GmbH, Berlin")

    def save(*evidence: Excerpt) -> object:
        rel = RelationRequest("same_as", page(1), page(2), evidence)
        return world.save("merge", "m1", {}, relations=(rel,))

    for code, excerpt in (
        ("quote_not_verbatim", Excerpt(page(1), "ACME Sensors GmbH", span=span)),
        ("span_not_of_source", Excerpt(page(2), "Acme Sensors GmbH", span=span)),
        ("unknown_span", Excerpt(page(1), "Acme Sensors GmbH", span="EX-0099")),
    ):
        with pytest.raises(SaveRefused) as e:
            save(excerpt)
        assert e.value.code == code
    saved = save(Excerpt(page(1), "Acme Sensors GmbH", span=span))
    (rel,) = world.artifacts.relations(world.view(), kind="notes.same_as")
    assert rel.evidence[0].span == span
    assert rel.carrier == saved.version.ref  # type: ignore[attr-defined]


def test_a_respan_of_the_same_words_keeps_the_canonical_hash(world: World) -> None:
    """Early cutoff: the same quote from a re-minted span is the same claim."""
    first = world.fetch("gather", page(1), PRICE_PAGE)
    v1 = world.save("matrix", None, _cited(first)).version
    _change(world, {"pages/P-0001": "2"})
    again = world.fetch("gather", page(1, "2"), PRICE_PAGE, run="R-0002")
    v2 = world.save("matrix", None, _cited(again), run="R-0002").version
    assert again != first and v2.ref.version == 2
    assert v1.canonical_hash == v2.canonical_hash and v1.content_hash != v2.content_hash
