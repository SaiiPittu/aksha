"""The event envelope: ``NewEvent`` (what a caller appends) and ``Event`` (what was stored).

``Event`` adds the fields the event store fills on append: ``seq``, ``owner``, ``at`` from
the injected clock, ``boot`` and ``schema_version`` (ADR-P01 Interface).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NewEvent:
    """A fact to append. The store validates it and fills the rest of the envelope.

    ``dedup_key`` is optional: when the event type has a template the store renders it,
    and a key the caller passes must equal the rendered one.
    """

    type: str
    payload: Mapping[str, Any]
    run: str | None = None
    instance: str | None = None
    step: str | None = None
    session: str | None = None
    dedup_key: str | None = None

    def context(self) -> dict[str, str | None]:
        """The envelope fields a dedup-key template may name (the owner is added on append)."""
        return {
            "run": self.run,
            "instance": self.instance,
            "step": self.step,
            "session": self.session,
        }


@dataclass(frozen=True, kw_only=True)
class Event(NewEvent):
    """A stored fact: a ``NewEvent`` plus what the store filled in.

    ``seq`` is global and strictly increasing in commit order. ``payload`` is deeply
    frozen. ``kw_only`` lets these required fields follow ``NewEvent``'s defaults.
    """

    seq: int
    owner: str
    at: str
    boot: int
    schema_version: int
