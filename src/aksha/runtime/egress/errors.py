"""``EgressRefused``: a destination the tool's egress policy does not allow (ADR-P26 D4)."""

from __future__ import annotations

from aksha.runtime.errors import AkshaError

EGRESS_REFUSED = "egress_refused"
"""The code of every egress refusal; the model reads it as a tool error, never retried."""


class EgressRefused(AkshaError):
    """The client refused a destination: ``host`` and the check's ``reason``."""

    default_code = EGRESS_REFUSED

    def __init__(self, host: str, reason: str, url: str = "") -> None:
        """Refuse ``host`` for ``reason`` (``host_not_allowed``, ``private_address``, ...)."""
        self.host = host
        self.reason = reason
        self.url = url
        super().__init__(EGRESS_REFUSED, f"{host or url!r}: {reason}")
