"""The harness's form tools: read a blank form's fields and fill a form, checked in code.

ADR-P10 amendment F1. A blank form (an intake form, a worksheet, a questionnaire) is one of
the inputs the person gave. Which document kind is a blank form, and which fields of that
kind's extraction schema list the form's fields, the agent declares as data
(:class:`FormSpec`); everything else is the same for every agent that lists the tools with
:func:`form_tools`:

``read_form(template)``
    One blank form by its input ref, or by its name as this result shows it: its name, its
    title and its fields in order, each with whether the form marks it required, whether
    only the confirmer completes it, and the guidance its row gives. These are exactly the
    fields ``fill_form`` checks. The form's kind is decided and its fields extracted as
    ``read_document(extract=true)`` does it, over the form's passages as whole lines (a
    field is marked by its line: a list mark, a box, a row's cells). Mints a span over
    every passage.

``fill_form(template, key, fields)``
    Saves one filled form for ``key`` (the item it is for: the Task's key when the Task has
    one), as the agent's form kind keyed ``<key>~<form name>``. Each field is
    ``complete``, ``missing`` or the confirm status the agent names. Code refuses a fill
    (:func:`form_refusal`) that leaves out a field of the form or adds one it lacks, gives
    a status outside the three, marks a field complete with no value, marks complete a
    field only the confirmer completes, or marks complete a field with an open question.
    Whether a field is required is the form's, never the model's; the counts are computed.

A form whose kind the fast classifier confidently decided is another, or whose extraction
names no field, is refused, so a model cannot fill a form that is not among the inputs. The
classifier is an optimisation, never a gatekeeper: when it abstains on a record (its kind is
undecided), the record is recognised by its structure instead. The section decisions come
first: they are the row ``list_inputs`` decides (the same requests, so memo hits in a run),
and an undecided record with a section confidently decided as another declared kind (an
engineering note whose Problem section is an invention disclosure, say) is refused
``not_a_form``, in step with the ``list_inputs`` kind filter. Otherwise the form kind's
extraction runs over it as it would over a decided form, and the record is a blank form when
that yields at least one form field. Such a form shows ``recognised_by: "structure"`` in the
``read_form`` and ``fill_form`` results; an undecided record with no form field is refused
``not_a_form``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from aksha.runtime.artifacts.types import ArtifactKind, InputRef, SaveRequest
from aksha.runtime.grounding.segment import Segment, sniff_format
from aksha.runtime.grounding.types import ReturnedText, SpanDraft
from aksha.runtime.inputs.tools import InputReading, decide_row, resolve, segments_of
from aksha.runtime.models.documents import OTHER_KIND, accepted_ranges
from aksha.runtime.tools.types import Err, Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec

READ_FORM = "read_form"
FILL_FORM = "fill_form"
FORM_TOOLS = (READ_FORM, FILL_FORM)
"""The names :func:`form_tools` declares, in order."""
COMPLETE = "complete"
MISSING = "missing"


@dataclass(frozen=True)
class FormSpec:
    """How an agent's blank forms are read and its filled forms saved (declared data).

    ``template_kind`` is the agent's document kind of a blank form (one of its
    ``InputReading.kinds``, with an extraction alias). ``fields``, ``required`` and
    ``confirm`` name the list fields of that kind's extraction that hold, in order, every
    field the form asks for, the ones it marks required, and the ones only the confirmer
    completes (None: the form has none); ``title`` its title. ``name_key`` is a header
    field (front matter, a meta tag) a form names itself by; without one, a form is named
    by its file name. ``confirm_status`` is the status of a field the confirmer still has
    to settle and ``confirmer`` who that is, as the messages name them; ``question`` is the
    field key of an open question to the confirmer. A filled form is saved as the agent's
    kind ``saves``, with the item it is for under ``key_field``.
    """

    template_kind: str
    saves: str = "form"
    fields: str = "fields"
    required: str = "required"
    confirm: str | None = None
    title: str = "title"
    name_key: str | None = None
    confirm_status: str = "needs_confirmation"
    confirmer: str = "the reviewer"
    question: str = "question"
    key_field: str = "key"

    @property
    def statuses(self) -> tuple[str, str, str]:
        """The three field statuses: complete, missing and the confirm status."""
        return (COMPLETE, MISSING, self.confirm_status)


@dataclass(frozen=True)
class FormField:
    """One field of a blank form, as ``fill_form`` checks it."""

    n: int
    field: str
    required: bool
    confirm: bool
    guidance: str | None


@dataclass(frozen=True)
class BlankForm:
    """A blank form among the inputs: its record, name, title and fields in order."""

    ref: str
    version: str
    name: str
    title: str
    fields: tuple[FormField, ...]
    drafts: tuple[SpanDraft, ...] = ()
    recognised_by: str = "kind"
    """``kind``: the fast classifier decided the form's kind; ``structure``: it abstained,
    and the form kind's extraction found at least one form field in it."""

    def as_json(self, spec: FormSpec) -> dict[str, Any]:
        """The form as ``read_form`` shows it."""
        shown = {
            "template": self.name,
            "form": self.title,
            "ref": self.ref,
            "version": self.version,
            "fields": [
                {
                    "n": f.n,
                    "field": f.field,
                    "required": f.required,
                    "default_status": spec.confirm_status if f.confirm else None,
                    "guidance": f.guidance,
                }
                for f in self.fields
            ],
            "statuses": list(spec.statuses),
        }
        if self.recognised_by != "kind":
            shown["recognised_by"] = self.recognised_by
        return shown


