"""Session event streams with a cursor, a resync and a catch-up marker (ADR-P19 D5, D6).

:func:`subscribe` is the one way an interface follows a session:

1. Replay: every stored event of the owner with a sequence number above ``after_seq``
   that belongs to the session, as :class:`EventItem`, in commit order. An event belongs
   to a session when its envelope names the session, when it belongs to a run the session
   started (``run.started`` with that session), or when it names neither a session nor a
   run (an owner-wide fact such as ``source.changed``, which every session's panels read).
2. If the replay would hold more than ``max_replay`` items, one :class:`Resync` at the
   head replaces it: the client refetches every view and moves its cursor there.
3. :class:`Synchronized` once, at the head the replay read to: the client has caught up.
4. Live: every later commit of the session, and a :class:`Heartbeat` after each quiet
   ``heartbeat_s``, until ``stopped()`` says so or the consumer closes the iterator.

:class:`Transient` items (live token or tool output) are never stored and never replayed
(D6); this module only defines their shape. :func:`decode` is the client half of
forward-compatible decoding: an item type the client does not know becomes
:class:`Unknown`, which advances the cursor and changes no view.

Payloads leave out the text of citation spans (``excerpts.minted``); an interface reads
quotes through the artifact views (v2 contract 4).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aksha.runtime.canonical import thaw
from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.ports import EventStore

STREAM_SCHEMA_VERSION = 1
"""Bumped only when an item type is renamed or removed (D5); adding one is compatible."""

DEFAULT_HEARTBEAT_S = 15.0
"""A quiet stream sends a heartbeat this often, so a proxy keeps the connection open."""

REDACTED_SPAN_FIELDS = frozenset({"text"})
"""Span fields a stream leaves out of ``excerpts.minted`` (the quoted source text)."""


@dataclass(frozen=True)
class EventItem:
    """One stored event as a client sees it; ``seq`` is the cursor it moves to."""

    seq: int
    event_type: str
    run: str | None
    instance: str | None
    step: str | None
    session: str | None
    at: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class Resync:
    """The replay was too large: refetch every view, then set the cursor to ``seq``."""

    seq: int


@dataclass(frozen=True)
class Synchronized:
    """Replay has reached the live tail at ``seq``; sent once per subscription."""

    seq: int


@dataclass(frozen=True)
class Transient:
    """A live delta (model text, tool output) while a step runs: never stored (D6)."""

    run: str
    step: str
    channel: Literal["text", "tool_output"]
    data: str


@dataclass(frozen=True)
class Heartbeat:
    """Nothing new; the stream is alive."""


@dataclass(frozen=True)
class Unknown:
    """Client side only: an item whose type this client does not know (D5)."""

    seq: int | None
    raw_type: str


StreamItem = EventItem | Resync | Synchronized | Transient | Heartbeat
"""What :func:`subscribe` yields."""

ITEM_KINDS = ("event", "resync", "synchronized", "transient", "heartbeat")
"""The ``kind`` of each item in its wire form, in the order of :data:`StreamItem`."""


def _redacted(event: Event) -> Mapping[str, Any]:
    payload = thaw(event.payload)
    if event.type == "excerpts.minted":
        spans = payload.get("spans") or []
        payload["spans"] = [
            {k: v for k, v in span.items() if k not in REDACTED_SPAN_FIELDS} for span in spans
        ]
    return dict(payload)


def event_item(event: Event) -> EventItem:
    """The stream item of a stored event, with span text left out."""
    return EventItem(
        seq=event.seq,
        event_type=event.type,
        run=event.run,
        instance=event.instance,
        step=event.step,
        session=event.session,
        at=event.at,
        payload=_redacted(event),
    )


class SessionFilter:
    """Which of an owner's events belong to one session (module doc, item 1).

    It learns the session's runs from ``run.started`` as events pass through it, so it
    must see every event of the owner in order from the start of the log.
    """

    def __init__(self, session: str) -> None:
        """Follow ``session``."""
        self.session = session
        self._runs: set[str] = set()

    def admits(self, event: Event) -> bool:
        """Whether ``event`` belongs to the session; records the runs the session starts."""
        if event.session == self.session:
            if event.type == "run.started" and event.run is not None:
                self._runs.add(event.run)
            return True
        if event.run is not None:
            return event.run in self._runs
        return event.session is None


def subscribe(
    store: EventStore,
    owner: str,
    session: str,
    after_seq: int | None = None,
    *,
    max_replay: int | None = None,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    stopped: Callable[[], bool] = lambda: False,
) -> Generator[StreamItem]:
    """Replay, then the marker, then live items of ``session`` (module doc)."""
    cursor = after_seq or 0
    seen = SessionFilter(session)
    replay: list[EventItem] = []
    head = 0
    for event in store.read(owner):
        head = event.seq
        if seen.admits(event) and event.seq > cursor:
            replay.append(event_item(event))
    if max_replay is not None and len(replay) > max_replay:
        yield Resync(head)
    else:
        yield from replay
    yield Synchronized(head)
    subscription = store.subscribe(owner, after_seq=head)
    try:
        while not stopped():
            events = subscription.wait(heartbeat_s)
            if not events:
                yield Heartbeat()
                continue
            for event in events:
                if seen.admits(event):
                    yield event_item(event)
    finally:
        subscription.close()


def encode(item: StreamItem) -> dict[str, Any]:
    """The wire form of ``item``: a JSON object with its ``kind``."""
    if isinstance(item, EventItem):
        return {
            "kind": "event",
            "seq": item.seq,
            "type": item.event_type,
            "run": item.run,
            "instance": item.instance,
            "step": item.step,
            "session": item.session,
            "at": item.at,
            "payload": dict(item.payload),
        }
    if isinstance(item, Resync):
        return {"kind": "resync", "seq": item.seq}
    if isinstance(item, Synchronized):
        return {"kind": "synchronized", "seq": item.seq}
    if isinstance(item, Transient):
        return {"kind": "transient", "run": item.run, "step": item.step,
                "channel": item.channel, "data": item.data}  # fmt: skip
    return {"kind": "heartbeat"}


def decode(raw: Mapping[str, Any], known: frozenset[str]) -> StreamItem | Unknown:
    """A wire item back as a value; an unknown item kind or event type is :class:`Unknown`.

    ``known`` is the event types the client understands. An unknown one keeps its
    ``seq``, so the client still moves its cursor past it.
    """
    kind = str(raw.get("kind", ""))
    seq = raw.get("seq")
    if kind == "event":
        event_type = str(raw.get("type", ""))
        if event_type not in known:
            return Unknown(int(seq) if isinstance(seq, int) else None, event_type)
        return EventItem(
            seq=int(raw["seq"]),
            event_type=event_type,
            run=raw.get("run"),
            instance=raw.get("instance"),
            step=raw.get("step"),
            session=raw.get("session"),
            at=str(raw.get("at", "")),
            payload=dict(raw.get("payload") or {}),
        )
    if kind == "resync" and isinstance(seq, int):
        return Resync(seq)
    if kind == "synchronized" and isinstance(seq, int):
        return Synchronized(seq)
    if kind == "heartbeat":
        return Heartbeat()
    if kind == "transient":
        channel = raw.get("channel")
        if channel in ("text", "tool_output"):
            return Transient(str(raw["run"]), str(raw["step"]), channel, str(raw["data"]))
    return Unknown(int(seq) if isinstance(seq, int) else None, kind)
