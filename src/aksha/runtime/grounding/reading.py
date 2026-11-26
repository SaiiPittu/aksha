"""The reader one attempt of a tool body sees: it keeps every text it returned (ADR-P13 D1).

:class:`AttemptReader` wraps the composition root's :class:`RecordReader` for one call
of one body. The agent loop hands it to the body in its ``ToolContext`` and, once the
body returns, mints the body's span drafts against :attr:`AttemptReader.returned`, so a
body can cite only a passage the runtime itself read for it, in that attempt. Without a
configured reader every read returns None, and a draft is refused as before.
"""

from __future__ import annotations

from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.grounding.types import ReturnedText


class AttemptReader:
    """One attempt's reader: delegates to ``inner`` and records each text it returned."""

    def __init__(self, inner: RecordReader | None) -> None:
        """Read through ``inner`` (None: no source is configured, every read is None)."""
        self._inner = inner
        self._returned: list[ReturnedText] = []

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's text, kept for minting; None when the record or the reader is absent."""
        if self._inner is None:
            return None
        text = self._inner.read(source, record_id)
        if text is not None:
            self._returned.append(text)
        return text

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The record IDs of ``collection``; listing returns no text, so it mints nothing."""
        if self._inner is None:
            return ()
        return tuple(self._inner.records(source, collection))

    @property
    def returned(self) -> tuple[ReturnedText, ...]:
        """Every text this attempt's reads returned, in order."""
        return tuple(self._returned)
