"""Approval values the suites share (ADR-P17): a generic action body and its request facts."""

from __future__ import annotations

from collections.abc import Mapping

from aksha.runtime.canonical import canonical_json
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.approvals import ApprovalTool, request_events
from aksha.runtime.interrupts.types import ActionBody, ApprovalRule
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.tools.types import ToolSpec

DEFINITION = "sha256:" + "0" * 64
"""A definition hash for bodies that never reach execution."""


def body(
    owner: str = "o",
    *,
    tool: str = "send",
    args: Mapping[str, object] | None = None,
    targets: tuple[tuple[str, str], ...] = (),
    key: str = "send:k1",
    facts: Mapping[str, str] | None = None,
) -> ActionBody:
    """A generic action body with neutral values."""
    return ActionBody(
        tool=tool, owner=owner, canonical_args=canonical_json(dict(args or {})),
        targets=targets, idempotency_key=key, definition_hash=DEFINITION,
        facts=dict(facts or {}),
    )  # fmt: skip


def requested(
    approval: str, run: str, *, owner: str = "o", instance: str | None = None
) -> tuple[NewEvent, NewEvent]:
    """``approval.requested`` and its ``interrupt.raised{approval}`` for a neutral body."""
    return request_events(approval, body(owner), run=run, instance=instance)


# --- a send tool that needs approval, over the invalidation bench's kinds ----------------


def _send_facts(view: object, args: Mapping[str, object]) -> Mapping[str, str]:
    """The recipient and its record version: what the person approves besides the packet."""
    from aksha.runtime.artifacts.versions import versions
    from aksha.runtime.projections.view import LogView

    assert isinstance(view, LogView)
    to = str(args["to"])
    found = versions(view, "p.note", to)
    return {"recipient": to, "recipient_version": str(found[-1].ref) if found else "none"}


def send_spec() -> ToolSpec:
    """``send``: an external tool whose call a person approves first (generic facts)."""
    return ToolSpec(
        name="send", owner="p", effect="external", description="Send one packet.",
        schema={"type": "object", "properties": {"packet": {"type": "string"},
                                                 "to": {"type": "string"}}},
        external=EffectSpec(system="mailer", key_fields=("/packet", "/to"), semantics="reconcile"),
        approval=ApprovalRule(facts=_send_facts, targets=lambda a: (str(a["packet"]),)),
    )  # fmt: skip


SEND = ApprovalTool(send_spec())
TOOLS = {"send": SEND}
