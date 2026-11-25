"""``render_result``: the capped model view of one tool outcome, made when it is recorded. Pure.

The view is the canonical JSON of the result. Over the cap it is cut in the middle, head
and tail kept, with a marker saying how much was cut; the full size and its SHA-256 go on
``step.completed``, so a rebuilt context recomputes nothing (D7).
"""

from __future__ import annotations

import hashlib
from typing import Any

from aksha.runtime.canonical import canonical_json
from aksha.runtime.tools.types import Ok, ToolOutcome, ToolResultView

DEFAULT_RESULT_CAP_CHARS = 8_000
"""The model view's cap in characters (code points) when the context policy sets none."""

_MIN_CAP = 64


def _marker(cut: int) -> str:
    return f"\n[... {cut} characters cut ...]\n"


def cap_text(text: str, cap_chars: int) -> tuple[str, bool]:
    """``text`` cut to at most ``cap_chars``, head and tail kept, and whether it was cut."""
    if cap_chars < _MIN_CAP:
        raise ValueError(f"cap_chars must be at least {_MIN_CAP}, got {cap_chars}")
    if len(text) <= cap_chars:
        return text, False
    keep = cap_chars - len(_marker(len(text)))
    head = keep - keep // 2
    tail = keep // 2
    return text[:head] + _marker(len(text) - keep) + text[len(text) - tail :], True


def _shown(outcome: ToolOutcome) -> dict[str, Any]:
    result = outcome.result
    if isinstance(result, Ok):
        return {"status": "ok", "data": result.data}
    return {
        "status": "error",
        "code": result.code,
        "message": result.message,
        "paths": list(result.paths),
        "item": result.item,
    }


def render_result(
    outcome: ToolOutcome, cap_chars: int = DEFAULT_RESULT_CAP_CHARS
) -> ToolResultView:
    """The model view of ``outcome``, capped at ``cap_chars``."""
    full = canonical_json(_shown(outcome))
    text, truncated = cap_text(full, cap_chars)
    return ToolResultView(
        status="ok" if isinstance(outcome.result, Ok) else "error",
        text=text,
        truncated=truncated,
        full_chars=len(full),
        full_hash="sha256:" + hashlib.sha256(full.encode("utf-8")).hexdigest(),
    )
