from aksha.runtime.artifacts.types import ArtifactKind, RelationKind


def test_artifact_kind_defaults_follow_the_adr() -> None:
    k = ArtifactKind("line", {"type": "object"})
    assert (k.keyed, k.writers, k.save, k.derived) == (False, "one", "any", None)


def test_relation_kind_ends_are_tuples() -> None:
    r = RelationKind("summarises", src=("summary",), dst=("line", "record:notes"))
    assert (r.symmetric, r.cites) == (False, "one")


def test_save_request_names_a_kind_key_and_content() -> None:
    from aksha.runtime.artifacts.types import SaveRequest

    req = SaveRequest("line", key="n1", content={"line": "x"})
    assert (req.kind, req.key, dict(req.content)) == ("line", "n1", {"line": "x"})


def test_refs_print_and_parse_back() -> None:
    import pytest

    from aksha.runtime.artifacts.types import ArtifactRef

    keyed, unkeyed = ArtifactRef("notes.line", "n1", 2), ArtifactRef("notes.summary", None, 1)
    assert (str(keyed), str(unkeyed)) == ("notes.line/n1@v2", "notes.summary@v1")
    assert ArtifactRef.parse("notes.line/n1@v2") == keyed
    assert ArtifactRef.parse("notes.summary@v1") == unkeyed
    assert ArtifactRef.parse("notes.tree/src/a.py@v3").key == "src/a.py"
    assert keyed.slot() == ("notes.line", "n1") and keyed.as_input().version == "v2"
    assert ArtifactRef.from_input(keyed.as_input()) == keyed
    for bad in ("notes.line/n1", "notes.line@v0", "a b@v1", "@v1"):
        with pytest.raises(ValueError):
            ArtifactRef.parse(bad)
    with pytest.raises(ValueError):
        ArtifactRef("notes.line", "n1", 0)


def test_input_refs_check_themselves_and_round_trip() -> None:
    import pytest

    from aksha.runtime.artifacts.types import InputRef, sorted_inputs

    scoped = InputRef(
        "artifact", "notes.tree", "v4", path="/files/a", entry_hash="sha256:" + "a" * 64
    )
    record = InputRef("record", "pages/P-0001", "3")
    for value in (scoped, record):
        assert InputRef.from_json(value.as_json()) == value
    assert str(scoped) == "artifact:notes.tree@v4#/files/a"
    assert sorted_inputs([record, scoped, record]) == (scoped, record)
    assert record.same_source(InputRef("record", "pages/P-0001", "4"))
    for kwargs in (
        {"kind": "file", "id": "x", "version": "1"},
        {"kind": "record", "id": "", "version": "1"},
        {"kind": "record", "id": "x", "version": "1", "path": "/a"},
        {"kind": "artifact", "id": "not a ref", "version": "v1"},
    ):
        with pytest.raises(ValueError):
            InputRef(**kwargs)  # type: ignore[arg-type]


def test_writers_and_excerpts_round_trip() -> None:
    from aksha.runtime.artifacts.types import Excerpt, InputRef, WriterId

    w = WriterId("notes", "compare", "C1")
    assert WriterId.from_json(w.as_json()) == w
    x = Excerpt(InputRef("record", "pages/P-0001", "1"), "a quote", "/body")
    assert Excerpt.from_json(x.as_json()) == x


def test_save_request_carries_primary_inputs_and_relations_by_default_empty() -> None:
    from aksha.runtime.artifacts.types import SaveRequest

    req = SaveRequest("line", None, {})
    assert (req.primary_inputs, req.relations) == ((), ())
