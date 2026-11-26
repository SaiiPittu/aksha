"""The patent agent's own tools: typed reads over the sources, the form tool and the send.

Every read returns a view and names what it read in ``Ok.reads``: the whole record at its
version, or the table rows of one candidate as a record part at that part's version
(``world.py``), so the lineage of what the agent saves reaches the source text. The
search and the two listings are recorded queries: each read is the query ID at the hash
of its hit set.

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
    InputRef,
    Ok,
    RetryPolicy,
    SaveRequest,
    SpanDraft,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    fetch_text,
    fetch_url_spec,
    page_read,
    part_version,
    web_search_tool,
    where_part,
)

from . import rules
from .world import NotInWorld, OneRecord, SourceWorld, days_between, query_id, query_version

AGENT = "patent"
RECIPIENT = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$"}
"""A recipient the person names (``counsel-primary``, an address): the approval binds the
exact one, and the counsel mail system delivers only to recipients it is configured for."""
FORM_KIND = "form"
"""Every filled form, whatever template it is, keyed ``<candidate>~<template>``."""
ALIASES = frozenset({"doc_kind", "note_structure", "table_roles", "candidate_of", "priorities",
                     "template_fields", "publication_biblio"})  # fmt: skip
"""The fast-class aliases the read tools ask (``world.py``)."""


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
_SLOW = RetryPolicy(timeout_ms=600_000, max_retries=1)
"""A read asks fast-class models about several records: a live model takes seconds each."""


def _whole(world: SourceWorld, record: str) -> InputRef:
    """A read of the whole record at its version."""
    return InputRef("record", record, world.found(record).version)


def _parts(world: SourceWorld, parts: Any) -> tuple[InputRef, ...]:
    """Reads of the table rows a view used: each a record part at that part's version.

    A view whose rows no one candidate column selects (rows joined by what they say)
    reads the whole table.
    """
    out = []
    for record, column, value in parts:
        if column is None or value is None:
            out.append(_whole(world, record))
            continue
        part = where_part(record, column, str(value).split(","))
        found = world.found(record)
        version = part_version(found.text, part.partition("#")[2], found.version)
        out.append(InputRef("record", part, version or found.version))
    return tuple(out)


def _shown(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "parts"}


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

    def list_candidates(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.candidates()
        read = InputRef("query", query_id("list_candidates", args),
                        query_version([*data["candidates"], *data["unread"]]))  # fmt: skip
        return ToolOutcome(Ok(data, reads=(read,)))

    def read_note(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.note(str(args["candidate"]))
        spans = _spans(world, data["file"])
        return ToolOutcome(Ok(data, spans=spans, reads=(_whole(world, data["file"]),)))

    def read_contributors(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.contributor_rows(str(args["candidate"]))
        spans = _spans(world, *(r["file"] for r in data["rows"]))
        return ToolOutcome(Ok(_shown(data), spans=spans, reads=_parts(world, data["parts"])))

    def read_people(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        ids = sorted(str(p) for p in args["person_ids"])
        data = world.people(ids)
        return ToolOutcome(Ok(_shown(data), reads=_parts(world, data["parts"])))

    def read_disclosures(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.disclosure_rows(str(args["candidate"]))
        reads = _parts(world, data["parts"])
        shown = _shown(data)
        if world.today is not None:  # derived from the dates; not part of the record
            shown["as_of"] = world.today.isoformat()
            shown["rows"] = [
                {**r, "elapsed_days": days_between(r["date"], world.today) if r["date"] else None}
                for r in data["rows"]
            ]
        spans = _spans(world, *(r["file"] for r in data["rows"]))
        return ToolOutcome(Ok(shown, spans=spans, reads=reads))

    def read_document(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        path = str(args["path"])
        if ".." in path.split("/") or path not in world.records():
            return ToolOutcome(Err("not_found", f"{path} is not among the inputs"))
        data = world.document(path)
        return ToolOutcome(Ok(data, spans=_spans(world, path), reads=(_whole(world, path),)))

    def read_priorities(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.priorities()
        reads = () if data["file"] is None else (_whole(world, data["file"]),)
        return ToolOutcome(Ok(data, spans=_spans(world, data["file"]), reads=reads))

    def list_templates(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date, ctx.models)
        data = world.templates()
        read = InputRef("query", query_id("list_templates", args),
                        query_version(data["templates"]))  # fmt: skip
        return ToolOutcome(Ok(data, reads=(read,)))

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

    def fetch_url(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        fetched = fetch_text(ctx, str(args["url"]))
        if isinstance(fetched, Err):
            return ToolOutcome(fetched)
        record = fetched.record_id
        world = bound.over(OneRecord(fetched.text), ctx.run_date, ctx.models)
        found = world.publication_ids()
        if found:
            data = {"kind": "publication", **world.publication(found[0])}
        else:
            data = {"kind": world.kind(record), **world.document(record)}
        data = {"url": fetched.page.url, "final_url": fetched.page.final_url, **data}
        return ToolOutcome(Ok(data, spans=_spans(world, record), reads=(page_read(fetched),)))

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
                "list_candidates",
                AGENT,
                "read",
                "List the invention candidates (one per invention note among the inputs). "
                "'unread' names the inputs no kind fits and in which the fast readers found "
                "no invention: read each with read_document and say what it holds.",
                _obj({}),
                collections=rec,
                recorded_query=True,
                result_key="/candidate",
                model_aliases=ALIASES,
                retry=_SLOW,
            ),
            list_candidates,
        ),
        (
            ToolSpec(
                "read_note",
                AGENT,
                "read",
                "Read one candidate's invention note: dated "
                "record, problem, mechanism steps, parameters, alternatives, evidence, open "
                "points and referenced files.",
                CANDIDATE,
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            read_note,
        ),
        (
            ToolSpec(
                "read_contributors",
                AGENT,
                "read",
                "Read the contributor records for one "
                "candidate (who contributed what, when, under which agreement).",
                CANDIDATE,
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            read_contributors,
        ),
        (
            ToolSpec(
                "read_people",
                AGENT,
                "read",
                "Read the names, employers and roles of people by person ID.",
                _obj(
                    {"person_ids": {"type": "array", "items": STRING, "minItems": 1}},
                    ("person_ids",),
                ),
                collections=rec,
                model_aliases=ALIASES,
                retry=_SLOW,
            ),
            read_people,
        ),
        (
            ToolSpec(
                "read_disclosures",
                AGENT,
                "read",
                "Read the dated disclosure history rows "
                "for one candidate, with elapsed days to today. Dates only; no legal view.",
                CANDIDATE,
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            read_disclosures,
        ),
        (
            ToolSpec(
                "read_document",
                AGENT,
                "read",
                "Read a record linked from the disclosure history (an email or an event record).",
                _obj({"path": STRING}, ("path",)),
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            read_document,
        ),
        (
            ToolSpec(
                "read_priorities",
                AGENT,
                "read",
                "Read the ranked business priorities.",
                _obj({}),
                collections=rec,
                model_aliases=ALIASES,
                mints_citation_spans=True,
                retry=_SLOW,
            ),
            read_priorities,
        ),
        (
            ToolSpec(
                "list_templates",
                AGENT,
                "read",
                "List the blank forms among the inputs (any number, any layout), each with "
                "its title.",
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
                "Read a blank form template's fields.",
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
            fetch_url_spec(
                AGENT,
                "Fetch a web page by URL. A publication reads as fetch_publication reads one "
                "(identifiers, dates, URL, every status wording as written, claims, text); any "
                "other page as a document. Cite what it returns, never a search snippet.",
                model_aliases=ALIASES,
                retry=_SLOW,
            ),
            fetch_url,
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
    return (web_search_tool(AGENT), *(ToolDecl(spec, _guard(body)) for spec, body in specs))
