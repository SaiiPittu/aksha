"""The conformance extension: one read tool, one projection and one event type (ADR-P20).

It proves an extension can be added with no runtime edit. It imports only
:mod:`aksha.runtime.api`.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from typing import Any

from aksha.runtime.api import (
    ArtifactsView,
    Event,
    EventTypeSpec,
    Extension,
    Ok,
    Projection,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)


def lookup(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Return the text it was given: the smallest read tool."""
    return ToolOutcome(Ok({"text": str(args["text"])}))


class RunsStarted:
    """Counts ``run.started`` events: the smallest contributed projection."""

    name = "runs_started"
    reads = frozenset({"run.started"})

    def initial(self, args: Hashable) -> int:
        """No runs yet."""
        return 0

    def apply(self, state: int, event: Event, artifacts: ArtifactsView) -> int:
        """One more run."""
        return state + 1


EXTENSION = Extension(
    name="echo",
    version="0.1.0",
    requires_api=">=2.0,<3",
    tools=(
        ToolDecl(
            ToolSpec(
                name="lookup",
                owner="echo",
                effect="read",
                description="Echo the given text back.",
                schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            body=lookup,
        ),
    ),
    projections=(Projection("runs_started", RunsStarted()),),
    event_types=(
        EventTypeSpec(
            name="echo.looked_up",
            schema={"type": "object", "properties": {"text": {"type": "string"}}},
            writer="echo",
            boundaries=frozenset({"U2"}),
        ),
    ),
)
