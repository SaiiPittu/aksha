"""The session API (ADR-P19): every interface changes state through one ``dispatch`` path.

Built: commands with receipts (ADR-P03 D5, D6), ``types.py``, ``errors.py`` and
``commands.py``. A command commits one ``command.accepted`` (or ``command.refused``) with
its own events in one transaction, under ``cmd:{owner}/{command_id}``, so a retried
command returns the first receipt. Planned: the named commands, resolutions, event
streams and capabilities.
"""
