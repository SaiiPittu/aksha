"""Input values (ADR-P27): the three lanes, a queued input and the event and refusal names.

A **lane** is what a person's message to an open run is: ``steer`` (rendered before the
target instance's next model call), ``follow_up`` (rendered when the instance proposes to
finish, as a veto of that finish) or ``amend`` (a correction of the run's last answered
question, which supersedes the answer and reaches invalidation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

Lane = Literal["steer", "follow_up", "amend"]
LANES: frozenset[str] = frozenset(get_args(Lane))
"""The three lanes (D1); no lane is a default for a bare message."""

RENDERED_LANES: frozenset[str] = frozenset({"steer", "follow_up"})
"""The lanes whose text reaches a model call; an amendment is applied when it commits."""

ConsumedBy = Literal["context", "stop_policy", "amend"]
"""What consumed an input: a model step's context, a stop decision's veto, an amendment."""

INPUT_QUEUED = "input.queued"
INPUT_CONSUMED = "input.consumed"
INTERRUPT_AMENDED = "interrupt.amended"

INPUT_PREFIX = "IN"
"""Input IDs are ``IN-0001``, counted from the owner's log."""

LANE_UNKNOWN = "lane_unknown"
TEXT_REQUIRED = "text_required"
RUN_ENDED = "run_ended"
INSTANCE_NOT_OPEN = "instance_not_open"
NOTHING_TO_AMEND = "nothing_to_amend"
ANSWERS_REQUIRED = "answers_required"


@dataclass(frozen=True)
class QueuedInput:
    """One ``input.queued`` fact: a person's message for ``run`` in ``lane``.

    ``instance`` is the Task instance it targets, or None for whichever of the run's
    instances calls the model (``steer``) or proposes to finish (``follow_up``) first.
    ``seq`` is the fact's position in the owner's log.
    """

    input_id: str
    run: str
    instance: str | None
    lane: str
    text: str
    seq: int
