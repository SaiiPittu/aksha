---
record_id: SYN-CW-NOTE-09
invention: CW-09
date: '2026-03-18'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: insurance claim bundle from a shipment record

Author: Jonas Petrov, hardware engineer, Coldwick
Date: 18 Mar 2026

## Background

Claims for spoiled cargo take weeks because the evidence sits in three systems.

## How it works

1. Collect the temperature trace, the custody record and the delivery photos of one shipment.
2. Assemble them into one signed claim bundle with a summary page.
3. Send the bundle to the insurer's claim inbox when the receiver rejects the cargo.

## Settings

| Name | Value | Unit |
|---|---|---|
| Bundle size limit | 20 | megabytes |

## Options we rejected

- Emailing each file. Rejected: insurers lose attachments.

## Test results

Whiteboard sketch only; not yet built.
