"""The status reads and the SSE stream: each opens a read-only store handle, never boots."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse

from aksha.interfaces import wire
from aksha.interfaces.http.auth import ReadTenant, signed_in
from aksha.interfaces.http.common import API, Context, error
from aksha.runtime.projections.view import load_view
from aksha.runtime.session.service import receipts
from aksha.runtime.session.streams import (
    EventItem,
    Heartbeat,
    Resync,
    StreamItem,
    Synchronized,
    encode,
    subscribe,
)
from aksha.runtime.session.views import inspect_run, run_statuses, session_status

router = APIRouter(prefix=API)
SignedInOnly = Annotated[None, Depends(signed_in)]


def sse(item: StreamItem) -> str:
    """One stream item as SSE text."""
    if isinstance(item, Heartbeat):
        return ": heartbeat\n\n"
    data = json.dumps(encode(item), separators=(",", ":"), sort_keys=True)
    if isinstance(item, EventItem):
        return f"id: {item.seq}\nevent: {item.event_type}\ndata: {data}\n\n"
    if isinstance(item, Synchronized | Resync):
        kind = "synchronized" if isinstance(item, Synchronized) else "resync"
        return f"id: {item.seq}\nevent: {kind}\ndata: {data}\n\n"
    return f"event: transient\ndata: {data}\n\n"


@router.get("/health")
def health(context: Context) -> dict[str, Any]:
    """Liveness, and the runtime thread's faults; needs no sign-in."""
    faults = [f.__dict__ for f in context.runtime.faults]
    return {"ok": context.runtime.alive, "faults": faults}


@router.get("/capabilities")
def caps(context: Context, _: SignedInOnly) -> dict[str, Any]:
    """The capability record (D7)."""
    return context.capability_record()


@router.get("/sessions/{session}")
def session(session: str, context: Context, tenant: ReadTenant) -> dict[str, Any]:
    """The session's status."""
    store = context.open_reader()
    try:
        return wire.session_to_json(session_status(load_view(store, tenant), session))
    finally:
        store.close()


@router.get("/runs")
def runs(context: Context, tenant: ReadTenant) -> dict[str, Any]:
    """Every run's status."""
    store = context.open_reader()
    try:
        statuses = run_statuses(load_view(store, tenant))
        return {"runs": [wire.run_to_json(r) for r in statuses]}
    finally:
        store.close()


@router.get("/runs/{run}", response_model=None)
def run(run: str, context: Context, tenant: ReadTenant) -> dict[str, Any] | JSONResponse:
    """The run inspector's report."""
    store = context.open_reader()
    try:
        report = inspect_run(load_view(store, tenant), run)
    finally:
        store.close()
    if report is None:
        return error(404, "not_found", f"no run {run}")
    return dict(report)


@router.get("/receipts/{command_id}", response_model=None)
def receipt(command_id: str, context: Context,
            tenant: ReadTenant) -> dict[str, Any] | JSONResponse:  # fmt: skip
    """A stored receipt."""
    store = context.open_reader()
    try:
        found = receipts(store, tenant, [command_id]).get(command_id)
    finally:
        store.close()
    if found is None:
        return error(404, "not_found", f"no receipt for {command_id}")
    return {"receipt": wire.receipt_to_json(found)}


@router.get("/verify")
def verify(context: Context, _: SignedInOnly) -> dict[str, Any]:
    """The hash chain check (ADR-P01 H)."""
    store = context.open_reader()
    try:
        broken = store.verify()
        seq, head = store.head()
    finally:
        store.close()
    if broken is None:
        return {"ok": True, "seq": seq, "head": head}
    return {"ok": False, "seq": seq, "break": {"seq": broken.seq, "reason": broken.reason}}


@router.get("/sessions/{session}/events")
def events(
    session: str,
    context: Context,
    tenant: ReadTenant,
    after: int | None = None,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """SSE from ``?after=`` or ``Last-Event-ID``."""
    cursor = after
    if cursor is None and last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)
    store = context.open_reader()

    def stream() -> Iterator[str]:
        try:
            for item in subscribe(store, tenant, session, cursor,
                                  heartbeat_s=context.heartbeat_s,
                                  stopped=context.closing.is_set):  # fmt: skip
                yield sse(item)
        finally:
            store.close()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)
