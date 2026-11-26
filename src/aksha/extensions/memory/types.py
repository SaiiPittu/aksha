"""Memory values (ADR-P09): the spec an agent declares, recall's answer and an episode.

Every value is frozen data. ``MemorySpec`` is what an agent declares to turn memory on
(D10); ``RecallHit`` and ``RecallResult`` are what ``recall`` returns as data (D8);
``Episode`` is one past run as the ``episodes`` projection reads it back (D6), never stored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

MemoryKind = Literal["memory.preference", "memory.fact", "memory.procedure"]
"""The three memory kinds, by full name (the extension's namespace is ``memory``)."""

MEMORY_KINDS: tuple[MemoryKind, ...] = get_args(MemoryKind)

Veracity = Literal["stated", "derived", "tool"]
"""Who the cited source says a record came from (D7): a person's statement, an artifact
version, or a source that carries instruction-like text, which recall scores lower."""

Outcome = Literal["done", "partial", "failed", "stopped"]
"""An episode's outcome, read from ``run.ended``."""

TOOL_NAMES: tuple[str, ...] = ("recall", "remember", "forget")
"""The tools ``MemoryExtension.apply`` adds to the agent, as the agent's own tools."""

_SUBJECT = re.compile(r"^[A-Za-z0-9_:.*-]+$")


class MemoryRefused(ValueError):
    """``apply`` or a spec refused: a stable ``code`` and a message."""

    def __init__(self, code: str, message: str) -> None:
        """Keep the code; the message is the error text."""
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class MemorySpec:
    """What an agent declares to turn memory on (D10); off when an agent declares none.

    ``subjects`` are the keys a record may take; a ``*`` in one matches a run of letters,
    digits, ``_``, ``-``, ``.`` and ``:`` (``repo:*.test_command``). ``intake_tasks`` are
    top-level Task IDs; each gets ``recall`` as its forced first call (D3).
    ``fact_sources`` are the artifact kinds (full names) a fact or procedure may cite; recall
    reads them to see whether a cited version has moved (D5) and whether its content
    carries instruction-like text (D7). ``recall_char_cap`` caps the texts of the hits
    returned, about 5,000 tokens at 4 characters per token (D8).
    """

    subjects: tuple[str, ...]
    write: frozenset[MemoryKind] = frozenset({"memory.preference", "memory.fact"})
    intake_tasks: tuple[str, ...] = ()
    fact_sources: frozenset[str] = frozenset()
    recall_limit: int = 8
    recall_char_cap: int = 20_000
    min_score: int = 1
    text_cap_chars: int = 2_000

    def __post_init__(self) -> None:
        """Refuse a spec ``apply`` could not honour."""
        if not self.subjects:
            raise MemoryRefused("spec_invalid", "a memory spec declares at least one subject")
        bad = [s for s in self.subjects if not _SUBJECT.match(s)]
        if bad:
            raise MemoryRefused("spec_invalid", f"not a subject pattern: {bad[0]!r}")
        unknown = sorted(set(self.write) - set(MEMORY_KINDS))
        if unknown or not self.write:
            raise MemoryRefused("spec_invalid", f"write names memory kinds only: {unknown}")
        if not self.intake_tasks:
            raise MemoryRefused("spec_invalid", "memory enters a run through an intake Task")
        for name, value in (
            ("recall_limit", self.recall_limit),
            ("recall_char_cap", self.recall_char_cap),
            ("min_score", self.min_score),
            ("text_cap_chars", self.text_cap_chars),
        ):
            if value < 1:
                raise MemoryRefused("spec_invalid", f"{name} is at least 1, not {value}")

    def matches(self, subject: str) -> bool:
        """Whether ``subject`` is one of ``subjects``, a ``*`` matching one run of name text."""
        return any(_pattern(s).fullmatch(subject) for s in self.subjects)


def _pattern(declared: str) -> re.Pattern[str]:
    parts = (re.escape(p) for p in declared.split("*"))
    return re.compile("[A-Za-z0-9_.:-]+".join(parts))


@dataclass(frozen=True)
class RecallHit:
    """One recalled record: its version, what it says, who said it and how it scored."""

    ref: str
    kind: MemoryKind
    subject: str
    text: str
    veracity: Veracity
    score: int
    flags: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """The hit as ``recall``'s data holds it."""
        out: dict[str, Any] = {
            "ref": self.ref,
            "kind": self.kind,
            "subject": self.subject,
            "text": self.text,
            "veracity": self.veracity,
            "score": self.score,
        }
        if self.flags:
            out["flags"] = list(self.flags)
        return out


@dataclass(frozen=True)
class RecallResult:
    """What ``recall`` returns (D8).

    ``skipped_invalidated`` counts facts recall left out because what they cite has moved
    (D5); ``truncated`` says the caps cut the list.
    """

    hits: tuple[RecallHit, ...]
    skipped_invalidated: int
    truncated: bool
    result_set_hash: str

    def as_json(self) -> dict[str, Any]:
        """The result as the step records it and the model reads it."""
        return {
            "hits": [h.as_json() for h in self.hits],
            "skipped_invalidated": self.skipped_invalidated,
            "truncated": self.truncated,
            "result_set_hash": self.result_set_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RecallResult:
        """Read :meth:`as_json`'s form back."""
        hits = tuple(
            RecallHit(
                ref=str(h["ref"]),
                kind=h["kind"],
                subject=str(h["subject"]),
                text=str(h["text"]),
                veracity=h["veracity"],
                score=int(h["score"]),
                flags=tuple(h.get("flags", ())),
            )
            for h in value["hits"]
        )
        return cls(
            hits, int(value["skipped_invalidated"]), bool(value["truncated"]),
            str(value["result_set_hash"]),
        )  # fmt: skip


@dataclass(frozen=True)
class Episode:
    """One past run of the agent for this tenant (D6): a projection, never stored.

    ``completed`` lists Task instances whose completion check passed; ``outputs`` the
    artifact versions the run saved; ``blockers`` what the run ended blocked on.
    """

    run_id: str
    agent: str
    outcome: Outcome
    completed: tuple[str, ...]
    blockers: tuple[str, ...]
    outputs: tuple[str, ...]
