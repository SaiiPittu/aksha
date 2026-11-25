"""Anchoring the hash chain (ADR-P01 H7): copy the head to a sink, verify against the sink.

Two helpers, with no command line around them: :func:`anchor_head` publishes the store's
current head to an :class:`~aksha.runtime.storage.ports.AnchorSink`, and
:func:`verify_against` verifies the store against every anchor that sink holds for it.
An anchor is only as good as its sink: a file the writer can rewrite proves little, while
another machine or a write-once (WORM) bucket makes truncation and a re-hashed rewrite
detectable. Scheduling (every N events, at every run end) is left to the caller.
"""

from __future__ import annotations

from aksha.runtime.canonical import iso_utc
from aksha.runtime.execution.ports import Clock
from aksha.runtime.storage.chain import Anchor, ChainBreak
from aksha.runtime.storage.ports import AnchorSink, EventStore


def anchor_head(store: EventStore, sink: AnchorSink, clock: Clock) -> Anchor:
    """Publish ``store``'s head ``(seq, hash)`` to ``sink`` and return the stored anchor."""
    seq, hash_ = store.head()
    anchor = Anchor(
        store_id=store.identity().store_id,
        seq=seq,
        hash=hash_,
        at=iso_utc(clock.now()),
        sink=sink.name,
    )
    return sink.publish(anchor)


def verify_against(store: EventStore, sink: AnchorSink) -> ChainBreak | None:
    """``store.verify()`` with every anchor ``sink`` holds for this store; None if all agree."""
    return store.verify(anchors=sink.read(store.identity().store_id))
