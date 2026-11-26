# Backfill coverage and publication

The target window is 24 November 2021 through 23 November 2026, inclusive, in Asia/Kolkata. The targets are 61 monthly option expiries, 201 weekly option expiries (the weekly dates that are not also the monthly expiry) and 61 calendar months of one-minute spot. Monthly and weekly files are kept per type: most files below exist as `-monthly` and `-weekly` pairs.

The current publication receipt is in [lfs-publications.csv](lfs-publications.csv). A verified receipt means an independent checkout with an empty LFS cache downloaded each object, matched the pointer's SHA-256 and byte size, and opened the Parquets. It establishes retrievability for the files in the recorded commit. The receipt has 324 rows, all `verified`: 61 `options-monthly`, 201 `options-weekly`, 61 `spot` and 1 `daily`. Requested ATM offset and session coverage are measured separately.

Unlike the Bank Nifty repository, no data was supplied as a baseline: every expiry and spot month was requested from Dhan, so `baseline_rows` is 0 throughout and every row is a new Dhan row.

## Backfill result (frozen window, 24 November 2021 to 23 November 2026)

Status: **partial, source-limited.** All 61 monthly expiries, 201 weekly expiries and 61 spot partitions are on `main` in Git LFS and verified from an independent checkout. Not every requested observation could be accepted. Read the per-expiry manifests ([monthly](expiry-manifest-monthly.csv), [weekly](expiry-manifest-weekly.csv)) before relying on an expiry. The numbers below were recomputed from those files and the Parquet footers.

| Measure | Monthly | Weekly |
| --- | ---: | ---: |
| Expiries published | 61 of 61 | 201 of 201 |
| Option Parquet files (expiry x strike x side) | 5,788 | 13,359 |
| Option rows | 20,094,841 | 17,892,021 |
| `full_requested` | 0 | 5 |
| `source_limited` | 12 | 168 |
| `unresolved` | 49 | 28 |
| Quarantined strike-minute keys (conflicting data) | 19,292 | 119 |
| Foreign-index rows excluded (`excluded_foreign_spot_rows`) | 20,784 | 22 |
| Rows outside the option session excluded | 50,562 | 47,694 |
| Calendar dates `calendar_verified` against NSE | 14 | 19 |
| Request batches `returned` / `empty` / `conflicting` / `split_transport` | 51,216 / 308 / 2,404 / 3,360 | 48,351 / 1,120 / 47 / 8,484 |

Both types together are 262 expiries, 19,147 files and 37,986,862 rows. The 5 `full_requested` weekly expiries are 17 April 2025, 2 December 2025, 1 September 2026, 6 October 2026 and 3 November 2026.

What the labels mean:

- `full_requested`: every requested offset and session returned data, with no quarantine and no held rows.
- `source_limited`: nothing is quarantined. Some requested offsets or sessions returned no data, or fewer minutes than the spot series has, or some option minutes have no spot minute.
- `unresolved`: at least one of the following. Dhan returned the same strike and minute under two different ATM offsets with different values, so those keys are **quarantined** (listed in `unresolved_offsets` of the offsets file, counted in `quarantined_keys` of the manifest, absent from the Parquets rather than guessed). Or `held_rows` is above zero: ledger rows that were neither published, quarantined nor excluded by the foreign-index or outside-session filters, in most cases minutes where the spot series has no validated candle. All 49 unresolved monthly expiries have quarantined keys. Of the 28 unresolved weekly expiries, 14 have quarantined keys (119 in total, 90 of them in 23 May 2024) and 14 have only held rows (41 to 126 each). The individual quarantined rows are kept only in the [checkpoint](../checkpoint/README.md).
- `unresolved_baseline`, `missing`, `unavailable` are labels the reporter can emit for supplied or absent data. None occurs here.
- A `split_transport` batch is a request that was divided into one-calendar-day provider requests, because Dhan rejects multi-day ranges. It is not a gap.
- Per-expiry totals of the requested coverage that Dhan could not supply are in `source_minute_deficit_floor_vs_spot` (243,964 monthly minutes and 298,923 weekly in total).
- The largest monthly losses to quarantine are 29 May 2025 (1,844 keys), 30 Jun 2022 (1,500), 29 Aug 2024 (1,477), 27 Jun 2024 (1,259) and 27 Feb 2025 (939).

Sanity columns in the manifests: `offset_order_inversions` counts observations at a non-zero ATM offset whose strike is on the wrong side of the ATM (offset 0) strike of the same minute (12,267 of 19,232,626 checks monthly, 25 of 17,071,478 weekly) and `option_spot_outside_minute_ohlc_rows` counts option rows whose `spot` field lies outside that minute's spot high-low range (357,840 monthly and 129,987 weekly). They are reported, not filtered.

