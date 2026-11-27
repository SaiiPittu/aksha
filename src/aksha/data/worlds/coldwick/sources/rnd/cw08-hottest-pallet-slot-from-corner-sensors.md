---
record_id: SYN-CW-NOTE-08
invention: CW-08
date: '2026-02-17'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: hottest pallet slot from corner sensors

Author: Mira Okafor, firmware lead, Coldwick
Date: 17 Feb 2026

## Background

Only one logger rides on a pallet, so the warmest carton is never measured.

## How it works

1. Mount four sensors at the pallet corners under the stretch wrap.
2. Fit a linear gradient across the pallet from the four corner readings.
3. Report the predicted warmest carton slot and its estimated temperature.

## Settings

| Name | Value | Unit |
|---|---|---|
| Corner sensors | 4 | sensors |

## Options we rejected

- One logger in the middle. Rejected: the middle is the coolest spot.

## Test results

Bench and field tested on 30 shipments between April and July 2026.
