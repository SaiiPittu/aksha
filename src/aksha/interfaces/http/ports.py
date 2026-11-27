"""The HTTP adapter's ports: what the server keeps about a session, and who a command is by.

A session's archive state and its title override are a person's view settings, not facts
of the runtime: they live in the server's own store (the accounts store implements this
port), never in the event store. Every method names the owner, so one owner never reads
another's settings.
"""

from __future__ import annotations

from typing import Protocol

from aksha.runtime.session.named import NamedCommand


class SessionMetaPort(Protocol):
    """Per-owner settings of a session: archived or not, and a title a person gave it."""

    def archived(self, owner: str, session: str) -> bool:
        """Whether ``owner`` archived ``session`` (False when never set)."""
        ...

    def set_archived(self, owner: str, session: str, archived: bool) -> None:
        """Archive ``session`` for ``owner``, or restore it."""
        ...

    def title(self, owner: str, session: str) -> str | None:
        """The title ``owner`` gave ``session``, or None for the default."""
        ...

    def set_title(self, owner: str, session: str, title: str | None) -> None:
        """Set ``session``'s title for ``owner``; None goes back to the default."""
        ...


class ByBinder(Protocol):
    """Sets a command's ``by`` from the principal that sent it (``auth.ByRule``)."""

    def bind(self, command: NamedCommand) -> NamedCommand:
        """``command`` as the principal may send it; ``WireInvalid`` (``by_not_allowed``) if not."""
        ...
