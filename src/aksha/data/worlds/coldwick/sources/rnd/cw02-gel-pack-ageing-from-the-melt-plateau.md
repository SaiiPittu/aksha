---
record_id: SYN-CW-NOTE-02
invention: CW-02
date: '2026-02-11'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: gel pack ageing from the melt plateau

Author: Lucia Ferreira, data engineer, Coldwick
Date: 11 Feb 2026

## Background

Reused gel packs lose holding time and nobody knows which packs to retire.

## How it works

1. Record the melt plateau duration of each gel pack during conditioning in the freezer.
2. Compare the plateau with the pack's first recorded plateau to estimate lost latent capacity.
3. Flag a pack for retirement when its plateau falls below seventy percent of the first one.

## Settings

| Name | Value | Unit |
|---|---|---|
| Retirement threshold | 70 | percent |

## Options we rejected

- Weighing each pack. Rejected: the weight does not change.

## Test results

Running in a pilot with one pharmacy chain since 2 Jun 2026, under NDA.
