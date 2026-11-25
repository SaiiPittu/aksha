from aksha.runtime.policy.matrix import MatrixRow, PermissionMatrix, matrix
from aksha.runtime.policy.types import Permissions, StopPolicyNarrowing
from tests.support.gate import Spy, agent, registry

A = agent(Spy())


def test_one_row_per_agent_task_and_tool() -> None:
    m = matrix([(A, registry(A))])
    tools = registry(A).names()
    assert len(m.rows) == len(A.tasks) * len(tools)
    assert {r.task for r in m.rows} == {"gather", "act", "build", "idle"}


def test_cells_say_allowed_with_a_route_or_the_refusal() -> None:
    m = matrix([(A, registry(A))])
    assert m.cell("probe", "gather", "fetch") == MatrixRow(
        "probe", "gather", "fetch", True, "dispatch", None, None
    )
    assert m.cell("probe", "act", "send").route == "approval"
    refused = m.cell("probe", "gather", "send")
    assert (refused.allowed, refused.code, refused.layer) == (False, "not_in_task", 3)


def test_a_ceiling_and_a_narrowing_change_the_cells() -> None:
    reg = registry(A)
    ceiling = {"probe": Permissions(frozenset({"fetch"}))}
    m = matrix([(A, reg)], ceilings=ceiling)
    assert m.cell("probe", "gather", "save_artifact").code == "not_allowed"
    plan = StopPolicyNarrowing(frozenset({"save_artifact"}), by="plan")
    n = matrix([(A, reg)], narrowing=plan)
    assert n.cell("probe", "act", "fetch").code == "narrowed"
    assert n.cell("probe", "act", "finish").allowed


def test_markdown_prints_every_row() -> None:
    m = PermissionMatrix(
        (
            MatrixRow("a", "t", "x", True, "dispatch", None, None),
            MatrixRow("a", "t", "y", False, None, "not_in_task", 3),
        )
    )
    text = m.markdown()
    assert text.splitlines()[0] == "| Agent | Task | Tool | Decision |"
    assert "| a | t | x | allowed (dispatch) |" in text
    assert "| a | t | y | refused: not_in_task (layer 3) |" in text


def test_nested_tasks_appear_by_path() -> None:
    from dataclasses import replace

    from aksha.runtime.api import Task

    nested = replace(A, tasks=(Task("outer", tools=("fetch",), children=(Task("inner"),)),))
    m = matrix([(nested, registry(nested))])
    assert m.cell("probe", "outer/inner", "fetch").code == "not_in_task"
