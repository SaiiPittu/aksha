"""The sandbox and execution environment (ADR-P12, P28): tier 3's port, in the runtime core.

The policy gate decides whether a program runs; the sandbox decides what it can reach.
Built: the values (``types.py``), the ``Sandbox`` port and ``ExecAPI`` (``ports.py``),
``effective_policy`` (``policy.py``), the ``refuse`` backend and ``select_backend``, the
exec API with its kill boundary (``exec.py``), environment images (``images.py``) and
``ExecutionTier`` (``tier.py``). Backends are adapters (``aksha.adapters.sandbox``).
Planned: the ``container`` and ``vm`` backends, the materialisation cache and nested tree
manifests.
"""
