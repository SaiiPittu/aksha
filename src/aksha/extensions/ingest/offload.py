"""Oversized inputs become documents with a reference (ADR D10, design note section 5).

- A text tool result at or under ``Caps.result_cap_chars`` is unchanged (None here).
- One over the cap becomes a one-version document, ``origin="tool_result"``, whose blob
  is the ``sha256:`` of the result's UTF-8 bytes: the same ref the blob store gives the
  full output (ADR-P04 D10), so nothing is stored twice. Its view keeps the head and the
  tail (ADR-P08 D4) and adds the header with the reference.
- A user message over ``Caps.message_cap_chars`` is stored the same way, ``origin=
  "message"``; its view starts with the message's head and its header keeps instruction
  authority (no data rule), because it is the person's own message.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from aksha.extensions.ingest.header import render_header
from aksha.extensions.ingest.text import plain
from aksha.extensions.ingest.types import Caps, Document, Representation


@dataclass(frozen=True)
class Offloaded:
    """An oversized text stored as a document, and the view a step shows instead."""

    document: Document
    representation: Representation
    view: str


def _blob(data: bytes, put: Callable[[bytes], str] | None) -> str:
    return put(data) if put is not None else "sha256:" + hashlib.sha256(data).hexdigest()


def offload_result(
    step_id: str,
    text: str,
    *,
    doc_key: str,
    caps: Caps | None = None,
    put: Callable[[bytes], str] | None = None,
    more: str | None = None,
) -> Offloaded | None:
    """``text`` as a document when it is over the result cap; None when it fits (module doc)."""
    caps = caps or Caps()
    if len(text) <= caps.result_cap_chars:
        return None
    data = text.encode("utf-8")
    doc = Document(doc_key, 1, _blob(data, put), step_id, "tool_result", step_id, None,
                   "text/plain", "utf-8", None, len(data))  # fmt: skip
    rep = plain(text)
    head, tail = text[: caps.head_chars], text[-caps.tail_chars :]
    shown = f"first {len(head):,} and last {len(tail):,} characters"
    header = render_header(doc, rep, shown=shown, more=more)
    hidden = len(text) - len(head) - len(tail)
    view = f"{header}\n\n{head}\n[... {hidden:,} characters not shown ...]\n{tail}"
    return Offloaded(doc, rep, view)


def offload_message(
    text: str,
    *,
    doc_key: str,
    caps: Caps | None = None,
    put: Callable[[bytes], str] | None = None,
    more: str | None = None,
) -> Offloaded | None:
    """A user message as a document when it is over the message cap; None when it fits."""
    caps = caps or Caps()
    if len(text) <= caps.message_cap_chars:
        return None
    data = text.encode("utf-8")
    doc = Document(doc_key, 1, _blob(data, put), "message", "message", None, None,
                   "text/plain", "utf-8", None, len(data))  # fmt: skip
    rep = plain(text)
    head = text[: caps.message_cap_chars]
    header = render_header(doc, rep, shown=f"characters 1 to {len(head):,}", more=more)
    return Offloaded(doc, rep, f"{header}\n\n{head}")
