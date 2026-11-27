"""What every router of the HTTP app shares: the error shape, the JSON body and the context.

Two rules keep a browser on another site from writing through a visitor's browser (CSRF),
including on the routes that need no sign-in (``/auth/claim``, ``/auth/signup``):

1. An unsafe request (POST, PUT, PATCH, DELETE) with a body sends it as
   ``application/json`` (:func:`json_body`), or ``multipart/form-data`` on the one upload
   route; anything else is refused 415 ``unsupported_media_type``. A cross-site page can
   send ``text/plain`` or a form without a preflight, but never ``application/json``, and
   this server answers no CORS preflight.
2. An unsafe request whose ``Origin`` header names another host than the request's
   ``Host`` is refused 403 ``cross_origin`` (:func:`cross_origin`). A reverse proxy in
   front of the server passes the browser's ``Host`` through.

A third keeps a page on a rebound DNS name out (DNS rebinding: ``attacker.example``
resolved to 127.0.0.1, so its requests are same-origin to the browser): any request whose
``Host`` is not on the :class:`HostAllowList` is refused 400 ``host_not_allowed``, on
every route, API and web app alike. Loopback names are always on it; ``aksha serve`` adds
the address it binds (unless a wildcard) and ``AKSHA_ALLOWED_HOSTS`` (:func:`allowed_hosts`).
"""

from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlsplit

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


UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MB = 1024 * 1024
UPLOAD_MAX_MB = 50
"""The default upload cap (``AKSHA_UPLOAD_MAX_MB``): a multipart upload or ``POST /inputs``."""
JSON_MEDIA = "application/json"
MULTIPART_MEDIA = "multipart/form-data"


class RequestRefused(Exception):
    """A request refused before any route logic: its HTTP status, code and message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Hold what the error body says."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def media_type(request: Request) -> str | None:
    """The request's media type without its parameters, lower case; None when not sent."""
    raw = request.headers.get("content-type")
    return None if raw is None else raw.split(";", 1)[0].strip().lower()


def require_media(request: Request, raw: bytes, allowed: str = JSON_MEDIA) -> None:
    """Refuse an unsafe request whose body is not ``allowed`` (module doc, rule 1).

    A request with no body and no ``Content-Type`` passes: it carries nothing to parse.
    """
    if request.method not in UNSAFE:
        return
    media = media_type(request)
    if media is None and not raw:
        return
    if media != allowed:
        raise RequestRefused(415, "unsupported_media_type",
                             f"send the body as {allowed}, not {media or 'untyped'}")  # fmt: skip


DEFAULT_PORTS = {"http": ":80", "https": ":443"}


def _authority(netloc: str, scheme: str | None = None) -> str:
    """``host[:port]`` in lower case, without the scheme's default port (any, for ``Host``)."""
    host = netloc.strip().lower()
    defaults = DEFAULT_PORTS.values() if scheme is None else (DEFAULT_PORTS.get(scheme, ""),)
    for default in defaults:
        if default and host.endswith(default):
            return host.removesuffix(default)
    return host


def cross_origin(request: Request) -> bool:
    """Whether an unsafe request's ``Origin`` names another host (module doc, rule 2).

    A request with no ``Origin`` (the CLI, the TUI, a same-origin GET) is not refused here;
    ``Origin: null`` (a sandboxed frame, a file) is cross-origin.
    """
    if request.method not in UNSAFE:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return False
    parts = urlsplit(origin.strip())
    if not parts.scheme or not parts.netloc:
        return True
    host = request.headers.get("host", "")
    return _authority(parts.netloc, parts.scheme) != _authority(host)


def loopback_address(text: str | None) -> bool:
    """Whether ``text`` is a loopback IP address (127.0.0.0/8, ``::1``)."""
    if not text:
        return False
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def host_name(host: str | None) -> str | None:
    """The name in a ``Host`` value, lower case, without its port, brackets or final dot.

    None when there is none or it is malformed (an unclosed bracket, a port that is not a
    number).
    """
    text = (host or "").strip().lower()
    if not text:
        return None
    if text.startswith("["):
        name, sep, rest = text[1:].partition("]")
        if not sep or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return None
    elif text.count(":") > 1:
        name = text  # a bare IPv6 address
    else:
        name, sep, port = text.partition(":")
        if sep and not port.isdigit():
            return None
    name = name.rstrip(".")
    return name or None


