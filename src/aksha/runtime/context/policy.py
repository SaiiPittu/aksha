"""The context policy as data: its hashed material and the loader's refusals (ADR-P08 D9).

``policy_hash`` covers every field and ``CONTEXT_POLICY_VERSION``, and the definition hash
includes it, so a run cannot resume under different numbers or different stub, notice or
recitation text. ``policy_refusals`` is what the loader refuses: the watermarks out of
order, a read block that alone fills the limit, an exempt tool the agent does not have, a
recent tail of zero pairs, a summary policy that cannot run, or resume numbers out of
range (decision 36).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.context.types import CONTEXT_POLICY_VERSION, ContextPolicy, effective_policy

POLICY_INVALID = "context_policy_invalid"

_MIN_CAP = 64
"""The smallest cap ``tools.results.cap_text`` accepts."""


def policy_material(policy: ContextPolicy) -> dict[str, Any]:
    """Every field of ``policy`` and the policy version, as JSON data."""
    summary = policy.summary
    return {
        "version": CONTEXT_POLICY_VERSION,
        "result_cap_chars": policy.result_cap_chars,
        "read_cap_chars": policy.read_cap_chars,
        "read_block_cap_chars": policy.read_block_cap_chars,
        "context_limit_chars": policy.context_limit_chars,
        "masking": policy.masking,
        "high_water_chars": policy.high_water_chars,
        "low_water_chars": policy.low_water_chars,
        "keep_recent_pairs": policy.keep_recent_pairs,
        "exempt_tools": sorted(policy.exempt_tools),
        "resume": {
            "min_prior_turns": policy.resume.min_prior_turns,
            "headroom_chars": policy.resume.headroom_chars,
            "max_stale_permille": policy.resume.max_stale_permille,
            "loop_repeats": policy.resume.loop_repeats,
            "premise_exempt": sorted(policy.resume.premise_exempt),
        },
        "summary": None
        if summary is None
        else {
            "alias": summary.alias,
            "trigger_chars": summary.trigger(policy),
            "keep_recent_pairs": summary.keep_recent_pairs,
            "max_per_instance": summary.max_per_instance,
        },
    }


def policy_hash(policy: ContextPolicy | None) -> str:
    """``sha256:`` over :func:`policy_material`; None hashes ``DEFAULT_POLICY``."""
    return "sha256:" + sha256_hex(policy_material(effective_policy(policy)))


def policy_refusals(policy: ContextPolicy, tools: Iterable[str]) -> list[str]:
    """Why the loader refuses ``policy`` for an agent with ``tools``; empty when it loads."""
    out: list[str] = []
    if not 0 < policy.low_water_chars < policy.high_water_chars < policy.context_limit_chars:
        out.append("0 < low_water_chars < high_water_chars < context_limit_chars must hold")
    if not policy.read_block_cap_chars < policy.context_limit_chars:
        out.append("read_block_cap_chars must be below context_limit_chars")
    if min(policy.result_cap_chars, policy.read_cap_chars) < _MIN_CAP:
        out.append(f"result_cap_chars and read_cap_chars are at least {_MIN_CAP}")
    if policy.keep_recent_pairs < 1:
        out.append("keep_recent_pairs must be at least 1")
    unknown = sorted(set(policy.exempt_tools) - set(tools))
    if unknown:
        out.append(f"exempt_tools names tools the agent does not have: {unknown}")
    r = policy.resume
    if r.min_prior_turns < 1 or r.headroom_chars < 0 or r.loop_repeats < 2:
        out.append("resume.min_prior_turns >= 1, resume.headroom_chars >= 0, loop_repeats >= 2")
    if not 0 <= r.max_stale_permille <= 1000:
        out.append("resume.max_stale_permille must lie between 0 and 1000")
    s = policy.summary
    if s is not None:
        if not s.alias:
            out.append("summary.alias must name a generation alias")
        if s.keep_recent_pairs < 1 or s.max_per_instance < 0:
            out.append("summary.keep_recent_pairs >= 1 and summary.max_per_instance >= 0")
        if not 0 < s.trigger(policy) < policy.context_limit_chars:
            out.append("summary.trigger_chars must lie between 0 and context_limit_chars")
    return out
