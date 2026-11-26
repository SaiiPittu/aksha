"""``aksha serve``: the runtime thread, the HTTP app and the server file (decision 22).

The runtime thread opens the session, and so the writer lock, before the server
listens: while it runs, an embedded mutating command is refused (exit code 8) and read
commands still work. The server file beside the store names the URL, so CLI commands
send their commands here; it is removed on exit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

import uvicorn

from aksha.app import RuntimeConfig, open_reader, open_session, session_capabilities
from aksha.interfaces.http.server import create_app
from aksha.runtime.session.worker import RuntimeThread


def serve(config: RuntimeConfig, host: str, port: int, *, say: Callable[[str], None]) -> int:
    """Serve until interrupted; returns the exit code."""
    runtime = RuntimeThread(lambda: open_session(config))
    runtime.start()
    record: dict[str, object] = {}

    def capability_record() -> dict[str, object]:
        if not record:
            future = runtime.call(lambda s: session_capabilities(s, config).as_json())
            record.update(future.result(timeout=30))
        return record

    app = create_app(runtime, lambda: open_reader(config), capability_record)
    url = f"http://{host}:{port}"
    server_file = config.server_file()
    server_file.parent.mkdir(parents=True, exist_ok=True)
    server_file.write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    say(f"serving {url}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        app.state.closing.set()
        runtime.stop()
        server_file.unlink(missing_ok=True)
    return 0
