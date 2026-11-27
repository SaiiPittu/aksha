---
record_id: SYN-CW-NOTE-13
invention: CW-13
date: '2026-01-22'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: shelf-life countdown on an e-ink label

Author: Jonas Petrov, hardware engineer, Coldwick
Date: 22 Jan 2026

## Background

Grocers cannot see how much shelf life a chilled product lost in transit.

## How it works

1. Compute remaining shelf life from the logged temperature history with a decay model.
2. Show the remaining days on an e-ink label printed onto the carton.
3. Refresh the e-ink label whenever the remaining shelf life drops by a day.

## Settings

| Name | Value | Unit |
|---|---|---|
| Refresh step | 1 | day |

## Options we rejected

- A colour-change sticker. Rejected: it cannot show days.

## Test results

Bench and field tested on 30 shipments between April and July 2026.
