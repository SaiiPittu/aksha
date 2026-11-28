"""Key sources read from the event store (ADR-P15 D3, S3)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from aksha.runtime.tasks.errors import KeyInvalid
from aksha.runtime.tasks.keys import (
    invalid_keys,
    is_valid_key,
    keys_listed_fact,
    keys_of,
    list_source_keys,
    listed_key_refusal,
    source_keys_listed,
)
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task, artifact_keys, source_keys
from tests.support.tasks import TaskLog


class Lister:
    def __init__(self, keys: Sequence[str]) -> None:
        self.found = list(keys)

    def keys(self, owner: str, collection: str) -> Sequence[str]:
        return self.found


def test_artifact_keys_are_none_before_the_artifact_and_sorted_after() -> None:
    log = TaskLog()
    source = artifact_keys("demo.selection", "ids")
    assert keys_of(log.view(), source) is None
    log.select("C3", "C1", "C2", "C1")
    assert keys_of(log.view(), source) == ("C1", "C2", "C3")


def test_artifact_keys_read_the_current_version_and_nested_fields() -> None:
    log = TaskLog()
    log.save("plan", None, {"body": {"steps": [{"id": "S2"}, {"key": "S1"}, 7, True, None]}})
    assert keys_of(log.view(), artifact_keys("demo.plan", "body.steps")) == ("7", "S1", "S2")
    assert keys_of(log.view(), artifact_keys("demo.plan", "body.nope")) == ()
    log.save("plan", None, {"body": {"steps": ["S9"]}})
    assert keys_of(log.view(), artifact_keys("demo.plan", "body.steps")) == ("S9",)


def test_a_local_kind_is_read_under_the_resolved_name() -> None:
    log = TaskLog()
    log.select("A")
    assert keys_of(log.view(), artifact_keys("selection", "ids"), kind="demo.selection") == ("A",)


def test_keys_an_instance_id_cannot_carry_are_left_out_and_named() -> None:
    log = TaskLog()
    log.select("ok", "a/b", "c[1]", "")
    source = artifact_keys("demo.selection", "ids")
    assert keys_of(log.view(), source) == ("ok",)
    assert invalid_keys(log.view(), source) == ("", "a/b", "c[1]")
    assert not is_valid_key("x]") and is_valid_key("P-0001")


def test_listed_objects_with_no_key_are_named_not_dropped() -> None:
    """On main a list of objects with no key or id fanned out to zero instances silently."""
    log = TaskLog()
    log.save("selection", None, {"ids": [{"candidate": "C1"}, {"candidate": "C4"}]})
    source = artifact_keys("demo.selection", "ids")
    assert keys_of(log.view(), source) == ()
    assert invalid_keys(log.view(), source) == ("[item 0]", "[item 1]")


def test_a_save_listing_an_unreadable_key_is_refused() -> None:
    tree = compile_tree("demo", (
        Task("pick", writes=("selection",), done=lambda v: CheckResult(True)),
        Task("work", per=artifact_keys("selection", "ids"), done=lambda v: CheckResult(True)),
    ))  # fmt: skip
    bad = listed_key_refusal(tree, "demo.selection", {"ids": ["C1", {"candidate": "C4"}]})
    assert bad is not None and bad[0] == "/content/ids/1"
    assert listed_key_refusal(tree, "demo.selection", {"ids": ["C1", {"key": "C4"}]}) is None
    assert listed_key_refusal(tree, "demo.other", {"ids": [{}]}) is None


def test_source_keys_follow_listing_and_later_source_changes() -> None:
    log = TaskLog()
    source = source_keys("notes")
    assert keys_of(log.view(), source) is None
    log.commit([keys_listed_fact("notes", ["n2", "n1"])])
    assert keys_of(log.view(), source) == ("n1", "n2")
    log.add("source.changed", {"records": {"notes/n1": None, "pages/x": "v2"}})
    assert keys_of(log.view(), source) == ("n2",)
    log.add("source.changed", {"records": {"notes/n1": "v3", "notes/n4": "v1"}})
    assert source_keys_listed(log.view(), "notes") == ("n1", "n2", "n4")


def test_list_source_keys_records_only_what_moved() -> None:
    log = TaskLog()
    lister = Lister(["b", "a"])
    facts = list_source_keys(log.view(), lister, ["notes"])
    assert [f.payload for f in facts] == [{"collection": "notes", "keys": ["a", "b"]}]
    log.commit(facts)
    assert list_source_keys(log.view(), lister, ["notes"]) == ()
    lister.found = ["a"]
    assert [f.payload["keys"] for f in list_source_keys(log.view(), lister, ["notes"])] == [["a"]]


def test_a_listed_key_an_instance_id_cannot_carry_is_refused() -> None:
    with pytest.raises(KeyInvalid) as e:
        keys_listed_fact("notes", ["a/b"])
    assert e.value.code == "key_invalid"
