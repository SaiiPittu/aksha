---
record_id: SYN-CW-NOTE-04
invention: CW-04
date: '2026-04-13'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: route ranking from forecast ambient heat

Author: Mira Okafor, firmware lead, Coldwick
Date: 13 Apr 2026

## Background

Dispatchers choose routes by distance and send boxes through the hottest afternoon roads.

## How it works

1. Fetch the ambient temperature forecast along each candidate delivery route.
2. Estimate the heat load each route adds to the shipper from forecast exposure hours.
3. Rank the delivery routes so the lowest forecast heat load comes first.

## Settings

| Name | Value | Unit |
|---|---|---|
| Forecast horizon | 12 | hours |

## Options we rejected

- Always the shortest route. Rejected: it ignores midday heat.

## Test results

Bench and field tested on 30 shipments between April and July 2026.
