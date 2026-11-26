"""Fault switches (ADR-P22 D9): seeded rules, kept outside every fixture, that fail a tool.

A rule names a tool (and optionally one source path), a stable error code and a chance in
parts per million. :func:`decide` is pure: the same seed, step and attempt always give the
same answer, so a restarted episode fails the same call again, and two rollouts with
different seeds can differ. That is the only way fake-mode rollouts differ, which is why a
fake-mode pass^k measures the harness's recovery from seeded faults and nothing more.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

PPM = 1_000_000


@dataclass(frozen=True)
class FaultRule:
    """Fail calls of ``tool`` (on ``path`` only, when given) with ``code``."""

    tool: str
    code: str = "timeout"
    probability_ppm: int = PPM
    path: str | None = None

    def matches(self, tool: str, path: str | None) -> bool:
        """Whether this rule applies to a call of ``tool`` on ``path``."""
        return self.tool == tool and (self.path is None or self.path == path)


@dataclass(frozen=True)
class FaultProfile:
    """A named set of fault rules; its digest goes on every result row."""

    name: str
    rules: tuple[FaultRule, ...] = ()

    @property
    def digest(self) -> str:
        """``sha256:`` over the rules, so a result row names the exact profile."""
        body = repr(sorted((r.tool, r.code, r.probability_ppm, r.path or "") for r in self.rules))
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def decide(rule: FaultRule, seed: int, step_id: str, attempt: int) -> bool:
    """Whether ``rule`` fires on this step and attempt under ``seed``. Pure."""
    if rule.probability_ppm >= PPM:
        return True
    if rule.probability_ppm <= 0:
        return False
    digest = hashlib.sha256(f"{seed}|{step_id}|{attempt}|{rule.tool}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % PPM < rule.probability_ppm
