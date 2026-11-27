---
record_id: SYN-CW-NOTE-20
invention: CW-20
date: '2026-02-29'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: cabin pressure compensation for air freight probes

Author: Mira Okafor, firmware lead, Coldwick
Date: 29 Feb 2026

## Background

Probe readings shift when air freight cabins lose pressure at altitude.

## How it works

1. Read the cabin pressure from a barometric sensor in the logger.
2. Correct the probe reading with a pressure coefficient measured on the bench.
3. Store the corrected and the raw reading side by side.

## Settings

| Name | Value | Unit |
|---|---|---|
| Pressure coefficient | 20 | millidegrees per kilopascal |

## Options we rejected

- Ignoring altitude. Rejected: readings shift by a degree.

## Test results

Whiteboard sketch only; not yet built.
