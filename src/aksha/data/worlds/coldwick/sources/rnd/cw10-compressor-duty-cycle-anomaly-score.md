---
record_id: SYN-CW-NOTE-10
invention: CW-10
date: '2026-04-19'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: compressor duty cycle anomaly score for reefer units

Author: Lucia Ferreira, data engineer, Coldwick
Date: 19 Apr 2026

## Background

Reefer container compressors fail on the road without warning.

## How it works

1. Measure the compressor duty cycle of the reefer unit in each hour.
2. Compare the duty cycle with the expected value for the outside temperature.
3. Warn the carrier when the deviation stays high for six hours.

## Settings

| Name | Value | Unit |
|---|---|---|
| Warning window | 6 | hours |

## Options we rejected

- Fixed service intervals. Rejected: they miss early wear.

## Test results

Prototype on two loggers; not field tested.
