"""Interfaces: the ways people and programs reach the runtime (the CLI and the HTTP API).

They call the session API through :mod:`aksha.app`; they hold no runtime logic and
are not extensions. Nothing is built yet: the CLI and the HTTP server arrive with the
session API (ADR-P19).
"""
