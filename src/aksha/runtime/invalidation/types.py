"""Invalidation (ADR-P05): a change invalidates exactly the work whose lineage reaches it.

It will hold the invalidation view projection, early cutoff, and the rebuild entries a
``source.changed`` produces. Built so far: ``VersionedSourceSpec``, the declaration of a
versioned source an agent or extension contributes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VersionedSourceSpec:
    """A versioned source: its name, the folder it reads (``root``) and its collections.

    The loader refuses a ``root`` inside an agent folder (ADR-P20 S3), because every file
    there enters the agent definition version hash. ``max_age_s`` is ADR-P25's.
    """

    name: str
    root: str | None = None
    collections: tuple[str, ...] = ()
    max_age_s: int | None = None
