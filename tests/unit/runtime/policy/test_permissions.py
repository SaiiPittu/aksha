from dataclasses import replace

import pytest

from aksha.runtime.policy.permissions import agent_tools, ceiling_of, route_of, task_tools
from aksha.runtime.tools.loader_added import LOADER_ADDED_DECLS
from tests.support.gate import NO_GIT, Spy, agent, registry, specs, task


def test_agent_tools_are_its_list_plus_the_implicit_tools() -> None:
    a = agent(Spy())
    assert agent_tools(a) == {
        "fetch",
        "write_file",
        "send",
        "post",
        "run",
        "classify",
        "save_artifact",
        "finish",
        "ask",
    }


def test_an_agent_with_no_tool_list_holds_only_the_implicit_tools() -> None:
    assert agent_tools(replace(agent(Spy()), tools=())) == {"finish", "ask"}


def test_task_tools_add_finish_and_ask_only_with_an_interrupt() -> None:
    a = agent(Spy())
    assert task_tools(task(a, "gather")) == {"fetch", "save_artifact", "finish"}
    assert task_tools(task(a, "act")) == {"fetch", "write_file", "send", "post", "finish", "ask"}
    assert task_tools(task(a, "idle")) == {"finish"}


def test_ceiling_of_is_the_agents_list_with_the_given_rules() -> None:
    a = agent(Spy())
    ceiling = ceiling_of(a, rules=(NO_GIT,))
    assert ceiling.tools == agent_tools(a)
    assert ceiling.rules == (NO_GIT,)


@pytest.mark.parametrize(
    ("name", "route"),
    [
        ("fetch", "dispatch"),
        ("write_file", "dispatch"),
        ("send", "approval"),
        ("post", "outbox"),
        ("run", "dispatch"),
        ("classify", "model"),
    ],
)
def test_route_of_follows_the_effect_class(name: str, route: str) -> None:
    spec = next(s for s in specs() if s.name == name)
    assert route_of(spec) == route


def test_reserved_tools_route_to_the_runtime_except_the_generic_save() -> None:
    assert route_of(LOADER_ADDED_DECLS["finish"].spec) == "reserved"
    assert route_of(LOADER_ADDED_DECLS["ask"].spec) == "reserved"
    assert route_of(LOADER_ADDED_DECLS["save_artifact"].spec) == "dispatch"


def test_registry_helper_holds_every_tool() -> None:
    assert "send" in registry(agent(Spy())).names()
