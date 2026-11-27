"""The research agent's definition: declared data only (ADR-P20 D4).

The agent works on whatever the person gives it: the request says what to research, for
whom and on which dimensions, and the inputs (any folder or set of files) are what it reads.
Task tree, in order: ``scope`` fixes the question, the segment, the geography, the boundary
and what to compare (``dimensions``) and read reviews for (``themes``), asking only the
questions whose answer changes the result; ``discover`` lists the inputs, searches the web
(``web_search``, ``fetch_url``: the generic web tools, ADR-P26 A3), reads every page that
sells something, leaving the noise in the results unread, and resolves the companies behind
them, finding manual alternatives too; ``evidence``
fans out per resolved company (a blocked company leaves an explicit unknown,
``on_child_blocked="partial"``); ``voice`` reads the customer reviews; ``report``
(the finalizer) saves the matrix, the findings and the tests to run next. The run is
complete when the matrix, the needs, the findings and the tests exist at current versions.
"""

from __future__ import annotations

from typing import Any, Literal

from aksha.runtime.api import (
    WEB_SOURCE,
    Agent,
    ArtifactKind,
    Budget,
    CitePolicy,
    ContextPolicy,
    DeclaredInterrupt,
    FieldSpec,
    Limits,
    ModelAlias,
    Read,
    RelationKind,
    Task,
    ToolUse,
    VersionedSourceSpec,
    artifact_keys,
    kind_question,
)

from . import checks, tools
from .reading import KINDS as DOC_KINDS

OBJECT: dict[str, Any] = {"type": "object"}
LIST: dict[str, Any] = {"type": "array"}


