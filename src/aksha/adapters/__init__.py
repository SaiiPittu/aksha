"""Adapters: concrete implementations of the runtime's ports.

Each family is a subpackage and each backend is one module named for it. Built: storage
(PostgreSQL, SQLite), anchor (file), blobs (filesystem), clock (system, fake) and sandbox
(a process group unit, ADR-P18). Planned: models, the sandbox backends, an object-store
blob backend and exporters.
Adapters import the runtime's ports and domain values; the runtime never imports an
adapter. Only :mod:`aksha.app` chooses one.
"""
