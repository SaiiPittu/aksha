"""No value of any synthetic world reaches the runtime, the agents' logic, their prompts or the app.

``test_agent_generality`` checks a hand-kept list of terms against the agents' logic. This
guard needs no list: it reads every world under ``src/aksha/data/worlds`` (its spec, answer
key, manifest and sources) and extracts the values that would only be there because someone
had that world in mind while writing code:

- names: the venture, its product and domain, people and employers, competitors and their
  aliases (proper nouns only: a value with no capital letter, digit or dot is a common word);
- identifiers: candidate, publication, priority, event and fact IDs, and their prefixes
  (``SYN-EV``, ``BRK``), as the key and the spec give them;
- file names: every source file's name and stem, when the stem is distinctive (it has a
  separator or a digit; a one-word stem is either a name, found above, or a common word);
- shapes: snake_case or camelCase column headers of delimited sources and keys of JSON
  sources, the vocabulary a reader written for one export would hard-code;
- numbers: the key's stated values, when distinctive (three or more significant digits, or
  a decimal fraction of two or more places), matched only as numeric literals or as numbers
  inside string literals of Python code.

It scans ``src/aksha/runtime/**``, ``src/aksha/agents/**`` (definitions, logic and prompts)
and ``src/aksha/app.py``. The offline models (``fakes.py``, ``fakes_*.py``) are test doubles
and are exempt here; whether they are general is measured by running them on held-out
worlds, not by this guard. Everything a world holds is synthetic.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "aksha"
WORLDS_DIR = SRC / "data" / "worlds"

COMMON = frozenset(
    {
        # words the spec and key use as values that are not names
        "contractor", "synthetic", "true", "false", "none", "null", "unknown", "other",
        # generic header and key vocabulary no world owns
        "created_at", "updated_at", "page_published", "per_as_written", "person_id",
        "file_name", "file_path", "content_type",
        # the runtime's own field names, which a world's export happens to share
        "as_of", "record_id", "event_type", "schema_version", "review_id",
        # bibliographic field names of patent publications in general (WIPO ST.9 terms)
        "kind_code", "priority_claims", "depends_on", "publication_number",
    }
)  # fmt: skip
"""Extracted values that are ordinary vocabulary, not a world's own."""

KNOWN_LEAKS: dict[tuple[str, str], str] = {}
"""Leaks found when this guard was written, by (file, lowercased value), each to be removed.

A known leak passes ``test_names_no_world_value`` but fails ``test_known_leaks_still_exist``
once it is gone, so the list only shrinks: delete the entry with the fix."""

_ID = re.compile(r"\b(?:SYN-[A-Z]+(?:-[A-Za-z0-9]+)+|[A-Z]{2,5}-\d{1,4}[A-Z]?)\b")
_NAMED = re.compile(
    r"(?:^|[{,]\s*|^\s*-?\s*)(?:name|product|employer|domain|canonical)\s*:\s*'?\"?([^,}\n'\"#]+)",
    re.M,
)
_SNAKE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_CAMEL = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]+)+$")


def _proper(value: str) -> bool:
    v = value.strip()
    return len(v) >= 3 and v.lower() not in COMMON and bool(re.search(r"[A-Z0-9.]", v))


def _stem(name: str) -> str:
    return name.split(".", 1)[0] if not name.startswith(".") else name


