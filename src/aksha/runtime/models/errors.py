"""Refusals of the model interface (ADR-P07), each with a stable code.

What an adapter raises: ``TransientModelError`` (``model_transient``, retried by the model
step with backoff on the injected clock, then ``model_unavailable``), ``PermanentModelError``
(``model_permanent``, or a more specific code such as ``script_exhausted``,
``replay_missing`` or ``replay_mismatch``) and ``ContextOverflow`` (``context_overflow``).
What the binder raises: ``BindRefused`` (``bind_refused``), listing every
:class:`BindRefusal` at once. What building a request raises: ``OptionsUnavailable``
(``options_unavailable``). No message ever carries a key or a configuration value. A live
adapter may attach the provider's :data:`ErrorDetail` (``detail``), which the model step
records on ``step.failed{error}`` or ``step.completed{error}``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aksha.runtime.errors import AkshaError

ErrorDetail = Mapping[str, str | int]
"""What a live adapter says about a provider's refusal, for the step's record: ``type``
(the provider library's error class), ``status`` (the HTTP status), ``provider`` and a
short ``message``. Never a key, a header or a request body (``failure_detail``)."""

DETAIL_FIELDS = frozenset({"type", "status", "provider", "message"})
DETAIL_MESSAGE_CHARS = 300


def failure_detail(detail: Mapping[str, object] | None) -> dict[str, str | int] | None:
    """``detail`` cut to the recorded shape: known fields, a status as an integer, short text."""
    if not detail:
        return None
    out: dict[str, str | int] = {}
    for name in sorted(DETAIL_FIELDS & set(detail)):
        value = detail[name]
        if name == "status":
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1000:
                out[name] = value
        elif isinstance(value, str) and value:
            out[name] = value[: 80 if name != "message" else DETAIL_MESSAGE_CHARS]
    return out or None


class _ModelError(AkshaError):
    """A model refusal that may carry the provider's :data:`ErrorDetail` for the record."""

    def __init__(
        self,
        code: str | None = None,
        message: str = "",
        *,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """As :class:`AkshaError`, plus ``detail`` (cut by :func:`failure_detail`)."""
        super().__init__(code, message)
        self.detail = failure_detail(detail)


class TransientModelError(_ModelError):
    """A failure worth retrying: a timeout, a rate limit, a server error.

    ``retry_after_ms`` is how long the provider asked to wait (its ``Retry-After``), when
    it said; the model step's backoff is at least that.
    """

    default_code = "model_transient"

    def __init__(
        self,
        code: str | None = None,
        message: str = "",
        *,
        detail: Mapping[str, object] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        """As :class:`_ModelError`, plus ``retry_after_ms``."""
        super().__init__(code, message, detail=detail)
        self.retry_after_ms = retry_after_ms if retry_after_ms and retry_after_ms > 0 else None


class PermanentModelError(_ModelError):
    """A failure no retry fixes: a refused request, a missing replay, a used-up script."""

    default_code = "model_permanent"


class ContextOverflow(_ModelError):
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
