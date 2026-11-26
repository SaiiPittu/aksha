---
record_id: SYN-TD-NOTE-1
invention: TD-1
date: '2026-01-19'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: salinity-compensated moisture reading

Author: Imran Sethi, sensor lead, Tilldew
Date: 19 Jan 2026

## Background

Capacitive probes read wet salty soil as wetter than it is, so growers in coastal districts get irrigation alerts they should ignore.

## How it works

1. Measure the probe capacitance at two excitation frequencies, 20 kHz and 1 MHz.
2. Estimate the pore-water salinity from the ratio of the two capacitance values.
3. Correct the moisture estimate with the salinity estimate using a lookup table per soil type.
4. Withhold an irrigation alert until two consecutive corrected readings, taken 30 minutes apart, both fall below the alert threshold.

## Settings

| Name | Value | Unit |
|---|---|---|
| Low excitation frequency | 20 | kHz |
| High excitation frequency | 1 | MHz |
| Confirmation interval | 30 | minutes |
| Alert threshold | 18 | percent volumetric water |

## Options we rejected

- A single 100 kHz reading with a fixed salinity offset. Rejected: the offset changes with every fertiliser dose.
- Asking farmers to enter soil salinity by hand. Rejected: nobody knows it.

## Test results

Bench tested on 40 soil samples and field trialled on 12 farms in the 2025 rabi season. Bench log reference SYN-TD-BL-07.

## Unresolved

- Why two confirming readings and not three: to be written up.
