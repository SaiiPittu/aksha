"""The edit-time check (decision 38, mode ``edit_guard``): never write over a change unseen.

A tool that declares ``ToolSpec.edits`` names one record of a versioned source it edits or
overwrites. In a run whose agent declared ``edit_guard``, ``notify`` or ``rebuild``, the
runtime checks that record before the body runs, inside the tool's durable step, so a
resumed step returns the recorded answer and never checks again:

1. The current version is asked from the run's record reader. A record that does not
   exist yet is created freely: there is nothing to overwrite.
2. The version the instance last read is its last ``read.recorded`` of that record ID, in
   log order, whatever generation of reads it is in. A body that writes the record
   returns the version it wrote in ``Ok.reads``, so the instance's next edit is checked
   against its own write.
3. A record whose current version differs from the version last read is refused
   ``stale_edit``: someone else changed it since the instance read it.
4. A whole-record overwrite (``EditSpec.overwrite``) of an existing record the instance
   never read is refused ``unread_overwrite``. An edit of a record never read is let
   through, because an edit quotes the text it changes and fails on its own when that
   text is not there.
5. With no record reader the target cannot be checked, and the edit is refused
   ``edit_unchecked`` (fail closed).

Each refusal says what to do and carries the current version and text, so the model can
re-read once and retry, rather than resend stale text in a loop. Nothing is rebased: a
refused edit changes nothing. The check never records a read; only the model's re-read
does, so lineage keeps saying what the agent actually read.

Known limit: reads are recorded once per version, so after the sequence read v1, read v2,
the record goes back to v1, the last recorded read is still v2 and the edit is refused
until the instance reads it again.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.policy.rules import argument_at
from aksha.runtime.projections.view import LogView
from aksha.runtime.tools.types import EditSpec, Err

STALE_EDIT = "stale_edit"
"""The code of an edit refused because the target changed since the instance read it."""

UNREAD_OVERWRITE = "unread_overwrite"
"""The code of a whole-record overwrite refused because the instance never read the target."""

EDIT_UNCHECKED = "edit_unchecked"
"""The code of an edit refused because the run has no record reader to check it with."""

EDIT_REFUSALS = frozenset({STALE_EDIT, UNREAD_OVERWRITE, EDIT_UNCHECKED})
"""Every code the check refuses with; none of them is retried."""

TEXT_LIMIT_CHARS = 4_000
"""The most of the current text a refusal quotes; the rest is named by its size."""


def target_of(edit: EditSpec, args: Mapping[str, Any]) -> str | None:
    """The record ID the call edits, from its arguments; None when the argument is missing."""
    found, value = argument_at(args, edit.record)
    if not found or not isinstance(value, str) or not value:
        return None
    return value if edit.collection is None else f"{edit.collection}/{value}"


def last_read(view: LogView, run: str, instance: str, record_id: str) -> str | None:
    """The version of ``record_id`` the instance read last (rule 2); None when never read."""
    out: str | None = None
    for e in view.events:
        if e.type != "read.recorded" or e.run != run or e.instance != instance:
            continue
        read = InputRef.from_json(e.payload["input"])
        if read.kind == "record" and read.id == record_id:
            out = read.version
    return out


def edit_refusal(
    view: LogView,
    reader: RecordReader | None,
    *,
    tool: str,
    edit: EditSpec,
    args: Mapping[str, Any],
    run: str,
    instance: str,
) -> Err | None:
    """The refusal of this edit by rules 1 to 5 (module doc), or None to let it run."""
    record_id = target_of(edit, args)
    if record_id is None:
        return None  # the arguments were validated: a bad target is the body's to refuse
    if reader is None:
        message = f"{tool}: {record_id} cannot be checked before the edit (no record reader)"
        return Err(EDIT_UNCHECKED, message, item=record_id)
    current = reader.read(edit.source, record_id)
    if current is None:
        return None
    seen = last_read(view, run, instance, record_id)
    if seen is None and not edit.overwrite:
        return None
    if seen == current.version:
        return None
    if seen is None:
        head = (
            f"{tool}: refused to overwrite {record_id}, which exists and has not been read "
            f"in this task. Read it first, then write it again with its current content in "
            f"mind. It is at version {current.version}"
        )
        return Err(UNREAD_OVERWRITE, _with_text(head, current.text), item=record_id)
    head = (
        f"{tool}: refused, {record_id} changed since this task read it (read at version "
        f"{seen}, now {current.version}). Nothing was written. Read it again and redo the "
        f"edit against the current content"
    )
    return Err(STALE_EDIT, _with_text(head, current.text), item=record_id)


def _with_text(head: str, text: str) -> str:
    shown = text[:TEXT_LIMIT_CHARS]
    more = len(text) - len(shown)
    tail = f"\n[... {more} more characters]" if more > 0 else ""
    return f"{head}. Its current text:\n{shown}{tail}"
