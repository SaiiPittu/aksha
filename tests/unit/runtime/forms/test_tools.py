"""The harness's form tools, for an agent that is not the patent agent (rule of three).

A small "checklist" agent declares its blank forms as data (``FormSpec``): its ``intake``
kind, the extraction fields that list a form's fields, and a reviewer who alone completes
some of them. Each guarantee has a test that fails when the code enforcing it goes:
``read_form`` shows the fields ``fill_form`` checks and refuses what is no blank form; a
fill that leaves out a field, adds one, gives an unknown status, a complete field with no
value, a complete reviewer field or a complete field with an open question is refused and
saves nothing; whether a field is required is the form's, and the counts are the code's.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from aksha.runtime.api import FORM_TOOLS, FormSpec, form_counts, form_tools
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.inputs.tools import InputReading
from aksha.runtime.models.documents import DocumentKind
from aksha.runtime.models.fixed import distribution_ppm, ppm
from aksha.runtime.models.offline import OfflineReaders
from aksha.runtime.models.rules import RuleClassificationModel, RuleExtractionModel
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    ModelAlias,
    SpanRef,
)
from aksha.runtime.tools.types import Err, Ok, ToolContext

INTAKE = (
    "---\nform_id: site-intake\n---\n# Site intake checklist\n\n"
    "- [ ] Site name (required)\n- [ ] Floor plan\n- [ ] Sign-off (reviewer completes)\n"
)
UNNAMED = "# Visit form\n\n- [ ] Visitor (required)\n"
EMPTY = "# Blank intake\n\nNothing to fill yet.\n"
NOTE = "Notes from the visit: the floor plan is out of date.\n"


class Source:
    """An in-memory versioned source: record ID to text, versioned by the text itself."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.texts = dict(texts)

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        text = self.texts.get(record_id)
        return None if text is None else ReturnedText(source, record_id, f"v{len(text)}", text)

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        return tuple(sorted(self.texts))

    def serves(self, source: str) -> bool:
        return True


class Kind(RuleClassificationModel):
    """``intake`` for a record with a heading, ``other`` for anything else; it abstains (an
    even split) on a record whose ref says ``undecided``, as a live fast classifier does."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        q = request.questions[0]
        pick = "intake" if "headings:" in request.state else "other"
        if "undecided" in request.state:
            even = {o: Decimal(1) / len(q.options) for o in q.options}
            return ClassificationResult(
                answers=((q.name, ClassAnswer(pick, distribution_ppm(even))),)
            )
        rest = (Decimal(1) - Decimal("0.95")) / (len(q.options) - 1)
        probs = {o: Decimal("0.95") if o == pick else rest for o in q.options}
        return ClassificationResult(answers=((q.name, ClassAnswer(pick, distribution_ppm(probs))),))


class Boxes(RuleExtractionModel):
    """``boxes``: each ``- [ ] <name>`` line a field, marked required or reviewer-only."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        found: dict[str, list[SpanRef]] = {"items": [], "must": [], "reviewer": [], "name": []}
        for s in request.state:
            if s.text.startswith("# "):
                found["name"].append(SpanRef(s.span_id, 2, len(s.text)))
            if not s.text.startswith("- [ ] "):
                continue
            body = s.text[6:]
            name = body.split(" (", 1)[0]
            ref = SpanRef(s.span_id, 6, 6 + len(name))
            found["items"].append(ref)
            if "(required)" in body:
                found["must"].append(ref)
            if "(reviewer completes)" in body:
                found["reviewer"].append(ref)
        answers = []
        for field, refs in found.items():
            if not refs:
                continue
            value = "x" if field == "name" else tuple("x" for _ in refs)
            answer = FieldAnswer(value, tuple(refs), ppm(Decimal("0.01")), ppm(Decimal("0.95")))
            answers.append((field, answer))
        return ExtractionResult(answers=tuple(answers))


KINDS = (DocumentKind("intake", "a blank checklist to fill", "boxes"),)
ALIASES = (
    ModelAlias("kind", "classification"),
    ModelAlias(
        "boxes",
        "extraction",
        fields=(
            FieldSpec("items", "list"),
            FieldSpec("must", "list"),
            FieldSpec("reviewer", "list"),
            FieldSpec("name", "text"),
        ),
    ),
)
READING = InputReading("inputs", "input", decide="kind", kinds=KINDS)
SPEC = FormSpec(
    "intake",
    saves="checklist",
    fields="items",
    required="must",
    confirm="reviewer",
    title="name",
    name_key="form_id",
    confirm_status="needs_review",
    confirmer="the site reviewer",
    question="review_question",
    key_field="site",
)
FORM = "input/forms/intake.md"


