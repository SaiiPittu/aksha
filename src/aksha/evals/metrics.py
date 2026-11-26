"""The numbers: pass@k, pass^k, k_effective, Wilson intervals, cost per pass, clusters.

pass@k is the chance that at least one of k rollouts passes; pass^k the chance that all k
pass. Both use the unbiased estimators over n rollouts with c passes (Chen and others,
2021): ``pass@k = 1 - C(n - c, k) / C(n, k)`` and ``pass^k = C(c, k) / C(n, k)``,
undefined (NaN) when n < k.

pass^k is reported only where ``k_effective > 1``, the number of distinct normalised traces
among the rollouts: a deterministic fake gives identical rollouts, so pass^k is pass^1 by
construction and the report says so instead of printing a reliability number.

Clustered standard errors group case scores by world cluster (Miller, arXiv 2411.00640);
with fewer than two clusters there is no interval, only counts.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass


def pass_at_k(c: int, n: int, k: int) -> float:
    """The unbiased pass@k over ``n`` rollouts with ``c`` passes; NaN when ``n < k``."""
    if n < k or k < 1:
        return math.nan
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(c: int, n: int, k: int) -> float:
    """The unbiased pass^k (all k pass) over ``n`` rollouts with ``c`` passes."""
    if n < k or k < 1:
        return math.nan
    return math.comb(c, k) / math.comb(n, k)


def k_effective(trace_hashes: Sequence[str]) -> int:
    """The number of distinct normalised traces among the rollouts."""
    return len(set(trace_hashes))


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval; (0, 1) for no rollouts."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Cost:
    """Cost per passing episode in steps, with the median and maximum per rollout."""

    total: int
    passes: int
    median: float
    maximum: int

    @property
    def per_pass(self) -> float:
        """Total steps over passes; infinite when nothing passed."""
        return math.inf if self.passes == 0 else self.total / self.passes


def cost_per_pass(steps: Sequence[int], passed: Sequence[bool]) -> Cost:
    """Charge every rollout's steps to the rollouts that passed (eval design 9)."""
    if len(steps) != len(passed):
        raise ValueError("one pass flag per rollout")
    return Cost(
        total=sum(steps),
        passes=sum(passed),
        median=statistics.median(steps) if steps else 0.0,
        maximum=max(steps, default=0),
    )


def clustered_se(scores: Sequence[float], clusters: Sequence[str]) -> tuple[float | None, int]:
    """The cluster-robust standard error of the mean score, and the cluster count.

    None below two clusters: with one world there is nothing to cluster over.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for s, c in zip(scores, clusters, strict=True):
        groups[c].append(s)
    g = len(groups)
    if g < 2:
        return None, g
    n = len(scores)
    mean = sum(scores) / n
    total = sum(sum(x - mean for x in xs) ** 2 for xs in groups.values())
    return math.sqrt(g / (g - 1) * total) / n, g


@dataclass(frozen=True)
class FailureCluster:
    """Failed criteria grouped by signature, so forty lines from two causes print as two."""

    signature: str
    count: int
    examples: tuple[str, ...]


def failure_clusters(failures: Sequence[tuple[str, str, str, str]]) -> list[FailureCluster]:
    """Group ``(criterion, layer, first evidence, where)`` failures by their signature."""
    groups: dict[str, list[str]] = defaultdict(list)
    for criterion, layer, evidence, where in failures:
        head = evidence.split(":", 1)[0] if evidence else ""
        groups[f"{layer} {criterion} {head}".strip()].append(where)
    return sorted(
        (FailureCluster(sig, len(w), tuple(w[:3])) for sig, w in groups.items()),
        key=lambda f: (-f.count, f.signature),
    )
