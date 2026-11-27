---
record_id: SYN-CW-NOTE-19
invention: CW-19
date: '2026-01-28'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: wash cycle tracking for reusable boxes

Author: Tariq Haddad, operations lead, Coldwick
Date: 28 Jan 2026

## Background

Reusable boxes go back into service without being washed.

## How it works

1. Detect a wash cycle from a humidity spike followed by a warm drying phase.
2. Count the wash cycles of each box in the logger memory.
3. Block a box from dispatch when it has no wash cycle since its last trip.

## Settings

| Name | Value | Unit |
|---|---|---|
| Humidity spike | 90 | percent |

## Options we rejected

- A wash log on paper. Rejected: it is not kept.

## Test results

Prototype on two loggers; not field tested.
