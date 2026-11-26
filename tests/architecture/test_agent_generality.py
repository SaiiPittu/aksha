"""The agents are general: no agent module but its offline test double knows a fixture.

The research and patent agents work on whatever a person uploads, so their logic (the
definition, tools, readers, rules, checks and prompts) must not carry any value of the
synthetic worlds they are tested on: no venture, person, company, product, candidate ID,
file name, folder or theme of ``stillroom``, ``stillroom-v2``, ``tilldew``, ``cindral`` or
``rimefold``. Only the offline models (``fakes.py``, ``fakes_*.py``) may know a fixture, and no
other module imports them. Each guard fails on a planted value.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from aksha import data

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "src" / "aksha" / "agents"
WORLDS = data.worlds()
FIXTURE_TERMS = (
    # ventures, people, companies and products
    "Stillroom", "Hushframe", "Tilldew", "Kelvra", "Veyra", "Tallyway", "Noctra",
    "Brightfield", "Corvane", "OccuPoint", "AquaSense", "KhetiMeter", "Rootline",
    "GreenFurrow", "FurrowPulse", "Dripwise", "Hydrafield", "Thermosight", "Asha Rao",
    "Wei Lin", "Imran Sethi", "Kofi Mensah", "Northgate", "Eastvale",
    "Cindral", "Nimbo", "Haldane", "PhysioGraph", "Corlew", "Tidewell", "Kestrel",
    "Priya Natarajan", "Tomasz Wilk", "Grace Obi",
    "Rimefold", "Glacivo", "TrackPoint", "Pellisar", "Coldhaven", "SafeTemp", "Frostmark",
    "Varrow", "Tundrel", "Ines Okafor",
    # candidate, element and family identifiers
    "C1", "C2", "C3", "C4", "TD-1", "TD-2", "TD-3", "F1", "F3", "P-05", "SYN-P-03",
    "SYN-PUB", "SYN-PRI", "SYN-Office", "CA-2025-11",
    # file names and the fixture folders
    "kelvra.example", "brightfield.example", "records.csv", "history.csv", "reviews.json",
    "business-priorities", "invention-disclosure-form", "filing-information-worksheet",
    "consultant-email", "placement-trial-02", "tariff-analysis", "INV-7", "INV-9",
    "price-comparison", "travel-times-2026", "feedback-export", "counsel-intake",
    "vendor-landscape-memo", "vendor-shortlist", "review-hub-export", "SYN-RV", "SYN-CAL",
    # the stillroom market's own words
    "thermopile", "occupancy", "stillness", "door contact", "works council",
)  # fmt: skip
"""Values of the fixture worlds an agent's logic must not name."""


def _logic_files() -> list[Path]:
    out = []
    for agent in ("research", "patent"):
        for p in sorted((AGENTS / agent).rglob("*")):
            if p.suffix in (".py", ".md") and not p.name.startswith("fakes"):
                out.append(p)
    return out


def _hits(text: str) -> list[str]:
    return [t for t in FIXTURE_TERMS if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w])", text, re.I)]


def test_the_terms_are_real_fixture_values() -> None:
    corpus = "\n".join(p.read_text("utf-8") for p in WORLDS.rglob("*") if p.is_file())
    missing = [t for t in FIXTURE_TERMS if t.lower() not in corpus.lower()]
    assert missing == []


@pytest.mark.parametrize("path", _logic_files(), ids=lambda p: str(p.relative_to(AGENTS)))
def test_agent_logic_names_no_fixture_value(path: Path) -> None:
    assert _hits(path.read_text(encoding="utf-8")) == []


def test_no_agent_logic_imports_its_fake() -> None:
    bad = []
    for path in _logic_files():
        if path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.split(".")[-1].startswith("fakes") for n in names):
                bad.append(str(path.relative_to(AGENTS)))
    assert bad == []


def test_the_guard_catches_a_planted_value() -> None:
    assert _hits('THEMES = {"battery": r"\\bbatter", "stillness": r"\\bsit"}') == ["stillness"]
    assert _hits('CANDIDATE = {"pattern": "^C[0-9]+$"} # e.g. C1') == ["C1"]
    assert _hits("read contributors/records.csv") == ["records.csv"]
    assert _hits("a C12 or TD-10 is another value") == []
