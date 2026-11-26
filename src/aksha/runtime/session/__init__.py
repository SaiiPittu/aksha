"""The session API (ADR-P19): every interface changes state through one ``dispatch`` path.

Built: command receipts (ADR-P03 D5, D6) in ``types.py``, ``errors.py`` and
``commands.py``; the closed union of named commands (``named.py``) and the single writer
that carries them out and drives runs one pass at a time (``service.py``, with the start
command in ``start.py`` and the server's runtime thread in ``worker.py``); the session
stream with a cursor, a resync and a catch-up marker (``streams.py``); the capability
record (``capabilities.py``); and the run and session views with the run inspector
(``views.py``). ``Steer`` (ADR-P27) is dispatched to ``inputs.commands``. Planned:
``OpenSession`` and transient items.
"""