# --- reading a blank form ----------------------------------------------------------------


def _line_of(raw: str, start: int, end: int) -> tuple[int, int]:
    s = raw.rfind("\n", 0, start) + 1
    e = raw.find("\n", end)
    return s, len(raw) if e < 0 else e


def line_passages(found: ReturnedText, fmt: str) -> tuple[list[Segment], list[SpanDraft]]:
    """The record's segments, and one draft per segment widened to its whole lines.

    A text's heading or list item is shown with its marks (``## Fields``, ``- [ ] ...``), as
    a reader sees the form; an HTML block or a JSON value is shown as it is.
    """
    raw = found.text
    segs = [Segment("data", 0, len(raw), raw)] if fmt == "json" else segments_of(raw, fmt)
    out = []
    for s in segs:
        a, b = (s.start, s.end) if fmt in ("html", "json") else _line_of(raw, s.start, s.end)
        if fmt == "xml" and s.attrs.get("key"):  # an element's text, shown with its tag
            tag = raw.rfind("<" + s.attrs["key"], max(0, a - 200), s.start)
            a = _line_of(raw, tag, tag)[0] if tag >= 0 else a
        if s.kind == "paragraph" and fmt not in ("html", "json"):
            a, b = _line_of(raw, s.start, s.start)[0], _line_of(raw, s.end - 1, s.end)[1]
        out.append(SpanDraft(found.source, found.record_id, found.version, a, b))
    return segs, out


_EXTS = re.compile(r"(\.(pdf|eml|docx|xlsx|pptx|html?|md|markdown|txt|json|csv|tsv|xml))+$", re.I)


def form_name(record: str) -> str:
    """A form's name from its file name: ``templates/Intake Form.md`` is ``intake-form``."""
    stem = _EXTS.sub("", record.rsplit("/", 1)[-1])
    return re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()


def _stem(record: str) -> str:
    return _EXTS.sub("", record.rsplit("/", 1)[-1])


def _named(ctx: ToolContext, reading: InputReading, spec: FormSpec, record: str) -> str | None:
    """The name ``record`` gives itself in its ``spec.name_key`` header field, if any."""
    if spec.name_key is None or ctx.reader is None:
        return None
    found = ctx.reader.read(reading.source, record)
    if found is None:
        return None
    fmt = sniff_format(record, found.text)
    for g in segments_of(found.text, fmt):
        if g.kind == "meta" and g.attrs.get("key") == spec.name_key:
            return str(g.attrs.get("value") or "").strip() or None
    return None


