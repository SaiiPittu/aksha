---
record_id: SYN-CW-NOTE-18
invention: CW-18
date: '2026-06-27'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: night setback for pharmacy fridges

Author: Lucia Ferreira, data engineer, Coldwick
Date: 27 Jun 2026

## Background

Pharmacy fridges run colder than needed overnight and waste energy.

## How it works

1. Learn the hours when the pharmacy fridge door stays shut from its door log.
2. Raise the fridge setpoint by one degree during those quiet hours.
3. Return to the normal setpoint thirty minutes before the first expected opening.

## Settings

| Name | Value | Unit |
|---|---|---|
| Setback | 1 | degrees |

## Options we rejected

- A timer on the plug. Rejected: it cuts the fridge off.

## Test results

Running in a pilot with one pharmacy chain since 2 Jun 2026, under NDA.
