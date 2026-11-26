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
    SaveRequest,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    part_version,
    where_part,
)

from . import rules
from .world import NotInWorld, SourceWorld, days_between, query_id, query_version

AGENT = "patent"
RECIPIENTS = ("counsel-primary",)
"""The configured recipients: the only addresses ``send_packet`` takes."""

FORM_KINDS = ("disclosure", "worksheet")
"""The two forms a counsel packet carries; which template is which is read from the template."""


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


def _parts(world: SourceWorld, parts: Any) -> tuple[InputRef, ...]:
    """Reads of the table rows a view used: each a record part at that part's version."""
    out = []
    for record, column, value in parts:
        part = where_part(record, column, str(value).split(","))
        found = world.found(record)
        version = part_version(found.text, part.partition("#")[2], found.version)
        out.append(InputRef("record", part, version or found.version))
    return tuple(out)


def _shown(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "parts"}


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
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.candidates()
        read = InputRef("query", query_id("list_candidates", args),
                        query_version(data["candidates"]))  # fmt: skip
        return ToolOutcome(Ok(data, reads=(read,)))

    def read_note(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.note(str(args["candidate"]))
        return ToolOutcome(Ok(data, reads=(_whole(world, data["file"]),)))

    def read_contributors(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.contributor_rows(str(args["candidate"]))
        return ToolOutcome(Ok(_shown(data), reads=_parts(world, data["parts"])))

    def read_people(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        ids = sorted(str(p) for p in args["person_ids"])
        data = world.people(ids)
        return ToolOutcome(Ok(_shown(data), reads=_parts(world, data["parts"])))

    def read_disclosures(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.disclosure_rows(str(args["candidate"]))
        reads = _parts(world, data["parts"])
        shown = _shown(data)
        if world.today is not None:  # derived from the dates; not part of the record
            shown["as_of"] = world.today.isoformat()
            shown["rows"] = [
                {**r, "elapsed_days": days_between(r["date"], world.today) if r["date"] else None}
                for r in data["rows"]
            ]
        return ToolOutcome(Ok(shown, reads=reads))

    def read_document(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        path = str(args["path"])
        if ".." in path.split("/") or path not in world.records():
            return ToolOutcome(Err("not_found", f"{path} is not among the inputs"))
        data = world.document(path)
        return ToolOutcome(Ok(data, reads=(_whole(world, path),)))

    def read_priorities(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.priorities()
        reads = () if data["file"] is None else (_whole(world, data["file"]),)
        return ToolOutcome(Ok(data, reads=reads))

    def list_templates(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.templates()
        read = InputRef("query", query_id("list_templates", args),
                        query_version(data["templates"]))  # fmt: skip
        return ToolOutcome(Ok(data, reads=(read,)))

    def read_template(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.template(str(args["template"]))
        return ToolOutcome(Ok(data, reads=(_whole(world, data["file"]),)))

    def search_publications(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        groups = [[str(t) for t in g] for g in args["all_of"]]
        hits = world.search(groups)
        qid = query_id("search_publications", {"all_of": groups})
        data = {"query": qid, "hits": hits}
        read = InputRef("query", qid, query_version([h["id"] for h in hits]))
        return ToolOutcome(Ok(data, reads=(read,)))

    def fetch_publication(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        data = world.publication(str(args["id"]))
        return ToolOutcome(Ok(data, reads=(_whole(world, data["file"]),)))

    def fill_form(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        world = bound.over(ctx.reader, ctx.run_date)
        name, candidate = str(args["template"]), str(args["candidate"])
        listed = {t["template"]: t["form"] for t in world.templates()["templates"]}
        kind = listed.get(name)
        if kind not in FORM_KINDS:
            return ToolOutcome(Err("unknown_template", f"{name!r} is no disclosure form or "
                                   f"filing worksheet among the inputs"))  # fmt: skip
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
            "template_version": read.version,
            "candidate": candidate,
            "fields": fields,
            "counts": counts,
        }
        save = SaveRequest(kind, candidate, content)
        return ToolOutcome(Ok({"saved": kind, "counts": counts}, reads=(read,)), saves=(save,))

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
                "List the invention candidates (one per invention note among the inputs).",
                _obj({}),
                collections=rec,
                recorded_query=True,
                result_key="/candidate",
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
            ),
            read_priorities,
        ),
        (
            ToolSpec(
                "list_templates",
                AGENT,
                "read",
                "List the blank form templates among the inputs, each with the form it is "
                "(disclosure or worksheet).",
                _obj({}),
                collections=rec,
                recorded_query=True,
                result_key="/template",
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
                writes_kinds=frozenset(f"patent.{k}" for k in FORM_KINDS),
            ),
            fill_form,
        ),
        (
            ToolSpec(
                "send_packet",
                AGENT,
                "external",
                "Send one counsel-review packet version to "
                "a configured recipient. Needs a person's approval of this exact action.",
                _obj(
                    {"packet": STRING, "recipient": {"enum": list(RECIPIENTS)}},
                    ("packet", "recipient"),
                ),
                external=EffectSpec("counsel_mail", ("/packet", "/recipient"), "reconcile"),
                approval=ApprovalRule(facts=_facts, targets=_targets),
            ),
            send_packet,
        ),
    ]
    return tuple(ToolDecl(spec, _guard(body)) for spec, body in specs)
