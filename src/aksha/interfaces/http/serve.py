"""``aksha serve``: the runtime thread, the HTTP app and the server file (decision 22).

The runtime thread opens the session, and so the writer lock, before the server
listens: while it runs, an embedded mutating command is refused (exit code 8) and read
commands still work. The server file beside the store names the URL, so CLI commands
send their commands here; it is removed on exit.

Authentication is real accounts by default, in ``accounts.sqlite`` beside the store
(``AKSHA_ACCOUNTS_PATH`` moves it): until someone signs up or claims the server, it runs
in local mode for loopback clients. ``AKSHA_GUESTS=1`` turns on the guest sandbox and
``AKSHA_COOKIE_SECURE=1`` marks the session cookie Secure behind a TLS proxy.
``AKSHA_UPLOAD_MAX_MB`` (default 50) caps an upload's body; a larger one is refused 413.
``--trust-user-header`` trusts ``X-Aksha-User`` instead, as before accounts existed.

``--web DIR`` (or ``AKSHA_WEB_DIST``) also serves the built web app from that folder on the
same port (``web.py``), so one process is the whole product: ``aksha serve --web web/dist``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

import uvicorn

from aksha.app import (
    RuntimeConfig,
    open_blob_store,
    open_reader,
    open_session,
    session_capabilities,
)
from aksha.interfaces.http.accounts.service import Accounts
from aksha.interfaces.http.accounts.store import AccountStore
from aksha.interfaces.http.auth import AuthConfig, command_tenant, principal_by, read_tenant
from aksha.interfaces.http.common import upload_max_bytes
from aksha.interfaces.http.reads import reads_router
from aksha.interfaces.http.server import create_app
from aksha.interfaces.http.web import check_dist, mount_web
from aksha.runtime.session.worker import RuntimeThread

ACCOUNTS_FILE = "accounts.sqlite"


def auth_config(config: RuntimeConfig, env: Mapping[str, str], *,
                trust_user_header: bool) -> AuthConfig:  # fmt: skip
    """The auth settings of ``aksha serve``, opening the accounts database unless trusting."""
    if trust_user_header:
        return AuthConfig(trust_user_header=True)
    raw = env.get("AKSHA_ACCOUNTS_PATH")
    path = Path(raw) if raw else config.storage.sqlite_path.parent / ACCOUNTS_FILE
    return AuthConfig(accounts=Accounts(AccountStore(path)), guests=env.get("AKSHA_GUESTS") == "1",
                      cookie_secure=env.get("AKSHA_COOKIE_SECURE") == "1")  # fmt: skip


def web_dist(web: Path | None, env: Mapping[str, str]) -> Path | None:
    """The web app folder to serve: ``--web``, else ``AKSHA_WEB_DIST``, else none; checked."""
    raw = (
        web
        if web is not None
        else (Path(env["AKSHA_WEB_DIST"]) if env.get("AKSHA_WEB_DIST") else None)
    )
    return check_dist(raw) if raw is not None else None


def serve(config: RuntimeConfig, host: str, port: int, *, say: Callable[[str], None],
          trust_user_header: bool = False, web: Path | None = None,
          env: Mapping[str, str] | None = None) -> int:  # fmt: skip
    """Serve until interrupted; returns the exit code.

    ``web`` (else ``AKSHA_WEB_DIST``) is a built web app folder to serve beside the API.
    """
    environ = os.environ if env is None else env
    dist = web_dist(web, environ)
    auth = auth_config(config, environ, trust_user_header=trust_user_header)
    runtime = RuntimeThread(lambda: open_session(config))
    runtime.start()
    record: dict[str, object] = {}

    def capability_record() -> dict[str, object]:
        if not record:
            future = runtime.call(lambda s: session_capabilities(s, config).as_json())
            record.update(future.result(timeout=30))
        return record

    cap = upload_max_bytes(environ)
    reads = reads_router(
        lambda: open_reader(config),
        lambda: open_blob_store(config.storage),
        read_tenant,
        submit=runtime.submit,
        session_meta=auth.accounts.store if auth.accounts is not None else None,
        command_tenant_dep=command_tenant,
        principal_by_dep=principal_by,
        upload_max_bytes=cap,
    )
    app = create_app(runtime, lambda: open_reader(config), capability_record, auth=auth,
                     extra_routers=(reads,), upload_max_bytes=cap)  # fmt: skip
    if dist is not None:
        mount_web(app, dist)
    url = f"http://{host}:{port}"
    server_file = config.server_file()
    server_file.parent.mkdir(parents=True, exist_ok=True)
    server_file.write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    say(f"serving {url}" + (" (trusting X-Aksha-User)" if trust_user_header else "")
        + (f", the web app from {dist}" if dist is not None else ""))  # fmt: skip
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        app.state.closing.set()
        runtime.stop()
        auth.close()
        server_file.unlink(missing_ok=True)
    return 0
