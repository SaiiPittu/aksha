"""The patent-disclosure agent's definition: declarations only (ADR-P20).

The Task tree, in the order the scheduler runs it:

The inputs are whatever the person uploaded (invention notes, contributor and disclosure
tables, emails, priorities, blank form templates, publications); ``world.py`` recognises
each by its content, never by its name or folder.

1. ``scope`` lists the invention candidates into ``portfolio``.
2. ``candidate[Cn]``, one instance per listed candidate, holds the per-candidate work as
   its children, each reading what the one before saved: ``extract`` (the only Task that
   reads the raw records; it asks the person, an ``input`` interrupt on that instance
   only, for a value the note leaves undecided), ``prior_art`` (search and fetch,
   families by priority), ``compare`` (the element-by-family matrix) and ``recommend``
   (the rule table).
3. ``table`` gathers every candidate's recommendation; ``select`` asks the person to
   choose (a ``choice`` interrupt) and saves the selection.
4. Per selected candidate, keyed by code from the selection: ``prepare`` fills the two
   forms through the form tool, ``packet`` assembles the counsel-review packet and
   ``send`` sends it, which needs a person's approval of that exact packet version.

The agent prepares; it never decides. No patentability or freedom-to-operate opinion,
no drafted claims, no legal deadline and no filing: see ``prompts/instructions.md``.
"""

from __future__ import annotations

from typing import Any

from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    Budget,
    ContextPolicy,
    DeclaredInterrupt,
    Limits,
    Read,
    Task,
    ToolUse,
    VersionedSourceSpec,
    artifact_keys,
)

from . import checks
from .tools import build_tools
from .world import COLLECTIONS, SOURCE_NAME, SourceWorld

OBJECT: dict[str, Any] = {"type": "object"}


def _schema(*required: str) -> dict[str, Any]:
    return {"type": "object", "required": list(required)}


KINDS = (
    ArtifactKind("portfolio", _schema("candidates")),
    ArtifactKind(
        "extraction", _schema("candidate", "mechanism", "contributors", "disclosures"), keyed=True
    ),
    ArtifactKind(
        "prior_art", _schema("candidate", "query", "publications", "families"), keyed=True
    ),
    ArtifactKind("comparison", _schema("candidate", "cells"), keyed=True),
    ArtifactKind("recommendation", _schema("candidate", "next_action", "why"), keyed=True),
    ArtifactKind("table", _schema("rows")),
    ArtifactKind("selection", _schema("selected", "based_on"), reviewable=True),
    ArtifactKind("disclosure", _schema("template", "candidate", "fields", "counts"), keyed=True),
    ArtifactKind("worksheet", _schema("template", "candidate", "fields", "counts"), keyed=True),
    ArtifactKind(
        "packet", _schema("candidate", "scope_note", "sections"), keyed=True, reviewable=True
    ),
    ArtifactKind("handoff", _schema("candidate", "packet", "status"), keyed=True),
)

SAVE = "save_artifact"
PER_CANDIDATE = artifact_keys("portfolio", "candidates")
PER_SELECTED = artifact_keys("selection", "selected")


def tasks() -> tuple[Task, ...]:
    """The Task tree (module doc)."""
    return (
        Task(
            "scope",
            tools=("list_candidates", SAVE),
            writes=("portfolio",),
            done=checks.portfolio_saved,
        ),
        Task(
            "candidate",
            per=PER_CANDIDATE,
            tools=(
                "read_note",
                "read_contributors",
                "read_disclosures",
                "read_document",
                "read_priorities",
                "search_publications",
                "fetch_publication",
                SAVE,
            ),
            writes=("extraction", "prior_art", "comparison", "recommendation"),
            children=(
                Task(
                    "extract",
                    extracts=True,
                    tools=(
                        "read_note",
                        "read_contributors",
                        "read_disclosures",
                        "read_document",
                        "read_priorities",
                        SAVE,
                    ),
                    writes=("extraction",),
                    interrupt=DeclaredInterrupt("input", "missing detail"),
                    done=checks.extraction_saved,
                ),
                Task(
                    "prior_art",
                    needs=("extract",),
                    reads=("extraction",),
                    tools=("search_publications", "fetch_publication", SAVE),
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
                    reads=("extraction", "comparison"),
                    tools=(SAVE,),
                    writes=("recommendation",),
                    done=checks.recommendation_follows_rules,
                ),
            ),
        ),
        Task(
            "table",
            needs=("candidate",),
            reads=(Read("patent.recommendation", keys="all"),),
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
            reads=("extraction", "comparison", "recommendation", "prior_art"),
            tools=("read_people", "list_templates", "read_template", "fill_form"),
            writes=("disclosure", "worksheet"),
            done=checks.forms_filled,
        ),
        Task(
            "packet",
            per=PER_SELECTED,
            needs=("prepare",),
            reads=(
                "disclosure",
                "worksheet",
                "comparison",
                "recommendation",
                "extraction",
                "prior_art",
            ),
            tools=(SAVE,),
            writes=("packet",),
            done=checks.packet_saved,
        ),
        Task(
            "send",
            per=PER_SELECTED,
            needs=("packet",),
            reads=("packet",),
            tools=("send_packet", SAVE),
            writes=("handoff",),
            done=checks.handoff_recorded,
        ),
    )


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
        reads=tuple(f"record:{c}" for c in COLLECTIONS),
        artifacts=KINDS,
        tools=(*build_tools(world), ToolUse(SAVE)),
        tasks=tasks(),
        budget=Budget(model_turns=120, tool_calls=240),
        limits=Limits(turn_limit=8),
        context_policy=ContextPolicy(
            result_cap_chars=60_000,
            read_cap_chars=60_000,
            read_block_cap_chars=200_000,
            context_limit_chars=600_000,
            high_water_chars=400_000,
            low_water_chars=300_000,
        ),
        sources=(VersionedSourceSpec(SOURCE_NAME, collections=COLLECTIONS),),
    )


AGENT = build_agent(SourceWorld())
