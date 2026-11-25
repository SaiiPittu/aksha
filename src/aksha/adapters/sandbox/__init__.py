"""Sandbox adapters (ADR-P12): where a tool body runs so the host can bound and end it.

Built so far: ``ProcessGroupUnit``, a child process in its own process group, which
implements cancellation's ``KillableUnit`` port (ADR-P18 D6). The sandbox backends that
confine a body (``refusing.py``, ``bwrap.py``) arrive with ADR-P12.
"""
