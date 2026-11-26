"""Approvals (ADR-P17 D5 to D8, S1, S3): the signature, the body, the state and the re-check.

1. :func:`approval_signature`: ``"v1:" + sha256`` over the canonical JSON of an
   :class:`ActionBody`. The version prefix keeps a later body schema from colliding.
2. :func:`build_body`: the generic body of one call of a tool that declares an
   ``ApprovalRule``. Its facts come from the rule's pure ``facts`` function, its targets
   from ``targets`` with each version's content hash, its idempotency key from the tool's
   ``EffectSpec`` (ADR-P03), so the body names the exact outbox entry it will create.
3. :func:`request_approval`: the ``approval.requested`` and ``interrupt.raised{approval}``
   facts a tool step returns (U2) or a command writes (U4). The approval and its interrupt
   share one ID. A request on a stale or unknown target is refused.
4. :func:`approvals` (defined in ``reducers.py``, the one approval reducer the runtime
   has, and re-exported here): every approval's state. ``requested`` until a person's
   ``approval.granted`` or ``approval.rejected``; ``voided`` by ``approval.voided`` (an edit,
   a withdrawn key); ``revoked`` by invalidation's ``approval.revoked``
   (a grant, or a request, whose target it confirmed invalidated); ``used`` once an
   ``outbox.enqueued`` names it.
5. :func:`verify_for_execution`: D8's re-check before an approved action runs, and again
   before every delivery (:func:`approval_authority`, the outbox ``Authority``): the
   definition, the targets' freshness, then the body rebuilt from current facts and its
   signature compared. A mismatch is a refusal; a fresh approval is needed.

No model output, tool result or document text writes any approval fact: only these
builders, called by a tool step the policy gate routed to ``approval`` and by a person's
commands (``interrupts/commands.py``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.artifacts.versions import version
from aksha.runtime.canonical import canonical_json, sha256_hex
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.raising import raised
from aksha.runtime.interrupts.reducers import (
    APPROVAL_GRANTED as APPROVAL_GRANTED,
)
from aksha.runtime.interrupts.reducers import (
    APPROVAL_REJECTED as APPROVAL_REJECTED,
)
from aksha.runtime.interrupts.reducers import (
    APPROVAL_REQUESTED as APPROVAL_REQUESTED,
)
from aksha.runtime.interrupts.reducers import (
    APPROVAL_REVOKED as APPROVAL_REVOKED,
)
from aksha.runtime.interrupts.reducers import (
    APPROVAL_VOIDED as APPROVAL_VOIDED,
)
from aksha.runtime.interrupts.reducers import (
    approvals as approvals,
)
from aksha.runtime.interrupts.types import ActionBody, InterruptRequest, Refusal
from aksha.runtime.invalidation.effects import effect_refusal
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.outbox.keys import Resolve, idempotency_key
from aksha.runtime.outbox.ports import Authority
from aksha.runtime.outbox.types import OutboxEntry
from aksha.runtime.projections.view import LogView
from aksha.runtime.tools.types import ToolSpec

SIGNATURE_VERSION = "v1"


NOT_FOUND = "approval_not_found"
NOT_GRANTED = "approval_not_granted"
ALREADY_USED = "approval_used"
SIGNATURE_MISMATCH = "signature_mismatch"
STALE_TARGET = "stale_target"
DEFINITION_CHANGED = "definition_changed"
TARGET_CHANGING = "target_changing"
NOT_APPROVABLE = "not_approvable"

OUTBOX_CODES: Mapping[str, str] = MappingProxyType(
    {
        NOT_FOUND: "approval_required",
        NOT_GRANTED: "approval_required",
        ALREADY_USED: "approval_required",
        SIGNATURE_MISMATCH: "hash_mismatch",
        STALE_TARGET: "target_invalidated",
        DEFINITION_CHANGED: "definition_changed",
    }
)
"""The outbox's names for a failed re-check (ADR-P03 D8): ``outbox.abandoned{reason}``."""


def approval_signature(body: ActionBody) -> str:
    """``'v1:'`` + sha256 over the canonical JSON of ``body`` (D5)."""
    return f"{SIGNATURE_VERSION}:{sha256_hex(body.as_json())}"


