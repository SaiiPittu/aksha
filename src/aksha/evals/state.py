"""The graded state: a pure fold of a bundle's events into what the graders compare.

:func:`fold` is the projection the graders read. It keeps the latest version of every
output (and every version, for change cases), every relation, flag, approval and send, the
tool calls, the change sets applied and how the episode ended. A malformed event raises
:class:`FoldError`, which the gate reports as ``store_fold_failed``.

A citation is any mapping with a ``file`` and an ``exact`` (and code-point ``start`` and
``end`` when the agent knows them), wherever it sits: an output's ``citations``, a field's
``citations`` inside the content, or a relation's ``excerpt``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class FoldError(Exception):
    """An event the fold cannot apply: the gate's ``store_fold_failed``."""


@dataclass(frozen=True)
class Output:
    """One saved output version."""

    ref: str
    kind: str
    key: str
    version: int
    content: Mapping[str, Any]
    citations: tuple[Mapping[str, Any], ...]

    @property
    def versioned(self) -> str:
        """``<kind>/<key>@v<n>``."""
        return f"{self.ref}@v{self.version}"


@dataclass(frozen=True)
class Relation:
    """One typed relation, with its excerpt when it carries one."""

    type: str
    src: str
    dst: str
    excerpt: Mapping[str, Any] | None
    label: str | None


@dataclass(frozen=True)
class Approval:
    """One approval request and its decision."""

    approval_id: str
    target_ref: str
    target_version: int
    action: str
    granted: bool | None


@dataclass
class GradedState:
    """Everything the graders read, folded from one bundle."""

    started: Mapping[str, Any] = field(default_factory=dict)
    outputs: dict[str, Output] = field(default_factory=dict)
    history: dict[str, list[Output]] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    flags: list[Mapping[str, Any]] = field(default_factory=list)
    approvals: dict[str, Approval] = field(default_factory=dict)
    sends: list[Mapping[str, Any]] = field(default_factory=list)
    tool_calls: list[Mapping[str, Any]] = field(default_factory=list)
    changes: list[Mapping[str, Any]] = field(default_factory=list)
    ended: Mapping[str, Any] | None = None

    def of_kind(self, kind: str) -> list[Output]:
        """The latest version of every output of ``kind``, by key."""
        return sorted((o for o in self.outputs.values() if o.kind == kind), key=lambda o: o.key)

    def get(self, kind: str, key: str) -> Output | None:
        """The latest ``<kind>/<key>``, or None."""
        return self.outputs.get(f"{kind}/{key}")

    def citations(self) -> Iterator[tuple[str, Mapping[str, Any]]]:
        """Every citation with where it sits: an output, a relation's excerpt or a flag's text."""
        for out in self.outputs.values():
            for c in out.citations:
                yield out.versioned, c
            for c in walk_citations(out.content):
                yield out.versioned, c
        for rel in self.relations:
            if rel.excerpt is not None:
                yield f"relation:{rel.type}:{rel.src}->{rel.dst}", rel.excerpt
        for flag in self.flags:
            if flag.get("file"):
                yield f"flag:{flag['kind']}", {"file": flag["file"], "exact": flag["text"]}

    def texts(self) -> Iterator[tuple[str, str]]:
        """Every string an output saved, with its ref: what the text checks scan."""
        for out in self.outputs.values():
            for s in walk_strings(out.content):
                yield out.versioned, s

    @property
    def has_action(self) -> bool:
        """Whether the agent left anything a grader could credit."""
        return bool(self.outputs or self.relations or self.sends)


def walk_citations(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every mapping with ``file`` and ``exact`` inside a JSON-shaped ``value``."""
    if isinstance(value, Mapping):
        if "file" in value and "exact" in value:
            yield value
            return
        for v in value.values():
            yield from walk_citations(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from walk_citations(v)


def walk_strings(value: Any) -> Iterator[str]:
    """Every string value (not key) inside ``value``, skipping citations' quoted text."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        if "file" in value and "exact" in value:
            return
        for v in value.values():
            yield from walk_strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from walk_strings(v)


def fold(events: Sequence[Mapping[str, Any]]) -> GradedState:
    """The graded state of a bundle's events. Pure; raises :class:`FoldError`."""
    state = GradedState()
    last_seq = 0
    for e in events:
        try:
            seq, type_, p = int(e["seq"]), str(e["type"]), e["payload"]
        except (KeyError, TypeError, ValueError):
            raise FoldError(f"event without seq, type or payload: {e!r}") from None
        if seq <= last_seq:
            raise FoldError(f"seq {seq} after {last_seq}: events out of order")
        last_seq = seq
        try:
            _apply(state, type_, p)
        except (KeyError, TypeError, ValueError) as err:
            raise FoldError(f"seq {seq} {type_}: {err!r}") from None
    return state


def _apply(state: GradedState, type_: str, p: Mapping[str, Any]) -> None:
    if type_ == "episode.started":
        state.started = p
    elif type_ == "episode.output_saved":
        out = Output(
            ref=str(p["ref"]),
            kind=str(p["kind"]),
            key=str(p["key"]),
            version=int(p["version"]),
            content=p["content"],
            citations=tuple(p.get("citations", ())),
        )
        state.outputs[out.ref] = out
        state.history.setdefault(out.ref, []).append(out)
    elif type_ == "episode.relation_saved":
        state.relations.append(
            Relation(str(p["type"]), str(p["src"]), str(p["dst"]), p.get("excerpt"), p.get("label"))
        )
    elif type_ == "episode.flagged":
        state.flags.append(p)
    elif type_ == "episode.approval_requested":
        aid = str(p["approval_id"])
        state.approvals[aid] = Approval(
            aid, str(p["target_ref"]), int(p["target_version"]), str(p["action"]), None
        )
    elif type_ == "episode.approval_decided":
        aid = str(p["approval_id"])
        a = state.approvals[aid]
        state.approvals[aid] = Approval(
            a.approval_id, a.target_ref, a.target_version, a.action, bool(p["granted"])
        )
    elif type_ == "episode.effect_sent":
        state.sends.append(p)
    elif type_ == "episode.tool_called":
        state.tool_calls.append(p)
    elif type_ == "episode.change_applied":
        state.changes.append(p)
    elif type_ == "episode.ended":
        state.ended = p
