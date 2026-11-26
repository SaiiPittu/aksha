"""The research agent's definition: declared data only (ADR-P20 D4).

Task tree, in order: ``scope`` asks only the questions whose answer changes the result;
``discover`` searches the corpus for competitors and manual alternatives and resolves
entities; ``evidence`` fans out per resolved company (a blocked company leaves an explicit
unknown, ``on_child_blocked="partial"``); ``voice`` reads the customer reviews; ``report``
(the finalizer) saves the matrix, the findings and the tests to run next. The run is
complete when the matrix, the needs, the findings and the tests exist at current versions.
"""

from __future__ import annotations

from typing import Any, Literal

from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    Budget,
    CitePolicy,
    ContextPolicy,
    DeclaredInterrupt,
    Limits,
    Read,
    RelationKind,
    Task,
    ToolUse,
    VersionedSourceSpec,
    artifact_keys,
)

from . import checks, tools

OBJECT: dict[str, Any] = {"type": "object"}
LIST: dict[str, Any] = {"type": "array"}


def _obj(*required: str, **props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


Form = Literal["span", "statement", "artifact"]
SPAN: frozenset[Form] = frozenset({"span"})
SPAN_OR_ARTIFACT: frozenset[Form] = frozenset({"span", "artifact"})
ARTIFACT: frozenset[Form] = frozenset({"artifact"})
CELLS = ("unit_price", "subscription", "power", "battery_life", "privacy")
"""The evidence grid's columns: each cell lists cited entries or explicit unknowns."""


def _cells(prefix: str, forms: frozenset[Form] = SPAN) -> tuple[CitePolicy, ...]:
    return tuple(
        CitePolicy(f"{prefix}/{c}/*", forms=forms, may_be_unknown=True, value_in_quote=True)
        for c in CELLS
    )


SCOPE = ArtifactKind(
    "scope",
    _obj(
        "question",
        "segment",
        "geography",
        "boundary",
        "asked",
        question={"type": "string"},
        segment=OBJECT,
        geography=OBJECT,
        boundary=OBJECT,
        asked=LIST,
    ),
    cites=(CitePolicy("/boundary/out/*", forms=SPAN),),
)
COMPETITORS = ArtifactKind(
    "competitors",
    _obj("companies", "pages_found", "resolved", "manual_alternatives", companies=LIST),
    cites=(
        CitePolicy("/companies/*/canonical", forms=SPAN, value_in_quote=True),
        CitePolicy("/companies/*/aliases/*", forms=SPAN, value_in_quote=True),
        CitePolicy("/companies/*/merge_evidence/*", forms=SPAN),
        CitePolicy("/manual_alternatives", may_be_unknown=True),
    ),
)
PROFILE = ArtifactKind(
    "profile",
    _obj("key", "name", "pages", *CELLS, "flags", "conflicts"),
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

PAGE = f"record:{tools.PAGES}"

AGENT = Agent(
    name="research",
    purpose=(
        "Research who sells a product, at what price and what customers say: competitors, "
        "prices, reviews and needs, every claim cited to a fetched page."
    ),
    instructions="prompts/instructions.md",
    reads=tuple(f"record:{c}" for c in tools.COLLECTIONS),
    artifacts=(SCOPE, COMPETITORS, PROFILE, NEEDS, MATRIX, FINDINGS, TESTS),
    relations=(
        RelationKind("same_entity_as", src=(PAGE,), dst=(PAGE,), symmetric=True),
        RelationKind("contradicts", src=(PAGE,), dst=(PAGE,), symmetric=True, cites="per_end"),
    ),
    tools=(*tools.TOOLS, ToolUse("save_artifact")),
    tasks=(
        Task(
            "scope",
            tools=("search", "read_note", "save_artifact"),
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
            tools=("search", "fetch_page", "save_artifact"),
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
            tools=("fetch_page", "save_artifact"),
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
            tools=("search", "read_reviews", "save_artifact"),
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
    budget=Budget(model_turns=45, tool_calls=70, active_ms=600_000),
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
