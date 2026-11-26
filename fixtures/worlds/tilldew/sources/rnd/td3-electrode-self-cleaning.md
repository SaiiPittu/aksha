---
record_id: SYN-TD-NOTE-3
invention: TD-3
date: '2026-02-10'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: electrode self-cleaning by polarity reversal

Author: Arjun Pillai, hardware engineer, Tilldew
Date: 10 Feb 2026

## Background

Mineral scale builds up on the probe electrodes over a season and the moisture values drift upward.

## How it works

1. Reverse the electrode polarity for 2 seconds after every 96 measurements.
2. Compare the values before and after each reversal to detect any remaining scale.
3. Raise a cleaning alert when that difference stays above 3 percent for three cycles.

## Settings

| Name | Value | Unit |
|---|---|---|
| Reversal pulse | 2 | seconds |
| Measurements between pulses | 96 | measurements |
| Scale alert difference | 3 | percent |

## Options we rejected

- A brush the farmer turns by hand. Rejected: farmers forget to turn it.

## Test results

Prototype on two probes; power draw not measured.
