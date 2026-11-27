"""``aksha chat``: the interactive terminal UI, a chat over the HTTP session API.

The terminal twin of the web app, and like it a client only: it reaches the runtime
through the HTTP API (commands with fresh command IDs, the session's SSE stream with its
cursor), never by opening the store. With no server running it starts ``aksha serve`` on
a free loopback port for its own lifetime (local mode, no sign-in).

- :mod:`.client`: the standard-library HTTP and SSE client, with the reconnect and cursor
  logic (no Textual).
- :mod:`.reducer`: a pure fold of stream events into transcript items (no Textual).
- :mod:`.ports`: the ``ChatApi`` protocol the app calls.
- :mod:`.connection`: find the running server, or start one and stop it on exit.
- :mod:`.render`: transcript items and views as Rich text.
- :mod:`.app` and :mod:`.screens`: the Textual app, its cards, drawer and pickers.
- :mod:`.command`: the ``aksha chat`` handler.

Textual is the optional extra ``aksha-harness[tui]``; the plain CLI commands stay as they
are for scripts.
"""
