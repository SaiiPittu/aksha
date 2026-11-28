"""The patent-disclosure agent's definition: declarations only (ADR-P20).

The Task tree, in the order the scheduler runs it:

The inputs are whatever the person uploaded (invention notes, contributor and disclosure
tables, emails, priorities, blank form templates, publications), read with the harness's
shared input tools; each is recognised by its content (``world.KINDS``), never by its name
or folder.

1. ``scope`` lists the invention candidates and the blank forms, and reads the request
   into the ``portfolio``'s plan: which candidates, whether to search prior art, which
   forms to fill, whether to send, and how many top business priorities count as top.
2. ``candidate``, the supervisor Task over the listed candidates (``portfolio.py``, the
   portfolio kit): ``extract[Cn]`` for every candidate (the triage tier, the only Task
   that reads the raw records; it asks the person, an ``input`` interrupt on that
   instance only, for a value the note leaves undecided), then ``deep[Cn]``, whose
   children each read what the one before saved: ``prior_art`` (search the uploaded
   publications and the web, fetch what matches and leave the noise, families by
   priority), ``compare`` (the element-by-family matrix) and ``recommend`` (the rule
   table, and the research outputs that bear on business importance, read by artifact ID
   and version and cited for business importance only). With at most eight candidates
   every one gets deep work and the supervisor takes no turn. Above that, the supervisor
   reads the work board and commits rounds of directives: which candidates get deep work
   and which are dropped with a reason; code caps the deep tier and its budget and gives
   deep work to an audit sample of the drops.
3. ``table`` gathers every recommendation and, when the supervisor decided, the batched
   review of its decisions, the drop list and the audit; ``select`` asks the person to
   choose (a ``choice`` interrupt, the drop list beside the options) and saves it.
4. Per selected candidate, keyed by code from the selection: ``prepare`` fills each blank
   form the plan names (every one found, when it names none) through the form tool,
   ``packet`` assembles the counsel-review packet and ``send`` sends it when the plan
   says to, which needs a person's approval of that exact packet version.

The agent prepares; it never decides. No patentability or freedom-to-operate opinion,
no drafted claims, no legal deadline and no filing: see ``prompts/instructions.md``.
"""

from __future__ import annotations

from typing import Any

from aksha.extensions.portfolio import commit_tool, supervisor_task
from aksha.extensions.portfolio import kinds as portfolio_kinds
from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    Budget,
    CitePolicy,
    ContextPolicy,
    DeclaredInterrupt,
    FieldSpec,
    ModelAlias,
    Read,
    Task,
    ToolUse,
    VersionedSourceSpec,
    artifact_keys,
    kind_question,
)

from . import checks, portfolio, rules
from .tools import build_tools
from .world import COLLECTIONS, SOURCE_NAME, SourceWorld
from .world import KINDS as DOC_KINDS

OBJECT: dict[str, Any] = {"type": "object"}


def _schema(*required: str) -> dict[str, Any]:
    return {"type": "object", "required": list(required)}


STATUS = {"enum": ["built_and_tested", "in_use_under_nda", "prototype", "idea_only", "unknown"]}
"""How far the invention is built: what the rule table reads (``rules.next_action``)."""
QUOTE = {"type": "object", "properties": {"file": {"type": "string"}, "quote": {"type": "string"}}}

EXTRACTION = {
    "type": "object",
    "required": ["candidate", "mechanism", "contributors", "disclosures", "implementation",
                 "business_priority", "missing_documents"],
    "properties": {
        "implementation": {"type": "object", "required": ["status"],
                           "properties": {"status": STATUS}},
        "business_priority": {
            "type": "object",
            "required": ["rank"],
            "properties": {"rank": {"type": ["integer", "null"], "minimum": 1}},
        },
        "missing_documents": {"type": "array"},
        "disclosures": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["public", "describes_fully"],
                "properties": {"public": {"type": "string"},
                               "describes_fully": {"type": "boolean"}},
            },
        },
    },
}  # fmt: skip
"""The extraction, with every key the rule table and its check read declared here."""

