---
record_id: SYN-CW-NOTE-14
invention: CW-14
date: '2026-02-23'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: solar lid charger for reusable shippers

Author: Lucia Ferreira, data engineer, Coldwick
Date: 23 Feb 2026

## Background

Reusable shippers come back with flat logger batteries.

## How it works

1. Laminate a thin solar film into the reusable shipper lid.
2. Charge the logger battery from the film whenever light reaches the lid.
3. Report the charge state with each upload so flat units are pulled from service.

## Settings

| Name | Value | Unit |
|---|---|---|
| Film output | 300 | milliwatts |

## Options we rejected

- Swapping batteries at the depot. Rejected: it needs a technician.

## Test results

Prototype on two loggers; not field tested.
