"""The one refusal of the accounts layer, carried to the HTTP error shape by the server."""

from __future__ import annotations


class AccountError(Exception):
    """A refused account or auth request: its HTTP status, code and message."""

    def __init__(self, status: int, code: str, message: str, details: object = None) -> None:
        """Hold what the error body says."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
