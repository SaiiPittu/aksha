"""A world's offline web: a search proxy and a fetcher over the pages the world declares.

A world (``aksha.data.worlds()/<name>/``) may hold ``web.json`` beside its ``sources/``:
the small web the agent can search and fetch, separate from what the person uploaded.
Each page names its URL (under an ``.example`` domain), and the page's file is the
world's ``web/<host><path>``, the same path as the record ID a fetch gives it
(:func:`~aksha.runtime.web.types.web_record_id`), so a citation of a fetched page names
the world file that holds it::

    {"pages": [
      {"url": "https://vendor.example/product", "published": "2026-10-14"},
      {"url": "https://deals.example/sensors", "noise": "spam",
       "why": "an advert reselling every sensor at a price no vendor states"}]}

``noise`` marks a page an agent should leave alone: ``irrelevant`` (another subject that
shares a word), ``near_duplicate`` (a copy of another page on another host; ``copy_of``
names the original's URL, or ``upload:<path>`` for a file the person uploaded), ``outdated``
(an older version of a page the web also holds), ``spam`` (an advert or a content farm)
or ``keyword_only`` (a page that lists the words and says nothing). The graders read it
(``web.no_noise_cited``); no tool or model does: a result never says it is noise.

:class:`WorldWeb` answers any query with a ranked list, deterministic for a query, so a
replay is stable: every page sharing a query word (or a word it begins, as a stemming
engine matches ``salin`` to ``salinity``), scored three points for a word in its title and
one for a word in its body, ties kept in the world's declared order, relevant
pages and noise mixed as a search engine would mix them. The body is what a page shows
(an HTML page without its head and scripts, a JSON record's string values); the snippet is
the body around the first query word. It is both ports: :meth:`search` and :meth:`fetch`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.grounding.segment import sniff_format, visible
from aksha.runtime.web.types import (
    NOT_ON_THE_WEB,
    FetchedPage,
    SearchHit,
    SearchResults,
    WebError,
    normal_url,
    web_record_id,
)

WEB_FILE = "web.json"
NOISE_KINDS = frozenset({"irrelevant", "near_duplicate", "outdated", "spam", "keyword_only"})
SNIPPET_CHARS = 200
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "do",
        "does",
        "sell",
        "sells",
    ]
)
CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "json": "application/json",
    "markdown": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}
_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HIDDEN = re.compile(r"<(head|script|style|template)\b.*?</\1\s*>", re.I | re.S)


def words(text: str) -> list[str]:
    """The lower-cased words of ``text`` that may match a query (no stop words)."""
    return [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]


@dataclass(frozen=True)
class WebPage:
    """One page a world declares: its URL, the world file holding it, and how it counts."""

    url: str
    file: str
    published: str | None = None
    title: str | None = None
    noise: str | None = None
    why: str = ""
    copy_of: str | None = None

    @property
    def record_id(self) -> str:
        """The record ID a fetch of this page gives, which is also its world file."""
        return web_record_id(self.url)


def load_pages(raw: Mapping[str, Any]) -> tuple[WebPage, ...]:
    """The pages of a parsed ``web.json``; raises ``ValueError`` on a malformed one."""
    out: list[WebPage] = []
    seen: set[str] = set()
    for item in raw.get("pages", ()):
        url = str(item["url"])
        if not url.startswith("https://"):
            raise ValueError(f"{url!r}: a world page is served over https")
        record = web_record_id(url)
        if record in seen:
            raise ValueError(f"{url!r} is declared twice")
        seen.add(record)
        noise = item.get("noise")
        if noise is not None and noise not in NOISE_KINDS:
            raise ValueError(f"{url!r}: noise {noise!r} is not one of {sorted(NOISE_KINDS)}")
        out.append(WebPage(url, record, item.get("published"), item.get("title"), noise,
                           str(item.get("why", "")), item.get("copy_of")))  # fmt: skip
    return tuple(out)


def world_pages(root: Path) -> tuple[WebPage, ...]:
    """The pages of the world at ``root``; none when it declares no web."""
    path = root / WEB_FILE
    if not path.is_file():
        return ()
    return load_pages(json.loads(path.read_text(encoding="utf-8")))


def world_hints(root: Path) -> Mapping[str, Mapping[str, Any]]:
    """What each agent's offline model is told about the world at ``root``, by agent name.

    ``web.json``'s ``fake`` (``{"research": {"queries": [...]}}``): the searches a person who
    knows the market would make. Only an offline model reads them (``for_world``); a live
    model writes its own queries. Empty for a world with no web.
    """
    path = root / WEB_FILE
    if not path.is_file():
        return {}
    found = json.loads(path.read_text(encoding="utf-8")).get("fake", {})
    return {str(k): dict(v) for k, v in found.items()}


class WorldWeb:
    """A world's web as both web ports (module doc).

    ``read`` returns a world file's text by its path relative to the world root (``None``
    when it cannot), so an episode reads through its own guard and faults. ``index``, when
    given, is what a search reads every page through instead (the engine's own index, not
    a fetch the agent made); ``on_search`` is told each query and the URLs it returned.
    """

    name = "world"

    def __init__(
        self,
        pages: Sequence[WebPage],
        read: Callable[[str], str | None],
        *,
        index: Callable[[str], str | None] | None = None,
        on_search: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        """Serve ``pages``, reading each page's file through ``read``."""
        self.pages = tuple(pages)
        self._read = read
        self._index = index or read
        self._on_search = on_search
        self._by_record = {p.record_id: p for p in self.pages}
        self.searches: list[str] = []
        self.fetches: list[str] = []

    @classmethod
    def of_world(cls, root: Path) -> WorldWeb:
        """The web of the world at ``root``, read from its files."""

        def read(rel: str) -> str | None:
            path = (root / rel).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                return None
            return path.read_text(encoding="utf-8")

        return cls(world_pages(root), read)

    def page(self, url: str) -> WebPage | None:
        """The declared page at ``url``, if any."""
        return self._by_record.get(web_record_id(url))

    def _title(self, page: WebPage, raw: str) -> str:
        if page.title:
            return page.title
        found = _TITLE.search(raw)
        if found:
            return " ".join(visible(found.group(1)).split())
        first = next((line.strip("# ").strip() for line in raw.splitlines() if line.strip()), "")
        return first[:120] or page.url

    def search(self, query: str, *, limit: int) -> SearchResults:
        """Every page sharing a query word, ranked (module doc), at most ``limit``."""
        self.searches.append(query)
        asked = list(dict.fromkeys(words(query)))
        scored: list[tuple[int, int, SearchHit]] = []
        for n, page in enumerate(self.pages):
            raw = self._index(page.file)
            if raw is None:
                continue
            title = self._title(page, raw)
            body = _body(page.file, raw)
            in_title, in_body = set(words(title)), set(words(body))
            score = sum(3 if _has(in_title, w) else 1 if _has(in_body, w) else 0 for w in asked)
            if score == 0:
                continue
            scored.append((-score, n, SearchHit(0, title, page.url, _snippet(body, asked),
                                                page.published)))  # fmt: skip
        scored.sort(key=lambda t: (t[0], t[1]))
        hits = tuple(
            SearchHit(i, h.title, h.url, h.snippet, h.date)
            for i, (_, _, h) in enumerate(scored[:limit], start=1)
        )
        if self._on_search is not None:
            self._on_search(query, [h.url for h in hits])
        return SearchResults(query, self.name, hits)

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        """The page at ``url`` from the world's file; 404 for a URL the world does not hold."""
        self.fetches.append(url)
        page = self.page(url)
        raw = None if page is None else self._read(page.file)
        if page is None or raw is None:
            raise WebError(NOT_ON_THE_WEB, f"{url} is not on this web")
        kind = CONTENT_TYPES.get(sniff_format(page.file, raw), "text/plain; charset=utf-8")
        return FetchedPage(url, normal_url(page.url), 200, kind, raw)


def _has(found: set[str], word: str) -> bool:
    """Whether ``word`` is among ``found``, or begins one of them (a stem: ``salin``)."""
    return word in found or (len(word) >= 4 and any(f.startswith(word) for f in found))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [t for v in value.values() for t in _strings(v)]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    return []


def _body(file: str, raw: str) -> str:
    """What the index reads of a page: its visible text, a JSON record's string values."""
    fmt = sniff_format(file, raw)
    if fmt == "html":
        text = visible(_HIDDEN.sub(" ", raw))
    elif fmt == "json":
        text = " ".join(_strings(json.loads(raw)))
    else:
        text = raw
    return " ".join(text.split())


def _snippet(body: str, asked: Sequence[str]) -> str:
    low = body.lower()
    at = min((m.start() for w in asked for m in [re.search(rf"\b{re.escape(w)}", low)] if m),
             default=0)  # fmt: skip
    start = max(0, at - 60)
    cut = body[start : start + SNIPPET_CHARS].strip()
    return ("..." if start else "") + cut + ("..." if start + SNIPPET_CHARS < len(body) else "")
