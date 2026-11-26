"""``select_backend``: one backend chosen at run start from side-effect-free probes (D2).

``choice`` is ``auto`` (the strongest available backend among those ``auto`` may pick, in
the order given), ``refuse`` or a backend name. A cloud backend is left out of ``auto``,
so a program goes to a cloud only when the operator names that backend (decision 37).
A named backend that does not probe available is not replaced by
another: the choice falls to ``refuse`` with the probe's reason, so a missing sandbox is
never a silent fallback to a weaker one. Every probe result is returned for the record
(decision 9).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.ports import Sandbox
from aksha.runtime.sandbox.refuse import REFUSE, RefuseSandbox
from aksha.runtime.sandbox.types import STRENGTHS, Availability

AUTO = "auto"


def select_backend(
    choice: str,
    available: Mapping[str, Callable[[], Sandbox]],
    auto: tuple[str, ...] | None = None,
) -> tuple[Sandbox, tuple[Availability, ...]]:
    """The chosen backend and every probe made (module doc). Raises ``sandbox_invalid``.

    ``auto`` names the backends ``auto`` may pick (default: all of ``available``).
    """
    if choice == REFUSE:
        box = RefuseSandbox("AKSHA_SANDBOX chose the refuse backend")
        return box, (box.probe(),)
    if choice != AUTO and choice not in available:
        names = ", ".join(sorted({AUTO, REFUSE, *available}))
        raise SandboxRefused("sandbox_invalid", f"no sandbox backend {choice!r}; use {names}")
    pool = list(available) if auto is None else [n for n in available if n in auto]
    chosen = pool if choice == AUTO else [choice]
    built = [available[n]() for n in chosen]
    probes = tuple(b.probe() for b in built)
    usable = [(b, p) for b, p in zip(built, probes, strict=True) if p.available]
    if not usable:
        why = "; ".join(f"{p.backend}: {p.reason}" for p in probes) or "no backend is built"
        return RefuseSandbox(why), probes
    best = max(usable, key=lambda bp: STRENGTHS.index(bp[1].strength))
    return best[0], probes
