"""Runtime tools over an episode: how a real agent's reads go through the episode's tools.

A runtime agent reads its sources through tools (ADR-P10), not through the file system.
For an eval, its adapter gives the agent these declarations in place of its usual source
tools: each body calls the :class:`~aksha.evals.episode.Episode`, so every read passes the
path guard and the fault switches, is recorded as ``episode.tool_called`` and lands in the
bundle's ``reads/``. That is what ``rel.same_calls``, the citation checks (which read each
version of a file the episode saw) and the fault profiles depend on. An agent that opened
``episode.sources`` directly would bypass all of them, so an adapter never hands the agent
that path.

The bodies turn the episode's refusals into tool results the model reads (``path_refused``,
``not_found``, a fault switch's code such as ``timeout``), as any tool body does. A change
step's restart is a ``BaseException`` and passes through the runtime's body wrapper to the
environment.

Saves, approvals and sends stay with the adapter: at the end of the run (or as the run's
effects reach the fixture receiver) it writes the agent's artifacts with
:meth:`Episode.save`, asks for approval with :meth:`Episode.request_approval` naming the
recipient, and sends with :meth:`Episode.send`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.evals.episode import Episode
from aksha.evals.errors import EvalError
from aksha.runtime.tools.types import Err, Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec

OWNER = "evals"
"""The ``owner`` the declarations carry: the eval environment, not an extension."""

_PATH = {"type": "string", "minLength": 1, "description": "a path relative to sources/"}


def episode_tools(episode: Episode, *, owner: str = OWNER) -> tuple[ToolDecl, ...]:
    """``list_sources`` and ``read_source`` declarations whose bodies call ``episode``."""

    def list_body(_ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        try:
            files = episode.list_sources(str(args.get("folder", "")))
        except EvalError as e:
            return ToolOutcome(Err(e.code, e.message, paths=("/folder",)))
        return ToolOutcome(Ok({"files": files}))

    def read_body(_ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        path = str(args["path"])
        try:
            text = episode.read_source(path)
        except EvalError as e:
            return ToolOutcome(Err(e.code, e.message, paths=("/path",), item=path))
        return ToolOutcome(Ok({"path": path, "text": text}))

    list_spec = ToolSpec(
        name="list_sources",
        owner=owner,
        effect="read",
        description="List the source files under a folder of sources/ (all of them if empty).",
        schema={
            "type": "object",
            "properties": {"folder": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    read_spec = ToolSpec(
        name="read_source",
        owner=owner,
        effect="read",
        description="Read one source file's raw text.",
        schema={
            "type": "object",
            "properties": {"path": _PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    return ToolDecl(list_spec, list_body), ToolDecl(read_spec, read_body)
