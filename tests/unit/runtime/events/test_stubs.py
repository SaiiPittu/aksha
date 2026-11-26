"""Blob stubs: the exact shape, and where they sit in a payload."""

from __future__ import annotations

from aksha.runtime.events.stubs import JSON_TYPE, TEXT_TYPE, is_blob_stub, stub_paths, under

REF = "sha256:" + "a" * 64


def test_a_stub_is_exactly_three_keys_with_a_blob_ref() -> None:
    assert is_blob_stub({"blob": REF, "bytes": 0, "type": TEXT_TYPE})
    assert is_blob_stub({"blob": REF, "bytes": 9, "type": JSON_TYPE})
    for bad in (
        {"blob": REF, "bytes": 1},
        {"blob": REF, "bytes": 1, "type": TEXT_TYPE, "x": 1},
        {"blob": "sha256:x", "bytes": 1, "type": TEXT_TYPE},
        {"blob": REF, "bytes": -1, "type": TEXT_TYPE},
        {"blob": REF, "bytes": True, "type": TEXT_TYPE},
        {"blob": REF, "bytes": 1, "type": "image/png"},
        "sha256:" + "a" * 64,
    ):
        assert not is_blob_stub(bad)


def test_stub_paths_finds_nested_stubs_but_never_the_root() -> None:
    stub = {"blob": REF, "bytes": 1, "type": TEXT_TYPE}
    payload = {"a": stub, "b": [1, {"c": stub}]}
    assert stub_paths(payload) == (("a",), ("b", 1, "c"))
    assert stub_paths(stub) == ()
    assert under(("b", 1, "c", "x"), stub_paths(payload)) and not under(("b",), stub_paths(payload))
