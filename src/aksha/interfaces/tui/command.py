"""``aksha chat [--session S] [--agent A] [--input PATH ...] [prompt]``: run the chat app."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence

from aksha.app import RuntimeConfig
from aksha.interfaces import wire
from aksha.interfaces.tui.app import ChatApp
from aksha.interfaces.tui.client import Api, read_inputs
from aksha.interfaces.tui.connection import ConnectError, connect


def run_chat(
    config: RuntimeConfig,
    owner: str,
    token: str | None,
    *,
    session: str | None,
    agent: str | None,
    inputs: Sequence[str],
    prompt: str | None,
    say: Callable[[str], None],
    env: Mapping[str, str] | None = None,
) -> int:
    """Connect (or start a local server), run the app, stop what it started."""
    try:
        files = read_inputs(inputs)
    except OSError as e:
        say(f"invalid: {e}")
        return wire.EXIT_USAGE
    try:
        connection = connect(
            config.server_url, config.server_file(), dict(os.environ if env is None else env)
        )
    except ConnectError as e:
        say(f"environment: {e}")
        return wire.EXIT_ENVIRONMENT
    note = "local server" if connection.started_here else connection.url
    api = Api(connection.url, owner, token)
    try:
        app = ChatApp(api, session, agent=agent, prompt=prompt, files=files, server_note=note)
        app.run()
        api.close_streams()
        say(f"session {app.session}  (aksha chat --session {app.session} resumes it)")
    finally:
        connection.close(wait=False)
    return wire.EXIT_OK
