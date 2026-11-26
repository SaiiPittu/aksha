"""Refusals of the model interface (ADR-P07), each with a stable code.

What an adapter raises: ``TransientModelError`` (``model_transient``, retried twice with
backoff on the injected clock, then ``model_unavailable``), ``PermanentModelError``
(``model_permanent``, or a more specific code such as ``script_exhausted``,
``replay_missing`` or ``replay_mismatch``) and ``ContextOverflow`` (``context_overflow``).
What the binder raises: ``BindRefused`` (``bind_refused``), listing every
:class:`BindRefusal` at once. What building a request raises: ``OptionsUnavailable``
(``options_unavailable``). No message ever carries a key or a configuration value.
"""

from __future__ import annotations

from dataclasses import dataclass

from aksha.runtime.errors import AkshaError


class TransientModelError(AkshaError):
    """A failure worth retrying: a timeout, a rate limit, a server error."""

    default_code = "model_transient"


class PermanentModelError(AkshaError):
    """A failure no retry fixes: a refused request, a missing replay, a used-up script."""

    default_code = "model_permanent"


class ContextOverflow(AkshaError):
    """The request is larger than the model's context window."""

    default_code = "context_overflow"


@dataclass(frozen=True)
class BindRefusal:
    """One reason an alias cannot be bound: ``unbound_alias``, ``model_class_mismatch``, ..."""

    alias: str
    code: str
    message: str


class BindRefused(AkshaError):
    """The run mode's adapters do not fit the agent's aliases; every refusal is listed."""

    default_code = "bind_refused"

    def __init__(self, refusals: tuple[BindRefusal, ...]) -> None:
        """Hold every refusal; the message lists their codes, each with what to do."""
        self.refusals = refusals
        super().__init__(
            message="; ".join(
                f"{r.alias}: {r.code}" + (f" ({r.message})" if r.message else "") for r in refusals
            )
        )


class OptionsUnavailable(AkshaError):
    """An ``options_from`` question's artifact is not saved yet, or lists under two options."""

    default_code = "options_unavailable"