COMPARISON = {
    "type": "object",
    "required": ["candidate", "cells"],
    "properties": {
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["element", "family", "state"],
                "properties": {
                    "state": {"enum": ["disclosed", "partial", "not_found", "not_checked"]}
                },
            },
        }
    },
}
PACKET = {
    "type": "object",
    "required": ["candidate", "scope_note", "sections"],
    "properties": {
        "sections": {
            "type": "array",
            "minItems": 1,
            "prefixItems": [
                {"type": "object", "required": ["title"],
                 "properties": {"title": {"const": "Needs counsel confirmation"}}}
            ],
        }
    },
}  # fmt: skip
"""The packet opens with what counsel must confirm (``checks.packet_saved``)."""
PORTFOLIO = {
    "type": "object",
    "required": ["candidates", "plan"],
    "properties": {
        "plan": {
            "type": "object",
            "required": ["candidates", "prior_art", "forms", "send", "priority_cutoff"],
            "properties": {
                "candidates": {"type": "array", "items": {"type": "string"}},
                "prior_art": {"type": "boolean"},
                "forms": {"type": "array", "items": {"type": "string"}},
                "send": {"type": "boolean"},
                "priority_cutoff": {"type": "integer", "minimum": 0},
            },
        }
    },
}
"""The candidates and the plan the request asks for: which candidates, whether to search
prior art, which blank forms to fill (an empty list fills every one found), whether to
send, and how many top business priorities count as top for the rule table."""

KINDS = (
    ArtifactKind("portfolio", PORTFOLIO),
    ArtifactKind("extraction", EXTRACTION, keyed=True),
    ArtifactKind(
        "prior_art", _schema("candidate", "query", "publications", "families"), keyed=True
    ),
    ArtifactKind("comparison", COMPARISON, keyed=True),
    ArtifactKind(
        "recommendation",
        _schema("candidate", "next_action", "why", rules.BUSINESS_PART),
        keyed=True,
        # Each research output read is cited by its exact version, and only here: market
        # research bears on business importance, never on novelty (checks, rules.RESEARCH_NOTE).
        cites=(
            CitePolicy(
                f"/{rules.BUSINESS_PART}/research/*",
                forms=frozenset({"artifact"}),
                artifact_kinds=frozenset(rules.RESEARCH_KINDS),
            ),
        ),
        compute=rules.computed_recommendation,  # business_importance.note is the code's
    ),
    ArtifactKind("table", _schema("rows")),
    ArtifactKind("selection", _schema("selected", "based_on"), reviewable=True),
    ArtifactKind(
        "form",
        _schema("template", "candidate", "fields", "counts"),
        keyed=True,
        compute=rules.computed_form,
    ),
    ArtifactKind("packet", PACKET, keyed=True, reviewable=True),
    ArtifactKind("handoff", _schema("candidate", "packet", "status"), keyed=True),
    *portfolio_kinds(),
)

SAVE = "save_artifact"
PER_SELECTED = artifact_keys("selection", "selected")


EXTRACT = Task(
    "extract",
    extracts=True,
    tools=("list_inputs", "search_inputs", "read_document", "todo", SAVE),
    writes=("extraction",),
    interrupt=DeclaredInterrupt("input", "missing detail"),
    done=checks.extraction_saved,
    reads=("portfolio",),
)
"""Triage: every candidate's extraction, the only Task that reads the raw records."""

DEEP = Task(
    "deep",
    extracts=True,
    tools=("search_publications", "fetch_publication", "web_search", "fetch_url", SAVE),
    writes=("prior_art", "comparison", "recommendation"),
    children=(
        Task(
            "prior_art",
            extracts=True,
            reads=("extraction", "portfolio"),
            tools=(
                "search_publications",
                "fetch_publication",
                "web_search",
                "fetch_url",
                SAVE,
            ),
            writes=("prior_art",),
            done=checks.prior_art_saved,
        ),
        Task(
            "compare",
            needs=("prior_art",),
            reads=("extraction", "prior_art"),
            tools=(SAVE,),
            writes=("comparison",),
            done=checks.comparison_saved,
        ),
        Task(
            "recommend",
            needs=("compare",),
            reads=(
                "extraction",
                "comparison",
                "portfolio",
                # Another agent's outputs, by kind: absent when the research agent has not
                # run, and then business importance from research is recorded as unknown.
                *(Read(kind) for kind in rules.RESEARCH_KINDS),
            ),
            tools=(SAVE,),
            writes=("recommendation",),
            done=checks.recommendation_follows_rules,
        ),
    ),
)
"""The deep tier: prior art, the matrix and the recommendation, for admitted candidates."""


