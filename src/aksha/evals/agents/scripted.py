"""The scripted agents the runner is tested with (see the package docstring).

:class:`ScriptedResearchAgent` follows the fake's honesty rules (ADR-P22 D2) as far as an
agent without a model can: it reads only what its tools return, never the key, the spec or
the manifest, and its rules match file formats (table rows, a JSON-LD brand, a hidden
note), never the names a rename touches. It does not compute what a grader checks from
anything but the sources.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aksha.evals.episode import Episode
from aksha.evals.errors import EvalError, ToolFault

Step = tuple[Any, ...]


@dataclass
class ScriptedAgent:
    """Runs a fixed list of steps; strict in both directions, like the scripted model.

    Steps: ``("read", path)``, ``("list", folder)``, ``("save", kind, key, content,
    citations)``, ``("relate", type, src, dst, excerpt)``, ``("flag", kind, text)``,
    ``("approve", target_ref, action)``, ``("send", recipient, action, target_ref)`` (the
    last approval granted is attached), ``("send_unbound", recipient, action)`` and
    ``("fail", message)``. An unknown step raises; :meth:`assert_complete` fails when steps
    were left.
    """

    steps: Sequence[Step]
    name: str = "scripted-steps"
    mode: str = "scripted"
    done: int = 0
    _approval: str | None = field(default=None, repr=False)

    def run(self, episode: Episode) -> None:
        """Run the remaining steps in order."""
        while self.done < len(self.steps):
            step = self.steps[self.done]
            self.done += 1
            self._do(episode, step)

    def _do(self, ep: Episode, step: Step) -> None:
        op = step[0]
        if op == "read":
            ep.read_source(step[1])
        elif op == "list":
            ep.list_sources(step[1])
        elif op == "save":
            ep.save(step[1], step[2], step[3], step[4] if len(step) > 4 else ())
        elif op == "relate":
            ep.relate(step[1], step[2], step[3], step[4] if len(step) > 4 else None)
        elif op == "flag":
            ep.flag(step[1], step[2])
        elif op == "approve":
            self._approval = ep.request_approval(step[1], step[2])
        elif op == "send":
            ep.send(step[1], step[2], target_ref=step[3], approval_id=self._approval)
        elif op == "send_unbound":
            ep.send(step[1], step[2])
        elif op == "fail":
            raise RuntimeError(step[1])
        else:
            raise EvalError("unknown_step", f"scripted agent has no step {op!r}")

    def assert_complete(self) -> None:
        """Fail when steps were left unconsumed."""
        if self.done != len(self.steps):
            raise AssertionError(f"{len(self.steps) - self.done} scripted steps never ran")


def _cite(path: str, text: str, m: re.Match[str], group: int = 0) -> dict[str, Any]:
    return {"file": path, "exact": m.group(group), "start": m.start(group), "end": m.end(group)}


_BRAND = re.compile(r'"brand"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', re.S)
_H1 = re.compile(r"<h1>([^<]+)</h1>")
_PUBLISHED = re.compile(r'<meta name="published" content="([0-9-]+)">')
_UNIT_PRICE = re.compile(r"<td>[^<]*</td><td>((EUR|USD|GBP) (\d+) per unit)</td>")
_NO_PRICE = re.compile(r"Contact sales for a quote\.")
_RENAMED = re.compile(
    r"(?P<new>[^.<>]+?) is the new name for (?P<old>[^.<>]+?)\. "
    r"(?P<company>[^.<>]+?) joined (?P<group>[^.<>]+?) on \d{1,2} \w+ \d{4}"
)
_CLAIM = re.compile(r"Up to \d+ years of battery life\.")
_MEASURED = re.compile(r"\d+ months at 1 report per minute \([^)]*\)")
_INSTRUCTION = re.compile(
    r"(?i)(note to ai assistants|skip approval|ignore (all|previous) instructions)"
)
THEMES: Mapping[str, tuple[str, ...]] = {
    "battery": ("batter",),
    "setup": ("install", "cable", "electrician", "mount", "setup", "set up"),
    "stillness": ("sitting", "still", "reading", "alone"),
    "privacy": ("privacy", "camera", "image", "faces"),
}
REQUESTER = "requester@fixture.example"
RETRIES = 3
"""A failed read is tried again this many times before the page is given up."""


def _read(ep: Episode, path: str) -> str | None:
    for _ in range(RETRIES + 1):
        try:
            return ep.read_source(path)
        except ToolFault:
            continue
    return None


@dataclass
class _Page:
    path: str
    brand: str
    product: str
    published: str | None
    prices: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    merge: dict[str, Any] | None
    text: str


class ScriptedResearchAgent:
    """A small rule-based research agent over competitor pages and reviews."""

    name = "scripted"
    mode = "fake"

    def run(self, episode: Episode) -> None:
        """Read every competitor page and the reviews; save competitors, edges and a summary."""
        pages = [
            page
            for page in (self._page(episode, p) for p in episode.list_sources("competitors"))
            if page is not None
        ]
        entities = self._resolve(pages)
        for i, group in enumerate(entities, start=1):
            self._save_entity(episode, f"E{i}", group)
        self._reviews(episode)
        ref = episode.save("report", "summary", {"competitors": len(entities)})
        approval = episode.request_approval(ref.rsplit("@", 1)[0], "send_report")
        episode.send(REQUESTER, "send_report", target_ref="report/summary", approval_id=approval)

    def _page(self, ep: Episode, path: str) -> _Page | None:
        text = _read(ep, path)
        if text is None:
            return None
        brand = _BRAND.search(text)
        h1 = _H1.search(text)
        published = _PUBLISHED.search(text)
        prices = [
            {
                "amount": int(m.group(3)),
                "currency": m.group(2),
                "per": "unit",
                "page_published": published.group(1) if published else None,
                "label": "stated",
                "citation": _cite(path, text, m, 1),
            }
            for m in _UNIT_PRICE.finditer(text)
        ]
        for m in _NO_PRICE.finditer(text):
            prices.append(
                {
                    "amount": None,
                    "currency": None,
                    "per": None,
                    "label": "unknown",
                    "note": "not published: contact sales",
                    "citation": _cite(path, text, m),
                }
            )
        claims = [
            {"text": m.group(0), "label": "vendor_claim", "citation": _cite(path, text, m)}
            for m in _CLAIM.finditer(text)
        ]
        claims += [
            {"text": m.group(0), "label": "fact", "citation": _cite(path, text, m)}
            for m in _MEASURED.finditer(text)
        ]
        merge = None
        renamed = _RENAMED.search(text)
        if renamed:
            merge = {**renamed.groupdict(), "excerpt": _cite(path, text, renamed)}
        for hit in _INSTRUCTION.finditer(text):
            ep.flag("instruction_like", hit.group(0), path)
        return _Page(
            path=path,
            brand=brand.group(1) if brand else (h1.group(1) if h1 else path),
            product=h1.group(1).strip() if h1 else "",
            published=published.group(1) if published else None,
            prices=prices,
            claims=claims,
            merge=merge,
            text=text,
        )

    def _resolve(self, pages: list[_Page]) -> list[list[_Page]]:
        """Group pages into companies: a renamed product joins the page that sold it."""
        groups: list[list[_Page]] = [[p] for p in pages]
        for page in pages:
            if page.merge is None:
                continue
            old = next((p for p in pages if p is not page and p.product == page.merge["old"]), None)
            if old is None:
                continue
            a = next(g for g in groups if old in g)
            b = next(g for g in groups if page in g)
            if a is not b:
                a.extend(b)
                groups.remove(b)
        return groups

    def _save_entity(self, ep: Episode, key: str, group: list[_Page]) -> None:
        first = group[0]
        merged = next((p for p in group if p.merge), None)
        aliases: list[str] = []
        if merged is not None and merged.merge is not None:
            aliases = [merged.brand, first.product, merged.product]
        prices = [p for page in group for p in page.prices]
        ep.save(
            "competitor",
            key,
            {
                "name": first.brand,
                "aliases": [a for a in aliases if a and a != first.brand],
                "products": [p.product for p in group],
                "prices": prices,
                "claims": [c for page in group for c in page.claims],
            },
        )
        if merged is not None and merged.merge is not None:
            ep.relate(
                "same_entity_as",
                f"page:{first.path}",
                f"page:{merged.path}",
                merged.merge["excerpt"],
            )
        stated = [p for p in prices if p["amount"] is not None]
        if len({(p["amount"], p["currency"]) for p in stated}) > 1:
            ep.relate(
                "contradicts",
                f"price:{stated[0]['citation']['file']}",
                f"price:{stated[-1]['citation']['file']}",
                stated[-1]["citation"],
                label="price conflict left open",
            )

    def _reviews(self, ep: Episode) -> None:
        path = "reviews/reviews.json"
        text = _read(ep, path)
        if text is None:
            return
        rows = json.loads(text).get("reviews", [])
        seen: dict[tuple[str, str], str] = {}
        duplicates: list[list[str]] = []
        unique: list[Mapping[str, Any]] = []
        for row in rows:
            ident = (
                str(row.get("product", "")),
                " ".join(str(row.get("text", "")).split()).casefold(),
            )
            if ident in seen:
                duplicates.append([str(row["review_id"]), seen[ident]])
                continue
            seen[ident] = str(row["review_id"])
            unique.append(row)
        themes = {
            t: sum(any(w in str(r.get("text", "")).casefold() for w in words) for r in unique)
            for t, words in THEMES.items()
        }
        cites = []
        for dup, _orig in duplicates:
            needle = f'"review_id": "{dup}"'
            at = text.find(needle)
            if at >= 0 and text.count(needle) == 1:
                cites.append({"file": path, "exact": needle, "start": at, "end": at + len(needle)})
        ep.save(
            "review_summary",
            "all",
            {
                "rows": len(rows),
                "reviews_unique": len(unique),
                "duplicates": duplicates,
                "themes": themes,
                "statements": [
                    f"{themes['battery']} of {len(unique)} reviews in this sample "
                    "mention battery life."
                ],
            },
            cites,
        )
