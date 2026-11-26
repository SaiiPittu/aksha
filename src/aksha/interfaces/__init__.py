"""Interfaces: the ways people and programs reach the runtime (the CLI and the HTTP API).

They call the session API through :mod:`aksha.app`; they hold no runtime logic and
are not extensions. ``cli/`` is the ``aksha`` command in server or embedded mode,
``http/`` the HTTP adapter (extra ``aksha-harness[server]``), and ``wire.py`` the JSON forms and
exit codes both share (ADR-P19, decision 22).
"""
