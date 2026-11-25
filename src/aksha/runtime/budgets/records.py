"""What each recorded fact counts for: the one reading every budget and limit reducer shares.

The reducers in ``meter.py`` and ``limits.py`` read facts other primitives write. The
fields they read are fixed here, so a writer has one list to keep:

==============================  =====================================================
Fact                            Counts as
==============================  =====================================================
``stop.decided``                a model turn when ``verdict`` is ``continue``, its
                                ``cause`` is not ``resume`` and ``resumes_turn`` is not
                                true; distinct by (instance, ``turn``) when it names one
``stop.decided{cause: resume}``  a resume record (the restart limit; an automatic start)
``stop.decided`` with           a stop veto: a completion check refused a finish
``candidate: finish`` and       of the deciding instance (``candidate_instance``, when
``verdict: continue``           present, names it)
``task.entered``,               the start of an instance entry (the veto count restarts)
``completion_check.reset``
``step.started{kind: tool}``    a tool call when ``counted`` is true (a retry adds none)
``step.completed{refused}``     a tool call when ``counted`` is true (``budget_exhausted``
                                is not)
``step.failed``,                active time: ``duration_ms``, plus ``backoff_ms`` of a
``step.completed``              retried failure
``step.completed{kind: model}``  tokens and money: ``usage``
``step.completed{kind: tool,``   progress, for the restart limit
``outcome: ok}``,
``artifact.saved``
``run.started{cause: rebuild}``  an automatic start
==============================  =====================================================

Every function is pure: it reads one event and returns what it counts for.
"""

from __future__ import annotations

from aksha.runtime.events.envelope import Event

STOP_DECIDED = "stop.decided"
TASK_ENTERED = "task.entered"
CHECK_RESET = "completion_check.reset"
VETO_COUNT_RESETS = frozenset({TASK_ENTERED, CHECK_RESET})
"""The facts after which an instance's stop vetoes are counted from zero again."""
RESUME = "resume"
"""The ``cause`` of a resume record: the stop decision recovery writes (ADR-P02 D7)."""

AUTOMATIC_CAUSES = frozenset({"resume", "continuation", "timer"})
"""Stop-decision causes the runtime writes with no person's command (ADR-P14 D7, S3)."""


def _verdict(e: Event) -> str:
    return str(e.payload.get("verdict", "")).lower()


def is_resume_record(e: Event) -> bool:
    """A stop decision recovery wrote for a turn end a crash swallowed."""
    return e.type == STOP_DECIDED and e.payload.get("cause") == RESUME


def charged_turn(e: Event) -> tuple[str, ...] | None:
    """The key of the model turn ``e`` authorises, or None when it charges no turn.

    Two records naming one (instance, turn) charge it once, so a turn a restart
    authorises again is never paid twice.
    """
    if e.type != STOP_DECIDED or _verdict(e) != "continue":
        return None
    if e.payload.get("cause") == RESUME or e.payload.get("resumes_turn") is True:
        return None
    turn = e.payload.get("turn")
    if turn is None:
        return ("seq", str(e.seq))
    return ("turn", e.instance or "", str(turn))


def is_veto(e: Event) -> bool:
    """A completion check refused a proposed finish, so the instance took another turn.

    The finish must be the deciding instance's own: when one evaluation pops the finishing
    instance and continues another (``candidate_instance`` names the first), the second
    instance's CONTINUE is its first turn, not a veto (ADR-P16 D3).
    """
    if e.type != STOP_DECIDED or e.payload.get("candidate") != "finish":
        return False
    if _verdict(e) != "continue":
        return False
    of = e.payload.get("candidate_instance")
    return of is None or of == e.instance


def is_progress(e: Event) -> bool:
    """A completed tool step or a saved artifact: what resets the restart limit (S2)."""
    if e.type == "artifact.saved":
        return True
    p = e.payload
    return e.type == "step.completed" and p.get("kind") == "tool" and p.get("outcome") == "ok"


def is_automatic_start(e: Event) -> bool:
    """A start the runtime made without a person's command: resume, rebuild, continuation."""
    if e.type == "run.started":
        return e.payload.get("cause") == "rebuild"
    return e.type == STOP_DECIDED and e.payload.get("cause") in AUTOMATIC_CAUSES


def active_ms(e: Event) -> int:
    """The active time ``e`` records: an attempt's duration plus a retry's backoff."""
    if e.type not in ("step.failed", "step.completed"):
        return 0
    p = e.payload
    spent = int(p.get("duration_ms", 0))
    if e.type == "step.failed" and p.get("retried") is True:
        spent += int(p.get("backoff_ms", 0))
    return spent


def usage(e: Event) -> tuple[int, int]:
    """``(tokens, cost_micro_usd)`` a model step reported, or zeros (ADR-P14 D9)."""
    if e.type != "step.completed" or e.payload.get("kind") != "model":
        return 0, 0
    u = e.payload.get("usage") or {}
    tokens = int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
    return tokens, int(u.get("cost_micro_usd", 0))
