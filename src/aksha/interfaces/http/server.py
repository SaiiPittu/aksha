"""The HTTP adapter of the session API: JSON commands and reads, and one SSE stream per session.

Every write goes to the runtime thread (the single writer) as one named command and
returns its receipt at commit (ADR-P19 D3); every read opens a read-only store handle and
never boots. Each request resolves to a principal (``auth.py``): the runtime tenant it
acts for and its role, from a session cookie, an API token, local mode on loopback, or
the trusted ``X-Aksha-User`` header (``AuthConfig(trust_user_header=True)``, the web twin
of ``--user``). Another tenant's ID reads as unknown (D9). Paths are under ``/api/v1``:

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
POST     ``/runs/{run}/stop``                        ``Stop`` (``?as_user=``: an Admin, audited)
POST     ``/inputs``                                 ``UploadInputs`` (``files``: path to text,
                                                     ``binary``: path to base64 bytes)
POST     ``/runs/{run}/steer``                       ``Steer`` (body ``text``, ``lane``,
                                                     ``instance``; ADR-P27)
GET      ``/sessions/{session}``                     the session's status
GET      ``/sessions/{session}/events``              SSE from ``?after=`` or ``Last-Event-ID``
GET      ``/runs``, ``/runs/{run}``                  run statuses; the run inspector's report
GET      ``/receipts/{command_id}``                  a stored receipt
GET      ``/verify``                                 the hash chain check (ADR-P01 H)
GET      ``/auth/status``                            ``mode`` (local, accounts, trusted),
                                                     guests, whether sign-up is open
POST     ``/auth/signup``, ``/auth/claim``           the first Owner and the org (claim:
                                                     loopback, tenant ``local``)
POST     ``/auth/login``, ``/auth/logout``           the session cookie ``aksha_session``
GET      ``/auth/me``                                user, org, role, csrf, tenant, guest
POST     ``/auth/password``                          change it; every session ends
GET      ``/auth/invites/{token}``                   an invite's preview and state
POST     ``/auth/invites/{token}/accept``            name and password; signs in
POST     ``/auth/guest``, ``/auth/guest/save``       a 24-hour guest (``AKSHA_GUESTS=1``);
                                                     keep it as an account
GET      ``/org/members``                            the org (Admin)
PATCH    ``/org/members/{user}``                     ``role``, ``active`` (Admin)
GET      ``/org/invites``, POST ``/org/invites``     list; invite ``emails`` with ``role``
POST     ``/org/invites/{id}/resend``                a new link and expiry (Admin)
DELETE   ``/org/invites/{id}``                       revoke (Admin)
GET      ``/org/audit``                              ``?limit&before&action&actor`` (Admin)
GET      ``/tokens``, POST ``/tokens``               API tokens; the secret is shown once
DELETE   ``/tokens/{id}``                            revoke one
GET      ``/{path}`` (outside ``/api``)              the built web app (``web.py``), with
                                                     ``aksha serve --web DIR``
=======  ==========================================  =========================================

Reads take ``?as_user=<user id>``: an Admin or the Owner reads a member of their org, and
an audit record is written first. A cookie's unsafe request sends ``X-CSRF-Token``. Every
unsafe request sends a JSON body (multipart on ``/inputs/upload``) and, when a browser
names its ``Origin``, comes from this server's own host (``common`` module doc).

The SSE stream sends each event with its ``seq`` as the SSE ``id``, then one
``synchronized`` event at the tail, then live events and a comment line as a heartbeat
(D5). A refused receipt is HTTP 409 (404 for an unknown item) with the receipt in the
error's ``details``. FastAPI is the optional extra ``aksha-harness[server]``.

The app is built from routers (``commands.py``, ``status.py``, ``account_routes.py``);
``create_app(..., extra_routers=...)`` includes more, such as the reads router.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from aksha.interfaces import wire
from aksha.interfaces.http import account_routes, commands, status
from aksha.interfaces.http.accounts.errors import AccountError
from aksha.interfaces.http.auth import AuthConfig, AuthState
from aksha.interfaces.http.common import (
    API,
    COMMAND_TIMEOUT_S,
    MB,
    UPLOAD_MAX_MB,
    AppContext,
    HostAllowList,
    RequestRefused,
    cross_origin,
    error,
)
from aksha.interfaces.http.status import sse
from aksha.runtime.session.worker import RuntimeThread
from aksha.runtime.storage.ports import EventStore

__all__ = ["API", "COMMAND_TIMEOUT_S", "create_app", "error", "sse"]

ROUTERS: tuple[APIRouter, ...] = (status.router, commands.router, account_routes.router)
"""The app's own routers, in order."""


def create_app(
    runtime: RuntimeThread,
    open_reader: Callable[[], EventStore],
    capability_record: Callable[[], dict[str, Any]],
    *,
    heartbeat_s: float = 15.0,
    auth: AuthConfig | None = None,
    extra_routers: Sequence[APIRouter] = (),
    upload_max_bytes: int = UPLOAD_MAX_MB * MB,
    allowed_hosts: HostAllowList | None = None,
) -> FastAPI:
    """The HTTP app over ``runtime`` (writes) and ``open_reader`` (reads).

    ``auth`` defaults to trusting ``X-Aksha-User`` (the session API tests and
    ``aksha serve --trust-user-header``); ``aksha serve`` passes accounts with local mode.
    ``upload_max_bytes`` caps ``POST /inputs`` (``AKSHA_UPLOAD_MAX_MB``). ``allowed_hosts``
    names the ``Host`` values served besides loopback names (default: loopback names only);
    any other is refused 400 ``host_not_allowed`` (``common`` module doc, rule 3).
    """
    hosts = allowed_hosts if allowed_hosts is not None else HostAllowList()
    config = auth if auth is not None else AuthConfig(trust_user_header=True)
    config.check()
    app = FastAPI(title="Aksha session API", version="1")
    closing = threading.Event()
    app.state.closing = closing
    app.state.context = AppContext(runtime, open_reader, capability_record, heartbeat_s, closing,
                                   upload_max_bytes)  # fmt: skip
    app.state.auth = AuthState(config)

    @app.exception_handler(wire.WireInvalid)
    async def invalid(_: Request, e: wire.WireInvalid) -> JSONResponse:
        return error(422, e.code, e.message)

    @app.exception_handler(AccountError)
    async def refused(_: Request, e: AccountError) -> JSONResponse:
        return error(e.status, e.code, e.message, e.details)

    @app.exception_handler(RequestRefused)
    async def refused_request(_: Request, e: RequestRefused) -> JSONResponse:
        return error(e.status, e.code, e.message)

    @app.middleware("http")
    async def same_origin(request: Request, call_next: Callable[[Request], Any]) -> Response:
        """Refuse an unknown ``Host`` and a cross-origin write; never sniff a body.

        ``common`` module doc, rules 2 and 3.
        """
        if not hosts.allows(request.headers.get("host")):
            return error(400, "host_not_allowed", "this server does not answer to that host "
                         "name; add it to AKSHA_ALLOWED_HOSTS")  # fmt: skip
        if cross_origin(request):
            return error(403, "cross_origin", "a write must come from this server's own origin")
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    for router in (*ROUTERS, *extra_routers):
        app.include_router(router)
    return app
