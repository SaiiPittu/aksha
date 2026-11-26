"""Refusals of the request router, each with a stable code.

``RouterInvalid``: ``route_agents_invalid`` (under two agents, a name twice, or an empty
purpose line) and ``route_model_invalid`` (the model is not a classifier that returns a
distribution). ``MessageUnknown`` (``message_unknown``): no such message in the session.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class RouterInvalid(AkshaError):
    """The router cannot be built from what it was given."""

    default_code = "route_agents_invalid"


class MessageUnknown(AkshaError):
    """``route_message`` was asked about a message the session does not hold."""

    default_code = "message_unknown"