def candidates() -> Task:
    """The supervisor over the candidates (``portfolio.py``): triage for all, deep for some."""
    return supervisor_task(portfolio.SPEC, triage=EXTRACT, deep=DEEP, brief="prompts/supervisor.md")


def tasks() -> tuple[Task, ...]:
    """The Task tree (module doc)."""
    return (
        Task(
            "scope",
            extracts=True,
            tools=("list_inputs", "search_inputs", "read_document", "list_templates", "todo", SAVE),
            writes=("portfolio",),
            done=checks.portfolio_saved,
        ),
        candidates(),
        Task(
            "table",
            needs=("candidate",),
            reads=(
                Read("patent.recommendation", keys="all"),
                Read("patent.extraction", keys="all"),
                Read("patent.directive", keys="all"),
                "round",
            ),
            tools=(SAVE,),
            writes=("table",),
            done=checks.table_saved,
        ),
        Task(
            "select",
            needs=("table",),
            reads=("table",),
            tools=(SAVE,),
            writes=("selection",),
            interrupt=DeclaredInterrupt("choice", "candidate selection"),
            done=checks.selection_saved,
        ),
        Task(
            "prepare",
            per=PER_SELECTED,
            extracts=True,
            reads=("extraction", "comparison", "recommendation", "prior_art", "portfolio"),
            tools=(
                "list_inputs",
                "search_inputs",
                "read_document",
                "list_templates",
                "read_template",
                "fill_form",
            ),
            writes=("form",),
            done=checks.forms_filled,
        ),
        Task(
            "packet",
            per=PER_SELECTED,
            needs=("prepare",),
            reads=(
                Read("patent.form", keys="all"),
                "comparison",
                "recommendation",
                "extraction",
                "prior_art",
                "portfolio",
            ),
            tools=(SAVE,),
            writes=("packet",),
            done=checks.packet_saved,
        ),
        Task(
            "send",
            per=PER_SELECTED,
            needs=("packet",),
            reads=("packet", "portfolio"),
            tools=("send_packet", SAVE),
            writes=("handoff",),
            done=checks.handoff_recorded,
        ),
    )


def _fields(*specs: tuple[str, str, str]) -> tuple[FieldSpec, ...]:
    return tuple(FieldSpec(n, t, description=d) for n, t, d in specs)  # type: ignore[arg-type]


