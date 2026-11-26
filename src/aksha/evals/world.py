"""Worlds: a fixture world on disk, its lock, its rename map and its change sets.

A world is ``fixtures/worlds/<name>/``: ``sources/`` is the only tool root, and
``spec.yaml``, ``manifest.json``, ``key.json`` and ``world.lock.json`` sit beside it, out of
every tool's reach. ``world.lock.json`` records the SHA-256 of every other file under the
world's root, change sets and their derived keys included, so the grader's gate can refuse
an episode run against a world that changed (``world_lock_mismatch``).

A variant world (``stillroom-v2``) names its base in its key (``variant_of``) and carries
the rename map in its ``spec.yaml``. The path map (file names that carry renamed names,
``comp05-brightfield.html`` to ``comp05-lumenstat.html``) is derived from the rename map's
domain entries and checked against both worlds' file lists.

A change set (``<world>/changes/<name>/changeset.json``) lists files to add or replace in a
running episode's world copy, and may carry its own derived key.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from aksha.evals.errors import EvalError

LOCK_FILE = "world.lock.json"
OUT_OF_REACH = ("key.json", "spec.yaml", "manifest.json", LOCK_FILE)
"""Files beside ``sources/`` that no tool path may resolve to."""


@dataclass(frozen=True)
class World:
    """One fixture world, loaded. ``cluster`` is the base world; variants share it."""

    name: str
    root: Path
    cluster: str
    variant_of: str | None
    frozen_today: date
    lock: Mapping[str, str] = field(default_factory=dict)

    @property
    def sources(self) -> Path:
        """``root / "sources"``: the only folder a tool may read."""
        return self.root / "sources"

    @property
    def key_path(self) -> Path:
        """The answer key; never inside the tool root."""
        return self.root / "key.json"

    @property
    def spec_path(self) -> Path:
        """The author-side spec, holding the rename map for a variant."""
        return self.root / "spec.yaml"


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def compute_lock(root: Path) -> dict[str, str]:
    """The SHA-256 of every file under ``root`` except the lock itself.

    Change sets (``changes/``) are covered too: a change set and its derived key are part
    of what an episode in that world can be graded against.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or rel == LOCK_FILE:
            continue
        out[rel] = _sha256(path)
    return out


def write_lock(root: Path) -> Path:
    """Write ``world.lock.json`` for the world at ``root``; returns its path."""
    lock = {"world": root.name, "files": compute_lock(root)}
    path = root / LOCK_FILE
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_world(name: str, fixtures_root: Path) -> World:
    """Load ``fixtures_root/worlds/<name>``; refuses ``world_missing`` if it is not there."""
    root = fixtures_root / "worlds" / name
    if not (root / "sources").is_dir() or not (root / "key.json").is_file():
        raise EvalError("world_missing", f"no world {name!r} under {fixtures_root / 'worlds'}")
    key = json.loads((root / "key.json").read_text(encoding="utf-8"))
    variant_of = key.get("variant_of")
    lock_path = root / LOCK_FILE
    lock: dict[str, str] = {}
    if lock_path.is_file():
        lock = dict(json.loads(lock_path.read_text(encoding="utf-8")).get("files", {}))
    return World(
        name=name,
        root=root,
        cluster=str(variant_of or name),
        variant_of=str(variant_of) if variant_of else None,
        frozen_today=date.fromisoformat(str(key["frozen_today"])),
        lock=lock,
    )


def verify_lock(world: World) -> list[str]:
    """Paths whose hash differs from the lock, are missing, or are new; empty when it holds.

    A world with no lock at all reports ``world.lock.json`` itself.
    """
    if not world.lock:
        return [LOCK_FILE]
    now = compute_lock(world.root)
    return sorted(p for p in set(now) | set(world.lock) if now.get(p) != world.lock.get(p))


def lock_digest(world: World) -> str:
    """One hash over the world's lock, recorded on every result row."""
    body = json.dumps(dict(world.lock), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


# --- rename and path maps --------------------------------------------------------------


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        quote = text[0]
        text = text[1:-1]
        if quote == "'":
            text = text.replace("''", "'")
    return text


def parse_rename_map(spec_text: str) -> dict[str, str]:
    """The ``rename_map:`` block of a variant's ``spec.yaml``: base string to variant string.

    The block is a mapping of names to two-item lists (base, variant). Only that block is
    read, with a small parser for exactly this shape, so the package needs no YAML library.
    """
    lines = spec_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "rename_map:")
    except StopIteration:
        return {}
    pairs: dict[str, str] = {}
    current: list[str] = []

    def flush() -> None:
        if len(current) == 2 and current[0] != current[1]:
            pairs[current[0]] = current[1]

    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            current.append(_scalar(stripped[2:]))
        elif stripped.endswith(":"):
            flush()
            current = []
    flush()
    return pairs


