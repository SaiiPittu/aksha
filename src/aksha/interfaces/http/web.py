"""The built web app, served by ``aksha serve --web DIR`` (or ``AKSHA_WEB_DIST``).

:func:`mount_web` adds a catch-all route after the API routers: a file that exists in the
build's folder is sent as is, and any other path that is not under ``/api`` gets
``index.html``, so the app's own routes (``/s/S-…``, ``/settings/members``) load on a
reload or a pasted link. Hashed build output under ``assets/`` is cached for a year as
immutable; ``index.html`` is sent with ``no-cache`` so a new build is picked up at once.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

from aksha.interfaces.http.common import error

INDEX = "index.html"
LONG_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"


class WebDistMissing(ValueError):
    """The folder given to serve the web app from has no ``index.html``."""


def check_dist(dist: Path) -> Path:
    """The resolved build folder; :class:`WebDistMissing` when it holds no ``index.html``."""
    root = dist.expanduser().resolve()
    if not (root / INDEX).is_file():
        raise WebDistMissing(
            f"{root} has no {INDEX}: build the web app first (cd web && npm run build)"
        )
    return root


def _within(root: Path, rel: str) -> Path | None:
    """The file ``rel`` names under ``root``, or None (missing, a folder, or outside it)."""
    if not rel:
        return None
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def mount_web(app: FastAPI, dist: Path) -> None:
    """Serve the web app built into ``dist`` from ``app``, after every API route."""
    root = check_dist(dist)
    index = root / INDEX

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def web(path: str, request: Request) -> Response:
        if path == "api" or path.startswith("api/"):
            return error(404, "not_found", f"no route {request.url.path}")
        found = _within(root, path)
        if found is None or found == index:
            return FileResponse(index, media_type="text/html", headers={"Cache-Control": NO_CACHE})
        hashed = found.parent.name == "assets"
        return FileResponse(found, headers={"Cache-Control": LONG_CACHE if hashed else NO_CACHE})
