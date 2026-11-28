"""Both agents on input sets shaped unlike any world: the generality critic's regression set.

Each folder under ``tests/fixtures/unseen`` is something a person might upload, with no
counterpart among the synthetic worlds: one long Markdown memo that holds sellers, customer
quotes and plans at once; one saved HTML page with a comparison table above its reviews; a
memo with one section per seller; a folder of plain lowercase ``.txt`` notes with no
headings. Each run goes through the command line, offline, as ``aksha run --input`` would.
Before the fixes these runs found no company at all (a mixed record was read as one kind),
dropped every table column but the price, found no invention in plain notes, ignored a
dated public post written as prose, and ended the patent run blocked by an ``ask`` raised
with no options.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from tests.support.session import env

UNSEEN = Path(__file__).resolve().parents[1] / "fixtures" / "unseen"


def aksha(tmp: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    extra = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS)}
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **extra))
    return code, out.getvalue()


def shown(tmp: Path, ref: str) -> dict[str, Any]:
    code, out = aksha(tmp, "show", ref)
    assert code == wire.EXIT_OK, out
    content: dict[str, Any] = json.loads(out)["content"]
    return content


def research(tmp: Path, folder: str, prompt: str) -> dict[str, Any]:
    """Run the research agent on ``folder``; answer its one scope question if it asks."""
    code, out = aksha(tmp, "run", prompt, "--agent", "research", "--input", str(UNSEEN / folder))
    asked = re.search(r"aksha answer (I-\d+)", out)
    if asked is not None:
        code, out = aksha(tmp, "answer", asked.group(1), "The buyers the request names.")
    assert code == wire.EXIT_OK and "research ended complete" in out, out
    return shown(tmp, "research.matrix")


def _rows(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["company"]["value"]: r for r in matrix["rows"]}


def _quotes(cells: list[dict[str, Any]]) -> list[str]:
    return [c["cite"]["quote"] for c in cells if "value" in c]


def test_one_memo_holding_sellers_quotes_and_plans_is_read_section_by_section(
    tmp_path: Path,
) -> None:
    prompt = (
        "Research the competitors for a bike-share fleet tracker, for city operators in "
        "Europe. Compare battery life (battery, months) and connectivity (LTE-M, GPS, "
        "Bluetooth). What do operators say about: battery (battery, die*, swap*); fees "
        "(fee*, monthly, subscription*); tracking (bluetooth, track*, lose)?"
    )
    rows = _rows(research(tmp_path, "memo", prompt))
    assert sorted(rows) == ["Pedalink", "Ridemesh", "Spokewise"]
    prices = {name: [p["cite"]["quote"] for p in r["prices"]] for name, r in rows.items()}
    assert prices["Pedalink"] == ["€39 per unit", "€2.50 per month for the data plan"]
    assert prices["Ridemesh"] == ["€24 per unit"]
    assert prices["Spokewise"] == ["€4 per bike per month"]
    # A seller's own sentence in the memo is evidence for that seller only.
    assert any("LTE-M" in q for q in _quotes(rows["Spokewise"]["cells"]["connectivity"]))
    assert _quotes(rows["Ridemesh"]["cells"]["battery_life"]) == []
    needs = {n["theme"]: n["phrase"] for n in shown(tmp_path, "research.needs")["needs"]}
    assert needs == {"battery": "2 of 4 reviews in this sample",
                     "fees": "1 of 4 reviews in this sample",
                     "tracking": "2 of 4 reviews in this sample"}  # fmt: skip


def test_a_saved_page_with_a_comparison_table_above_its_reviews(tmp_path: Path) -> None:
    prompt = (
        "Research the competitors for an office cold-brew dispenser, for office managers in "
        "the US. Compare capacity (litres, L). What do office managers say about: cost "
        "(lease, monthly, price*); cleaning (clean*, lines); capacity (litres, gone, empty)?"
    )
    rows = _rows(research(tmp_path, "page", prompt))
    assert sorted(rows) == ["Brewvault", "Coldline", "Kegly"]
    # A thousands separator inside a cell does not split the row.
    assert [p["cite"]["quote"] for p in rows["Brewvault"]["prices"]] == ["$1,299"]
    # Every column of the table, not only the price, is a citable spec of that row's seller.
    capacity = {name: _quotes(r["cells"]["capacity"]) for name, r in rows.items()}
    assert capacity == {"Brewvault": ["10 L"], "Coldline": ["5 L"], "Kegly": ["19 L"]}
    needs = shown(tmp_path, "research.needs")
    assert needs["sample"]["unique"] == 3


def test_a_memo_with_one_section_per_seller(tmp_path: Path) -> None:
    prompt = (
        "Research the competitors for a kiln controller, for small pottery studios in the "
        "US. Compare connectivity (Wi-Fi, Bluetooth). What do potters say about: price "
        "(price*, cost*)?"
    )
    rows = _rows(research(tmp_path, "sections", prompt))
    assert sorted(rows) == ["Firewise", "Glazeline", "Kilnmate"]
    assert any("Bluetooth" in q for q in _quotes(rows["Kilnmate"]["cells"]["connectivity"]))


def test_plain_notes_with_no_headings_give_candidates_and_a_dated_post_needs_counsel(
    tmp_path: Path,
) -> None:
    request = "Prepare our stair-building invention candidates for counsel review."
    code, out = aksha(tmp_path, "run", request, "--agent", "patent", "--input",
                      str(UNSEEN / "notes"))  # fmt: skip
    assert code == wire.EXIT_WAITING_INPUT and "option n1: " in out and "option n2: " in out, out
    portfolio = shown(tmp_path, "patent.portfolio")
    titles = {n["candidate"]: n["title"] for n in portfolio["notes"]}
    assert titles["n2"] == "pre-cut tread template"
    # The people note holds no invention: it is listed for the model to read, not dropped.
    assert [u["file"] for u in portfolio["unread"]] == ["input/n4.txt"]
    jig = shown(tmp_path, "patent.extraction/n1")
    (post,) = jig["disclosures"]
    assert (post["date"], post["file"], post["public"]) == ("2026-09-02", "input/n3.txt",
                                                            "unknown")  # fmt: skip
    assert post["status"] == "needs_counsel_confirmation"
    risks = shown(tmp_path, "patent.recommendation/n1")["risks"]
    assert any("does not say whether it was public" in r for r in risks)
    template = shown(tmp_path, "patent.extraction/n2")
    assert template["implementation"]["status"] == "idea_only"


def test_inputs_with_no_invention_record_an_empty_selection_without_asking(
    tmp_path: Path,
) -> None:
    request = "Prepare our invention candidates for counsel review."
    code, out = aksha(tmp_path, "run", request, "--agent", "patent", "--input",
                      str(UNSEEN / "memo"))  # fmt: skip
    assert code == wire.EXIT_OK and "patent ended complete" in out, out
    selection = shown(tmp_path, "patent.selection")
    assert selection["selected"] == [] and "no invention candidate" in selection["note"]
