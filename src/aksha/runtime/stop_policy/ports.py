"""The stop-policy Protocol (ADR-P16 D1): a pure judge with three methods.

Every implementation is one of the runtime's built-ins (``core.py``, ``builtins.py``); an
agent never writes one (decision 10). Each is a frozen dataclass built fresh from the
definition at every evaluation, and reads only the ``StopView`` and the candidate.
"""

from __future__ import annotations

from typing import Protocol

from aksha.runtime.stop_policy.types import Prep, StopCandidate, StopView, Verdict


class StopPolicy(Protocol):
    """One level of the stop-policy stack."""

    @property
    def ref(self) -> str:
        """The policy's ID in the stack and the trail: ``run:R-0001``, ``task:extract[C1]``."""
        ...

    def prepare(self, view: StopView) -> Prep:
        """What this policy adds to the next model request. Called outer to inner."""
        ...

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """Answer one candidate. Called inner to outer."""
        ...

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """Replace a proposed CONTINUE this level cannot pay for, or None. Outer to inner."""
        ...
