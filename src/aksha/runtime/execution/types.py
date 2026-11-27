"""Step values: ``StepId``, ``StepSemantics``, ``StepPolicy``, ``StepOutput``, ``StepResult``.

A step ID such as ``R-0002/compare[C1]/t03/c02`` is computed from the run's structure,
never from a clock or a random number (ADR-P02 D2), so a restarted process finds "the
second call of turn 3" again and returns its recorded result. The grammar is v2 05a
section 2.7, without the walk and special segments (those belong to the stop policy):

.. code-block:: text

    step_id  := run [ "/" instance ( "/" child )* [ "/" turn [ "/" call [ "/" sub ]
                                                                | "/" turnsub ]
                                                  | "/" check ] ]
    run      := "R-" 4 or more digits                R-0002
    instance := task [ "[" key "]" ]                 compare[C1], scope
    turn     := "t" 2 or more digits, from 1         t03
    call     := "c" 2 or more digits, from 1         c02, the position in the model's reply
    sub      := "d" 2 digits | "derived"             d01, a fast model-class step
    turnsub  := "compact"                            t12/compact, a summary compaction step
    check    := "k" 2 or more digits, from 1         k01, a completion check

``turnsub`` follows a turn and nothing follows it: the recorded summary step that runs
before that turn's model call (ADR-P08 D7).

A Task may not be named like a reserved segment (:func:`is_reserved_segment`); the
loader refuses it (``task_id_reserved``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from aksha.runtime.budgets.backoff import jittered, next_backoff_ms
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.storage.rows import ArtifactRow

StepSemantics = Literal["at_least_once", "reconcile", "at_most_once"]
"""How a step left dangling by a crash is finished (D4).

``at_least_once``: safe to repeat. ``reconcile``: look the idempotency key up in the
system of record. ``at_most_once``: never repeated once dispatched; the outcome is
recorded as ``interrupted`` with code ``outcome_unknown``.
"""

STEP_SEMANTICS: frozenset[str] = frozenset(get_args(StepSemantics))
"""The three names, for checks of values that arrive untyped from an agent module."""

StepKind = Literal["model", "tool", "check"]
"""What a step is: a model call, a tool call or a completion check."""

OnUnknown = Literal["tell_model", "interrupt"]
"""What follows an ``outcome_unknown``: the model reads it, or a person is asked (P17)."""

Outcome = Literal[
    "ok", "error", "refused", "interrupted", "skipped", "cancelled", "effects_unknown"
]
"""How a step ended. ``interrupted`` is an at-most-once step whose outcome is unknown;
``skipped``, ``cancelled`` and ``effects_unknown`` are a person's stop (ADR-P18 D5, D6)."""

USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "cost_micro_usd"})
"""What a model step may report spending on ``step.completed{usage}`` (ADR-P14 D9)."""

OUTCOME_UNKNOWN = "outcome_unknown"
"""The code of an ``interrupted`` step: it was dispatched and may or may not have landed."""

_RUN = re.compile(r"^R-[0-9]{4,}$")
_INSTANCE = re.compile(r"^(?P<task>[a-z][a-z0-9_]*)(\[(?P<key>[^/\[\]]+)\])?$")
_NUMBERED = re.compile(r"^(?P<letter>[tck])(?P<n>[0-9]{2,})$")
_SUB = re.compile(r"^(d[0-9]{2}|derived)$")
_RESERVED = re.compile(r"^[tckwxd][0-9]+$")
_TURN_SUB = "compact"
_RESERVED_NAMES = frozenset({"end", "handoff", "derived", _TURN_SUB})


def is_reserved_segment(name: str) -> bool:
    """True for a name the step-ID grammar keeps: ``t03``, ``c1``, ``w043``, ``end``, ..."""
    return bool(_RESERVED.match(name)) or name in _RESERVED_NAMES


def _instance_refusal(instance: str) -> str | None:
    for segment in instance.split("/"):
        m = _INSTANCE.match(segment)
        if m is None:
            return f"{segment!r} is not a Task name with an optional [key]"
        if is_reserved_segment(m.group("task")):
            return f"{segment!r} is a reserved step-ID segment"
    return None


