---
record_id: SYN-CW-NOTE-01
invention: CW-01
date: '2026-01-10'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: lid-opening grace window for excursion alarms

Author: Jonas Petrov, hardware engineer, Coldwick
Date: 10 Jan 2026

## Background

Couriers open the shipper lid for inspection and the logger raises a false excursion alarm.

## How it works

1. Detect a lid opening from the reed switch mounted under the shipper lid.
2. Suspend the excursion alarm while the lid stays open for under ninety seconds.
3. Resume alarming when the cavity temperature recovers within two degrees of setpoint.

## Settings

| Name | Value | Unit |
|---|---|---|
| Grace window | 90 | seconds |

## Options we rejected

- A fixed alarm delay on every reading. Rejected: it hides real excursions.

## Test results

Bench and field tested on 30 shipments between April and July 2026.
