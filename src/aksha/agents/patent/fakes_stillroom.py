"""Fixture knowledge of the patent agent's offline model, for the stillroom worlds only.

A test double's lexicon, never agent logic: the agent's modules never import it, and the
general rules of ``fakes.py`` serve any other inputs. It holds, per stillroom candidate,
the elements a skilled reader would pick out of the engineering note, each with the terms
that find its mechanism step and the term groups that find it in a reference (all groups:
``disclosed``; at least half: ``partial``; fewer: ``not_found``). It was written by reading
the source texts, never the answer key. An entry applies only to the note it was written
for: the same candidate ID *and* the same title, so another venture's note never meets it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TITLES: Mapping[str, str] = {
    "C1": "Engineering note: self-calibrating thermal baseline",
    "C2": "Engineering note: placement planner for ceiling units",
    "C3": "Engineering note: counting across overlapping units without images",
    "C4": "Engineering note: event-driven sampling for a two-year battery",
}
"""The note each entry was written for."""

LEXICON: Mapping[str, Mapping[str, Any]] = {
    "C1": {
        "search": [["background"], ["sensor"]],
        "elements": {
            "F1": (
                "per-pixel thermopile empty-room baseline",
                ["thermopile array"],
                [
                    ["thermal array", "thermopile", "thermal occupancy sensor"],
                    ["background frame", "background estimate", "baseline"],
                    ["pixel"],
                ],
            ),
            "F2": (
                "baseline update only after a PIR quiet window",
                ["no motion for 20 minutes"],
                [
                    ["background"],
                    ["no motion", "low activity"],
                    ["quiet period", "holding period", "no motion has been detected"],
                ],
            ),
            "F3": (
                "baseline update also gated on the door contact reporting closed",
                ["door contact reports closed"],
                [["door"]],
            ),
            "F4": (
                "slow EMA update with per-pixel freeze after detection",
                ["Freeze learning"],
                [
                    ["running average", "moving average", "stationary occupant is not absorbed"],
                    ["freeze", "update weight"],
                ],
            ),
            "F5": (
                "per-pixel threshold from that pixel's noise",
                ["detection threshold"],
                [["threshold derived from", "noise estimate of that pixel", "noise"]],
            ),
        },
        "public_terms": {"F2": ["no motion"], "F3": ["door"]},
    },
    "C2": {
        "search": [["mount point", "placement"]],
        "elements": {
            "P1": (
                "footprint at seated head height from field of view",
                ["footprint"],
                [["footprint"], ["working height", "head height"], ["field of view"]],
            ),
            "P2": (
                "layout score over seat coverage, overlap, heat-source and doorway exposure",
                ["Score each layout"],
                [
                    ["coverage of seat", "seat locations", "seats"],
                    ["doorway"],
                    ["heat source", "vent"],
                ],
            ),
            "P3": (
                "post-install walk test against the plan",
                ["walk one person"],
                [["walk test", "walk"]],
            ),
        },
    },
    "C3": {
        "search": [["overlap"]],
        "elements": {
            "Q1": (
                "on-device processing, counts only",
                ["outputs counts only"],
                [
                    ["locally", "on the device", "on-device"],
                    ["transmitting only the counts", "transmit only", "transmits only"],
                ],
            ),
            "Q2": (
                "overlap zone marked in each unit's pixel coordinates at install",
                ["overlap zone is marked"],
                [["overlap"], ["pixel coordinates"]],
            ),
            "Q3": (
                "each unit reports total and overlap counts",
                ["reports two integers"],
                [
                    ["count of persons inside", "counting persons within the overlap region"],
                    ["transmits only", "transmitting only"],
                ],
            ),
            "Q4": (
                "room = A + B - min(overlap_A, overlap_B)",
                ["min(overlap_A"],
                [["counted once", "minus the overlap count"], ["min(", "minimum of"]],
            ),
            "Q5": (
                "single-occupant rooms grouped in roll-ups",
                ["roll up to floors"],
                [["floor level", "roll up"], ["grouped", "single regular occupant"]],
            ),
        },
    },
    "C4": {
        "search": [["passive infrared", "PIR"], ["battery"]],
        "elements": {
            "B1": (
                "PIR wakes the thermopile for a 60 s window at 1 Hz",
                ["When the PIR fires"],
                [
                    ["passive infrared"],
                    ["wakes"],
                    ["window"],
                    ["60 s", "60 second", "sixty second"],
                    ["1 Hz", "once per second", "once a second"],
                ],
            ),
            "B2": (
                "fall back to 10 s sampling then PIR-only",
                ["drop back"],
                [["10 s", "every 10 seconds", "reduced rate", "fallback"], ["switched off"]],
            ),
            "B3": (
                "report on change plus 15 min heartbeat",
                ["heartbeat every 15 minutes"],
                [
                    ["only when the occupant count changes", "only when an occupant count changes"],
                    ["heartbeat"],
                    ["15 min", "15-minute", "fifteen minute", "every 15 minutes"],
                ],
            ),
            "B4": (
                "estimated 0.2 mA average (estimate, not measured)",
                ["Average current"],
                [["0.2 mA", "average current"]],
            ),
        },
    },
}
