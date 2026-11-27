"""What every router of the HTTP app shares: the error shape, the JSON body and the context."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from aksha.interfaces import wire
from aksha.runtime.session.worker import RuntimeThread
from aksha.runtime.storage.ports import EventStore

API = "/api/v1"
COMMAND_TIMEOUT_S = 60.0
"""How long a request waits for its command to commit before answering 504."""


def error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    """The one error shape: ``{"error": {"code", "message", "details"}}``."""
    return JSONResponse({"error": {"code": code, "message": message, "details": details}},
                        status_code=status)  # fmt: skip


async def json_body(request: Request) -> dict[str, Any]:
    """The request's JSON object body, ``{}`` when empty; ``validation`` otherwise."""
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


@dataclass(frozen=True)
class AppContext:
    """The runtime thread (writes), the read-only store opener and the stream settings."""

    runtime: RuntimeThread
    open_reader: Callable[[], EventStore]
    capability_record: Callable[[], dict[str, Any]]
    heartbeat_s: float
    closing: threading.Event


def app_context(request: Request) -> AppContext:
    """The app's :class:`AppContext` (``app.state.context``)."""
    context: AppContext = request.app.state.context
    return context


Context = Annotated[AppContext, Depends(app_context)]
