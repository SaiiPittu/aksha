"""Egress policy (ADR-P26): which hosts a tool body may reach, through the runtime's client.

Built: ``EgressPolicy`` (deny by default), ``check_destination`` (pure), the ``HttpClient``,
``Resolver`` and ``Transport`` ports, ``PolicedHttpClient`` (checked after resolution and
on every redirect, each decision recorded) and ``RefusingHttpClient``. The resolver and
transport are adapters (``aksha.adapters.egress``).
"""