@pytest.fixture
def source() -> Source:
    return Source(
        {
            FORM: INTAKE,
            "input/forms/Visit Form.md": UNNAMED,
            "input/forms/empty.md": EMPTY,
            "input/notes.txt": NOTE,
        }
    )


def _tools() -> dict[str, Any]:
    return {t.spec.name: t for t in form_tools("checklist", READING, SPEC)}


def _ctx(reader: Any, key: str | None = "S1") -> ToolContext:
    models = OfflineReaders({"kind": Kind(), "boxes": Boxes()}, ALIASES)
    return ToolContext("o", "checklist", "fill", key, "s", 1, reader=reader, models=models)


def _call(name: str, reader: Any, args: dict[str, Any], key: str | None = "S1") -> Any:
    return _tools()[name].body(_ctx(reader, key), args)


def _fields(**over: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"field": "Site name", "value": "North hall", "status": "complete"},
        {"field": "Floor plan", "value": None, "status": "missing"},
        {"field": "Sign-off", "value": None, "status": "needs_review"},
    ]
    for f in out:
        f.update(over.get(str(f["field"]), {}))
    return out


def test_the_form_tools_are_declared_for_any_agent() -> None:
    tools = _tools()
    assert tuple(tools) == FORM_TOOLS == ("read_form", "fill_form")
    read, fill = tools["read_form"].spec, tools["fill_form"].spec
    assert (read.owner, read.effect, read.mints_citation_spans) == ("checklist", "read", True)
    assert (fill.effect, fill.writes_kinds) == ("store", frozenset({"checklist.checklist"}))
    assert read.collections == fill.collections == frozenset({"input"})
    assert read.model_aliases == fill.model_aliases == frozenset({"kind", "boxes"})
    assert "the site reviewer" in fill.description and "needs_review" in fill.description


def test_read_form_shows_the_fields_fill_form_checks(source: Source) -> None:
    out = _call("read_form", source, {"template": FORM})
    assert isinstance(out.result, Ok)
    data = out.result.data
    assert (data["template"], data["form"], data["ref"]) == (
        "site-intake",  # the form's own name, from its front matter
        "Site intake checklist",
        FORM,
    )
    assert [(f["n"], f["field"], f["required"], f["default_status"]) for f in data["fields"]] == [
        (1, "Site name", True, None),
        (2, "Floor plan", False, None),
        (3, "Sign-off", False, "needs_review"),
    ]
    assert data["statuses"] == ["complete", "missing", "needs_review"]
    assert out.result.spans
    assert [str(r) for r in out.result.reads] == [f"record:{FORM}@v{len(INTAKE)}"]
    named = _call("read_form", source, {"template": "forms/Visit Form.md"})
    assert isinstance(named.result, Ok) and named.result.data["template"] == "visit-form"


def test_read_form_refuses_what_is_no_blank_form(source: Source) -> None:
    other = _call("read_form", source, {"template": "input/notes.txt"}).result
    assert isinstance(other, Err) and other.code == "not_a_form"
    assert "['intake']" in other.message
    empty = _call("read_form", source, {"template": "input/forms/empty.md"}).result
    assert isinstance(empty, Err) and empty.code == "no_fields"
    absent = _call("read_form", source, {"template": "input/forms/none.md"}).result
    assert isinstance(absent, Err) and absent.code == "not_found"
    bare = form_tools("checklist", InputReading("inputs", "input"), SPEC)[0]
    assert bare.body is not None
    refused = bare.body(_ctx(source), {"template": FORM}).result
    assert isinstance(refused, Err) and refused.code == "no_forms"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (_fields()[1:], "fields_mismatch"),
        ([*_fields(), {"field": "Extra", "value": 1, "status": "complete"}], "fields_mismatch"),
        (_fields(**{"Floor plan": {"status": "done"}}), "status_invalid"),
        (_fields(**{"Site name": {"value": ""}}), "complete_needs_value"),
        (_fields(**{"Sign-off": {"value": "yes", "status": "complete"}}), "confirm_only"),
        (_fields(**{"Site name": {"review_question": "Which hall?"}}), "confirm_only"),
    ],
)
def test_fill_form_refuses_a_fill_the_rules_forbid(
    source: Source, fields: list[dict[str, Any]], code: str
) -> None:
    out = _call("fill_form", source, {"template": FORM, "key": "S1", "fields": fields})
    assert isinstance(out.result, Err) and out.result.code == code
    assert out.saves == ()  # a refused fill saves nothing


