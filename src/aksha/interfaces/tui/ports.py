"""The port the chat app calls: :class:`~aksha.interfaces.tui.client.Api` over HTTP in use."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from aksha.runtime.session.types import CommandReceipt

if TYPE_CHECKING:
    from aksha.interfaces.tui.client import EventStream


class ChatApi(Protocol):
    """What the chat app calls; :class:`Api` over HTTP, or a test double."""

    def send_message(self, session: str, text: str, agent: str | None) -> CommandReceipt:
        """Post a message to a session (starts a routed or named run)."""
        ...

    def steer(self, run: str, text: str, lane: str) -> CommandReceipt:
        """Send a message to an open run in a lane: steer, follow_up or amend."""
        ...

    def answer(
        self,
        interrupt: str,
        *,
        text: str | None = None,
        answers: Mapping[str, str] | None = None,
        agent: str | None = None,
    ) -> CommandReceipt:
        """Answer a question (one text, answers per question, or a route's agent)."""
        ...

    def approve(self, approval: str, signature: str) -> CommandReceipt:
        """Grant an approval for exactly the signature shown."""
        ...

    def reject(self, approval: str, note: str | None) -> CommandReceipt:
        """Refuse an approval."""
        ...

    def stop(self, run: str, reason: str) -> CommandReceipt:
        """Ask a run to stop."""
        ...

    def upload(self, files: Mapping[str, bytes]) -> CommandReceipt:
        """Upload files (relative path to bytes) to the owner's inputs."""
        ...

    def sessions(self) -> list[dict[str, Any]]:
        """The owner's sessions, newest first."""
        ...

    def artifacts(self, session: str) -> list[dict[str, Any]] | None:
        """A session's artifacts, or None when the server has no artifact reads."""
        ...

    def artifact(self, ref: str) -> dict[str, Any] | None:
        """One artifact version, or None when the server has no artifact reads."""
        ...

    def documents(self) -> list[dict[str, Any]] | None:
        """The owner's uploaded documents, or None when the server has no document reads."""
        ...

    def document(self, doc_key: str, version: int | None) -> str | None:
        """One document's text, or None when the server has no document reads."""
        ...

    def events(self, session: str, cursor: int | None, stop: threading.Event) -> EventStream:
        """The session's stream from ``cursor``, ending when ``stop`` is set."""
        ...
