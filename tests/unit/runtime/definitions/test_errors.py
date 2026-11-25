from aksha.runtime.definitions.errors import LoadError, LoadRefusal
from aksha.runtime.errors import AkshaError


def test_refusal_names_module_task_and_field() -> None:
    r = LoadRefusal("toy/agent.py", "collect/write", "tools", "tool_widens_parent", "'x' widens")
    assert str(r) == "toy/agent.py: Task 'collect/write', field 'tools': 'x' widens"
    assert (
        str(LoadRefusal("toy/agent.py", None, "name", "c", "m")) == "toy/agent.py: field 'name': m"
    )


def test_load_error_carries_every_refusal_and_the_first_code() -> None:
    first = LoadRefusal("a", None, "f", "api_mismatch", "m1")
    err = LoadError((first, LoadRefusal("b", None, "g", "unknown_tool", "m2")))
    assert isinstance(err, AkshaError)
    assert err.code == "api_mismatch" and len(err.refusals) == 2
    assert "m1" in str(err) and "m2" in str(err)


def test_refusals_collects_in_order_for_one_module() -> None:
    from aksha.runtime.definitions.errors import Refusals

    out = Refusals("m/agent.py")
    out.add("tools", "unknown_tool", "first")
    out.add("done", "leaf_without_check", "second", task="a/b")
    assert [(r.module, r.task, r.code) for r in out.items] == [
        ("m/agent.py", None, "unknown_tool"),
        ("m/agent.py", "a/b", "leaf_without_check"),
    ]
