---
record_id: NOTE-C4
candidate: C4
author_role: P-02
date: '2026-01-28'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Engineering note: event-driven sampling for a two-year battery

Author: Ravi Menon, embedded engineer, Stillroom
Date: 28 Jan 2026
Candidate: C4, battery-saving sampling

## Problem

Reading the thermopile and sending over the radio every second empties a 3,000 mAh battery in about two months.

## Mechanism

1. Keep the thermopile and the radio off by default; the PIR stays on.
2. When the PIR fires, power the thermopile and sample once per second for a 60 second window.
3. If no occupant is classified in the window, drop back to one sample every 10 seconds, then to PIR-only after 5 minutes of empty readings.
4. Send a radio message only when the count changes, plus a heartbeat every 15 minutes so silence is not mistaken for a dead unit.

## Parameters

| Parameter | Value | Unit | Note |
|---|---|---|---|
| Battery capacity | 3000 | mAh | |
| Occupied sample rate | 1 | per second | |
| Empty sample interval | 10 | seconds | |
| Heartbeat interval | 15 | minutes | |
| Average current, estimated | 0.2 | mA | engineering estimate, not measured |

## Alternatives considered

- Fixed one-second sampling with radio batching every minute. Rejected: the thermopile alone draws too much.

## Evidence

Prototype firmware fw-0.8.1 on two units. Power profile not yet measured.