def _walk_json(obj: object) -> Iterator[tuple[str | None, object]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from _walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield None, v
            yield from _walk_json(v)


def _headers(text: str) -> list[str]:
    for line in text.splitlines():
        if not line.strip():
            continue
        delim = max(",;\t|", key=line.count)
        if line.count(delim) < 1:
            continue
        cells = next(csv.reader(io.StringIO(line), delimiter=delim), [])
        heads = [c.strip() for c in cells if c.strip()]
        if len(heads) >= 2 and all(
            _SNAKE.match(h) or _CAMEL.match(h) or h.isalpha() for h in heads
        ):
            return heads
    return []


def _numbers(value: object) -> Iterator[str]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        text = repr(value)
        digits = text.lstrip("-").replace(".", "").lstrip("0")
        frac = text.split(".")[1] if "." in text else ""
        if (abs(value) >= 100 and len(digits.rstrip("0")) >= 3) or (len(frac) >= 2 and frac != "0"):
            yield text
    elif isinstance(value, dict):
        for v in value.values():
            yield from _numbers(v)
    elif isinstance(value, list):
        for v in value:
            yield from _numbers(v)


@cache
def world_tokens() -> dict[str, dict[str, set[str]]]:
    """Per world, per kind (``name``, ``id``, ``file``, ``shape``, ``number``), its values."""
    out: dict[str, dict[str, set[str]]] = {}
    for world in sorted(p for p in WORLDS_DIR.iterdir() if (p / "key.json").is_file()):
        got: dict[str, set[str]] = {k: set() for k in ("name", "id", "file", "shape", "number")}
        spec = (
            (world / "spec.yaml").read_text(encoding="utf-8")
            if (world / "spec.yaml").is_file()
            else ""
        )
        key = json.loads((world / "key.json").read_text(encoding="utf-8"))
        got["name"].add(world.name)
        for m in _NAMED.finditer(spec):
            value = m.group(1).strip().rstrip(".")
            if _proper(value):
                got["name"].add(value)
        for comp in (key.get("competitors") or {}).values():
            for value in [comp.get("canonical", ""), *comp.get("aliases", [])]:
                if _proper(value):
                    got["name"].add(value)
        key_text = json.dumps(key)
        for m in _ID.finditer(spec + "\n" + key_text):
            ident = m.group(0)
            if ident.startswith("T-"):  # trap IDs are the grader's vocabulary
                continue
            got["id"].add(ident)
            prefix = re.sub(r"-\d+[A-Z]?$", "", ident)
            if prefix != ident and len(prefix) >= 3 and not prefix.startswith("SYN-PUB"):
                got["id"].add(prefix)
        for fact in key.get("facts", []):
            got["number"].update(_numbers(fact.get("value")))
        sources = world / "sources"
        for path in sorted(p for p in sources.rglob("*") if p.is_file()):
            stem = _stem(path.name)
            if re.search(r"[-_\d]", stem):
                got["file"].update({path.name, stem})
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix in (".csv", ".tsv") or path.name.endswith((".csv.txt", ".tsv.txt")):
                got["shape"].update(h for h in _headers(text) if _SNAKE.match(h) or _CAMEL.match(h))
            if path.suffix in (".json", ".jsonl"):
                docs = []
                try:
                    docs = [json.loads(text)]
                except json.JSONDecodeError:
                    docs = [
                        json.loads(ln) for ln in text.splitlines() if ln.strip().startswith("{")
                    ]
                for doc in docs:
                    for k, _ in _walk_json(doc):
                        if k and (_SNAKE.match(k) or _CAMEL.match(k)):
                            got["shape"].add(k)
        for kind in got:
            got[kind] = {t for t in got[kind] if t.lower() not in COMMON}
        out[world.name] = got
    return out


def scanned_files() -> list[Path]:
    """The runtime, the agents (logic, definitions, prompts; not the fakes) and the app."""
    files = [
        p
        for p in (SRC / "runtime").rglob("*")
        if p.is_file() and p.suffix in (".py", ".md", ".json", ".toml", ".yaml")
    ]
    files += [
        p
        for p in (SRC / "agents").rglob("*")
        if p.is_file()
        and p.suffix in (".py", ".md", ".json", ".toml", ".yaml")
        and not p.name.startswith("fakes")
    ]
    files.append(SRC / "app.py")
    return sorted(set(files))


@cache  # about a thousand tokens: more than the re module's own cache keeps compiled
def _text_pattern(token: str) -> re.Pattern[str]:
    flags = 0 if len(token) < 5 or token.isupper() else re.I
    return re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w])", flags)


def _code_numbers(path: Path, text: str) -> set[str]:
    if path.suffix != ".py":
        return set()
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                found.add(repr(node.value))
            elif isinstance(node.value, str):
                found.update(
                    re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])", node.value.replace(",", ""))
                )
    return found


def leaks(text: str, path: Path | None = None) -> list[tuple[str, str, str]]:
    """Every (world, kind, value) whose value the text names."""
    hits = []
    numbers = _code_numbers(path, text) if path is not None else set()
    lowered = text.lower() if text.isascii() else None
    for world, kinds in world_tokens().items():
        for kind, values in kinds.items():
            for value in sorted(values):
                if kind == "number":
                    if value in numbers:
                        hits.append((world, kind, value))
                elif _may_name(value, text, lowered) and _text_pattern(value).search(text):
                    hits.append((world, kind, value))
    return hits


