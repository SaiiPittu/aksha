---
record_id: SYN-CW-NOTE-07
invention: CW-07
date: '2026-01-16'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: probe drift check against an ice slurry at the depot

Author: Tariq Haddad, operations lead, Coldwick
Date: 16 Jan 2026

## Background

Logger probes drift over months and nobody recalibrates them between trips.

## How it works

1. At the depot the operator dips the probe tip into a stirred ice slurry.
2. The logger compares its reading in the slurry with zero degrees and stores the offset.
3. Later readings are corrected by the stored offset until the next slurry check.

## Settings

| Name | Value | Unit |
|---|---|---|
| Allowed offset | 5 | tenths of a degree |

## Options we rejected

- Factory calibration only. Rejected: drift starts after shipping.

## Test results

Bench and field tested on 30 shipments between April and July 2026.