def _stems(rename: Mapping[str, str]) -> dict[str, str]:
    """Domain stems (``kelvra`` to ``thermyx``) from the map's ``*.example`` entries."""
    out: dict[str, str] = {}
    for base, variant in rename.items():
        if base.endswith(".example") and variant.endswith(".example") and "@" not in base:
            b = base.removesuffix(".example").replace(".", "")
            v = variant.removesuffix(".example").replace(".", "")
            if b != v:
                out[b] = v
    return out


def derive_path_map(base: World, variant: World, rename: Mapping[str, str]) -> dict[str, str]:
    """Base path to variant path for every source file whose name the rename changes.

    A base file present under the same path in the variant maps to itself and is left out.
    A base file whose renamed path is not in the variant is refused (``path_map_broken``),
    so a rename that the map cannot explain is caught, not guessed.
    """
    stems = _stems(rename)
    base_files = {p.relative_to(base.sources).as_posix() for p in base.sources.rglob("*")}
    var_files = {p.relative_to(variant.sources).as_posix() for p in variant.sources.rglob("*")}
    out: dict[str, str] = {}
    for rel in sorted(base_files - var_files):
        mapped = rel
        for b, v in sorted(stems.items(), key=lambda kv: -len(kv[0])):
            mapped = mapped.replace(b, v)
        if mapped not in var_files:
            raise EvalError("path_map_broken", f"{rel}: no variant file {mapped}")
        out[rel] = mapped
    return out


@dataclass(frozen=True)
class Renaming:
    """The two maps that carry a base world's names and paths into its variant."""

    names: Mapping[str, str]
    paths: Mapping[str, str]

    def text(self, value: str) -> str:
        """``value`` with every base name replaced by its variant name, longest first."""
        if not self.names:
            return value
        pattern = _pattern(tuple(sorted(self.names, key=len, reverse=True)))
        return pattern.sub(lambda m: self.names[m.group(0)], value)

    def path(self, value: str) -> str:
        """A source path through the path map (unchanged when it is not renamed)."""
        return self.paths.get(value, value)

    def apply(self, value: Any) -> Any:
        """Every string in a JSON-shaped ``value`` through both maps."""
        if isinstance(value, str):
            return self.text(self.path(value))
        if isinstance(value, Mapping):
            return {self.apply(k): self.apply(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.apply(v) for v in value]
        return value


_PATTERNS: dict[tuple[str, ...], re.Pattern[str]] = {}


def _pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    if names not in _PATTERNS:
        alternatives = "|".join(re.escape(n) for n in names)
        _PATTERNS[names] = re.compile(rf"(?<![\w.@-])(?:{alternatives})(?![\w@-])")
    return _PATTERNS[names]


IDENTITY = Renaming(names={}, paths={})
"""The renaming of a world into itself."""


def renaming(base: World, variant: World) -> Renaming:
    """The rename map from the variant's spec and the path map derived from it."""
    if variant.variant_of != base.name:
        raise EvalError("not_a_variant", f"{variant.name} is not a variant of {base.name}")
    names = parse_rename_map(variant.spec_path.read_text(encoding="utf-8"))
    return Renaming(names=names, paths=derive_path_map(base, variant, names))


# --- change sets -----------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeSet:
    """Files to add or replace in a running episode's world copy.

    ``added`` and ``replaced`` map a path under ``sources/`` to the file holding the new
    content. ``key`` is the change set's own derived key when it has one.
    """

    name: str
    added: Mapping[str, Path]
    replaced: Mapping[str, Path]
    key: Path | None
    derived_from: str | None


def load_change_set(world: World, name: str) -> ChangeSet:
    """Load ``<world>/changes/<name>/changeset.json``.

    A target is checked against the episode's path guard when it is applied
    (:meth:`aksha.evals.environment.Environment.apply_change`); a content file must sit
    inside the change set's folder. The file lists ``added`` and ``replaced`` as maps from
    a source path to a file under the change set's folder, and optionally ``derived_from``.
    A ``key.json`` beside it is the derived key.
    """
    folder = world.root / "changes" / name
    meta_path = folder / "changeset.json"
    if not meta_path.is_file():
        raise EvalError("change_set_missing", f"no change set {name!r} in world {world.name}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    def files(section: str) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for target, src in dict(meta.get(section, {})).items():
            path = folder / str(src)
            if not path.resolve().is_relative_to(folder.resolve()):
                raise EvalError("change_set_broken", f"{name}: {src} is outside the change set")
            if not path.is_file():
                raise EvalError("change_set_broken", f"{name}: {src} is missing")
            out[str(target)] = path
        return out

    key = folder / "key.json"
    return ChangeSet(
        name=name,
        added=files("added"),
        replaced=files("replaced"),
        key=key if key.is_file() else None,
        derived_from=meta.get("derived_from"),
    )
