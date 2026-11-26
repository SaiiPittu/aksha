"""The effective sandbox policy (ADR-P12 decision 2, Terms): it can only narrow. Pure.

The run ceiling, then the agent's policy, then the Task's, then the tool's declared needs,
each narrowing the one before; a layer that asks for more raises ``PolicyWidened``. A tool
whose workspace tree is writable needs the whole tree writable (``.``).
"""

from __future__ import annotations

from aksha.runtime.sandbox.errors import PolicyWidened
from aksha.runtime.sandbox.types import ExecPolicy, SandboxNeeds, SandboxPolicy


def effective_policy(
    ceiling: ExecPolicy,
    agent: SandboxPolicy | None,
    task: SandboxPolicy | None,
    needs: SandboxNeeds,
) -> ExecPolicy:
    """The field-wise intersection of every layer with the tool's needs (module doc).

    The limits are the smaller of the ceiling's and the tool's, field by field.
    """
    policy = ceiling.paths
    for layer in (agent, task):
        if layer is not None:
            policy = policy.narrow(layer)
    policy = policy.narrow(needs.policy)
    if needs.tree_writable and "." not in policy.writable:
        raise PolicyWidened(message="a writable tree needs '.' writable at every layer")
    return ExecPolicy(policy, ceiling.limits.narrow(needs.limits))
