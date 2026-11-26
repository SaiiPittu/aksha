"""Refusals of the Task model and scheduler (ADR-P15), each with a stable code.

``TreeInvalid`` (``tree_invalid``, or the first problem's code): ``compile_tree`` found a
tree the v1 scheduler cannot run; it lists every problem, not the first.
``TaskUnknown`` (``task_unknown``): a Task path or a writer the compiled tree does not hold.
``KeyInvalid`` (``key_invalid``): a listed key the instance-ID grammar cannot carry.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class TreeInvalid(AkshaError):
    """The tree cannot be compiled; ``problems`` holds ``(code, path, message)`` for each."""

    default_code = "tree_invalid"

    def __init__(self, problems: tuple[tuple[str, str, str], ...]) -> None:
        """Carry every problem; the error's code is the first one's."""
        self.problems = problems
        text = "; ".join(f"{path}: {message}" for _, path, message in problems)
        super().__init__(problems[0][0] if problems else None, text)


class TaskUnknown(AkshaError):
    """A Task path, or a writer key, that names no Task of the compiled tree."""

    default_code = "task_unknown"


class KeyInvalid(AkshaError):
    """A key holding ``/``, ``[`` or ``]``, or empty: an instance ID could not carry it."""

    default_code = "key_invalid"
