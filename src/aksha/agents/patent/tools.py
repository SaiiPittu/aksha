"""The patent agent's tools: the harness's shared tools, plus the one action of its own.

Everything the agent reads or fills goes through tools the harness declares, the same for
every agent:

* the inputs with ``list_inputs``, ``read_document`` and ``search_inputs``, given
  :data:`READING` (its source and its document kinds, ``world.KINDS``), so a listing names
  each input's kind, ``search_inputs(kinds=[...])`` searches only the inputs of some kinds
  and ``read_document(extract=true)`` fills that kind's typed fields. The uploaded
  publications are inputs of the kind ``prior_art_publication``: a search over them is
  ``search_inputs`` with that kind, a read of one is ``read_document``, and its
  ``publication_biblio`` extraction gives its numbers, dates, priority claims (the family
  key), status wordings verbatim and claims;
* the web with ``web_search`` and ``fetch_url``, the plan with ``todo``;
* the blank forms with ``read_form`` and ``fill_form`` (``runtime/forms``), given
  :data:`FORMS`: which kind is a blank form and which fields of its ``template_fields``
  extraction list its fields, the required ones and the ones only counsel completes. The
  form tool checks every fill in code, so a model cannot mark a counsel field complete.

Joining what the papers say (which rows are one candidate's, which publications are one
family, how many days ago a disclosure was) is the model's reading, not a tool's.

The agent's own tool is its one external effect: ``send_packet`` needs a person's approval
of the exact action (the packet version and the recipient), and the outbox sends it once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import (
    ApprovalRule,
    EffectSpec,
    Err,
    FormSpec,
    InputReading,
    RetryPolicy,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    fetch_url_tool,
    form_tools,
    input_tools,
    todo_tool,
    web_search_tool,
)

from .world import COLLECTION, DECIDE, KINDS, SOURCE_NAME

AGENT = "patent"
RECIPIENT = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$"}
"""A recipient the person names (``counsel-primary``, an address): the approval binds the
exact one, and the counsel mail system delivers only to recipients it is configured for."""
FORM_KIND = "form"
"""Every filled form, whatever template it is, keyed ``<candidate>~<template>``."""
COUNSEL = "needs_counsel_confirmation"
"""The status of a field counsel still has to settle."""
_SLOW = RetryPolicy(timeout_ms=600_000, max_retries=1)
"""A read asks fast-class models about several records: a live model takes seconds each."""
READING = InputReading(
    SOURCE_NAME,
    COLLECTION,
    decide=DECIDE,
    kinds=KINDS,
    page_lines=2_000,
    page_chars=40_000,
    result_chars=60_000,
    retry=_SLOW,
    search_kinds=True,
)
"""How the shared input tools read for this agent: a window fits its 60,000-character cap."""
FORMS = FormSpec(
    "form_template",
    saves=FORM_KIND,
    fields="fields",
    required="required",
    confirm="counsel_only",
    title="title",
    name_key="template_id",
    confirm_status=COUNSEL,
    confirmer="counsel",
    question="counsel_question",
    key_field="candidate",
)
"""The patent agent's blank forms, as data: the ``form_template`` kind, its
``template_fields`` extraction (``agent.MODEL_ALIASES``), and counsel as the confirmer."""


def form_key(candidate: str, template: str) -> str:
    """The key of ``candidate``'s filled ``template``."""
    return f"{candidate}~{template}"


STRING = {"type": "string", "minLength": 1}


def send_packet(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Never called directly: the outbox sends through the counsel mail system."""
    return ToolOutcome(Err("outbox_only", "the send runs through the outbox"))


def _facts(view: Any, args: Mapping[str, object]) -> Mapping[str, str]:
    return {"recipient": str(args["recipient"]), "packet": str(args["packet"])}


def _targets(args: Mapping[str, object]) -> tuple[str, ...]:
    return (str(args["packet"]),)


SEND = ToolDecl(
    ToolSpec(
        "send_packet",
        AGENT,
        "external",
        "Send one counsel-review packet version to the recipient the person names. Needs a "
        "person's approval of this exact action, recipient included.",
        {
            "type": "object",
            "properties": {"packet": STRING, "recipient": RECIPIENT},
            "required": ["packet", "recipient"],
            "additionalProperties": False,
        },
        external=EffectSpec("counsel_mail", ("/packet", "/recipient"), "reconcile"),
        approval=ApprovalRule(facts=_facts, targets=_targets),
    ),
    send_packet,
)
"""The agent's one external effect: the counsel-review packet, sent once after approval."""


def build_tools() -> tuple[ToolDecl, ...]:
    """The agent's tools: the harness's shared ones, then its own send."""
    return (
        web_search_tool(AGENT),
        fetch_url_tool(AGENT, READING),
        *input_tools(AGENT, READING),
        todo_tool(AGENT),
        *form_tools(AGENT, READING, FORMS),
        SEND,
    )