def is_loopback_name(name: str) -> bool:
    """``localhost``, a ``*.localhost`` name or a loopback address: never a rebound name."""
    return name == "localhost" or name.endswith(".localhost") or loopback_address(name)


WILDCARD_BINDS = frozenset({"", "0.0.0.0", "::"})
"""Bind addresses that name no host (every interface)."""


@dataclass(frozen=True)
class HostAllowList:
    """The ``Host`` names this server answers to: loopback names, plus ``names``."""

    names: frozenset[str] = frozenset()

    @classmethod
    def of(cls, names: Iterable[str]) -> HostAllowList:
        """The allow-list of ``names`` (``Host``-shaped: a port, case and a final dot ignored)."""
        return cls(frozenset(n for n in (host_name(x) for x in names) if n))

    def allows(self, host: str | None) -> bool:
        """Whether a request with this ``Host`` header is served (module doc, rule 3)."""
        name = host_name(host)
        return name is not None and (is_loopback_name(name) or name in self.names)


def allowed_hosts(bind: str, env: Mapping[str, str]) -> HostAllowList:
    """``aksha serve``'s allow-list: the bind address unless a wildcard, ``AKSHA_ALLOWED_HOSTS``.

    ``AKSHA_ALLOWED_HOSTS`` is comma-separated: the names people reach the server by, such
    as its public host name behind a proxy.
    """
    extra = [x for x in env.get("AKSHA_ALLOWED_HOSTS", "").split(",") if x.strip()]
    bound = [] if bind.strip() in WILDCARD_BINDS else [bind]
    return HostAllowList.of([*bound, *extra])


def upload_max_bytes(env: Mapping[str, str]) -> int:
    """The largest upload body, from ``AKSHA_UPLOAD_MAX_MB`` (default 50 MB)."""
    raw = env.get("AKSHA_UPLOAD_MAX_MB", "").strip()
    try:
        mb = float(raw) if raw else UPLOAD_MAX_MB
    except ValueError:
        raise ValueError(f"AKSHA_UPLOAD_MAX_MB is a number of megabytes, not {raw!r}") from None
    if mb <= 0:
        raise ValueError("AKSHA_UPLOAD_MAX_MB is above zero")
    return int(mb * MB)


def _too_large(limit: int) -> RequestRefused:
    return RequestRefused(413, "too_large", f"the body is over {limit // MB or limit} "
                          f"{'MB' if limit >= MB else 'bytes'} (AKSHA_UPLOAD_MAX_MB)")  # fmt: skip


async def read_body(request: Request, limit: int | None = None) -> bytes:
    """The request's body; over ``limit`` bytes it is refused 413 ``too_large`` unread.

    A ``Content-Length`` over the limit is refused before any byte is read, and a body
    sent without one stops being read at the limit.
    """
    if limit is None:
        return await request.body()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def replayed(request: Request, raw: bytes) -> Request:
    """A request over the same scope whose body is ``raw``, already read (to parse a form)."""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(request.scope, receive)


async def json_body(request: Request, *, limit: int | None = None) -> dict[str, Any]:
    """The request's JSON object body, ``{}`` when empty; ``validation`` otherwise.

    An unsafe request's body must be ``application/json`` (``unsupported_media_type``);
    over ``limit`` bytes it is refused ``too_large``.
    """
    raw = await read_body(request, limit)
    require_media(request, raw)
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
    """The runtime thread (writes), the read-only store opener, the stream settings, the cap."""

    runtime: RuntimeThread
    open_reader: Callable[[], EventStore]
    capability_record: Callable[[], dict[str, Any]]
    heartbeat_s: float
    closing: threading.Event
    upload_max_bytes: int = UPLOAD_MAX_MB * MB


def app_context(request: Request) -> AppContext:
    """The app's :class:`AppContext` (``app.state.context``)."""
    context: AppContext = request.app.state.context
    return context


Context = Annotated[AppContext, Depends(app_context)]
