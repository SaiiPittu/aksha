"""Where a recorder appends: a folder of JSON Lines files, one per stream.

A recording folder holds ``models.jsonl``, ``search.jsonl`` and ``fetch.jsonl``: each line
one answer, appended as it is given and flushed, so a run that dies keeps every answer it
was given. :func:`read_stream` reads a stream back in recorded order.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

STREAMS = ("models", "search", "fetch")


class Sink:
    """Appends JSON objects to a recording's streams (base: records nothing)."""

    def append(self, stream: str, item: Mapping[str, Any]) -> None:
        """Append ``item`` to ``stream``."""


class JsonlSink(Sink):
    """A recording folder's streams as ``<stream>.jsonl`` files (module doc)."""

    def __init__(self, folder: Path) -> None:
        """Append into ``folder`` (created when missing)."""
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.counts: dict[str, int] = {}

    def append(self, stream: str, item: Mapping[str, Any]) -> None:
        """One line, sorted keys, flushed before returning."""
        if stream not in STREAMS:
            raise ValueError(f"no recording stream {stream!r}")
        line = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with (self.folder / f"{stream}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.counts[stream] = self.counts.get(stream, 0) + 1


def read_stream(folder: Path, stream: str) -> Iterator[dict[str, Any]]:
    """Every line of ``stream`` in ``folder``, in recorded order; none when it is absent."""
    path = folder / f"{stream}.jsonl"
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)
