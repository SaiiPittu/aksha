---
record_id: SYN-TD-NOTE-2
invention: TD-2
date: '2026-03-04'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: irrigation timing from evapotranspiration and the power tariff

Author: Neha Bansal, firmware engineer, Tilldew
Date: 4 Mar 2026

## Background

Farmers run pumps whenever power arrives, often at the peak tariff, and sometimes water a field the day before rain.

## How it works

1. Forecast the next day's crop evapotranspiration from the district weather feed.
2. Schedule the pump inside the cheapest electricity tariff window that still covers the evapotranspiration deficit.
3. Cancel the scheduled pump run when forecast rainfall exceeds 5 mm.

## Settings

| Name | Value | Unit |
|---|---|---|
| Rain cancel threshold | 5 | mm |
| Tariff table source | not decided | n/a |

## Options we rejected

- A fixed 05:00 pump start. Rejected: it ignores both the tariff and the weather.

## Test results

Whiteboard sketch only; not yet built. The cost model is in tariff-analysis.xlsx.