@dataclass(frozen=True)
class ApprovalTool:
    """What rebuilding a body for one tool needs: its spec, the facts view and key resolver.

    ``view_of`` turns the owner's log into the view the rule's ``facts`` function reads (a
    ``TaskView`` for a Task's tool); it must be pure. ``resolve`` reads an idempotency key
    field that names an artifact (ADR-P03's ``Resolve``).
    """

    spec: ToolSpec
    view_of: Callable[[LogView], Any] = lambda log: log
    resolve: Resolve | None = None

    def __post_init__(self) -> None:
        """Only a tool with an approval rule and an external effect can be approved."""
        if self.spec.approval is None or self.spec.external is None:
            raise ValueError(f"{self.spec.name} declares no approval rule on an external effect")


def build_body(
    view: LogView, tool: ApprovalTool, args: Mapping[str, Any], *, definition_hash: str
) -> ActionBody | Refusal:
    """The action body of calling ``tool`` with ``args`` now, or a refusal (D6). Pure.

    Refused ``stale_target`` when a target names no saved version.
    """
    rule, effect = tool.spec.approval, tool.spec.external
    assert rule is not None and effect is not None  # checked by ApprovalTool
    refs = tuple(rule.targets(args)) if rule.targets is not None else ()
    targets: list[tuple[str, str]] = []
    for ref in refs:
        found = version(view, ArtifactRef.parse(ref))
        if found is None:
            return Refusal(STALE_TARGET, f"{ref} names no saved version")
        targets.append((ref, found.content_hash))
    facts = rule.facts(tool.view_of(view), args)
    key = idempotency_key(tool.spec.name, view.owner, args, effect, resolve=tool.resolve)
    return ActionBody(
        tool=tool.spec.name,
        owner=view.owner,
        canonical_args=canonical_json(args),
        targets=tuple(targets),
        idempotency_key=key,
        definition_hash=definition_hash,
        facts=MappingProxyType({str(k): str(v) for k, v in facts.items()}),
    )


def _moved(value: Any, old: str, new: str) -> Any:
    """``value`` with every string equal to ``old`` replaced by ``new``, at any depth."""
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, Mapping):
        return {k: _moved(v, old, new) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_moved(v, old, new) for v in value]
    return value


def retarget(
    body: ActionBody,
    old: str,
    new: str,
    content_hash: str,
    *,
    view: LogView | None = None,
    tool: ApprovalTool | None = None,
) -> ActionBody:
    """``body`` moved from target ``old`` to ``new`` at ``content_hash``: a new signature.

    The fresh request after an accepted review (D7) covers the revision the person accepted,
    so the call itself moves too: every argument naming ``old`` names ``new``. Given the
    ``tool`` and the ``view``, the body is rebuilt from those arguments (facts, targets and
    the idempotency key all recomputed), so it equals the body execution rebuilds (D8) and
    the person sees the new version in the facts. Without the tool only the targets, the
    arguments and the facts move; the key cannot be recomputed, so execution refuses it
    ``signature_mismatch`` and a fresh call is needed.
    """
    args = _moved(json.loads(body.canonical_args), old, new)
    if view is not None and tool is not None and tool.spec.name == body.tool:
        rebuilt = build_body(view, tool, args, definition_hash=body.definition_hash)
        if not isinstance(rebuilt, Refusal):
            return rebuilt
    targets = tuple((new, content_hash) if ref == old else (ref, h) for ref, h in body.targets)
    facts = MappingProxyType({k: new if v == old else v for k, v in body.facts.items()})
    return replace(body, targets=targets, canonical_args=canonical_json(args), facts=facts)


def request_events(
    approval_id: str,
    body: ActionBody,
    *,
    run: str | None,
    instance: str | None,
    replaces: str | None = None,
    step: str | None = None,
) -> tuple[NewEvent, NewEvent]:
    """``approval.requested`` and its ``interrupt.raised{kind: approval}``, one ID for both."""
    signature = approval_signature(body)
    payload: dict[str, Any] = {
        "approval": approval_id,
        "body": body.as_json(),
        "signature": signature,
        "targets": list(body.target_refs()),
    }
    if replaces is not None:
        payload["replaces"] = replaces
    requested = NewEvent(APPROVAL_REQUESTED, payload, run=run, instance=instance, step=step)
    extra = {"approval": approval_id, "signature": signature, "tool": body.tool,
             "facts": dict(body.facts), "targets": list(body.target_refs())}  # fmt: skip
    interrupt = raised(InterruptRequest("approval", run=run, instance=instance, extra=extra),
                       approval_id, step=step)  # fmt: skip
    return requested, interrupt


