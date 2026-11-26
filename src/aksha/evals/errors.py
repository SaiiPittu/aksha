"""Eval refusals, each with a stable code (:class:`aksha.runtime.errors.AkshaError`)."""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class EvalError(AkshaError):
    """Any eval refusal: a bad case file, an unknown agent, a world that will not load."""

    default_code = "eval_error"


class KeyRefMissing(EvalError):
    """A dotted reference into an answer key names a field the key does not have."""

    default_code = "key_ref_missing"


class CaseInvalid(EvalError):
    """A case breaks a pairing rule or names something that does not exist."""

    default_code = "case_invalid"


class PathRefused(EvalError):
    """A tool path resolves outside the episode's tool root (the path guard)."""

    default_code = "path_refused"


class ToolFault(EvalError):
    """A seeded fault switch failed a tool call (ADR-P22 D9)."""

    default_code = "tool_fault"


class Restart(Exception):
    """Raised out of an agent's run when a change step asks for a restart."""
