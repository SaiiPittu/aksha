from dataclasses import FrozenInstanceError

import pytest

from aksha.runtime.tasks.types import (
    CheckResult,
    FromField,
    KeySource,
    Read,
    Task,
    artifact_keys,
    from_field,
    source_keys,
)


def _ok(view: object) -> CheckResult:
    return CheckResult(ok=True)


def test_source_keys_names_a_collection() -> None:
    assert source_keys("notes") == KeySource(origin="source", name="notes")


def test_artifact_keys_names_a_kind_and_a_path() -> None:
    ks = artifact_keys("plan.steps", "steps", unless=_ok)
    assert (ks.origin, ks.name, ks.path, ks.unless) == ("artifact", "plan.steps", "steps", _ok)


def test_from_field_builds_a_key_binding() -> None:
    assert from_field("a.list", "ids") == FromField("a.list", "ids")
    assert Read("a.item", keys=from_field("a.list", "ids")).keys == FromField("a.list", "ids")


def test_task_defaults_follow_the_adr() -> None:
    t = Task("leaf", done=_ok)
    assert (t.turn_limit, t.max_finish_vetoes, t.on_child_blocked) == (10, 3, "block")
    assert t.model_aliases == () and t.stop_policies == () and t.interrupt is None
    with pytest.raises(FrozenInstanceError):
        t.id = "other"  # type: ignore[misc]


def test_check_result_carries_failing_keys() -> None:
    r = CheckResult(ok=False, message="missing", count=2, keys=("a", "b"))
    assert (r.ok, r.count, r.keys, r.clears) == (False, 2, ("a", "b"), None)
