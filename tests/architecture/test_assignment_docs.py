"""The reviewer's guide cannot rot: every path, node ID and link in ``docs/assignment`` exists.

The pages in ``docs/assignment/`` back each claim with a repository path or a pytest node ID
in backticks, and link to each other and to the project docs. This test reads every page
and checks three things:

1. Each backticked token that starts with a top-level folder of the repository (``src/``,
   ``tests/``, ``docs/``, ``examples/``, ``scripts/``, ``web/``) names a file or folder that
   exists. A token with ``*`` must match at least one file. A token holding ``<`` is a
   placeholder and is skipped.
2. Each pytest node ID (``tests/....py::name``, ``tests/....py::Class::name``) names a test
   function in that module, found by parsing it, so this test collects nothing. A short form
   ``::name`` refers to the module of the last full node ID before it on the same page.
3. Each relative Markdown link resolves to a file or folder, and a ``#fragment`` on a link
   to a Markdown page matches one of that page's headings (GitHub's heading anchors).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.architecture.test_requirements_evidence import _names

ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "docs" / "assignment"
TOP = ("src/", "tests/", "docs/", "examples/", "scripts/", "web/")

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_NODE = re.compile(r"(tests/[\w./-]+\.py)((?:::\w+){1,2})(\[[^\]]*\])?")
_SHORT = re.compile(r"(?<![\w/.])::(\w+(?:::\w+)?)")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _code_spans(text: str) -> list[str]:
    """Every inline code span and every fenced block, in page order."""
    spans: list[tuple[int, str]] = [(m.start(), m.group(0)) for m in _FENCE.finditer(text)]
    prose = _FENCE.sub(lambda m: " " * len(m.group(0)), text)
    spans += [(m.start(), m.group(1)) for m in _INLINE.finditer(prose)]
    return [span for _, span in sorted(spans)]


def anchors(page: str) -> set[str]:
    """GitHub's heading anchors for ``page``: lower case, punctuation dropped, spaces to hyphens."""
    prose = _FENCE.sub("", page)
    seen: dict[str, int] = {}
    found: set[str] = set()
    for heading in _HEADING.findall(prose):
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def _path_exists(token: str, root: Path) -> bool:
    if "*" in token:
        return any(root.glob(token))
    return (root / token).exists()


def missing_refs(text: str, page: Path, root: Path = ROOT) -> list[str]:
    """Each path, node ID or link on one page that does not resolve under ``root``."""
    missing: list[str] = []
    module: str | None = None
    for span in _code_spans(text):
        for node in _NODE.finditer(span):
            module, name = node.group(1), node.group(2)[2:]
            if not (root / module).is_file() or name not in _names(root / module):
                missing.append(f"test {node.group(0)}")
        rest = _NODE.sub(" ", span)
        for short in _SHORT.finditer(rest):
            if module is None or short.group(1) not in _names(root / module):
                missing.append(f"test {module}::{short.group(1)}")
        for token in re.split(r"[\s,;()\"']+", rest):
            token = token.rstrip(".:")
            if token.startswith(TOP) and "<" not in token and not _path_exists(token, root):
                missing.append(f"path {token}")
    prose = _FENCE.sub("", text)
    for target in _LINK.findall(prose):
        if re.match(r"[a-z]+:", target):
            continue
        file, _, fragment = target.partition("#")
        resolved = (page.parent / file).resolve() if file else page
        if not resolved.exists():
            missing.append(f"link {target}")
        elif fragment and resolved.suffix == ".md":
            target_text = resolved.read_text(encoding="utf-8") if file else text
            if fragment not in anchors(target_text):
                missing.append(f"anchor {target}")
    return missing


def _pages() -> list[Path]:
    return sorted(PAGES.glob("*.md"))


def test_the_guide_has_its_pages() -> None:
    names = [page.name for page in _pages()]
    assert "README.md" in names
    assert len(names) >= 7, names


def test_every_path_node_id_and_link_exists() -> None:
    missing = {
        page.name: found
        for page in _pages()
        if (found := missing_refs(page.read_text(encoding="utf-8"), page))
    }
    assert missing == {}


def test_the_guard_catches_planted_refs() -> None:
    page = PAGES / "README.md"
    planted = "\n".join(
        [
            "# A heading",
            "See `src/aksha/no_such_module.py` and `tests/no_such_dir/`.",
            "Run `tests/architecture/test_assignment_docs.py::test_nothing`.",
            "Then `tests/architecture/test_assignment_docs.py::test_the_guide_has_its_pages`,",
            "`::test_also_nothing`.",
            "Read [this](no-such-page.md) and [that](README.md#no-such-heading).",
            "Fine: [here](#a-heading), `src/aksha/runtime/api.py`, `src/aksha/<name>/x.py`.",
        ]
    )
    assert sorted(missing_refs(planted, page)) == [
        "anchor README.md#no-such-heading",
        "link no-such-page.md",
        "path src/aksha/no_such_module.py",
        "path tests/no_such_dir/",
        "test tests/architecture/test_assignment_docs.py::test_also_nothing",
        "test tests/architecture/test_assignment_docs.py::test_nothing",
    ]


def test_anchors_follow_github() -> None:
    page = "\n".join(
        [
            "# Run (b): interactive research",
            "## The per-key bug, and what is being fixed",
            "## Run (b): interactive research",
        ]
    )
    assert anchors(page) == {
        "run-b-interactive-research",
        "the-per-key-bug-and-what-is-being-fixed",
        "run-b-interactive-research-1",
    }