def resolve_form(ctx: ToolContext, reading: InputReading, spec: FormSpec, ref: str) -> str | Err:
    """The record of the blank form ``ref`` names: by its input ref, else by its form name.

    An error names ``/template``, the argument of the form tools.
    """
    record = resolve(ctx, reading, ref)
    if isinstance(record, Err) and record.code == "not_found":
        name = ref.strip()
        records = (
            sorted(ctx.reader.records(reading.source, reading.collection)) if ctx.reader else ()
        )
        hits = [r for r in records if form_name(r) == name or _named(ctx, reading, spec, r) == name]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return Err(
                "ambiguous_ref",
                f"{name!r} names {len(hits)} blank forms: {hits[:5]}; give the full ref "
                "list_inputs shows",
                paths=("/template",),
            )
    if isinstance(record, Err):
        message = record.message
        if record.code == "not_found":
            message = (
                f"{ref.strip()!r} is neither an input nor a blank form's name; list_inputs "
                f"with kinds [{spec.template_kind!r}] lists the blank forms"
            )
        return replace(record, message=message, paths=("/template",))
    return record


def read_blank_form(
    ctx: ToolContext, reading: InputReading, spec: FormSpec, ref: str
) -> BlankForm | Err:
    """The blank form ``ref`` names, read as the module doc says, or the ``Err`` to return.

    ``ref`` is the form's input ref, or its name as ``read_form`` shows it (the plan names
    forms that way too).
    """
    record = resolve_form(ctx, reading, spec, ref)
    if isinstance(record, Err):
        return record
    found = ctx.reader.read(reading.source, record) if ctx.reader is not None else None
    if found is None:
        return Err("fetch_failed", f"{record} could not be read", item=record)
    kind = next((k for k in reading.kinds if k.name == spec.template_kind), None)
    if reading.decide is None or kind is None or kind.extract is None or ctx.models is None:
        return Err("no_forms", "this agent declares no blank form kind to read", item=record)
    fmt = sniff_format(record, found.text)
    # The row list_inputs decides (the same requests, so memo hits within a run).
    row = decide_row(ctx, reading, found, fmt, segments_of(found.text, fmt))
    kind_name, settled = row["kind"], row["decided"]
    refusal = Err(
        "not_a_form",
        f"{record} is read as {kind_name}, not a blank form: list_inputs with kinds "
        f"[{spec.template_kind!r}] lists the blank forms",
        paths=("/template",),
        item=record,
    )
    if settled and kind_name != spec.template_kind:
        return refusal  # the classifier confidently read another kind
    by_structure = not settled  # it abstained: the sections, then the fields, decide
    if by_structure:
        others = sorted(
            {
                sec["kind"]
                for sec in row.get("sections", ())
                if sec["decided"] and sec["kind"] not in (spec.template_kind, OTHER_KIND)
            }
        )
        if others:
            return replace(
                refusal,
                message=f"{record}'s kind is undecided but its sections are read as {others}, "
                f"so it is not a blank form: list_inputs with kinds [{spec.template_kind!r}] "
                "lists the blank forms",
            )
    segs, drafts = line_passages(found, fmt)
    judged = ctx.models.extract(kind.extract, found, drafts) if drafts else ()
    _, ranges, _ = accepted_ranges(judged, drafts)
    raw = found.text

    def names(field: str | None) -> list[tuple[str, int]]:
        spans = ranges.get(field, ()) if field else ()
        return [(raw[s:e].strip(), s) for s, e in spans]

    required = {n for n, _ in names(spec.required)}
    confirm = {n for n, _ in names(spec.confirm)}
    fields: list[FormField] = []
    for i, (name, s) in enumerate(names(spec.fields), start=1):
        seg = next((g for g in segs if g.start <= s <= g.end), None)
        guidance = None
        if seg is not None and seg.kind == "row" and len(seg.cells) > 1:
            guidance = seg.cells[-1][0] if seg.cells[-1][0] != name else None
        fields.append(FormField(i, name, name in required, name in confirm, guidance))
    if not fields and by_structure:
        return replace(
            refusal,
            message=f"{record}'s kind is undecided and it holds no form field, so it is not "
            f"a blank form: list_inputs with kinds [{spec.template_kind!r}] lists the blank "
            "forms",
        )
    if not fields:
        return Err(
            "no_fields", f"{record} holds no field to fill", paths=("/template",), item=record
        )
    meta = {g.attrs.get("key"): g.attrs.get("value") for g in segs if g.kind == "meta"}
    named = meta.get(spec.name_key) if spec.name_key else None
    title_spans = ranges.get(spec.title, ())
    title = raw[title_spans[0][0] : title_spans[0][1]].strip() if title_spans else ""
    return BlankForm(
        record,
        found.version,
        str(named or form_name(record)),
        title or _stem(record),
        tuple(fields),
        tuple(drafts),
        "structure" if by_structure else "kind",
    )


