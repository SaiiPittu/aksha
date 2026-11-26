from jsonschema import Draft202012Validator

from aksha.runtime.tools.bodies import call_body
from aksha.runtime.tools.loader_added import LOADER_ADDED_DECLS, LOADER_ADDED_TOOLS, RUNTIME_OWNER
from aksha.runtime.tools.types import Ok, ToolContext

CTX = ToolContext(owner="o", agent="a", task_path="t", key=None, step_id="s", attempt=1)


def test_every_reserved_tool_has_a_runtime_owned_spec() -> None:
    assert set(LOADER_ADDED_DECLS) == LOADER_ADDED_TOOLS
    for name, decl in LOADER_ADDED_DECLS.items():
        assert decl.spec.name == name and decl.spec.owner == RUNTIME_OWNER
        Draft202012Validator.check_schema(dict(decl.spec.schema))


def test_finish_and_ask_are_reserved_with_no_body() -> None:
    for name in ("finish", "ask"):
        decl = LOADER_ADDED_DECLS[name]
        assert (decl.spec.effect, decl.body) == ("reserved", None)


def test_save_artifact_is_a_store_tool_returning_one_save_request() -> None:
    decl = LOADER_ADDED_DECLS["save_artifact"]
    assert decl.spec.effect == "store"
    outcome = call_body(decl, CTX, {"kind": "line", "key": "n1", "content": {"line": "x"}})
    assert isinstance(outcome.result, Ok)
    (save,) = outcome.saves
    assert (save.kind, save.key, dict(save.content)) == ("line", "n1", {"line": "x"})


def test_save_artifact_reads_primary_inputs_and_relations_from_json() -> None:
    from aksha.runtime.artifacts.types import SELF, Excerpt, InputRef, RelationRequest

    page = {"kind": "record", "id": "pages/P-0001", "version": "1"}
    args = {
        "kind": "merge",
        "key": "m1",
        "content": {},
        "primary_inputs": [page],
        "relations": [
            {"kind": "same_as", "src": "self", "dst": page, "label": None,
             "evidence": [{"source": page, "quote": "Acme", "span": "EX-0001"}]},
        ],
    }  # fmt: skip
    decl = LOADER_ADDED_DECLS["save_artifact"]
    from jsonschema import Draft202012Validator

    Draft202012Validator(dict(decl.spec.schema)).validate(args)
    assert decl.body is not None
    (req,) = decl.body(CTX, args).saves
    record = InputRef("record", "pages/P-0001", "1")
    assert req.primary_inputs == (record,)
    evidence = (Excerpt(record, "Acme", span="EX-0001"),)
    assert req.relations == (RelationRequest("same_as", SELF, record, evidence),)
