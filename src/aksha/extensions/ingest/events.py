"""The payloads of the three ``document.*`` facts (design note 6.2), built from the records.

The event types are in the core catalogue (``events/catalogue.py``, writer ``inputs``),
because an upload commits them with its source change (U8) before any agent loads. These
builders keep every payload to integers, strings and lists, well under the 64 KiB cap:
the text itself lives in the input workspace and the original in the blob store.
"""

from __future__ import annotations

from typing import Any

from aksha.extensions.ingest.ingest import Ingested
from aksha.extensions.ingest.types import Refusal


def stored(item: Ingested, path: str) -> dict[str, Any]:
    """``document.stored`` for ``item``, served at input ``path``."""
    payload: dict[str, Any] = {**item.document.as_json(), "path": path}
    rep = item.representation
    if rep is not None:
        payload["representation"] = {
            "chars": len(rep.text),
            "anchors": len(rep.anchors),
            "converters": list(rep.converters),
            "dropped": list(rep.dropped),
            "served": "as_given" if item.as_given else "converted",
        }
    if item.route is not None:
        route: dict[str, Any] = {"kind": item.route.kind}
        if item.route.reason is not None:
            route["reason"] = item.route.reason
        sent = item.route.sent
        if sent is not None:
            route.update(profile=sent.profile_id, sent=sent.blob, width=sent.sent_wh[0],
                         height=sent.sent_wh[1], tokens=sent.est_tokens,
                         passed_through=sent.passed_through)  # fmt: skip
        payload["route"] = route
    return payload


def gap(item: Ingested, path: str) -> dict[str, Any]:
    """``document.gap`` for ``item`` (which has a gap)."""
    assert item.gap is not None
    return {"doc_key": item.document.doc_key, "version": item.document.version,
            "path": path, "reason": item.gap.reason, "detail": item.gap.detail,
            "steps": list(item.gap.steps)}  # fmt: skip


def refused(refusal: Refusal, path: str | None = None) -> dict[str, Any]:
    """``document.refused`` for a cap that failed before storing."""
    payload: dict[str, Any] = {"cap": refusal.cap, "value": refusal.value,
                               "limit": refusal.limit, "origin": refusal.origin,
                               "name": refusal.name_as_given}  # fmt: skip
    if path is not None:
        payload["path"] = path
    return payload