def _may_name(token: str, text: str, lowered: str | None) -> bool:
    """A substring test that is False only where the token's pattern cannot match.

    It only skips the slow regex search: a case-sensitive pattern needs the token as it is,
    and a case-insensitive one needs it lowercased when token and text are ASCII (outside
    ASCII, case folding pairs other characters, so the regex decides).
    """
    if _text_pattern(token).flags & re.I == 0:
        return token in text
    if lowered is not None and token.isascii():
        return token.lower() in lowered
    return True


def test_every_world_yields_tokens() -> None:
    tokens = world_tokens()
    assert len(tokens) >= 10
    for world, kinds in tokens.items():
        assert len(kinds["name"]) >= 3, world
        assert len(kinds["id"]) >= 3, world
        assert len(kinds["file"]) >= 3, world


def _rel(path: Path) -> str:
    return str(path.relative_to(SRC))


@pytest.mark.parametrize("path", scanned_files(), ids=_rel)
def test_names_no_world_value(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [h for h in leaks(text, path) if (_rel(path), h[2].lower()) not in KNOWN_LEAKS]
    assert found == []


def test_known_leaks_still_exist() -> None:
    present = {
        (_rel(p), value.lower())
        for p in scanned_files()
        for _w, _k, value in leaks(p.read_text(encoding="utf-8"), p)
    }
    assert sorted(set(KNOWN_LEAKS) - present) == []


def test_the_guard_catches_planted_values() -> None:
    tokens = world_tokens()
    world = sorted(tokens)[0]
    name = sorted(tokens[world]["name"])[0]
    ident = sorted(tokens[world]["id"])[0]
    file = sorted(tokens[world]["file"])[0]
    planted = f'VENDOR = "{name}"  # see {file}\nPATTERN = r"{ident}"\n'
    kinds = {(w, k) for w, k, _ in leaks(planted, Path("planted.py"))}
    assert {(world, "name"), (world, "id"), (world, "file")} <= kinds
    assert leaks("def read(rows):\n    return [r for r in rows if r]\n", Path("clean.py")) == []


FAKE_KNOWN_LEAKS: dict[tuple[str, str], str] = {
    ("agents/patent/fakes.py", "stillroom"): "names the hand-written lexicon it falls back to",
    ("agents/patent/fakes_readers.py", "source_url"): "a publication key, read before item 19",
    ("agents/patent/fakes_readers.py", "status_observations"): "the same, a status list key",
    ("agents/research/fakes.py", "country_code"): "a function name that a world's header shares",
    ("agents/research/fakes.py", "siltgrove"): "a docstring example",
    ("agents/research/fakes.py", "siltgrove inc."): "a docstring example",
    ("agents/research/fakes_readers.py", "brackpond"): "a docstring example",
    ("agents/research/fakes_readers.py", "siltgrove"): "a docstring example",
    ("agents/research/fakes_readers.py", "siltgrove inc."): "a docstring example",
    ("agents/research/fakes_readers.py", "siltgrove logs"): "a docstring example",
}
"""Values the offline models named before they were scanned, by (file, lowercased value).

The fakes are measured on held-out worlds, so a held-out world's own value in a fake would
tune it to that world and make the measurement meaningless. This list only shrinks."""


def fake_files() -> list[Path]:
    """The offline models, except the stillroom lexicon, which is one world's by design."""
    return sorted(p for p in (SRC / "agents").rglob("fakes*.py") if p.name != "fakes_stillroom.py")


@pytest.mark.parametrize("path", fake_files(), ids=_rel)
def test_offline_models_name_no_world_value(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [h for h in leaks(text, path) if (_rel(path), h[2].lower()) not in FAKE_KNOWN_LEAKS]
    assert found == []


def test_known_fake_leaks_still_exist() -> None:
    present = {
        (_rel(p), value.lower())
        for p in fake_files()
        for _w, _k, value in leaks(p.read_text(encoding="utf-8"), p)
    }
    assert sorted(set(FAKE_KNOWN_LEAKS) - present) == []
