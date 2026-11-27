"""The command routes: each write is one named command, submitted to the runtime thread."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aksha.interfaces import wire
from aksha.interfaces.http.auth import Commander, StopTenant
from aksha.interfaces.http.common import (
    API,
    COMMAND_TIMEOUT_S,
    AppContext,
    Context,
    error,
    json_body,
)
from aksha.runtime.errors import AkshaError
from aksha.runtime.session import named
from aksha.runtime.session.errors import CommandIdReused

router = APIRouter(prefix=API)


def submit(context: AppContext, command: named.NamedCommand) -> JSONResponse:
    """Submit to the single writer and answer with the receipt at commit (ADR-P19 D3)."""
    try:
        receipt = context.runtime.submit(command).result(timeout=COMMAND_TIMEOUT_S)
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


def command(context: AppContext, owner: str, name: str, args: dict[str, Any],
            body: dict[str, Any]) -> JSONResponse:  # fmt: skip
    """Build the named command ``name`` for ``owner`` and submit it."""
    try:
        parsed = wire.command_from_json(owner, {
            "command": name, "args": args, "command_id": body.get("command_id"),
            "by": body.get("by"),
        })  # fmt: skip
    except wire.WireInvalid as e:
        return error(422, e.code, e.message)
    return submit(context, parsed)


@router.post("/commands")
async def any_command(request: Request, context: Context, who: Commander) -> JSONResponse:
    """Any named command (``wire`` module doc)."""
    body = await json_body(request)
    try:
        parsed = wire.command_from_json(who.tenant, body)
    except wire.WireInvalid as e:
        return error(422, e.code, e.message)
    return submit(context, parsed)


@router.post("/sessions/{session}/messages")
async def message(session: str, request: Request, context: Context,
                  who: Commander) -> JSONResponse:  # fmt: skip
    """``SendMessage``."""
    body = await json_body(request)
    args = {"session": session, "text": body.get("text")}
    for key in ("agent", "headless"):
        if key in body:
            args[key] = body[key]
    if not isinstance(args["text"], str) or not args["text"]:
        return error(422, "validation", "text is a non-empty string")
    return command(context, who.tenant, "SendMessage", args, body)


@router.post("/interrupts/{interrupt}/answer")
async def answer(interrupt: str, request: Request, context: Context,
                 who: Commander) -> JSONResponse:  # fmt: skip
    """``AnswerInterrupt`` (``amend``: ADR-P27)."""
    body = await json_body(request)
    args = {"interrupt_id": interrupt}
    args.update({k: body[k] for k in ("text", "answers", "agent", "amend") if k in body})
    return command(context, who.tenant, "AnswerInterrupt", args, body)


@router.post("/approvals/{approval}/approve")
async def approve(approval: str, request: Request, context: Context,
                  who: Commander) -> JSONResponse:  # fmt: skip
    """``Approve`` (body ``shown_signature``)."""
    body = await json_body(request)
    args = {"approval_id": approval, "shown_signature": body.get("shown_signature")}
    return command(context, who.tenant, "Approve", args, body)


@router.post("/approvals/{approval}/reject")
async def reject(approval: str, request: Request, context: Context,
                 who: Commander) -> JSONResponse:  # fmt: skip
    """``Reject``."""
    body = await json_body(request)
    args: dict[str, Any] = {"approval_id": approval}
    if "note" in body:
        args["note"] = body["note"]
    return command(context, who.tenant, "Reject", args, body)


@router.post("/runs/{run}/stop")
async def stop(run: str, request: Request, context: Context, tenant: StopTenant) -> JSONResponse:
    """``Stop``; an Admin may stop a member's run with ``?as_user=`` (audited first)."""
    body = await json_body(request)
    args: dict[str, Any] = {"run_id": run}
    if "reason" in body:
        args["reason"] = body["reason"]
    return command(context, tenant, "Stop", args, body)


@router.post("/inputs")
async def inputs(request: Request, context: Context, who: Commander) -> JSONResponse:
    """``UploadInputs`` (``files``: path to text, ``binary``: path to base64 bytes)."""
    body = await json_body(request)
    files = body.get("files", {})
    binary = body.get("binary", {})
    maps = (files, binary)
    if (
        not all(isinstance(m, dict) for m in maps)
        or not (files or binary)
        or not all(isinstance(k, str) and isinstance(v, str) for m in maps
                   for k, v in m.items())
    ):  # fmt: skip
        return error(422, "validation", "files maps each relative path to its UTF-8 text, "
                     "binary to its bytes in base64")  # fmt: skip
    args: dict[str, Any] = {"files": files}
    if binary:
        args["binary"] = binary
    return command(context, who.tenant, "UploadInputs", args, body)


@router.post("/runs/{run}/steer")
async def steer(run: str, request: Request, context: Context, who: Commander) -> JSONResponse:
    """``Steer`` (body ``text``, ``lane``, ``instance``; ADR-P27)."""
    body = await json_body(request)
    args: dict[str, Any] = {"run_id": run, "text": body.get("text")}
    if not isinstance(args["text"], str) or not args["text"]:
        return error(422, "validation", "text is a non-empty string")
    for key in ("lane", "instance"):
        if key in body:
            args[key] = body[key]
    return command(context, who.tenant, "Steer", args, body)
