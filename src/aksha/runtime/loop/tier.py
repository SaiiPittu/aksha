"""The agent loop's side of tier 3: one attempt's HTTP client, exec API and their records.

ADR-P26 D2 to D4, ADR-P12 decisions 2, 9 and T6, ADR-P28 D5.

- :func:`http_for`: the client bound to the tool's egress policy, or one that refuses
  everything: a tool with no policy (``no_egress_policy``), a process with the network off
  (``network_off``), a policy no loaded extension declares (``egress_policy_unknown``).
- :func:`exec_for`: an exec tool's ``BoxedExec`` under the effective policy (the run ceiling
  narrowed by the tool's needs), its environment resolved through the recorded images.
  A policy that widens refuses every run of the attempt (``policy_widened``).
- :func:`with_records`: the step's value with ``egress`` (every hop's decision) and
  ``exec`` (every box's record) added, so both are on ``step.completed``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.definitions.names import resolve_kind
from aksha.runtime.egress.client import (
    NETWORK_OFF,
    NO_EGRESS_POLICY,
    PolicedHttpClient,
    RefusingHttpClient,
)
from aksha.runtime.execution.types import StepOutput
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.projections.view import LogView
from aksha.runtime.sandbox.errors import PolicyWidened
from aksha.runtime.sandbox.exec import BoxedExec, ExecUnit
from aksha.runtime.sandbox.images import EnvironmentImages, recorded_images
from aksha.runtime.sandbox.policy import effective_policy
from aksha.runtime.tools.types import ToolSpec

EGRESS_FIELD = "egress"
EXEC_FIELD = "exec"
EGRESS_POLICY_UNKNOWN = "egress_policy_unknown"

HttpFor = PolicedHttpClient | RefusingHttpClient


def http_for(setup: LoopSetup, spec: ToolSpec) -> HttpFor:
    """The HTTP client of one attempt of ``spec`` (module doc)."""
    if spec.egress is None:
        return RefusingHttpClient(NO_EGRESS_POLICY)
    name = resolve_kind(spec.egress, spec.owner)
    policy = setup.tier.egress.get(name)
    if policy is None:
        return RefusingHttpClient(EGRESS_POLICY_UNKNOWN, name)
    network = setup.tier.network
    if network is None:
        return RefusingHttpClient(NETWORK_OFF, name)
    return PolicedHttpClient(policy, network.resolver, network.transport)


def _built(view: LogView) -> dict[str, str]:
    values = (e.payload.get("value") or {} for e in view.events if e.type == "step.completed")
    return recorded_images(v for v in values if isinstance(v, Mapping))


def exec_for(
    setup: LoopSetup, spec: ToolSpec, step: str, attempt: int, unit: ExecUnit
) -> BoxedExec | None:
    """The exec API of one attempt of an exec tool, or None for any other tool."""
    needs = spec.sandbox
    if spec.effect != "exec" or needs is None:
        return None
    tier = setup.tier
    refusal: tuple[str, str] | None = None
    policy = tier.ceiling
    try:
        policy = effective_policy(tier.ceiling, None, None, needs)
    except PolicyWidened as widened:
        refusal = (widened.code, widened.message)
    images = EnvironmentImages(
        tier.environments, tier.egress, tier.network, tier.box, tier.availability,
        setup.blobs, recorded=lambda: _built(setup.view()), approved=tier.approved_images,
        unit=unit,
    )  # fmt: skip

    def environments(name: str, tree: Any, files: Mapping[str, bytes]) -> Any:
        return images.resolve(resolve_kind(name, spec.owner), tree, files)

    return BoxedExec(
        tier.box, tier.availability, needs, policy, invocation=f"{step}#a{attempt}",
        blobs=setup.blobs, environments=environments, unit=unit, refusal=refusal,
    )  # fmt: skip


def with_records(out: StepOutput, http: HttpFor, exe: BoxedExec | None) -> StepOutput:
    """``out`` with the attempt's egress decisions and exec records in its value."""
    extra: dict[str, Any] = {}
    if http.decisions:
        extra[EGRESS_FIELD] = [d.as_json() for d in http.decisions]
    if exe is not None and exe.records:
        extra[EXEC_FIELD] = [dict(r) for r in exe.records]
    if not extra:
        return out
    value = {**(out.value or {}), **extra}
    return StepOutput(value, out.outcome, out.code, out.events, out.artifacts, out.usage)
