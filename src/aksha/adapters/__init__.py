"""Adapters: concrete implementations of the runtime's ports.

Each family is a subpackage and each backend is one module named for it. Built: storage
(PostgreSQL, SQLite), anchor (file) and clock (system, fake). Planned: models, sandbox
and exporters.
Adapters import the runtime's ports and domain values; the runtime never imports an
adapter. Only :mod:`aksha.app` chooses one.
"""
