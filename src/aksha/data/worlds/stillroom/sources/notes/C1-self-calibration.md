---
record_id: NOTE-C1
candidate: C1
author_role: P-01
date: '2026-01-14'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Engineering note: self-calibrating thermal baseline

Author: Asha Rao, firmware lead, Stillroom
Date: 14 Jan 2026
Candidate: C1, self-calibration

## Problem

The thermal baseline drifts when HVAC (heating, ventilation and air conditioning) cycles, causing false "occupied" readings in empty rooms. Sun on the floor in the afternoon does the same.

## Mechanism

1. The Stillroom S1 ceiling unit holds an 8 x 8 thermopile array (64 pixels) and a PIR (passive infrared) motion sensor, and reads the room's door contact over the local radio link.
2. Treat the room as verified empty only when the PIR has seen no motion for 20 minutes and the door contact reports closed.
3. During a verified-empty window, record each pixel's mean temperature and its standard deviation over the window.
4. Update the baseline slowly: new baseline = 0.99 x old baseline + 0.01 x window mean, once per minute.
5. Freeze learning in any pixel where a person was detected in the last 10 minutes, so a still person is never absorbed into the background.
6. Set each pixel's detection threshold to baseline + 4 x that pixel's standard deviation.

## Parameters

| Parameter | Value | Unit | Note |
|---|---|---|---|
| PIR quiet window | 20 | minutes | |
| Door state required | closed | n/a | from the door contact |
| Baseline update weight | 0.01 | per minute | exponential moving average |
| Pixel freeze after detection | 10 | minutes | |
| Threshold multiplier k | 4 | standard deviations | |

## Alternatives considered

- Fixed nightly recalibration at 03:00. Rejected: it fails in 24-hour offices and misses afternoon sun drift.
- Updating whenever the PIR is quiet, without the door gate. Rejected in bench tests: a person reading at a desk can stay PIR-quiet for 20 minutes.

## Evidence

Firmware build fw-0.9.3 runs the gated update on the four test units in the 3rd-floor meeting room. Bench log reference BL-2026-011.

## Open points

- Why 20 minutes: to be written up.
