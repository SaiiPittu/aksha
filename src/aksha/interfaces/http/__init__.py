"""The HTTP interface: the session API over JSON and Server-Sent Events (ADR-P19).

``server.py`` builds the FastAPI app over the runtime thread and a read-only store;
``serve.py`` runs it with uvicorn for ``aksha serve``. Both need the optional extra
``aksha[server]``; nothing else in the package imports them.
"""
