---
synthetic: true
title: Open Ingest Bench, 2026 round
publisher: Open Ingest Bench volunteers (oib.example)
published: 2026-10-14
---
> SYNTHETIC FIXTURE. Invented for testing; not a real benchmark, organisation or result.

# Open Ingest Bench, 2026 round

Open Ingest Bench is a volunteer-run, vendor-neutral benchmark of hosted log search. No vendor paid for or reviewed this round. Each product was set up on its self-serve plan with result caches turned off.

## Method

- Dataset: 500 GB of mixed application, proxy and audit logs (1.2 billion lines), replayed over 24 hours.
- Queries: 120 searches taken from public incident write-ups, run cold, three times each; we report the median.
- Products: Siltgrove Logs (Team plan) and Ferrowind (Usage plan). Two other vendors declined to take part.

## Results

| Product | Median query (s) | 95th percentile (s) | Queries timed out |
|---|---|---|---|
| Siltgrove Logs | 1.4 | 6.8 | 0 |
| Ferrowind | 2.9 | 9.1 | 2 |

On the 500 GB dataset, Siltgrove's median query was 2.1x faster than Ferrowind's.

At the 95th percentile the gap narrows to 1.3x. Both products ingested the full 24-hour replay with no dropped lines.

## Limits

One region, one client, one 24-hour window. Results on cached queries, or on datasets far smaller than 500 GB, may differ.