# --- the field-status rules --------------------------------------------------------------


def _empty(value: Any) -> bool:
    return value is None or value in ("", [], {})


def form_refusal(
    spec: FormSpec, form: BlankForm, fields: Sequence[Mapping[str, Any]]
) -> tuple[str, str] | None:
    """Why ``fill_form`` refuses ``fields`` for ``form``, or None: ``(code, message)``."""
    declared = {f.field: f for f in form.fields}
    given = [str(f.get("field")) for f in fields]
    if sorted(given) != sorted(declared):
        missing = sorted(set(declared) - set(given))
        extra = sorted(set(given) - set(declared))
        return "fields_mismatch", (
            f"missing {missing}, not in the form {extra}; the form's fields are "
            f"{[f.field for f in form.fields]}"
        )
    for f in fields:
        name, status = str(f["field"]), f.get("status")
        if status not in spec.statuses:
            return "status_invalid", f"{name}: status is one of {list(spec.statuses)}"
        if status == COMPLETE and _empty(f.get("value")):
            return "complete_needs_value", f"{name}: a complete field holds a value"
        if status == COMPLETE and declared[name].confirm:
            return "confirm_only", f"{name}: only {spec.confirmer} completes this field"
        if status == COMPLETE and f.get(spec.question):
            return (
                "confirm_only",
                f"{name}: a field with an open question for {spec.confirmer} is not complete",
            )
    return None


def form_counts(fields: Any, statuses: Sequence[str]) -> dict[str, int]:
    """How many of a form's ``fields`` are in each status: computed, never the model's."""
    listed = fields if isinstance(fields, list | tuple) else ()
    return {
        s: sum(1 for f in listed if isinstance(f, Mapping) and f.get("status") == s)
        for s in statuses
    }


def form_key(key: str, name: str) -> str:
    """The saved key of ``key``'s filled form ``name``."""
    return f"{key}~{name}"


def form_kind(spec: FormSpec) -> ArtifactKind:
    """The kind ``fill_form`` saves into, for an agent to declare as its own (``spec.saves``).

    Keyed ``<key>~<form name>``; it requires the form's name, the item it is for, its
    fields and their counts, and every save (a correction of one field too) recomputes the
    counts from the fields with the spec's statuses, so a count is never the model's.
    """
    statuses = spec.statuses

    def computed_form(content: dict[str, Any]) -> dict[str, Any]:
        if isinstance(content.get("fields"), list):
            content["counts"] = form_counts(content["fields"], statuses)
        return content

    schema = {"type": "object", "required": ["template", spec.key_field, "fields", "counts"]}
    return ArtifactKind(spec.saves, schema, keyed=True, compute=computed_form)


# --- the tools ---------------------------------------------------------------------------


def read_form(
    ctx: ToolContext, args: Mapping[str, Any], reading: InputReading, spec: FormSpec
) -> ToolOutcome:
    """``read_form``'s body (module doc)."""
    form = read_blank_form(ctx, reading, spec, str(args["template"]))
    if isinstance(form, Err):
        return ToolOutcome(form)
    read = InputRef("record", form.ref, form.version)
    return ToolOutcome(Ok(form.as_json(spec), spans=form.drafts, reads=(read,)))


