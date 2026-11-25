"""Refusals of the session API, each with a stable code.

``CommandIdReused`` (``command_id_reused``): a command ID already names another command
(another name or other arguments), so the store's receipt is not this command's.
``CommandNameMismatch`` (``command_name_mismatch``): a command dispatched with the spec of
another command.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class CommandIdReused(AkshaError):
    """The command ID's receipt belongs to a different command; nothing is written."""

    default_code = "command_id_reused"


class CommandNameMismatch(AkshaError):
    """The spec passed to ``dispatch`` is not the command's own."""

    default_code = "command_name_mismatch"
