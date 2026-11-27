---
record_id: SYN-CW-NOTE-11
invention: CW-11
date: '2026-05-20'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: dry ice sublimation estimate from box weight

Author: Tariq Haddad, operations lead, Coldwick
Date: 20 May 2026

## Background

Shippers cannot tell how much dry ice is left in a box during transit.

## How it works

1. Weigh the box on a load cell built into the base plate at each scan point.
2. Subtract the packed payload weight to estimate the remaining dry ice mass.
3. Predict the hours left before the dry ice runs out at the measured sublimation rate.

## Settings

| Name | Value | Unit |
|---|---|---|
| Load cell resolution | 10 | grams |

## Options we rejected

- Counting pellets at packing. Rejected: the rate varies by box.

## Test results

Bench and field tested on 30 shipments between April and July 2026. The soak-test results are in cw11-soak-test.csv (attached separately).
