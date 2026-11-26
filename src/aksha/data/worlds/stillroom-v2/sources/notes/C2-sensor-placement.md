---
record_id: NOTE-C2
candidate: C2
author_role: P-03
date: '2026-02-03'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Engineering note: placement planner for ceiling units

Author: Priya Natarajan, hardware engineer, Hushframe
Date: 3 Feb 2026
Candidate: C2, sensor placement

## Problem

Installers mount units where the ceiling tile is convenient. Mounted near the door, a unit counts the corridor; under a vent, it sees air flow as people.

## Mechanism (whiteboard sketch, not yet built)

1. Take the floor plan with ceiling height, doors, windows, vents and seats.
2. For each candidate mount point, compute the sensor footprint at seated head height, 1.2 m above the floor, from the 90 degree field of view.
3. Score each layout on seat coverage, overlap between units, and exposure to heat sources and doorways.
4. After install, walk one person to marked spots and check each unit sees them where the plan predicts.

## Parameters

| Parameter | Value | Unit | Note |
|---|---|---|---|
| Field of view | 90 | degrees | thermopile lens |
| Seated head height | 1.2 | m | |
| Minimum distance from a vent | not decided | m | |

## Alternatives considered

- One unit per room at the centre of the ceiling. Rejected for rooms over about 10 square metres: the ends of the table fall outside the footprint.

## Evidence

Whiteboard photo taken 3 Feb 2026. The walk-test results are in placement-trial-02.csv (attached separately).
