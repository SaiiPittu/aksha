"""The patent agent's tools: the harness's shared reads, plus the domain's own actions.

The inputs are read with the harness's input tools (``list_inputs``, ``read_document``,
``search_inputs``), the web with ``web_search`` and ``fetch_url``, the plan with ``todo``,
all declared by the harness; the agent gives them :data:`READING`, its source and its
document kinds (``world.KINDS``), so a listing names each input's kind and
``read_document(extract=true)`` fills that kind's typed fields. Joining what the papers say
(which rows are one candidate's, who a person is, how many days ago a disclosure was) is
the model's reading, not a tool's.

The agent's own tools are the domain's: the prior-art search over the uploaded
publications and the read of one publication (``search_publications``,
``fetch_publication``), the blank forms that ``fill_form`` checks a filled form against
(``list_templates``, ``read_template``: the form tool's contract, so they show exactly the
fields it checks), and the two actions. Every read names what it read in ``Ok.reads``; the
search and the listing are recorded queries: each read is the query ID at the hash of its
hit set.

``fill_form`` is a store tool: it checks the fields against the blank template in code
(``rules.form_refusal``) and saves the form, so a model cannot mark a counsel field
complete. ``send_packet`` is an external effect: it needs a person's approval of the exact
action (the packet version and the recipient), and the outbox sends it once.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aksha.runtime.api import (
    ApprovalRule,
    EffectSpec,
    Err,
    InputReading,
    InputRef,
    Ok,
    RetryPolicy,
    SaveRequest,
    SpanDraft,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    fetch_url_tool,
    input_tools,
    todo_tool,
    web_search_tool,
)

from . import rules
from .world import (
    COLLECTION,
    DECIDE,
    KINDS,
    SOURCE_NAME,
    NotInWorld,
    SourceWorld,
    query_id,
    query_version,
)

AGENT = "patent"
RECIPIENT = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$"}
"""A recipient the person names (``counsel-primary``, an address): the approval binds the
exact one, and the counsel mail system delivers only to recipients it is configured for."""
FORM_KIND = "form"
"""Every filled form, whatever template it is, keyed ``<candidate>~<template>``."""
ALIASES = frozenset({"doc_kind", "template_fields", "publication_biblio"})
"""The fast-class aliases the domain tools ask (``world.py``)."""
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
)
"""How the shared input tools read for this agent: a window fits its 60,000-character cap."""


def form_key(candidate: str, template: str) -> str:
    """The key of ``candidate``'s filled ``template``."""
    return f"{candidate}~{template}"


def _obj(props: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(props),
        "required": list(required),
        "additionalProperties": False,
    }


