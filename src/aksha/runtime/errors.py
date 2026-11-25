"""The base error every runtime refusal derives from.

Every runtime error carries a stable snake_case ``code`` (``already_recorded``,
``payload_invalid``, ``not_allowed``) that tests, the CLI and the model read; the message
is for people. Subsystems define their own subclasses next to their code.
"""

from __future__ import annotations


class AkshaError(Exception):
    """A refusal with a stable ``code``.

    Subclasses set ``default_code``; a caller may still pass a more specific code, as
    :class:`aksha.runtime.storage.errors.StoreError` does for its many small refusals.
    """

    default_code = "aksha_error"

    def __init__(self, code: str | None = None, message: str = "") -> None:
        """Build the error from a code (or the class default) and a human message."""
        self.code = code or self.default_code
        self.message = message
        super().__init__(f"{self.code}: {message}" if message else self.code)
