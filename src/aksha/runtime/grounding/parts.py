"""Record parts: the version of one part of a record, the same wherever it is computed.

A tool that reads only some rows of a table records what it read as a *part*, so a change
to rows it never read moves nothing it built on (ADR-P05 D6). A part is a record ID with a
fragment, ``<record>#<selector>``, and its version is a hash of exactly the part's raw
text. A source adapter answers the part's current version with :func:`part_version` over
the record's current text, and a tool records the version :func:`part_version` gave it
over the text it read, so both always agree.

Selectors:

``where:<column>=<value>[|<value>...]``
    The rows of a CSV record whose ``column`` holds one of the values, with the header
    line. The part's text is the header row and each matching row's raw text, in file
    order. A row is one logical CSV row: a quoted field may hold line breaks, and its
    continuation lines belong to the row. A record that is not CSV, or has no such
    column, has no such part (None).

Any other fragment names the whole record: its version is the record's own.
"""

from __future__ import annotations

import csv
import hashlib
import io

WHERE = "where:"


def where_part(record: str, column: str, values: list[str] | tuple[str, ...]) -> str:
    """The part ID of ``record``'s CSV rows whose ``column`` is one of ``values``."""
    return f"{record}#{WHERE}{column}={'|'.join(values)}"


def part_text(text: str, fragment: str) -> str | None:
    """The raw text of the part ``fragment`` names in ``text`` (module doc); None if none."""
    if not fragment.startswith(WHERE):
        return text
    column, sep, raw_values = fragment[len(WHERE) :].partition("=")
    if not sep:
        return None
    values = set(raw_values.split("|"))
    rows = _rows(text)
    if not rows:
        return None
    header = next(csv.reader(io.StringIO(rows[0])), [])
    if column not in header:
        return None
    at = header.index(column)
    kept = [rows[0]]
    for row in rows[1:]:
        cells = next(csv.reader(io.StringIO(row)), [])
        if at < len(cells) and cells[at] in values:
            kept.append(row)
    return "".join(kept)


def _rows(text: str) -> list[str]:
    """``text``'s logical CSV rows, each as its raw text.

    A row ends at a line break outside quotes, so a quoted field's line breaks stay inside
    it (an escaped quote is two quotes, which keeps the count's parity).
    """
    rows: list[str] = []
    pending = ""
    for line in text.splitlines(keepends=True):
        pending += line
        if pending.count('"') % 2 == 0:
            rows.append(pending)
            pending = ""
    if pending:
        rows.append(pending)
    return rows


def part_version(text: str, fragment: str, whole: str) -> str | None:
    """The version of part ``fragment`` of a record whose text is ``text``.

    ``whole`` is the record's own version, which a fragment naming the whole record has.
    """
    if not fragment.startswith(WHERE):
        return whole
    found = part_text(text, fragment)
    if found is None:
        return None
    return "part:" + hashlib.sha256(found.encode("utf-8")).hexdigest()
