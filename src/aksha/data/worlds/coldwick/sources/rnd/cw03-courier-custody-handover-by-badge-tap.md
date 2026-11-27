---
record_id: SYN-CW-NOTE-03
invention: CW-03
date: '2026-03-12'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: courier custody handover by badge tap

Author: Tariq Haddad, operations lead, Coldwick
Date: 12 Mar 2026

## Background

When a shipment arrives warm, nobody can say which courier held it at the time.

## How it works

1. Each courier taps an NFC badge on the logger lid at pickup and at drop-off.
2. The logger stamps the custody interval of each badge into its temperature record.
3. A warm reading is attributed to the badge whose custody interval contains it.

## Settings

| Name | Value | Unit |
|---|---|---|
| Badge read range | 4 | centimetres |

## Options we rejected

- Paper signature sheets. Rejected: they are filled in afterwards.

## Test results

Bench and field tested on 30 shipments between April and July 2026.
