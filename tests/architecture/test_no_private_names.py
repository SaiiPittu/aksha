"""No tracked text file names the organisation that set the assignment, or its people.

The pages say "the assignment" instead. The names themselves are never written in the
repository: this test holds only the SHA-256 hashes of their lower-case forms, and checks
every word and every pair of adjacent words in every tracked or new (not ignored) text file
against them. ``scratch-pad/`` is exempt. A word is a run of ASCII letters and digits, so
punctuation such as a dot in a domain splits it into two words, which the pair check covers.
"""

from __future__ import annotations

import functools
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXEMPT = ("scratch-pad/",)

# SHA-256 of each lower-case name: words, and two-word names joined by one space.
NAME_HASHES = frozenset(
    {
        "6171df4ff59d1f469d08386d5043214364d52dc8400e115349ada2b90a178ff7",
        "497e485a6dc43feb769b636684258f2e7b3aa9048b654446cd2ae26c78e8a123",
        "935e5c2636ab0e532c3e3e923e7c00cca0ece4e688c52dce17e308c59810cb5e",
        "a3d289174b7f52ba022b64dfe9560ce358d27bc181c9e05561c21f84c491aa12",
        "c87676b84d4ad90741c8334d327b4a916c599f53d2deea7e988a55a1afa214ae",
        "316ecc7df4d5e0d1eb3cb6136ece9a6973678e88779cc74e2052eb4356d04627",
        "42570c45c81fdb5d1964f9e7aaead823df063412a3ae28ddca46dadb25bf6282",
        "d390dc28908ce2e33da77b31baf4efe3d401c8ae755d9984f6cf0ddc2542ee8a",
        "6a1dc203aeacc8eb2a67ee823a74a4e7fbee1b7fa800ddb8950b1a0a94459972",
    }
)

_WORD = re.compile(rb"[a-z0-9]+")


@functools.cache
def _hash(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def named_lines(data: bytes, hashes: frozenset[str] = NAME_HASHES) -> list[int]:
    """The 1-based line numbers in ``data`` that hold a name, as one word or two adjacent words."""
    hits: list[int] = []
    words = [(m.group(0), m.start()) for m in _WORD.finditer(data.lower())]
    for i, (word, at) in enumerate(words):
        pair = word + b" " + words[i + 1][0] if i + 1 < len(words) else None
        if _hash(word) in hashes or (pair is not None and _hash(pair) in hashes):
            hits.append(data.count(b"\n", 0, at) + 1)
    return hits


def _files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [name for name in listed.decode().split("\0") if name and not name.startswith(EXEMPT)]


def test_no_text_file_names_them() -> None:
    found: dict[str, list[int]] = {}
    for name in _files():
        path = ROOT / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        if hits := named_lines(data):
            found[name] = hits
    assert found == {}, "replace each name with 'the assignment' (file: line numbers)"


def test_the_guard_catches_a_planted_name() -> None:
    # Stand-in names, so that no real one is written here: a one-word name and a two-word name
    # that a line break or a dot splits.
    planted = frozenset({_hash(b"zorbek"), _hash(b"quell vantry")})
    assert named_lines(b"nothing to see\n", planted) == []
    assert named_lines(b"line one\nask Zorbek.\n", planted) == [2]
    assert named_lines(b"line one\nfrom Quell\nVantry, today\n", planted) == [2]
    assert named_lines(b"see quell.vantry for more", planted) == [1]
    assert named_lines(b"quell alone, vantry alone", planted) == []
    assert len(NAME_HASHES) == 9 and all(len(digest) == 64 for digest in NAME_HASHES)
