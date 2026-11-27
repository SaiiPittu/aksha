"""The patent reader finds what it reads by structure, whatever shape the inputs take.

Each test writes a small synthetic input folder in a shape the fixture worlds do not share
(a notebook with several ideas, an XML tracker, a spoken procedure, a search export, a
register of key-value blocks) and reads it through :class:`SourceWorld` with the agent's
offline fast models, as a read tool does. Nothing here names a fixture world.
"""

# ruff: noqa: E501  (the transcript's cues are one line each, as WebVTT writes them)
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from aksha.adapters.sources.folder import FolderSource
from aksha.agents.patent import rules
from aksha.agents.patent.fakes import ADAPTERS
from aksha.agents.patent.world import SourceWorld, label_steps, quantity, record_units
from aksha.runtime.api import segment
from tests.support.fast import models_for
from tests.support.patent import AGENT_DIR

NOTEBOOK = """SYNTHETIC FIXTURE - a bench notebook.
BENCH NOTEBOOK, VOLUME 1

----------------------------------------
p.3  Tue 6 Jan 2026
Re: SYN-K-1  Stable drip control

Method as I would build it:
  (a) Read the flow sensor every 5 seconds and keep a 60 second average.
  (b) When the average drifts more than 4 % from the target, nudge the valve by one step.
  (c) Stop nudging after 3 steps in a row and raise an alarm instead.

[margin, added 9 Feb 2026] (b1) Before each nudge, check the line pressure is above 1 bar.

Tried instead and dropped:
  - A fixed valve schedule: dropped, it drifted within a week.

----------------------------------------
p.4  Thu 8 Jan 2026
Re: SYN-K-2  Leak hint

  (a) Compare the inlet and outlet totals every hour.
  (b) Flag a leak when the outlet total is more than 8 % below the inlet total.

----------------------------------------
p.9  Mon 2 Mar 2026
Re: SYN-K-1  Field run

Ran on two lines for a week. No alarms raised.
"""

TRACKER = """<?xml version="1.0" encoding="UTF-8"?>
<export tool="board">
  <entry key="SYN-T-1" created="2026-02-01">
    <summary>Quiet hours for the pump</summary>
    <step n="1">Read the tariff band for the next hour.</step>
    <step n="2">Defer pumping when the band is the dearest one.</step>
  </entry>
  <entry key="SYN-T-2" created="2026-03-01">
    <summary>Filter clog warning</summary>
    <step n="1">Measure the pressure drop across the filter each morning.</step>
    <step n="2">Warn when the drop has risen by half over a week.</step>
  </entry>
</export>
"""

TRANSCRIPT = """WEBVTT

NOTE
Project: SYN-V-7, frame filtering, recorded 3 March 2026

1
00:00:01.000 --> 00:00:09.000
<v Ana Ruiz>So the first step is a gate. Firmware only keeps a frame if it is sharp: we only keep a frame when its edge energy stays above 40 units for 2 frames.

2
00:00:09.000 --> 00:00:16.000
<v Ben Okafor>Then we average the kept frames over a 10 second window before we count.

3
00:00:16.000 --> 00:00:24.000
<v Ana Ruiz>The first build kept every frame. We dropped it because blurred frames doubled the count.

4
00:00:24.000 --> 00:00:30.000
<v Ben Okafor>One more thing: nobody has written down why it is 40 units and not 30.
"""

EXPORT = """{
  "notice": "SYNTHETIC search export",
  "hits": [
    {"rank": 1, "record": {"ids": {"pub": "SYN-PUB-9001", "kind": "A1"},
      "title": "Frame gating for a counting camera",
      "timeline": [{"event": "priority", "date": "2020-01-02", "ref": "SYN-PRI-2020-0102"}],
      "text": {"abstract": "A camera keeps a frame only when it is sharp.",
               "claims": ["1. A method comprising keeping a frame only when it is sharp."]},
      "legal": [{"source": "office register", "says": "Application published"}]}},
    {"rank": 2, "record": {"ids": {"pub": "SYN-PUB-9002", "kind": "B1"},
      "title": "Frame gating for a counting camera",
      "timeline": [{"event": "priority", "date": "2020-01-02", "ref": "SYN-PRI-2020-0102"}],
      "text": {"abstract": "A camera keeps sharp frames and averages them.",
               "claims": ["1. A camera configured to keep sharp frames."]},
      "legal": [{"source": "office register", "says": "Granted"}]}}
  ]
}
"""

REGISTER = """SYNTHETIC FIXTURE. A sharing register.

Event: SYN-EV-901
Idea: SYN-V-7
Date: 4 April 2026
What left: A demo of the frame gate at a trade fair
Public: yes

Event: SYN-EV-902
Idea: SYN-V-7
Date:
What left: The firmware to a test partner under NDA
Public: no
"""


def _world(tmp_path: Path, files: dict[str, str]) -> SourceWorld:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    source = FolderSource("inputs", tmp_path, "input")
    return SourceWorld(date(2026, 11, 26)).over(source, models=models_for(AGENT_DIR, ADAPTERS,
                                                                          "read_note"))  # fmt: skip


def _ids(world: SourceWorld) -> list[str]:
    return [str(c["candidate"]) for c in world.candidates()["candidates"]]


