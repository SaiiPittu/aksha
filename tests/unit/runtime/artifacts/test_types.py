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
