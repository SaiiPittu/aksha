"""Adapters: concrete implementations of the runtime's ports.

Each family is a subpackage and each backend is one module named for it. Built: storage
(PostgreSQL, SQLite), anchor (file), blobs (filesystem), clock (system, fake), sandbox
(a process group unit, ADR-P18, and the bubblewrap backend, ADR-P12), egress (the
resolver and transport, ADR-P26), systems (a mock mailbox) and models (scripted, replay,
live; ADR-P07). Planned: container and VM sandbox backends, an object-store blob backend
and exporters.
Adapters import the runtime's ports and domain values; the runtime never imports an
adapter. Only :mod:`aksha.app` chooses one.
"""
