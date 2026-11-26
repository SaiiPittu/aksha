"""Sandbox adapters (ADR-P12): where a program runs so the host can bound, confine and end it.

``process.py``: ``ProcessGroupUnit``, a child process in its own process group, which
implements cancellation's ``KillableUnit`` port (ADR-P18 D6). ``bwrap.py``: the bubblewrap
backend of strength ``os-jail``. The ``refuse`` backend lives in the runtime core.
"""
