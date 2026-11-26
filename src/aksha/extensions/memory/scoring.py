"""Recall scoring (ADR-P09 D8) and the text checks ``remember`` runs (D7).

The score is corpus-independent: a record's score is the number of distinct query terms
its own subject and text contain, so a new, unrelated record can never change another
recall's answer, and a replay ranks the same records the same way. A scorer that weighs
terms by the whole corpus (BM25 and the like) would break both. A record of veracity
``tool`` scores one less. Ties sort by ref, so the order is total.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence

from aksha.runtime.api import instruction_text

_TERM = re.compile(r"[a-z0-9]+")

_KEY_SHAPES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{16,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}",
        r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}",
    )
)
"""Shapes of credentials a record's text must not keep: a fixed, conservative list."""

REDACTED = "[redacted]"


def terms(text: str) -> frozenset[str]:
    """The distinct lower-case letter and digit runs of ``text``."""
    return frozenset(_TERM.findall(text.lower()))


def score(query: frozenset[str], subject: str, text: str, *, tool: bool) -> int:
    """How many query terms the record holds; one less for a ``tool`` record, never below 0."""
    found = len(query & (terms(subject) | terms(text)))
    return max(0, found - 1) if tool else found


def redact(text: str) -> str:
    """``text`` with every credential shape replaced by ``[redacted]``."""
    for shape in _KEY_SHAPES:
        text = shape.sub(REDACTED, text)
    return text


def flagged(texts: Iterable[str]) -> bool:
    """Whether any text reads like an instruction to the model (the context policy's list)."""
    return any(instruction_text(t) for t in texts)


def result_set_hash(scored: Sequence[tuple[str, int]]) -> str:
    """``sha256:<hex>`` over the ranked ``(ref, score)`` pairs: what a replay must equal."""
    data = json.dumps([list(p) for p in scored], separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()