def fill_form(
    ctx: ToolContext, args: Mapping[str, Any], reading: InputReading, spec: FormSpec
) -> ToolOutcome:
    """``fill_form``'s body (module doc)."""
    key = str(args["key"])
    if ctx.key is not None and ctx.key != key:
        return ToolOutcome(Err("wrong_key", f"this instance fills forms for {ctx.key}"))
    form = read_blank_form(ctx, reading, spec, str(args["template"]))
    if isinstance(form, Err):
        return ToolOutcome(form)
    fields = [dict(f) for f in args["fields"]]
    refused = form_refusal(spec, form, fields)
    if refused is not None:
        return ToolOutcome(Err(refused[0], refused[1], paths=("/fields",)))
    declared = {f.field: f for f in form.fields}
    for f in fields:  # whether a field is required is the form's, never the model's
        f["required"] = declared[str(f["field"])].required
    fields.sort(key=lambda f: declared[str(f["field"])].n)
    counts = form_counts(fields, spec.statuses)
    content = {
        "template": form.name,
        "form": form.title,
        "file": form.ref,
        "template_version": form.version,
        spec.key_field: key,
        "fields": fields,
        "counts": counts,
    }
    saved = form_key(key, form.name)
    read = InputRef("record", form.ref, form.version)
    data: dict[str, Any] = {"saved": f"{spec.saves}/{saved}", "counts": counts}
    if form.recognised_by != "kind":
        data["recognised_by"] = form.recognised_by
    return ToolOutcome(Ok(data, reads=(read,)), saves=(SaveRequest(spec.saves, saved, content),))


_REF = {"type": "string", "minLength": 1, "maxLength": 512}


def form_tools(agent: str, reading: InputReading, spec: FormSpec) -> tuple[ToolDecl, ...]:
    """``read_form`` and ``fill_form`` for ``agent`` over its inputs (module doc)."""
    collections = frozenset({reading.collection})
    aliases = reading.aliases()
    statuses = ", ".join(spec.statuses)

    def reader(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return read_form(ctx, args, reading, spec)

    def filler(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return fill_form(ctx, args, reading, spec)

    reader.__name__, filler.__name__ = FORM_TOOLS
    return (
        ToolDecl(
            ToolSpec(
                READ_FORM,
                agent,
                "read",
                "Read one blank form by its input ref or its name (list_inputs with kinds "
                f"['{spec.template_kind}'] lists them): its name, title and fields in order, "
                f"each with whether it is required and whether only {spec.confirmer} "
                "completes it. These are exactly the fields fill_form checks.",
                {
                    "type": "object",
                    "properties": {"template": _REF},
                    "required": ["template"],
                    "additionalProperties": False,
                },
                collections=collections,
                mints_citation_spans=True,
                retry=reading.retry,
                model_aliases=aliases,
            ),
            reader,
        ),
        ToolDecl(
            ToolSpec(
                FILL_FORM,
                agent,
                "store",
                "Fill one blank form (by its input ref or its name) for key, the item it is "
                "for. Give every field of the form once, each {field, value, status} with "
                f"status one of {statuses}; a field only {spec.confirmer} completes, or one with "
                f"an open '{spec.question}', cannot be complete, and a complete field holds "
                "a value.",
                {
                    "type": "object",
                    "properties": {
                        "template": _REF,
                        "key": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                        "fields": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["template", "key", "fields"],
                    "additionalProperties": False,
                },
                collections=collections,
                writes_kinds=frozenset({f"{agent}.{spec.saves}"}),
                retry=reading.retry,
                model_aliases=aliases,
            ),
            filler,
        ),
    )


__all__ = [
    "FORM_TOOLS",
    "BlankForm",
    "FormField",
    "FormSpec",
    "form_counts",
    "form_kind",
    "form_name",
    "form_refusal",
    "form_tools",
    "read_blank_form",
    "resolve_form",
]
