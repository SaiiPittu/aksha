---
record_id: SYN-CW-NOTE-06
invention: CW-06
date: '2026-06-15'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: battery-free logging tag powered by the reader field

Author: Lucia Ferreira, data engineer, Coldwick
Date: 15 Jun 2026

## Background

Disposable loggers with batteries cost too much for single food parcels.

## How it works

1. Harvest energy from the warehouse RFID reader field into a small capacitor on the tag.
2. Sample the thermistor only while the stored charge is above the sampling threshold.
3. Write each sample into ferroelectric memory so no reading needs battery backup.

## Settings

| Name | Value | Unit |
|---|---|---|
| Sampling threshold | 18 | tenths of a volt |

## Options we rejected

- A coin cell per tag. Rejected: the cell costs more than the parcel margin.

## Test results

Prototype on two loggers; not field tested.