def test_a_notebook_holds_one_candidate_per_idea_and_joins_its_later_pages(tmp_path: Path) -> None:
    world = _world(tmp_path, {"lab/book.txt": NOTEBOOK})
    assert _ids(world) == ["SYN-K-1", "SYN-K-2"]
    note = world.note("SYN-K-1")
    steps = {s["label"]: s for s in note["mechanism_steps"]}
    # (b1), added later in the margin, takes its place between (b) and (c)
    assert steps["S3"]["text"].startswith("Before each nudge")
    assert steps["S4"]["text"].startswith("Stop nudging")
    assert steps["S3"]["dated"]["date"] == "2026-02-09"  # its own record, not the page's
    assert note["date"] == "2026-01-06"
    assert any(p["Value"] == "5" and p["Unit"] == "seconds" for p in note["parameters"])
    assert note["alternatives"] == ["A fixed valve schedule: dropped, it drifted within a week."]


def test_an_xml_tracker_holds_one_candidate_per_record(tmp_path: Path) -> None:
    world = _world(tmp_path, {"tracker/export.xml": TRACKER})
    assert _ids(world) == ["SYN-T-1", "SYN-T-2"]
    note = world.note("SYN-T-2")
    assert note["title"] == "Filter clog warning"
    assert note["date"] == "2026-03-01"
    assert [s["label"] for s in note["mechanism_steps"]] == ["S1", "S2"]


def test_a_procedure_said_in_a_meeting_is_read_from_the_speakers_turns(tmp_path: Path) -> None:
    world = _world(tmp_path, {"calls/review.vtt": TRANSCRIPT})
    assert world.kind("input/calls/review.vtt") == "meeting_notes"
    assert _ids(world) == ["SYN-V-7"]
    note = world.note("SYN-V-7")
    texts = [s["text"] for s in note["mechanism_steps"]]
    assert texts[0].startswith("we only keep a frame when its edge energy stays above 40 units")
    assert texts[1].startswith("Then we average the kept frames")
    assert len(texts) == 2  # the dropped first build is an alternative, not a step
    assert note["alternatives"][0].startswith("The first build kept every frame.")
    assert note["open_points"] == ["nobody has written down why it is 40 units and not 30."]


def test_one_export_holds_several_publications_grouped_by_their_priority(tmp_path: Path) -> None:
    world = _world(tmp_path, {"art/export.json": EXPORT})
    assert world.publication_ids() == ["SYN-PUB-9001", "SYN-PUB-9002"]
    pubs = [world.publication(p) for p in world.publication_ids()]
    assert [p["priority_claims"][0]["number"] for p in pubs] == ["SYN-PRI-2020-0102"] * 2
    assert [s["wording"] for s in pubs[1]["status"]] == ["Granted"]
    families = rules.family_of(pubs)
    assert list(families.values()) == [["SYN-PUB-9001", "SYN-PUB-9002"]]


def test_a_register_of_key_value_blocks_is_a_disclosure_table(tmp_path: Path) -> None:
    world = _world(tmp_path, {"calls/review.vtt": TRANSCRIPT, "ops/register.txt": REGISTER})
    rows = world.disclosure_rows("SYN-V-7")["rows"]
    got = [(r["disclosure_id"], r["date"], r["public"]) for r in rows]
    assert got == [("SYN-EV-901", "2026-04-04", "yes"), ("SYN-EV-902", None, "no")]


def test_a_ticket_key_gives_way_to_the_id_the_other_inputs_use(tmp_path: Path) -> None:
    ticket = (
        '{\n  "issues": [\n    {\n      "key": "TCK-77",\n'
        '      "summary": "Counter warm-up (SYN-W-3)",\n      "created": "2026-05-02",\n'
        '      "body": {\n        "approach": [\n'
        '          "W1: hold the count for the first 30 seconds after power-up.",\n'
        '          "W2: then release the count and log the warm-up time."\n'
        "        ]\n      }\n    }\n  ]\n}\n"
    )
    plan = "# Goals for the half\n\n1. Fewer false counts at start-up (SYN-W-3)\n2. Nothing else\n"
    world = _world(tmp_path, {"t/export.json": ticket, "plan/goals.md": plan})
    assert _ids(world) == ["SYN-W-3"]
    assert [s["label"] for s in world.note("SYN-W-3")["mechanism_steps"]] == ["W1", "W2"]


def test_labels_quantities_and_units_follow_the_written_marks() -> None:
    steps: list[dict[str, Any]] = [{"marker": m, "text": m} for m in ("a", "c", "b2", "b")]
    assert [s["marker"] for s in label_steps(steps)] == ["a", "b", "b2", "c"]
    own: list[dict[str, Any]] = [{"marker": m, "text": m} for m in ("E1", "E2")]
    assert [s["label"] for s in label_steps(own)] == ["E1", "E2"]
    assert quantity("stays low for 6 consecutive hours") == (14, "6", "hours")
    assert quantity("after the test on 2 June we agreed 6 mA") == (35, "6", "mA")
    raw = "Deck\n\n--- page 1 ---\nOne\n\n--- page 2 ---\nTwo\n"
    assert len(record_units(raw, "text", segment(raw, "text"))) == 3
