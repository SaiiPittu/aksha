---
record_id: SYN-CW-NOTE-16
invention: CW-16
date: '2026-04-25'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: hash chain of readings for audit

Author: Mira Okafor, firmware lead, Coldwick
Date: 25 Apr 2026

## Background

Auditors ask whether a temperature record was edited after the trip.

## How it works

1. Hash each reading together with the hash of the previous reading on the logger.
2. Publish the latest hash of the chain to the receiver at delivery.
3. Let an auditor verify the chain from the first reading to the published hash.

## Settings

| Name | Value | Unit |
|---|---|---|
| Hash length | 32 | bytes |

## Options we rejected

- Signed PDF reports. Rejected: they do not cover each reading.

## Test results

Whiteboard sketch only; not yet built.