def test_a_refusal_names_the_forms_fields_and_the_confirmer(source: Source) -> None:
    out = _call("fill_form", source, {"template": FORM, "key": "S1", "fields": _fields()[1:]})
    assert isinstance(out.result, Err)
    assert "['Site name', 'Floor plan', 'Sign-off']" in out.result.message
    done = _fields(**{"Sign-off": {"value": "yes", "status": "complete"}})
    out = _call("fill_form", source, {"template": FORM, "key": "S1", "fields": done})
    assert isinstance(out.result, Err) and "the site reviewer" in out.result.message


def test_fill_form_saves_required_from_the_form_and_computed_counts(source: Source) -> None:
    fields = list(reversed(_fields()))
    for f in fields:
        f["required"] = False  # the model claims nothing is required
    out = _call("fill_form", source, {"template": FORM, "key": "S1", "fields": fields})
    assert isinstance(out.result, Ok)
    (save,) = out.saves
    assert (save.kind, save.key) == ("checklist", "S1~site-intake")
    content = save.content
    assert [(f["field"], f["required"]) for f in content["fields"]] == [
        ("Site name", True),
        ("Floor plan", False),
        ("Sign-off", False),
    ]  # in the form's order, required as the form marks it
    assert content["counts"] == {"complete": 1, "missing": 1, "needs_review": 1}
    assert (content["site"], content["template"], content["file"]) == ("S1", "site-intake", FORM)
    assert content["template_version"] == f"v{len(INTAKE)}"
    assert out.result.data == {"saved": "checklist/S1~site-intake", "counts": content["counts"]}
    assert [str(r) for r in out.result.reads] == [f"record:{FORM}@v{len(INTAKE)}"]


def test_fill_form_fills_only_for_the_instance_key(source: Source) -> None:
    args = {"template": FORM, "key": "S2", "fields": _fields()}
    out = _call("fill_form", source, args)
    assert isinstance(out.result, Err) and out.result.code == "wrong_key"
    unkeyed = _call("fill_form", source, args, key=None)
    assert isinstance(unkeyed.result, Ok) and unkeyed.saves[0].key == "S2~site-intake"


def test_a_form_that_is_not_among_the_inputs_cannot_be_filled(source: Source) -> None:
    args = {"template": "input/notes.txt", "key": "S1", "fields": _fields()}
    out = _call("fill_form", source, args)
    assert isinstance(out.result, Err) and out.result.code == "not_a_form" and out.saves == ()


def test_counts_are_computed_from_the_fields() -> None:
    fields = [{"status": "complete"}, {"status": "missing"}, {"status": "missing"}, "x"]
    assert form_counts(fields, SPEC.statuses) == {"complete": 1, "missing": 2, "needs_review": 0}
    assert form_counts(None, SPEC.statuses) == {"complete": 0, "missing": 0, "needs_review": 0}


# --- a toy agent that lists the tools, loaded and saved through the runtime -------------


def _done(view: Any) -> Any:
    from aksha.runtime.api import CheckResult

    return CheckResult(True)


def _checklist_agent(*, with_kind: bool = True) -> Any:
    """A minimal ``checklist`` agent: one Task listing the shared form tools, nothing else."""
    from aksha.runtime.api import Agent, Task, VersionedSourceSpec, form_kind

    tools = form_tools("checklist", READING, SPEC)
    writes = ("checklist",) if with_kind else ()
    return Agent(
        name="checklist",
        purpose="Fill a site's intake checklist.",
        instructions="prompts/instructions.md",
        reads=("record:input",),
        artifacts=(form_kind(SPEC),) if with_kind else (),
        tools=tools,
        tasks=(Task("fill", extracts=True, tools=FORM_TOOLS, writes=writes, done=_done),),
        model_aliases=ALIASES,
        sources=(VersionedSourceSpec("inputs", collections=("input",)),),
    )


def test_a_toy_agent_that_lists_the_form_tools_loads_and_saves_through_the_store(
    source: Source, tmp_path: Any
) -> None:
    """Rule of three: a non-patent agent declares ``form_kind`` and the loader takes the tools;
    the fill it saves passes the kind's schema and its counts are the store's computation."""
    from aksha.runtime.artifacts.store import ArtifactStore
    from aksha.runtime.artifacts.types import SaveRequest, WriterId
    from aksha.runtime.canonical import thaw
    from aksha.runtime.definitions.validate import validate_agent
    from tests.support.artifacts import RUN, World

    agent = _checklist_agent()
    assert validate_agent(agent, module="checklist/agent.py", extensions=()) == []
    out = _call("fill_form", source, {"template": "site-intake", "key": "S1", "fields": _fields()})
    assert isinstance(out.result, Ok)
    (save,) = out.saves
    lying = SaveRequest(save.kind, save.key, {**save.content, "counts": {"complete": 3}})
    (kind,) = agent.artifacts
    world = World(tmp_path)
    try:
        store = ArtifactStore({"checklist.checklist": kind})
        saved = store.save(world.view(), lying, writer=WriterId("checklist", "fill", "S1"),
                           run=RUN, instance="fill", step="s",
                           writes={"checklist.checklist"}).version  # fmt: skip
        assert (saved.ref.kind, saved.ref.key) == ("checklist.checklist", "S1~site-intake")
        assert thaw(saved.content)["counts"] == {"complete": 1, "missing": 1, "needs_review": 1}
    finally:
        world.close()
    without = _checklist_agent(with_kind=False)
    codes = [r.code for r in validate_agent(without, module="checklist/agent.py", extensions=())]
    assert "write_kind_not_owned" in codes