@dataclass(frozen=True)
class StepId:
    """A deterministic step ID: run, Task instance, turn, call and an optional sub-step.

    ``check`` names a completion check of the instance instead of a turn. Every field is
    checked on construction, so an invalid ID never reaches the event store.
    """

    run: str
    instance: str | None = None
    turn: int | None = None
    call: int | None = None
    sub: str | None = None
    check: int | None = None

    def __post_init__(self) -> None:
        """Refuse a value the grammar cannot print and parse back (``step_id_invalid``)."""
        problem = self._refusal()
        if problem is not None:
            raise StepIdInvalid(message=problem)

    def _refusal(self) -> str | None:
        if not _RUN.match(self.run):
            return f"run {self.run!r} is not R- followed by four or more digits"
        if self.instance is None:
            if any(v is not None for v in (self.turn, self.call, self.sub, self.check)):
                return "a turn, call, sub-step or check needs a Task instance"
            return None
        problem = _instance_refusal(self.instance)
        if problem is not None:
            return problem
        for name, value in (("turn", self.turn), ("call", self.call), ("check", self.check)):
            if value is not None and value < 1:
                return f"{name} {value} is counted from 1"
        if self.check is not None and self.turn is not None:
            return "a check belongs to the instance, not to a turn"
        if self.call is not None and self.turn is None:
            return "a call needs a turn"
        if self.sub == _TURN_SUB:
            if self.turn is None or self.call is not None:
                return f"sub-step {_TURN_SUB!r} follows a turn and no call"
        elif self.sub is not None and (self.call is None or not _SUB.match(self.sub)):
            return f"sub-step {self.sub!r} needs a call and is d<2 digits> or derived"
        return None

    def __str__(self) -> str:
        """The printed ID, ``R-0002/compare[C1]/t03/c02``."""
        parts = [self.run]
        if self.instance is not None:
            parts.append(self.instance)
        if self.turn is not None:
            parts.append(f"t{self.turn:02d}")
        if self.call is not None:
            parts.append(f"c{self.call:02d}")
        if self.sub is not None:
            parts.append(self.sub)
        if self.check is not None:
            parts.append(f"k{self.check:02d}")
        return "/".join(parts)

    @classmethod
    def parse(cls, text: str) -> StepId:
        """The inverse of ``str``; ``StepIdInvalid`` for anything the grammar does not make."""
        segments = text.split("/")
        if "" in segments:
            raise StepIdInvalid(message=f"{text!r} has an empty segment")
        run, rest = segments[0], segments[1:]
        names: list[str] = []
        while rest and not is_reserved_segment(rest[0].split("[", 1)[0]):
            names.append(rest.pop(0))
        numbers: dict[str, int] = {}
        sub: str | None = None
        for segment in rest:
            m = _NUMBERED.match(segment)
            letter = m.group("letter") if m else None
            if m and letter and letter not in numbers and _in_order(letter, numbers, sub):
                numbers[letter] = int(m.group("n"))
            elif sub is None and _sub_fits(segment, numbers):
                sub = segment
            else:
                raise StepIdInvalid(message=f"{text!r}: {segment!r} is not expected here")
        return cls(
            run,
            "/".join(names) if names else None,
            turn=numbers.get("t"),
            call=numbers.get("c"),
            sub=sub,
            check=numbers.get("k"),
        )

    def with_turn(self, turn: int) -> StepId:
        """This instance's step for ``turn``."""
        return StepId(self.run, self.instance, turn=turn)

    def with_call(self, call: int) -> StepId:
        """This turn's step for ``call``."""
        return StepId(self.run, self.instance, turn=self.turn, call=call)

    def compaction(self) -> StepId:
        """This turn's summary compaction step, ``.../t12/compact`` (ADR-P08 D7)."""
        return StepId(self.run, self.instance, turn=self.turn, sub=_TURN_SUB)


def _sub_fits(segment: str, seen: Mapping[str, int]) -> bool:
    """A call's sub-step after ``c``, or ``compact`` straight after ``t``."""
    if "c" in seen:
        return bool(_SUB.match(segment))
    return set(seen) == {"t"} and segment == _TURN_SUB


def _in_order(letter: str, seen: Mapping[str, int], sub: str | None) -> bool:
    """``t`` first, then ``c`` after ``t``; ``k`` alone; nothing after a sub-step."""
    if sub is not None or "k" in seen:
        return False
    if letter == "t" or letter == "k":
        return not seen
    return letter == "c" and set(seen) == {"t"}


