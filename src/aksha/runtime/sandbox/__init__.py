"""The sandbox and execution environment (ADR-P12): where an allowed program may reach.

The policy gate decides whether a program runs; the sandbox decides what it can reach.
Built: ``types.py`` (``SandboxNeeds``, ``SandboxPolicy``, ``Availability`` and the
declared backend and environment specs). Planned: the ``SandboxBackend`` port; backends
are adapters.
"""
