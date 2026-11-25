from aksha.runtime.api import ArtifactKind, Task, ToolDecl, ToolSpec
from aksha.runtime.definitions.contributions import CONTRIBUTIONS, local_name, merged
from aksha.runtime.definitions.errors import Refusals


def test_local_name_of_each_kind_of_contribution() -> None:
    spec = ToolSpec(name="lookup", owner="e", effect="model", description="d", schema={})
    assert local_name(ToolDecl(spec, None)) == "lookup"
    assert local_name(Task("collect")) == "collect"
    assert local_name(ArtifactKind("line", {"type": "object"})) == "line"
    assert "event_types" not in CONTRIBUTIONS  # checked by event_type_refusals instead


def test_merged_refuses_a_key_two_parts_declare() -> None:
    out = Refusals("m")
    assert merged("artifacts", [{"a.x": 1}, {"b.y": 2}], out) == {"a.x": 1, "b.y": 2}
    assert out.items == []
    assert merged("artifacts", [{"a.x": 1}, {"a.x": 2}], out) == {"a.x": 1}
    assert [(r.code, r.field) for r in out.items] == [("duplicate_name", "artifacts")]
