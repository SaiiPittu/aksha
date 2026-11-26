"""The agent definition version hash, and the fields it puts on ``run.started`` (D7).

The hash is SHA-256 over the canonical list of (relative path, file hash) for every file
in the agent folder except ``tests/`` and bytecode, each imported extension's name,
version and file hashes, the tool registry's canonical hash (ADR-P10 D4) and
``API_VERSION``. File hashes are used, never only version
strings, because a version string can stay the same while the code changes. A run never
resumes under a different hash.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.definitions.versions import API_VERSION

if TYPE_CHECKING:
    from aksha.runtime.definitions.loader import LoadedDefinition, LoadedExtension

FileHashes = tuple[tuple[str, str], ...]
"""(relative POSIX path, hex SHA-256 of the bytes), sorted by path."""

_SKIPPED_DIRS = frozenset({"tests", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})


def file_hashes(folder: Path) -> FileHashes:
    """Hash every file under ``folder``, skipping a top-level ``tests/`` and every cache."""
    out: list[tuple[str, str]] = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if not path.is_file() or path.suffix == ".pyc":
            continue
        if rel.parts[0] == "tests" or any(p in _SKIPPED_DIRS - {"tests"} for p in rel.parts):
            continue
        out.append((rel.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(out)


def tree_hash(hashes: FileHashes) -> str:
    """``sha256:<hex>`` over a list of file hashes."""
    return "sha256:" + sha256_hex([list(pair) for pair in hashes])


def definition_hash(
    folder: Path,
    extensions: Sequence[LoadedExtension],
    api_version: str,
    *,
    tools_hash: str,
    context_hash: str | None = None,
    invalidation: str | None = None,
    invalidation_keys: str | None = None,
) -> str:
    """The agent definition version hash of the agent in ``folder`` (``sha256:<64 hex>``).

    ``tools_hash`` is ``ToolRegistry.canonical_hash()``, so a changed tool set or schema
    changes the hash even where file hashes alone would not show it. ``context_hash`` is
    ``context.policy.policy_hash`` of the agent's context policy (ADR-P08 D9): it carries
    the runtime's ``CONTEXT_POLICY_VERSION``, which no file of the agent shows.
    ``invalidation`` is the agent's resolved invalidation mode (decision 38): a definition
    that declares none gets the runtime's default, which no file of the agent shows either.
    ``invalidation_keys`` (ADR-P05 M2) is hashed only when it is not the default ``reads``,
    so a definition written before the setting keeps its hash.
    """
    material = {
        "agent": [list(pair) for pair in file_hashes(folder)],
        "extensions": [
            {
                "name": e.ext.name,
                "version": e.ext.version,
                "files": [list(p) for p in e.file_hashes],
            }
            for e in sorted(extensions, key=lambda e: e.ext.name)
        ],
        "tools": tools_hash,
        "context": context_hash,
        "api_version": api_version,
    }
    if invalidation is not None:
        material["invalidation"] = invalidation
    if invalidation_keys is not None and invalidation_keys != "reads":
        material["invalidation_keys"] = invalidation_keys
    return "sha256:" + sha256_hex(material)


def run_started_fields(
    loaded: LoadedDefinition, *, runtime_commit: str, dirty: bool
) -> Mapping[str, Any]:
    """The fields D7 adds to ``run.started``; the composition root reads the commit.

    ``invalidation`` is the agent's mode (decision 38), so every reader of the log derives
    what invalidation the run gets from the run's own facts.
    """
    return {
        "definition_hash": loaded.definition_hash,
        "extension_versions": {
            e.ext.name: {"version": e.ext.version, "hash": tree_hash(e.file_hashes)}
            for e in loaded.extensions
        },
        "api_version": API_VERSION,
        "runtime_commit": runtime_commit,
        "dirty": dirty,
        "invalidation": loaded.agent.invalidation,
        "invalidation_keys": loaded.agent.invalidation_keys,
    }


def resume_refusal(recorded: str | None, loaded: str) -> str | None:
    """``definition_changed`` when a run recorded a different hash (or none); else None."""
    return None if recorded == loaded else "definition_changed"
