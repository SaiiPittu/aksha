---
record_id: SYN-CW-NOTE-17
invention: CW-17
date: '2026-05-26'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: shock and vibration log for fragile biologics

Author: Jonas Petrov, hardware engineer, Coldwick
Date: 26 May 2026

## Background

Biologics are damaged by drops that no temperature logger records.

## How it works

1. Record acceleration peaks above a shock threshold with an accelerometer in the logger.
2. Attach each shock event to the nearest temperature reading.
3. Mark the shipment for inspection when a shock exceeds the product's limit.

## Settings

| Name | Value | Unit |
|---|---|---|
| Shock threshold | 8 | g |

## Options we rejected

- A tilt indicator sticker. Rejected: it shows tilt, not drops.

## Test results

Prototype on two loggers; not field tested. The soak-test results are in cw17-soak-test.csv (attached separately).
