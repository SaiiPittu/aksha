"""``call_body``: call one tool body, turning every failure into a result (ADR-P10 D6).

A raising body becomes ``tool_exception``, except an egress refusal the body let through,
which becomes ``egress_refused`` naming the host and the reason (ADR-P26 D4); a body
that returns anything but a ``ToolOutcome`` becomes ``class_violation``; neither escapes
as an exception, so the model reads the error in its next turn. ``tool_exception`` is
not in any default ``retry_on``, because a bug repeats. The durable step executor
(ADR-P02) records the outcome; this function only calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.canonical import freeze
from aksha.runtime.egress.errors import EGRESS_REFUSED, EgressRefused
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.tools.types import Err, Ok, ToolContext, ToolDecl, ToolOutcome


def call_body(decl: ToolDecl, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Run ``decl``'s body on a read-only copy of ``args``; never raises an ``Exception``."""
    name = decl.spec.name
    if decl.body is None:
        return ToolOutcome(Err("class_violation", f"tool {name!r} has no body to call"))
    try:
        outcome: object = decl.body(ctx, freeze(args))  # agent code is not type-checked
    except TransientStepError:  # a fast-model call worth retrying: the step's policy decides
        raise
    except EgressRefused as refused:  # the destination, not the body, was refused
        message = f"tool {name!r}: egress refused for {refused.host!r}: {refused.reason}"
        return ToolOutcome(Err(EGRESS_REFUSED, message, item=refused.host or None))
    except Exception as exc:  # a body's own bug is a recorded result, not a crash
        return ToolOutcome(
            Err("tool_exception", f"tool {name!r} raised {type(exc).__name__}: {exc}")
        )
    if not isinstance(outcome, ToolOutcome) or not isinstance(outcome.result, Ok | Err):
        got = type(outcome).__name__
        return ToolOutcome(Err("class_violation", f"tool {name!r} returned {got}, not ToolOutcome"))
    return outcome
