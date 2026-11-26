"""The ingestion port (ADR ingestion extension, D1, D2): uploads go through an ingester.

The runtime never parses a file. When the composition root configures a
:class:`DocumentIngester`, ``UploadInputs`` hands it each file's bytes and writes what it
returns to the input workspace: the text the model reads under the same path (for a
converted file, a header that says what was done, then the representation), plus one
record per attachment. The facts it returns (``document.stored``, ``document.gap``,
``document.refused``) commit with the upload's source change, in one transaction (U8).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from aksha.runtime.inputs.types import IngestedInput, KnownDocument


class DocumentIngester(Protocol):
    """Turns uploaded bytes into what the input workspace serves, with the facts to record.

    ``known`` is every input path's latest document (its doc key, version and blob), so a
    path uploaded again keeps its doc key and moves its version only when the bytes moved.
    ``first_key`` is the number the next new doc key takes (``D-<first_key:04d>``).
    ``put`` stores bytes in the blob store and returns their ref; None stores nothing.
    """

    def ingest(
        self,
        files: Mapping[str, bytes],
        known: Mapping[str, KnownDocument],
        first_key: int,
        put: Callable[[bytes], str] | None,
    ) -> tuple[IngestedInput, ...]:
        """One entry per input path to write, attachments included."""
        ...