MODEL_ALIASES = (
    ModelAlias(
        "doc_kind",
        "classification",
        allow_adapted=True,
        on_exhausted="unknown",
        questions=(kind_question(DOC_KINDS),),
    ),
    ModelAlias(
        "note_structure",
        "extraction",
        allow_adapted=True,
        on_exhausted="unknown",
        fields=_fields(
            ("candidate_id", "text", "the invention's own ID if the document gives one"),
            ("title", "text", "the invention's title"),
            ("date", "date", "the date of the earliest record of the invention in this document"),
            ("problem", "text", "the problem the invention solves"),
            (
                "mechanism_steps",
                "list",
                "each step of how the invention works, in order; empty "
                "when the document describes no invention",
            ),
            ("parameters", "list", "each named parameter or setting with its value"),
            ("alternatives", "list", "each alternative considered or rejected"),
            ("evidence", "text", "what says how far it is built or tested"),
            (
                "implementation_status",
                "text",
                "one of built_and_tested, in_use_under_nda, prototype, idea_only, unknown",
            ),
            ("open_points", "list", "each question or point the document leaves open"),
            ("references", "list", "each other document the text names by file name"),
            (
                "opinions",
                "list",
                "each view a person gives of the idea itself (how strong or new they think "
                "it is), as written: an opinion to report, never a finding",
            ),
        ),
    ),
    ModelAlias(
        "priorities",
        "extraction",
        allow_adapted=True,
        on_exhausted="unknown",
        fields=_fields(
            (
                "items",
                "list",
                "each ranked business priority, most important first; empty "
                "when the document states no business priorities",
            ),
        ),
    ),
    ModelAlias(
        "template_fields",
        "extraction",
        allow_adapted=True,
        on_exhausted="unknown",
        fields=_fields(
            ("title", "text", "the blank form's title"),
            (
                "fields",
                "list",
                "each field the blank form asks for, by its name, in order; "
                "empty when the document is not a blank form",
            ),
            ("required", "list", "each field name the form marks required"),
            ("counsel_only", "list", "each field name only counsel completes"),
        ),
    ),
    ModelAlias(
        "publication_biblio",
        "extraction",
        allow_adapted=True,
        on_exhausted="unknown",
        fields=_fields(
            ("number", "text", "the patent publication's number; absent for anything else"),
            ("kind_code", "text", "its kind code"),
            ("office", "text", "the office that published it"),
            ("title", "text", "its title"),
            ("filing_date", "date", "its filing date"),
            ("priority_date", "date", "its earliest priority date"),
            ("publication_date", "date", "its publication date"),
            ("grant_date", "date", "its grant date"),
            ("priority_claims", "list", "each priority claim: the number with its date"),
            ("status", "list", "each legal status wording, verbatim"),
            ("claims", "list", "each claim, in order"),
            ("abstract", "text", "its abstract"),
            ("description", "text", "its description"),
            ("url", "text", "where it was found"),
        ),
    ),
)
"""The fast-class aliases the tools ask (``world.py``): the decide alias and one schema per
document kind, asked by the shared input tools (``tools.READING``) and the domain tools.
Which model answers each is configuration (fakes offline; a native or an adapted general
model live). Which column of a table holds what, and which candidate a row is about, the
generation model reads itself."""


def build_agent(world: SourceWorld) -> Agent:
    """The agent with its read tools bound to ``world``."""
    return Agent(
        name="patent",
        purpose=(
            "Prepare any venture's invention candidates for counsel review from its own "
            "papers: extract each mechanism, its contributors and dated disclosures, search "
            "and compare prior art, recommend a next step, and send a counsel-review packet "
            "only after a person approves it."
        ),
        instructions="prompts/instructions.md",
        # Prior art and the papers move, and an approved packet must follow (decision 38).
        invalidation="rebuild",
        # A version rests on what it cites and on its scoped claims (the portfolio's
        # listings); one that cites nothing stays keyed on every read (ADR-P05 M2).
        invalidation_keys="citations",
        reads=(*(f"record:{c}" for c in COLLECTIONS), *rules.RESEARCH_KINDS),
        artifacts=KINDS,
        tools=(*build_tools(world), commit_tool(portfolio.SPEC), ToolUse(SAVE)),
        tasks=tasks(),
        # Sized for a portfolio of about twenty candidates under the supervisor: triage for
        # every one and deep work for about ten (measured on the twenty-candidate portfolio
        # world: 138 turns and about 740 calls to the packet, most of the calls the shared
        # input tools' listing and reads in triage). Running the deep tier for all twenty
        # would need about 196 turns; the supervisor's deep budget (portfolio.DEEP_BUDGET)
        # keeps it inside this. No Task declares a turn limit (decision 42): long live work
        # runs to its check, and this budget is the runaway backstop (live mode triples it).
        budget=Budget(model_turns=160, tool_calls=900),
        model_aliases=MODEL_ALIASES,
        context_policy=ContextPolicy(
            result_cap_chars=60_000,
            read_cap_chars=60_000,
            read_block_cap_chars=200_000,
            context_limit_chars=600_000,
            high_water_chars=400_000,
            low_water_chars=300_000,
        ),
        sources=(VersionedSourceSpec(SOURCE_NAME, collections=COLLECTIONS),),
        extensions=("portfolio",),
    )


AGENT = build_agent(SourceWorld())