@dataclass(frozen=True)
class StepPolicy:
    """Timeout, retries, backoff and crash meaning of one step (ADR-P02 Interface).

    Retries are counted from the event store, so a restart never resets them. An
    ``at_most_once`` step is never retried: running it again is what it forbids.

    ``jitter`` cuts each backoff by up to that share, drawn from ``jitter_source`` (none
    by default, so the delays are the same every run). ``wait_cap_ms`` bounds the step's
    active time (its attempts and the backoffs between them, as budget accounting charges
    them): a retry whose backoff would reach it is not made, and the step ends with the
    failure's code. None is no bound beyond ``max_retries``. The model step sets it to what
    is left of the run's active-time budget (ADR-P14 amendment B2).
    """

    timeout_ms: int = 10_000
    max_retries: int = 2
    retry_on: frozenset[str] = frozenset({"timeout", "transient"})
    backoff: Backoff = field(default_factory=Backoff)
    semantics: StepSemantics = "at_least_once"
    on_unknown: OnUnknown = "tell_model"
    counted: bool = True
    wait_cap_ms: int | None = None
    jitter: float = 0.0
    jitter_source: Callable[[], float] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Refuse a semantics name outside the three, and negative limits."""
        if self.semantics not in STEP_SEMANTICS:
            raise ValueError(f"semantics {self.semantics!r} is not one of {sorted(STEP_SEMANTICS)}")
        if self.timeout_ms <= 0 or self.max_retries < 0:
            raise ValueError("timeout_ms is positive and max_retries is not negative")
        if self.wait_cap_ms is not None and self.wait_cap_ms < 0:
            raise ValueError("wait_cap_ms is not negative")
        if not 0.0 <= self.jitter <= 1.0 or (self.jitter > 0 and self.jitter_source is None):
            raise ValueError("jitter is a share from 0 to 1, drawn from an injected jitter_source")

    def delay_ms(self, attempt: int, retry_after_ms: int | None = None) -> int:
        """The backoff after failed ``attempt`` (1-based): ``base * factor ** (attempt - 1)``.

        With ``jitter``, the delay is cut by a random share of up to that. ``retry_after_ms``
        is how long the failure asked to be left alone (a provider's ``Retry-After``): the
        backoff is at least that, up to :data:`RETRY_AFTER_CEILING_MS`.
        """
        delay = next_backoff_ms(attempt, self.backoff)
        if self.jitter > 0 and self.jitter_source is not None:
            delay = jittered(delay, self.jitter, self.jitter_source())
        if retry_after_ms is not None and retry_after_ms > 0:
            delay = max(delay, min(retry_after_ms, RETRY_AFTER_CEILING_MS))
        return delay


RETRY_AFTER_CEILING_MS = 600_000
"""The longest wait a failure's ``Retry-After`` is honoured for (ten minutes); the step's
``wait_cap_ms`` (the run's active-time budget) bounds it further."""


@dataclass(frozen=True)
class Deadline:
    """When an attempt's time runs out, on the real monotonic clock (``Clock.mono_ms``)."""

    at_ms: int
    mono_ms: Callable[[], int] = field(compare=False, repr=False)

    def remaining_ms(self) -> int:
        """Milliseconds left, never negative."""
        return max(0, self.at_ms - self.mono_ms())

    def expired(self) -> bool:
        """Whether the time is up."""
        return self.remaining_ms() == 0


@dataclass(frozen=True)
class StepOutput:
    """What a step's work returns: its recorded value and the facts that commit with it.

    ``value`` is JSON data, recorded on ``step.completed`` and returned on a restart
    instead of running the work again. ``outcome="error"`` is a result the step itself
    reports (a tool's ``Err``), recorded and never retried; it needs a ``code``.
    ``events`` and ``artifacts`` commit in the same transaction as ``step.completed``
    (boundary U2): saves, relations, an interrupt. ``usage`` is what a model step reports
    spending (``input_tokens``, ``output_tokens``, ``cost_micro_usd``), which budget
    accounting reads (ADR-P14 D9). ``error`` is what an error outcome's provider said
    (``step.completed{error}``, ADR-P07 G6): its error type, status and a short message.
    """

    value: Mapping[str, Any] | None = None
    outcome: Literal["ok", "error"] = "ok"
    code: str | None = None
    events: tuple[NewEvent, ...] = ()
    artifacts: tuple[ArtifactRow, ...] = ()
    usage: Mapping[str, int] | None = None
    error: Mapping[str, str | int] | None = None

    def __post_init__(self) -> None:
        """An error names its code; usage holds only the three known counts, none negative."""
        if self.outcome == "error" and not self.code:
            raise ValueError("an error output needs a code")
        if self.usage is not None:
            unknown = set(self.usage) - USAGE_FIELDS
            if unknown or any(not isinstance(n, int) or n < 0 for n in self.usage.values()):
                raise ValueError(f"usage holds {sorted(USAGE_FIELDS)} as counts, not {self.usage}")


@dataclass(frozen=True)
class StepResult:
    """How one step ended, as recorded. ``replayed`` is True when nothing ran this time."""

    step_id: StepId
    outcome: Outcome
    value: Mapping[str, Any] | None
    attempts: int
    replayed: bool
    code: str | None = None


RunClass = Literal["stop_requested", "definition_changed", "suspended", "held", "in_flight"]
"""What recovery makes of an open run (D6), judged in this order.

``held`` is an in-flight run whose agent a person's stop holds (ADR-P18 D4); ADR-P02 D6
does not name it yet. ``suspended_with_rebuilds`` (S1) needs P05 and is not built. A run
with both a stop request and a changed definition is ``stop_requested`` (ADR-P18 D3 judges
the stop first), while ADR-P02 D7 lists the changed definition first: the two ADRs
disagree, and this order waits on the maintainer's call."""


@dataclass(frozen=True)
class DanglingStep:
    """A step with a ``step.started`` and no ``step.completed``: a crash interrupted it."""

    step_id: str
    run: str | None
    instance: str | None
    kind: str
    name: str | None
    semantics: StepSemantics
    on_unknown: OnUnknown
    dispatched: bool
    attempts: int
