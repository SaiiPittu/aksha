---
record_id: NOTE-C3
candidate: C3
author_role: P-04
date: '2026-02-20'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Engineering note: counting across overlapping units without images

Author: Kenji Mori, data engineer, Hushframe
Date: 20 Feb 2026
Candidate: C3, privacy-preserving coordination

## Problem

Large rooms need two units with overlapping views. A person in the overlap is seen by both, and adding the counts double-counts. Sending both heat images to a server to merge them would move raw sensor data off the device.

## Mechanism

1. Each unit processes frames on the device and outputs counts only; no frame ever leaves the unit.
2. At install time, the overlap zone is marked in each unit's own pixel coordinates.
3. Each unit reports two integers every 30 seconds: total people seen, and people seen inside the overlap zone.
4. The room aggregator computes room count = A + B - min(overlap_A, overlap_B).
5. Above room level, counts roll up to floors, and any room with a single regular occupant is grouped with its neighbours so no one person is singled out.

## Parameters

| Parameter | Value | Unit | Note |
|---|---|---|---|
| Report interval | 30 | seconds | |
| Minimum group size for roll-up | 3 | rooms | |

## Alternatives considered

- Server-side fusion of both heat frames. Rejected: raw frames leave the device.
- Assigning each overlap pixel to one unit only. Rejected: a person standing on the boundary flickers between units.

## Evidence

Aggregator running in the pilot at a customer office since 15 Apr 2026, under NDA.
