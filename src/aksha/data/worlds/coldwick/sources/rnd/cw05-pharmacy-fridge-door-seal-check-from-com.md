---
record_id: SYN-CW-NOTE-05
invention: CW-05
date: '2026-05-14'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# Design note: pharmacy fridge door seal check from compressor restarts

Author: Jonas Petrov, hardware engineer, Coldwick
Date: 14 May 2026

## Background

A worn fridge door seal shows up only after a vaccine batch is lost.

## How it works

1. Count compressor restarts per hour from the fridge power draw signature.
2. Learn the normal restart rate for each fridge during its first two weeks.
3. Raise a seal inspection task when restarts exceed the learned rate by half for a day.

## Settings

| Name | Value | Unit |
|---|---|---|
| Learning period | 14 | days |

## Options we rejected

- A door contact sensor. Rejected: it needs wiring in each fridge.

## Test results

Running in a pilot with one pharmacy chain since 2 Jun 2026, under NDA.
