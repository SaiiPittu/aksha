"""Anchor sinks: places outside the event store that keep copies of its chain hashes.

Each implements :class:`aksha.runtime.storage.ports.AnchorSink` (ADR-P01 H7). Built: a
file sink. A sink is worth having only if the store's writer cannot rewrite it, so
production wants another machine or a write-once (WORM) bucket; those come later.
"""
