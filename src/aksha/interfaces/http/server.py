"""The HTTP adapter of the session API: JSON commands and reads, and one SSE stream per session.

Every write goes to the runtime thread (the single writer) as one named command and
returns its receipt at commit (ADR-P19 D3); every read opens a read-only store handle and
never boots. The owner is the ``X-Aksha-User`` header, the web twin of ``--user``;
another owner's ID reads as unknown (D9). Paths are under ``/api/v1``:

=======  ==========================================  =========================================
Method   Path                                        What
=======  ==========================================  =========================================
GET      ``/health``                                 liveness, and the runtime thread's faults
GET      ``/capabilities``                           the capability record (D7)
POST     ``/commands``                               any named command (``wire`` module doc)
POST     ``/sessions/{session}/messages``            ``SendMessage``
POST     ``/interrupts/{interrupt}/answer``          ``AnswerInterrupt`` (``amend``: ADR-P27)
POST     ``/approvals/{approval}/approve``           ``Approve`` (body ``shown_signature``)
POST     ``/approvals/{approval}/reject``            ``Reject``
POST     ``/runs/{run}/stop``                        ``Stop``
POST     ``/inputs``                                 ``UploadInputs`` (body ``files``: path to text)
POST     ``/runs/{run}/steer``                       ``Steer`` (body ``text``, ``lane``,
                                                     ``instance``; ADR-P27)
GET      ``/sessions/{session}``                     the session's status
GET      ``/sessions/{session}/events``              SSE from ``?after=`` or ``Last-Event-ID``
GET      ``/runs``, ``/runs/{run}``                  run statuses; the run inspector's report
GET      ``/receipts/{command_id}``                  a stored receipt
GET      ``/verify``                                 the hash chain check (ADR-P01 H)
=======  ==========================================  =========================================

The SSE stream sends each event with its ``seq`` as the SSE ``id``, then one
``synchronized`` event at the tail, then live events and a comment line as a heartbeat
(D5). A refused receipt is HTTP 409 (404 for an unknown item) with the receipt in the
error's ``details``. FastAPI is the optional extra ``aksha-harness[server]``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aksha.interfaces import wire
from aksha.runtime.errors import AkshaError
from aksha.runtime.projections.view import load_view
from aksha.runtime.session import named
from aksha.runtime.session.errors import CommandIdReused
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
from aksha.runtime.session.worker import RuntimeThread
from aksha.runtime.storage.ports import EventStore

API = "/api/v1"
COMMAND_TIMEOUT_S = 60.0
"""How long a request waits for its command to commit before answering 504."""


def error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    """The one error shape: ``{"error": {"code", "message", "details"}}``."""
    return JSONResponse({"error": {"code": code, "message": message, "details": details}},
                        status_code=status)  # fmt: skip


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


def create_app(
    runtime: RuntimeThread,
    open_reader: Callable[[], EventStore],
    capability_record: Callable[[], dict[str, Any]],
    *,
    heartbeat_s: float = 15.0,
) -> FastAPI:
    """The HTTP app over ``runtime`` (writes) and ``open_reader`` (reads)."""
    app = FastAPI(title="Aksha session API", version="1")
    closing = threading.Event()
    app.state.closing = closing

    def reader() -> EventStore:
        return open_reader()

    def submit(command: named.NamedCommand) -> JSONResponse:
        try:
            receipt = runtime.submit(command).result(timeout=COMMAND_TIMEOUT_S)
        except CommandIdReused as e:
            return error(409, e.code, e.message)
        except AkshaError as e:
            return error(409, e.code, e.message)
        except TimeoutError:
            return error(504, "timeout", "the command did not commit in time")
        body = wire.receipt_to_json(receipt)
        if not receipt.accepted:
            return error(wire.refusal_status(receipt.refused), receipt.refused or "refused",
                         f"{type(command).__name__} refused", body)  # fmt: skip
        return JSONResponse({"receipt": body})

    def command(owner: str, name: str, args: dict[str, Any], body: dict[str, Any]) -> JSONResponse:
        try:
            parsed = wire.command_from_json(owner, {
                "command": name, "args": args, "command_id": body.get("command_id"),
                "by": body.get("by"),
            })  # fmt: skip
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)
        return submit(parsed)

    async def json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise wire.WireInvalid(message=f"the body is not JSON: {e}") from None
        if not isinstance(data, dict):
            raise wire.WireInvalid(message="the body is a JSON object")
        return data

    @app.exception_handler(wire.WireInvalid)
    async def invalid(_: Request, e: wire.WireInvalid) -> JSONResponse:
        return error(422, e.code, e.message)

    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        faults = [f.__dict__ for f in runtime.faults]
        return {"ok": runtime.alive, "faults": faults}

    @app.get(f"{API}/capabilities")
    def caps() -> dict[str, Any]:
        return capability_record()

    @app.post(f"{API}/commands")
    async def any_command(request: Request, x_aksha_user: str = Header()) -> JSONResponse:
        body = await json_body(request)
        try:
            parsed = wire.command_from_json(x_aksha_user, body)
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)
        return submit(parsed)

    @app.post(f"{API}/sessions/{{session}}/messages")
    async def message(session: str, request: Request,
                      x_aksha_user: str = Header()) -> JSONResponse:  # fmt: skip
        body = await json_body(request)
        args = {"session": session, "text": body.get("text")}
        for key in ("agent", "headless"):
            if key in body:
                args[key] = body[key]
        if not isinstance(args["text"], str) or not args["text"]:
            return error(422, "validation", "text is a non-empty string")
        return command(x_aksha_user, "SendMessage", args, body)

    @app.post(f"{API}/interrupts/{{interrupt}}/answer")
    async def answer(interrupt: str, request: Request,
                     x_aksha_user: str = Header()) -> JSONResponse:  # fmt: skip
        body = await json_body(request)
        args = {"interrupt_id": interrupt}
        args.update({k: body[k] for k in ("text", "answers", "agent", "amend") if k in body})
        return command(x_aksha_user, "AnswerInterrupt", args, body)

    @app.post(f"{API}/approvals/{{approval}}/approve")
    async def approve(approval: str, request: Request,
                      x_aksha_user: str = Header()) -> JSONResponse:  # fmt: skip
        body = await json_body(request)
        args = {"approval_id": approval, "shown_signature": body.get("shown_signature")}
        return command(x_aksha_user, "Approve", args, body)

    @app.post(f"{API}/approvals/{{approval}}/reject")
    async def reject(approval: str, request: Request,
                     x_aksha_user: str = Header()) -> JSONResponse:  # fmt: skip
        body = await json_body(request)
        args: dict[str, Any] = {"approval_id": approval}
        if "note" in body:
            args["note"] = body["note"]
        return command(x_aksha_user, "Reject", args, body)

    @app.post(f"{API}/runs/{{run}}/stop")
    async def stop(run: str, request: Request, x_aksha_user: str = Header()) -> JSONResponse:
        body = await json_body(request)
        args: dict[str, Any] = {"run_id": run}
        if "reason" in body:
            args["reason"] = body["reason"]
        return command(x_aksha_user, "Stop", args, body)

    @app.post(f"{API}/inputs")
    async def inputs(request: Request, x_aksha_user: str = Header()) -> JSONResponse:
        body = await json_body(request)
        files = body.get("files")
        if (
            not isinstance(files, dict)
            or not files
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
        ):
            return error(422, "validation", "files maps each relative path to its UTF-8 text")
        return command(x_aksha_user, "UploadInputs", {"files": files}, body)

    @app.post(f"{API}/runs/{{run}}/steer")
    async def steer(run: str, request: Request, x_aksha_user: str = Header()) -> JSONResponse:
        body = await json_body(request)
        args: dict[str, Any] = {"run_id": run, "text": body.get("text")}
        if not isinstance(args["text"], str) or not args["text"]:
            return error(422, "validation", "text is a non-empty string")
        for key in ("lane", "instance"):
            if key in body:
                args[key] = body[key]
        return command(x_aksha_user, "Steer", args, body)

    @app.get(f"{API}/sessions/{{session}}")
    def session(session: str, x_aksha_user: str = Header()) -> dict[str, Any]:
        store = reader()
        try:
            return wire.session_to_json(session_status(load_view(store, x_aksha_user), session))
        finally:
            store.close()

    @app.get(f"{API}/runs")
    def runs(x_aksha_user: str = Header()) -> dict[str, Any]:
        store = reader()
        try:
            statuses = run_statuses(load_view(store, x_aksha_user))
            return {"runs": [wire.run_to_json(r) for r in statuses]}
        finally:
            store.close()

    @app.get(f"{API}/runs/{{run}}", response_model=None)
    def run(run: str, x_aksha_user: str = Header()) -> dict[str, Any] | JSONResponse:
        store = reader()
        try:
            report = inspect_run(load_view(store, x_aksha_user), run)
        finally:
            store.close()
        if report is None:
            return error(404, "not_found", f"no run {run}")
        return dict(report)

    @app.get(f"{API}/receipts/{{command_id}}", response_model=None)
    def receipt(command_id: str, x_aksha_user: str = Header()) -> dict[str, Any] | JSONResponse:
        store = reader()
        try:
            found = receipts(store, x_aksha_user, [command_id]).get(command_id)
        finally:
            store.close()
        if found is None:
            return error(404, "not_found", f"no receipt for {command_id}")
        return {"receipt": wire.receipt_to_json(found)}

    @app.get(f"{API}/verify")
    def verify() -> dict[str, Any]:
        store = reader()
        try:
            broken = store.verify()
            seq, head = store.head()
        finally:
            store.close()
        if broken is None:
            return {"ok": True, "seq": seq, "head": head}
        return {"ok": False, "seq": seq, "break": {"seq": broken.seq, "reason": broken.reason}}

    @app.get(f"{API}/sessions/{{session}}/events")
    def events(
        session: str,
        after: int | None = None,
        x_aksha_user: str = Header(),
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        cursor = after
        if cursor is None and last_event_id and last_event_id.isdigit():
            cursor = int(last_event_id)
        store = reader()

        def stream() -> Iterator[str]:
            try:
                for item in subscribe(store, x_aksha_user, session, cursor,
                                      heartbeat_s=heartbeat_s,
                                      stopped=closing.is_set):  # fmt: skip
                    yield sse(item)
            finally:
                store.close()

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    return app