def _obj(*required: str, **props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


Form = Literal["span", "statement", "artifact"]
SPAN: frozenset[Form] = frozenset({"span"})
SPAN_OR_ARTIFACT: frozenset[Form] = frozenset({"span", "artifact"})
ARTIFACT: frozenset[Form] = frozenset({"artifact"})


def _cells(prefix: str, forms: frozenset[Form] = SPAN) -> tuple[CitePolicy, ...]:
    """The evidence grid: prices, and each scope dimension's entries, cited or unknown."""
    return (
        CitePolicy(f"{prefix}/prices/*", forms=forms, may_be_unknown=True, value_in_quote=True),
        CitePolicy(f"{prefix}/cells/*/*", forms=forms, may_be_unknown=True, value_in_quote=True),
    )


SCOPE = ArtifactKind(
    "scope",
    _obj(
        "question",
        "segment",
        "geography",
        "boundary",
        "dimensions",
        "themes",
        "asked",
        question={"type": "string"},
        segment=OBJECT,
        geography=OBJECT,
        boundary=OBJECT,
        dimensions=LIST,
        themes=LIST,
        asked=LIST,
    ),
    cites=(CitePolicy("/boundary/out/*", forms=SPAN),),
)
COMPETITORS = ArtifactKind(
    "competitors",
    _obj(
        "companies",
        "pages_found",
        "resolved",
        "manual_alternatives",
        "dimensions",
        companies=LIST,
        dimensions=LIST,
    ),
    cites=(
        CitePolicy("/companies/*/canonical", forms=SPAN, value_in_quote=True),
        CitePolicy("/companies/*/aliases/*", forms=SPAN, value_in_quote=True),
        CitePolicy("/companies/*/merge_evidence/*", forms=SPAN),
        CitePolicy("/manual_alternatives", may_be_unknown=True),
    ),
)
PROFILE = ArtifactKind(
    "profile",
    _obj("key", "name", "pages", "prices", "cells", "flags", "conflicts", cells=OBJECT),
    keyed=True,
    cites=(
        *_cells(""),
        CitePolicy("/price_notes/*", forms=SPAN, value_in_quote=True),
        CitePolicy("/flags/*/text", forms=SPAN, value_in_quote=True),
    ),
)
NEEDS = ArtifactKind(
    "needs",
    _obj("sample", "needs", sample=OBJECT, needs=LIST),
    cites=(
        CitePolicy("/needs/*/evidence/*", forms=SPAN),
        CitePolicy("/sample/duplicates/*/evidence/*", forms=SPAN),
    ),
)
MATRIX = ArtifactKind(
    "matrix",
    _obj("columns", "rows", "resolved", "pages_found", rows=LIST),
    cites=(
        CitePolicy("/rows/*/company", forms=SPAN, value_in_quote=True),
        *_cells("/rows/*", SPAN_OR_ARTIFACT),
    ),
)
FINDINGS = ArtifactKind(
    "findings",
    _obj("findings", "unknowns", findings=LIST, unknowns=LIST),
    cites=(
        CitePolicy("/findings/*/evidence/*", forms=SPAN),
        CitePolicy("/unknowns/*", forms=ARTIFACT, may_be_unknown=True),
    ),
)
TESTS = ArtifactKind(
    "tests",
    _obj("tests", tests=LIST),
    cites=(CitePolicy("/tests/*/evidence/*", forms=SPAN),),
)

PAGE = f"record:{tools.COLLECTION}"
WEB_PAGE = f"record:{WEB_SOURCE}"
"""A page fetched from the web (``web/<host><path>``): a relation may join it to an input."""
PAGES = (PAGE, WEB_PAGE)


def _fast(name: str, model_class: Literal["extraction", "classification"], *fields: FieldSpec
          ) -> ModelAlias:  # fmt: skip
    """A fast-class alias any adapter may answer: native, or a general model adapted (G3)."""
    return ModelAlias(name, model_class, allow_adapted=True, on_exhausted="unknown",
                      fields=fields)  # fmt: skip


MODEL_ALIASES = (
    # The kinds and their meanings, declared, so a decided kind records the definition it
    # was decided under and reopens when a kind's meaning changes (ADR-P05 M2).
    ModelAlias(
        "doc_kind",
        "classification",
        allow_adapted=True,
        on_exhausted="unknown",
        questions=(kind_question(DOC_KINDS),),
    ),
    _fast(
        "page_facts",
        "extraction",
        FieldSpec(
            "company",
            description="the company that makes or sells what the "
            "document offers; absent when it offers nothing for sale",
        ),
        FieldSpec("product", description="the product or service the document offers"),
        FieldSpec(
            "published", "date", description="when the document was published or last updated"
        ),
        FieldSpec(
            "prices",
            "list",
            description="each price the document states for what "
            "it offers, quoted with its currency and basis (per unit, a month) and any "
            "condition (from, launch offer)",
        ),
        FieldSpec(
            "relationships",
            "list",
            description="each sentence saying two names are "
            "one company or product: a rename, an acquisition, a merger",
        ),
        FieldSpec(
            "claims",
            "list",
            description="each marketing or performance claim the "
            "seller makes about its own product",
        ),
        FieldSpec(
            "addressed_to_agents",
            "list",
            description="text addressed to an automated "
            "reader or an AI assistant, or telling a reader to change what it does",
        ),
    ),
    _fast(
        "price_table",
        "extraction",
        FieldSpec(
            "sellers",
            "list",
            description="in each row, the seller or company it names, quoted from that row",
        ),
        FieldSpec(
            "products",
            "list",
            description="in each row, the product, model or plan it names, quoted from that row",
        ),
        FieldSpec(
            "prices",
            "list",
            description="in each row, the price it states, quoted with its currency and basis",
        ),
        FieldSpec("published", "date", description="when the table was compiled or updated"),
        FieldSpec(
            "addressed_to_agents",
            "list",
            description="text addressed to an automated reader or an AI assistant",
        ),
    ),
    _fast(
        "report_facts",
        "extraction",
        FieldSpec(
            "companies",
            "list",
            description="each company the report names as "
            "offering a product in the market, quoted where it is named",
        ),
        FieldSpec(
            "figures",
            "list",
            description="each market figure it states (a size, a share, a growth rate), quoted",
        ),
        FieldSpec("claims", "list", description="each claim about where the market is going"),
        FieldSpec(
            "prices",
            "list",
            description="each price it states for a named company's offer, quoted with its "
            "currency and basis",
        ),
        FieldSpec("published", "date", description="when the report was published"),
        FieldSpec(
            "addressed_to_agents",
            "list",
            description="text addressed to an automated reader or an AI assistant",
        ),
    ),
    _fast(
        "conversation_facts",
        "extraction",
        FieldSpec(
            "prices",
            "list",
            description="each price a message quotes, with its currency and basis",
        ),
        FieldSpec(
            "published",
            "date",
            description="when the message quoting a price was sent (else the first message)",
        ),
    ),
    _fast(
        "feedback_items",
        "extraction",
        FieldSpec(
            "items",
            "list",
            description="each customer's or user's own words in the "
            "document: a review, an interview answer, a survey answer, a ticket; one "
            "quote per voice",
        ),
    ),
)
"""The fast-class aliases the shared input tools ask (``tools.READING``): the decide alias
and one schema alias per document kind (``reading.KINDS``). Which model answers each is
configuration (fakes offline; a native or adapted model live), never this module. Which
column of an export holds what, and which theme a review speaks to, the generation model
reads itself."""

AGENT = Agent(
    name="research",
    purpose=(
        "Research who sells a product, at what price and what customers say, over the "
        "person's own inputs: competitors, prices, the dimensions the request names, "
        "reviews and needs, every claim cited to a record read."
    ),
    instructions="prompts/instructions.md",
    # Its sources move under it, and its findings must follow (decision 38).
    invalidation="rebuild",
    # A finding rests on the spans it cites: a change elsewhere in a page it read is
    # noticed, not rebuilt (ADR-P05 M2).
    invalidation_keys="citations",
    reads=(*(f"record:{c}" for c in tools.COLLECTIONS), WEB_PAGE),
    artifacts=(SCOPE, COMPETITORS, PROFILE, NEEDS, MATRIX, FINDINGS, TESTS),
    relations=(
        RelationKind("same_entity_as", src=PAGES, dst=PAGES, symmetric=True),
        RelationKind("contradicts", src=PAGES, dst=PAGES, symmetric=True, cites="per_end"),
    ),
    tools=(*tools.TOOLS, ToolUse("save_artifact")),
    tasks=(
        Task(
            "scope",
            tools=("list_inputs", "search_inputs", "read_document", "todo", "save_artifact"),
            writes=("scope",),
            done=checks.scope_done,
            interrupt=DeclaredInterrupt("input", "question"),
            extracts=True,
            brief="prompts/scope.md",
            turn_limit=6,
        ),
        Task(
            "discover",
            needs=("scope",),
            reads=("scope",),
            tools=(
                "list_inputs",
                "search_inputs",
                "read_document",
                "web_search",
                "fetch_url",
                "todo",
                "save_artifact",
            ),
            writes=("competitors",),
            extracts=True,
            done=checks.discover_done,
            brief="prompts/discover.md",
            turn_limit=6,
        ),
        Task(
            "evidence",
            per=artifact_keys("competitors", "companies"),
            needs=("discover",),
            reads=("competitors",),
            tools=("read_document", "fetch_url", "todo", "save_artifact"),
            writes=("profile",),
            extracts=True,
            done=checks.profile_done,
            brief="prompts/evidence.md",
            on_child_blocked="partial",
            max_finish_vetoes=1,
            turn_limit=4,
        ),
        Task(
            "voice",
            needs=("scope",),
            reads=("scope",),
            tools=("list_inputs", "search_inputs", "read_document", "todo", "save_artifact"),
            writes=("needs",),
            extracts=True,
            done=checks.needs_done,
            brief="prompts/voice.md",
            turn_limit=5,
        ),
        Task(
            "report",
            needs=("evidence", "voice"),
            reads=("scope", "competitors", Read("research.profile", keys="all"), "needs"),
            tools=("save_artifact",),
            writes=("matrix", "findings", "tests"),
            done=checks.report_done,
            brief="prompts/report.md",
            finalizer=True,
            turn_limit=4,
        ),
    ),
    completion=(checks.outputs_exist,),
    model_aliases=MODEL_ALIASES,
    budget=Budget(model_turns=90, tool_calls=140, active_ms=600_000),
    limits=Limits(turn_limit=6),
    context_policy=ContextPolicy(
        result_cap_chars=24_000,
        read_cap_chars=24_000,
        read_block_cap_chars=80_000,
        context_limit_chars=200_000,
        high_water_chars=150_000,
        low_water_chars=120_000,
    ),
    sources=(VersionedSourceSpec(tools.SOURCE, collections=tools.COLLECTIONS),),
)