Known corrections and open items:

- **April and May 2025 overrides.** The 24 April 2025 monthly is fetched through Dhan's `WEEK` series and 29 May 2025 through `MONTH` code 2 until 30 April. See [dhan-flag-evidence.csv](dhan-flag-evidence.csv), [dhan-flag-checks.csv](dhan-flag-checks.csv) (37 pass, 5 `unavailable`, 0 fail) and `backfill/dhan-overrides.csv`. The 24 April contract has candles only from 4 April 2025 (`unresolved`, 25 quarantined keys).
- **Calendar verification.** `calendar_verified` is true for 14 monthly and 19 weekly expiries. The others are derived dates, backed by the Dhan expiry-day checks ([monthly](dhan-expiry-day-check-monthly.csv): 60 pass, 1 `no_data`; [weekly](dhan-expiry-day-check-weekly.csv): 199 pass, 2 fail on thresholds, 12 September 2024 and 8 May 2025). These are evidence, not NSE verification.
- [request-results-monthly.csv](request-results-monthly.csv) (7.8 MB) and [request-results-weekly.csv](request-results-weekly.csv) (7.4 MB) are in ordinary Git. Move them to LFS if they grow past 20 MB.

## Spot validation

All 61 minute partitions contain 463,250 valid candles across 1,241 exchange sessions, which equals the 1,241 daily candles. Of the 61 spot months, 42 are `full_requested` (321,482 candles), 12 are `source_limited` (88,051 candles) and 7 are `unresolved` (53,717 candles). The 12 source-limited months have missing minutes or special sessions (the inventory's `special_sessions` are 24 Oct 2022, 12 Nov 2023, 2 Mar 2024, 18 May 2024 and 1 Nov 2024, and `regular_minute_gaps` also records the short sessions of 21 Oct 2025 and 8 Nov 2026 with 315 and 305 missing regular minutes). The 7 unresolved months are 2022-03, 2022-04 and 2024-04 to 2024-08: 11 minutes in total (1, 1, 2, 2, 1, 2 and 2) failed the OHLC ordering check even after a retry and were excluded, not repaired. They are listed as `malformed` in [request-results-spot.csv](request-results-spot.csv), and [spot-inventory.csv](spot-inventory.csv) lists the observed and missing minutes per month. The 54 spot batches that returned and the 7 malformed ones, plus 6 daily batches, are the whole spot request ledger.

## Coverage files

- [Monthly](expiry-manifest-monthly.csv) and [weekly](expiry-manifest-weekly.csv) expiry manifests record session and contract totals, offset coverage by expiry code and side, `calendar_verified`, quarantine and held counts, and the label (`coverage_status`).
- [Spot inventory](spot-inventory.csv) records the 61 calendar partitions, observed and missing sessions, minute counts and status.
- Options by session and offset, [monthly](option-session-offsets-monthly.csv) and [weekly](option-session-offsets-weekly.csv), record offsets with raw rows as well as returned, empty, unsupported, aged-out and unresolved outcomes for each exchange session and side.
- Request outcomes, [monthly](request-results-monthly.csv), [weekly](request-results-weekly.csv) and [spot](request-results-spot.csv), record every attempted range. The `status` is `returned`, `empty`, `conflicting`, `split_transport` or `malformed`.
- Dhan code mapping, [monthly](dhan-rollover-evidence-monthly.csv) and [weekly](dhan-rollover-evidence-weekly.csv), and the same evidence with an added `expiry` column that the downloader reads as `--mapping-evidence`, [monthly](rollover-mapping-monthly.csv) and [weekly](rollover-mapping-weekly.csv), record the Dhan-internal comparisons that validate the relative expiry codes.
- [Dhan flag evidence](dhan-flag-evidence.csv) and [flag checks](dhan-flag-checks.csv) back the 2025 overrides.
- Dhan expiry-day checks: [monthly](dhan-expiry-day-check-monthly.csv) and [weekly](dhan-expiry-day-check-weekly.csv).
- [Spot-gap contract evidence](spot-gap-contract-evidence.csv) is empty (header only) because no spot gap needed contract evidence.
- [LFS publication receipts](lfs-publications.csv) record the commit at which each expiry and spot partition was verified (24 distinct commits).

See [HANDOVER.md](../HANDOVER.md) for the decisions behind these results and [the backfill instructions](../backfill/README.md) to resume or re-run.
