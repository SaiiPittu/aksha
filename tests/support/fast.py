"""Fast-class model handles for tool tests: an agent's fakes bound as its loop would bind them.

``models_for(agent_dir, adapters, tool)`` returns the ``ToolContext.models`` handle a
dispatched body of ``tool`` gets in ``fake`` mode: the agent's declared aliases bound to
``adapters`` (its ``fakes.ADAPTERS``) through the real binder, limited to the aliases the
tool declares, so a test calls a body exactly as the loop does.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from aksha.runtime.definitions.loader import load_agents
from aksha.runtime.models.calls import ToolModelCalls
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.tools.types import ToolDecl


def models_for(agent_dir: Path, adapters: Mapping[str, object], tool: str) -> ToolModelCalls:
    """The fake-mode ``ToolContext.models`` of ``tool`` (module doc)."""
    (loaded,) = load_agents([agent_dir], [])
    table = bind_aliases(loaded.agent.model_aliases, "fake", adapters)
    spec = next(
        t.spec for t in loaded.agent.tools if isinstance(t, ToolDecl) and t.spec.name == tool
    )
    return ToolModelCalls(table, spec.model_aliases, spec.name, "o")
