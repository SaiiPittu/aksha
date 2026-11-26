"""The patent agent's own tools: typed reads over the sources, the form tool and the send.

Every read returns a view and names it in ``Ok.reads`` as a record at the view's
version (``world.py``), so the lineage of what the agent saves reaches the source text.
The search is a recorded query: its read is the query ID at the hash of its hit set.

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
)

from . import rules
from .world import (
    NotInWorld,
    SourceWorld,
    days_between,
    query_id,
    query_version,
    view_version,
)

AGENT = "patent"
RECIPIENTS = ("counsel-primary",)
"""The configured recipients: the only addresses ``send_packet`` takes."""

FORMS = {
    "invention-disclosure-form": "disclosure",
    "filing-information-worksheet": "worksheet",
}
"""Each blank template and the artifact kind its filled form is saved as."""


def _obj(props: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(props),
        "required": list(required),
        "additionalProperties": False,
    }


CANDIDATE = _obj({"candidate": {"type": "string", "pattern": "^C[0-9]+$"}}, ("candidate",))
STRING = {"type": "string", "minLength": 1}


def _record(record_id: str, data: Any) -> InputRef:
    return InputRef("record", record_id, view_version(data))


def _guard(body: Callable[[ToolContext, Mapping[str, Any]], ToolOutcome]) -> Any:
    def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        try:
            return body(ctx, args)
        except NotInWorld as e:
            return ToolOutcome(Err("not_found", str(e)))

    run.__name__ = body.__name__
    run.__doc__ = body.__doc__
    return run


def build_tools(world: SourceWorld) -> tuple[ToolDecl, ...]:
    """The agent's own tools, reading ``world``."""

    def list_candidates(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        data = world.candidates()
        return ToolOutcome(Ok(data, reads=(_record("notes/index#portfolio", data),)))

    def read_note(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        data = world.note(str(args["candidate"]))
        return ToolOutcome(Ok(data, reads=(_record(f"{data['file']}#note", data),)))

    def read_contributors(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        c = str(args["candidate"])
        data = world.contributor_rows(c)
        return ToolOutcome(Ok(data, reads=(_record(f"{data['file']}#rows:{c}", data),)))

    def read_people(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        ids = sorted(str(p) for p in args["person_ids"])
        data = world.people(ids)
        rid = f"{data['file']}#people:{','.join(ids)}"
        return ToolOutcome(Ok(data, reads=(_record(rid, data),)))

    def read_disclosures(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        c = str(args["candidate"])
        data = world.disclosure_rows(c)
        read = _record(f"{data['file']}#rows:{c}", data)
        shown = dict(data)
        if world.today is not None:  # derived from the dates; not part of the record
            shown["as_of"] = world.today.isoformat()
            shown["rows"] = [
                {**r, "elapsed_days": days_between(r["date"], world.today) if r["date"] else None}
                for r in data["rows"]
            ]
        return ToolOutcome(Ok(shown, reads=(read,)))

    def read_document(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        path = str(args["path"])
        if not path.startswith("disclosures/") or ".." in path.split("/"):
            return ToolOutcome(Err("not_allowed", "read_document reads disclosure records only"))
        data = world.document(path)
        return ToolOutcome(Ok(data, reads=(_record(f"{path}#doc", data),)))

    def read_priorities(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        data = world.priorities()
        return ToolOutcome(Ok(data, reads=(_record(f"{data['file']}#items", data),)))

    def read_template(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        name = str(args["template"])
        data = world.template(name)
        return ToolOutcome(Ok(data, reads=(_record(f"{data['file']}#template:{name}", data),)))

    def search_publications(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        groups = [[str(t) for t in g] for g in args["all_of"]]
        hits = world.search(groups)
        qid = query_id(groups)
        data = {"query": qid, "hits": hits}
        return ToolOutcome(Ok(data, reads=(InputRef("query", qid, query_version(hits)),)))

    def fetch_publication(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        data = world.publication(str(args["id"]))
        return ToolOutcome(Ok(data, reads=(_record(f"{data['file']}#publication", data),)))

    def fill_form(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        name, candidate = str(args["template"]), str(args["candidate"])
        kind = FORMS.get(name)
        if kind is None:
            return ToolOutcome(Err("unknown_template", f"no blank template {name!r}"))
        if ctx.key is not None and ctx.key != candidate:
            return ToolOutcome(Err("wrong_candidate", f"this instance prepares {ctx.key}"))
        template = world.template(name)
        read = _record(f"{template['file']}#template:{name}", template)
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

    rec = frozenset({"notes"})
    specs: list[tuple[ToolSpec, Any]] = [
        (
            ToolSpec(
                "list_candidates",
                AGENT,
                "read",
                "List the invention candidates (one per engineering note).",
                _obj({}),
                collections=rec,
            ),
            list_candidates,
        ),
        (
            ToolSpec(
                "read_note",
                AGENT,
                "read",
                "Read one candidate's engineering note: dated "
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
                collections=frozenset({"contributors"}),
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
                collections=frozenset({"contributors"}),
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
                collections=frozenset({"disclosures"}),
            ),
            read_disclosures,
        ),
        (
            ToolSpec(
                "read_document",
                AGENT,
                "read",
                "Read a disclosure record linked from the history (an email or an event record).",
                _obj({"path": STRING}, ("path",)),
                collections=frozenset({"disclosures"}),
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
                collections=frozenset({"priorities"}),
            ),
            read_priorities,
        ),
        (
            ToolSpec(
                "read_template",
                AGENT,
                "read",
                "Read a blank form template's fields.",
                _obj({"template": {"enum": sorted(FORMS)}}, ("template",)),
                collections=frozenset({"templates"}),
            ),
            read_template,
        ),
        (
            ToolSpec(
                "search_publications",
                AGENT,
                "read",
                "Search the publication fixtures: a "
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
                collections=frozenset({"publications"}),
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
                _obj({"id": {"type": "string", "pattern": "^SYN-PUB-[0-9]{4}$"}}, ("id",)),
                collections=frozenset({"publications"}),
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
                        "template": {"enum": sorted(FORMS)},
                        "candidate": STRING,
                        "fields": {"type": "array", "items": {"type": "object"}},
                    },
                    ("template", "candidate", "fields"),
                ),
                collections=frozenset({"templates"}),
                writes_kinds=frozenset({"patent.disclosure", "patent.worksheet"}),
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
