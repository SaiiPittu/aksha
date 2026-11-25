"""Argument validation against a tool's published schema, and JSON pointers (RFC 6901).

``schema_refusal`` turns every schema error into one ``Err`` with code ``schema`` that
lists each failing path, so the model can fix all of them in its next turn. A missing
required property is reported at its own path (``/url``), not at its parent object.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from aksha.runtime.tools.types import Err

_POINTER = re.compile(r"^(/([^~/]|~[01])*)*$")


def is_json_pointer(text: str) -> bool:
    """True for ``""`` (the whole document) or ``/a/0/b~1c``; ``~`` must be ``~0`` or ``~1``."""
    return bool(_POINTER.fullmatch(text))


def pointer_of(path: Iterable[str | int]) -> str:
    """The JSON pointer of a path of keys and indexes: ``["a", 0]`` -> ``/a/0``."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _paths(error: ValidationError) -> list[str]:
    """The argument paths one error is about; a missing required property names its own."""
    base = list(error.absolute_path)
    if error.validator == "required" and isinstance(error.instance, Mapping):
        wanted = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [name for name in wanted if name not in error.instance]
        return [pointer_of([*base, name]) for name in missing]
    if error.validator == "additionalProperties" and isinstance(error.instance, Mapping):
        allowed = error.schema.get("properties", {}) if isinstance(error.schema, Mapping) else {}
        extra = [name for name in error.instance if name not in allowed]
        return [pointer_of([*base, name]) for name in extra] or [pointer_of(base)]
    return [pointer_of(base)]


def schema_refusal(validator: Draft202012Validator, args: Mapping[str, Any]) -> Err | None:
    """None when ``args`` match the schema, else one ``schema`` error naming every path."""
    found: dict[str, str] = {}
    for error in validator.iter_errors(args):
        for path in _paths(error):
            found.setdefault(path, error.message)
    if not found:
        return None
    paths = tuple(sorted(found))
    message = "; ".join(f"{path or '/'}: {found[path]}" for path in paths)
    return Err("schema", f"arguments do not match the schema: {message}", paths=paths)