CANDIDATE = _obj({"candidate": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"}},
                 ("candidate",))  # fmt: skip
STRING = {"type": "string", "minLength": 1}


def _whole(world: SourceWorld, record: str) -> InputRef:
    """A read of the whole record at its version."""
    return InputRef("record", record, world.found(record).version)


def _spans(world: SourceWorld, *records: str | None) -> tuple[SpanDraft, ...]:
    """Drafts over every passage of ``records``: what the view may quote, minted as spans."""
    out: list[SpanDraft] = []
    for r in dict.fromkeys(x for x in records if x):
        out.extend(world.quoted(r))
    return tuple(out)


def _guard(body: Callable[[ToolContext, Mapping[str, Any]], ToolOutcome]) -> Any:
    def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        try:
            return body(ctx, args)
        except NotInWorld as e:
            return ToolOutcome(Err("not_found", str(e)))

    run.__name__ = body.__name__
    run.__doc__ = body.__doc__
    return run


def build_tools(bound: SourceWorld) -> tuple[ToolDecl, ...]:
    """The agent's own tools, reading the inputs through the call's record reader.

    ``bound`` carries the date elapsed days count to, when a caller fixes it.
    """

    def list_templates(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.templates()
        read = InputRef("query", query_id("list_templates", args),
                        query_version(data["templates"]))  # fmt: skip
        return ToolOutcome(Ok({**data, "query": read.id}, reads=(read,)))

    def read_template(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.template(str(args["template"]))
        spans = _spans(world, data["file"])
        return ToolOutcome(Ok(data, spans=spans, reads=(_whole(world, data["file"]),)))

    def search_publications(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        groups = [[str(t) for t in g] for g in args["all_of"]]
        hits = world.search(groups)
        qid = query_id("search_publications", {"all_of": groups})
        searched = len(world.publication_ids())
        data = {"query": qid, "hits": hits, "publications_searched": searched}
        if not searched:
            data["note"] = (
                "no publication is among the inputs: nothing was searched, so no "
                "element can be called not_found against prior art"
            )
        read = InputRef("query", qid, query_version([h["id"] for h in hits]))
        return ToolOutcome(Ok(data, reads=(read,)))

    def fetch_publication(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.publication(str(args["id"]))
        spans = _spans(world, data["file"])
        return ToolOutcome(Ok(data, spans=spans, reads=(_whole(world, data["file"]),)))

    def fill_form(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        name, candidate = str(args["template"]), str(args["candidate"])
        listed = {t["template"]: t["form"] for t in world.templates()["templates"]}
        if name not in listed:
            return ToolOutcome(Err("unknown_template", f"{name!r} is no blank form among the "
                                   f"inputs; list_templates names {sorted(listed)}"))  # fmt: skip
        if ctx.key is not None and ctx.key != candidate:
            return ToolOutcome(Err("wrong_candidate", f"this instance prepares {ctx.key}"))
        template = world.template(name)
        read = _whole(world, template["file"])
        fields = [dict(f) for f in args["fields"]]
        refused = rules.form_refusal(template, fields)
        if refused is not None:
            return ToolOutcome(Err(refused[0], refused[1], paths=("/fields",)))
        declared = {f["field"]: f for f in template["fields"]}
        for f in fields:  # whether a field is required is the template's, never the model's
            f["required"] = bool(declared[f["field"]]["required"])
        fields.sort(key=lambda f: declared[f["field"]]["n"])
        counts = {s: sum(1 for f in fields if f["status"] == s) for s in rules.FIELD_STATUSES}
        content = {
            "template": name,
            "form": listed[name],
            "template_version": read.version,
            "candidate": candidate,
            "fields": fields,
            "counts": counts,
        }
        key = form_key(candidate, name)
        save = SaveRequest(FORM_KIND, key, content)
        data = {"saved": f"{FORM_KIND}/{key}", "counts": counts}
        return ToolOutcome(Ok(data, reads=(read,)), saves=(save,))

    def send_packet(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        # Never called directly: the outbox sends through the counsel mail system.
        return ToolOutcome(Err("outbox_only", "the send runs through the outbox"))

    def _facts(view: Any, args: Mapping[str, object]) -> Mapping[str, str]:
        return {"recipient": str(args["recipient"]), "packet": str(args["packet"])}

    def _targets(args: Mapping[str, object]) -> tuple[str, ...]:
        return (str(args["packet"]),)

    rec = frozenset({"input"})
    specs: list[tuple[ToolSpec, Any]] = [
        (
            ToolSpec(
                "list_templates",
                AGENT,
                "read",
                "List the blank forms among the inputs (any number, any layout), each with "
                "its title: the names fill_form takes.",
                _obj({}),
                collections=rec,
                recorded_query=True,
                result_key="/template",
                model_aliases=ALIASES,
                retry=_SLOW,
            ),
            list_templates,
        ),
        (
            ToolSpec(
                "read_template",
                AGENT,
                "read",
                "Read a blank form template's fields as fill_form checks them: which are "
                "required and which only counsel completes.",
                _obj({"template": STRING}, ("template",)),
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            read_template,
        ),
        (
            ToolSpec(
                "search_publications",
                AGENT,
                "read",
                "Search the publications among the inputs: a "
                "hit matches every group (any one term of each group).",
                _obj(
                    {
                        "all_of": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "array", "minItems": 1, "items": STRING},
                        }
                    },
                    ("all_of",),
                ),
                collections=rec,
                recorded_query=True,
                result_key="/id",
                model_aliases=ALIASES,
                retry=_SLOW,
            ),
            search_publications,
        ),
        (
            ToolSpec(
                "fetch_publication",
                AGENT,
                "read",
                "Fetch one publication: identifiers, "
                "dates, URL, every status wording as written, claims and text.",
                _obj({"id": STRING}, ("id",)),
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            fetch_publication,
        ),
        (
            ToolSpec(
                "fill_form",
                AGENT,
                "store",
                "Fill a blank template for one candidate. Each "
                "field is complete, missing or needs_counsel_confirmation; counsel fields "
                "cannot be marked complete.",
                _obj(
                    {
                        "template": STRING,
                        "candidate": STRING,
                        "fields": {"type": "array", "items": {"type": "object"}},
                    },
                    ("template", "candidate", "fields"),
                ),
                collections=rec,
                writes_kinds=frozenset({f"patent.{FORM_KIND}"}),
                model_aliases=ALIASES,
            ),
            fill_form,
        ),
        (
            ToolSpec(
                "send_packet",
                AGENT,
                "external",
                "Send one counsel-review packet version to "
                "the recipient the person names. Needs a person's approval of this exact "
                "action, recipient included.",
                _obj(
                    {"packet": STRING, "recipient": RECIPIENT},
                    ("packet", "recipient"),
                ),
                external=EffectSpec("counsel_mail", ("/packet", "/recipient"), "reconcile"),
                approval=ApprovalRule(facts=_facts, targets=_targets),
            ),
            send_packet,
        ),
    ]
    shared = (web_search_tool(AGENT), fetch_url_tool(AGENT, READING),
              *input_tools(AGENT, READING), todo_tool(AGENT))  # fmt: skip
    return (*shared, *(ToolDecl(spec, _guard(body)) for spec, body in specs))