def request_approval(
    view: LogView,
    tool: ApprovalTool,
    args: Mapping[str, Any],
    approval_id: str,
    *,
    definition_hash: str,
    run: str,
    instance: str | None = None,
    step: str | None = None,
    replaces: str | None = None,
) -> tuple[NewEvent, ...] | Refusal:
    """The facts that ask a person to approve this call, or a refusal. Pure.

    Refused ``stale_target`` when a target is invalidated, dirty or pending review
    (ADR-P05 D9): no approval request runs on a version that is changing.
    """
    body = build_body(view, tool, args, definition_hash=definition_hash)
    if isinstance(body, Refusal):
        return body
    stale = effect_refusal(invalidation_view(view), body.target_refs())
    if stale is not None:
        return Refusal(STALE_TARGET, f"a target of {tool.spec.name} is {stale}")
    return request_events(approval_id, body, run=run, instance=instance, step=step,
                          replaces=replaces)  # fmt: skip


def verify_for_execution(
    view: LogView,
    approval_id: str,
    tool: ApprovalTool,
    *,
    definition_hash: str,
    entry_key: str | None = None,
) -> ActionBody | Refusal:
    """D8: the body to execute, or why not. Pure.

    The approval must be granted (or already used by ``entry_key``, the outbox entry the
    worker is about to deliver). Then, in order: the definition is the one the person
    approved under (``definition_changed``), every target is fresh (``stale_target``), and
    the body rebuilt from current facts has the approved signature (``signature_mismatch``).
    """
    a = approvals(view).get(approval_id)
    if a is None:
        return Refusal(NOT_FOUND, f"no approval {approval_id}")
    if a.state == "used" and (entry_key is None or a.used_by != entry_key):
        return Refusal(ALREADY_USED, f"{approval_id} was used by {a.used_by}")
    if a.state not in ("granted", "used"):
        return Refusal(NOT_GRANTED, f"{approval_id} is {a.state}")
    if a.body.definition_hash != definition_hash:
        return Refusal(DEFINITION_CHANGED, f"{approval_id} was approved under another definition")
    stale = effect_refusal(invalidation_view(view), a.body.target_refs())
    if stale is not None:
        return Refusal(STALE_TARGET, f"a target of {approval_id} is {stale}")
    if tool.spec.name != a.body.tool:
        return Refusal(SIGNATURE_MISMATCH, f"{approval_id} covers {a.body.tool}")
    now = build_body(view, tool, a.body.args(), definition_hash=definition_hash)
    if isinstance(now, Refusal):
        return now
    if approval_signature(now) != a.signature:
        return Refusal(SIGNATURE_MISMATCH, f"the facts behind {approval_id} changed")
    return now


def approval_authority(
    tools: Mapping[str, ApprovalTool], definition_hash: Callable[[OutboxEntry], str]
) -> Authority:
    """The outbox's ``Authority`` for approved entries (D8): the re-check before each delivery.

    An entry with no ``approval_id`` passes (another authority covers it); an approved
    entry is re-verified, and a failure is the outbox's code (:data:`OUTBOX_CODES`), which
    the worker commits as ``outbox.abandoned{reason}`` instead of delivering.
    ``definition_hash`` reads the hash the entry's run is under now.
    """

    def check(entry: OutboxEntry, view: LogView, /) -> str | None:
        if entry.approval_id is None:
            return None
        tool = tools.get(entry.tool)
        if tool is None:
            return OUTBOX_CODES[NOT_FOUND]
        found = verify_for_execution(view, entry.approval_id, tool,
                                     definition_hash=definition_hash(entry),
                                     entry_key=entry.key)  # fmt: skip
        if isinstance(found, Refusal):
            return OUTBOX_CODES.get(found.code, found.code)
        if found.idempotency_key != entry.key:
            return OUTBOX_CODES[SIGNATURE_MISMATCH]
        return None

    return check