def test_fill_form_takes_a_forms_name_and_points_errors_at_template(source: Source) -> None:
    """read_form shows ``template`` as the form's name; a model that copies it fills the form."""
    by_name = _call("fill_form", source, {"template": "site-intake", "key": "S1",
                                          "fields": _fields()})  # fmt: skip
    assert isinstance(by_name.result, Ok) and by_name.saves[0].content["file"] == FORM
    by_file = _call("read_form", source, {"template": "visit-form"})
    assert isinstance(by_file.result, Ok)
    assert by_file.result.data["ref"] == "input/forms/Visit Form.md"
    absent = _call("read_form", source, {"template": "no-such-form"}).result
    assert isinstance(absent, Err) and absent.code == "not_found"
    assert absent.paths == ("/template",) and "['intake']" in absent.message
    twice = Source({FORM: INTAKE, "input/forms/copy.md": INTAKE})
    both = _call("read_form", twice, {"template": "site-intake"}).result
    assert isinstance(both, Err) and both.code == "ambiguous_ref" and both.paths == ("/template",)


# --- a fast classifier is an optimisation, never a gatekeeper ---------------------------

UNDECIDED = "input/forms/undecided-disclosure.md"


def test_a_blank_form_the_classifier_abstains_on_is_read_and_filled_by_its_structure(
    source: Source,
) -> None:
    """The live failure: the fast kind decision abstained on a blank form, so it was listed as
    no form and every fill was refused. Its fields, extracted as for a decided form, make it
    one; the result says it was recognised by structure."""
    from aksha.runtime.inputs.tools import input_tools

    source.texts[UNDECIDED] = INTAKE
    listing = {t.spec.name: t for t in input_tools("checklist", READING)}["list_inputs"].body
    assert listing is not None
    listed = listing(_ctx(source), {"kinds": ["intake"]}).result
    assert isinstance(listed, Ok)
    rows = {r["ref"]: r for r in listed.data["inputs"]}
    assert (rows[UNDECIDED]["kind"], rows[UNDECIDED]["decided"]) == ("other", False)
    assert rows[UNDECIDED]["note"].startswith("kind undecided")  # listed, marked, not hidden
    assert "input/notes.txt" not in rows  # confidently another kind: still not listed
    assert "note" not in rows[FORM]
    read = _call("read_form", source, {"template": UNDECIDED}).result
    assert isinstance(read, Ok) and read.data["recognised_by"] == "structure"
    assert [f["field"] for f in read.data["fields"]] == ["Site name", "Floor plan", "Sign-off"]
    assert "recognised_by" not in _call("read_form", source, {"template": FORM}).result.data
    args = {"template": UNDECIDED, "key": "S1", "fields": _fields()}
    filled = _call("fill_form", source, args)
    assert isinstance(filled.result, Ok) and filled.result.data["recognised_by"] == "structure"
    (save,) = filled.saves
    assert save.content["file"] == UNDECIDED
    assert save.content["counts"] == {"complete": 1, "missing": 1, "needs_review": 1}
    refused = _call("fill_form", source, {**args, "fields": _fields()[1:]}).result
    assert isinstance(refused, Err) and refused.code == "fields_mismatch"  # rules still hold


def test_a_record_confidently_read_as_another_kind_is_still_refused(source: Source) -> None:
    """Structure decides only when the classifier abstains: a record it confidently read as
    another kind is refused with the same message, even when it has form fields."""
    source.texts["input/boxes.txt"] = "- [ ] Site name (required)\n"  # no heading: other
    for ref in ("input/boxes.txt", "input/notes.txt"):
        out = _call("fill_form", source, {"template": ref, "key": "S1", "fields": _fields()})
        assert isinstance(out.result, Err) and out.result.code == "not_a_form" and not out.saves
        assert out.result.message.startswith(f"{ref} is read as other, not a blank form")
    source.texts["input/undecided-notes.txt"] = NOTE  # undecided, and no form field in it
    out = _call("read_form", source, {"template": "input/undecided-notes.txt"}).result
    assert isinstance(out, Err) and out.code == "not_a_form" and "undecided" in out.message
