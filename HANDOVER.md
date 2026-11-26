# Handover: NIFTY 50 one-minute backfill

Read this first. It explains what the repository holds, why it looks the way it does, and how to resume or audit the work. Last updated 26 November 2026. It is the NIFTY counterpart of the Bank Nifty repository's handover, with weekly options added.

## Goal

Publish one-minute NIFTY 50 spot and monthly and weekly options data for the frozen window **24 November 2021 to 23 November 2026 inclusive**, which is the interval `[2021-11-24, 2026-11-24)` in Asia/Kolkata. The targets are 61 monthly expiries (Nov 2021 to Nov 2026), 201 weekly expiries that are not also monthly, and 61 monthly spot partitions. The source is the Dhan Data API. NSE bhavcopy files, supplied manually, provide the exchange calendar and contract listings used to verify part of the expiry calendar.

## Status: partial and source-limited

All 262 expiries and 61 spot months are on `main` in Git LFS, and every unit has a `verified` receipt in [coverage/lfs-publications.csv](coverage/lfs-publications.csv) (324 receipts: 61 monthly, 201 weekly, 61 spot, 1 daily). Not every requested observation could be accepted. Totals come from the manifests and the Parquet footers, which agree.

| Series | Expiries | Files | Rows | `full_requested` | `source_limited` | `unresolved` | Quarantined keys | Foreign rows excluded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Monthly options | 61 | 5,788 | 20,094,841 | 0 | 12 | 49 | 19,292 | 20,784 |
| Weekly options | 201 | 13,359 | 17,892,021 | 5 | 168 | 28 | 119 | 22 |
| Spot (months) | 61 | 61 + daily | 463,250 minute candles, 1,241 daily | 42 | 12 | 7 | n/a | n/a |

- `unresolved` monthly expiries (49) all have quarantined keys: Dhan returned the same strike and minute under two ATM offsets with different values, and both observations were kept out of the Parquets. The worst is 29 May 2025 (1,844 keys).
- `unresolved` weekly expiries (28) split into 14 with quarantined keys (119 in total; 23 May 2024 has 90 of them) and 14 with only 41 to 126 `held_rows` each, option minutes that could not be validated because the spot series has no validated candle for them.
- 7 spot months (2022-03, 2022-04, 2024-04 to 2024-08) are `unresolved`: one or two source minutes in each failed the OHLC ordering check even after a retry (1 + 1 + 2 + 2 + 1 + 2 + 2 = 11 minutes). They are excluded, not repaired, and the months were left `unresolved` on purpose.

Per-expiry detail is in [coverage/expiry-manifest-monthly.csv](coverage/expiry-manifest-monthly.csv) and [coverage/expiry-manifest-weekly.csv](coverage/expiry-manifest-weekly.csv). Label definitions and the spot status are in [coverage/README.md](coverage/README.md).

## Layout

| Path | Contents |
| --- | --- |
| [`data/monthly/`](data/README.md) `YYYY-MM-DD/` | One Zstandard Parquet per strike and side for each NSE monthly expiry. |
| [`data/weekly/`](data/README.md) `YYYY-MM-DD/` | The same for weekly expiries that are not the monthly expiry. |
| [`spot/`](spot/README.md) | `NIFTY_SPOT_YYYY-MM.parquet` monthly minute candles and `NIFTY_SPOT_DAILY.parquet`. |
| [`coverage/`](coverage/README.md) | Manifests, per-session offset coverage, request outcomes, LFS publication receipts and the Dhan code-mapping and flag evidence. |
| [`checkpoint/`](checkpoint/README.md) | Compressed SQLite checkpoint of every Dhan observation, including quarantined rows. Not downloaded by default. |
| [`backfill/`](backfill/README.md) | Downloader, checkpoint ledger, calendar builder, publisher, verifiers, NSE reference tables and the resume script. |

All Parquets and the checkpoint are stored in Git LFS (`.gitattributes`).

## Monthly versus weekly

The monthly contract is NSE's: the last expiry whose **nominal** date (the scheduled weekday before any holiday shift) falls in the calendar month. It is stored only under `data/monthly/`. Every other expiry is a weekly and is stored under `data/weekly/`. So the last weekly of a month is the monthly contract and has no weekly folder: 262 expiry dates in the window give 61 monthly folders and 201 weekly folders. `backfill/expiries-weekly.csv` lists every weekly date with a `monthly` column, and the downloader refuses to fetch a `monthly=true` date as a weekly.

April 2025 is the only month where the NSE rule differs from "last expiry by actual date". The weekly nominally due 1 May 2025 (a holiday) moved to Wednesday 30 April, after the NSE monthly of Thursday **24 April 2025**. So the monthly is `2025-04-24` and the weekly is `2025-04-30`.

## Parquet schema

Option files, `data/<monthly|weekly>/<expiry>/NIFTY_<expiry>_<strike>_<CE|PE>.parquet`, read from the real files:

| Column | Type | Meaning |
| --- | --- | --- |
| `expiry` | date32 | Contract expiry. |
| `strike` | int64 | Actual strike price, read from Dhan's response. |
| `side` | string | `CE` or `PE`. |
| `timestamp` | timestamp[ms, Asia/Kolkata] | Candle start. |
| `trade_date` | date32 | Exchange session date. |
| `open`, `high`, `low`, `close` | double | Option premium. |
| `volume`, `oi` | double | As supplied by Dhan. |
| `iv` | double | Implied volatility as supplied, no rescaling. |
| `spot` | double | Underlying value Dhan recorded with the candle. |

None of the option columns is null in the files checked (monthly 2025-04-24 and weekly 2025-04-30). Minute spot files have `timestamp`, `trade_date`, `open`, `high`, `low`, `close` and `volume` (double). `NIFTY_SPOT_DAILY.parquet` has `trade_date` (date32), `open`, `high`, `low`, `close` and no `volume`. The row key for options is `expiry + strike + side + timestamp`. Candles run 09:15 to 15:29 IST.

## How the Dhan source behaves, and why

- Endpoint: `POST /v2/charts/rollingoption` for expired options and `POST /v2/charts/intraday` for spot (security id 13, segment `IDX_I`). `toDate` is exclusive.
- Requests are relative: `expiryFlag` is `MONTH` for monthly and `WEEK` for weekly, with `expiryCode` 1 or 2 and a strike label ATM-10 to ATM+10. The real strike is read from the response (`strike[i]`), not from the label.
- **Code mapping.** Code 1 is the nearest expiry on or after the session date, so a contract is code 1 on its own expiry day, and code 2 is the next expiry of that flag. For expiry E with previous expiry P of the same type, E is fetched as code 2 on P itself (the rollover day) and as code 1 for sessions after P up to E. The evidence is Dhan-internal (straddle time value and next-day open and open interest continuity at a rollover): [coverage/rollover-mapping-monthly.csv](coverage/rollover-mapping-monthly.csv) (30 Dec 2025, 27 Jan 2026) and [coverage/rollover-mapping-weekly.csv](coverage/rollover-mapping-weekly.csv) (4 dates from 6 Jan 2026). An NSE open-price check (`verify_mapping.py`) exists but was not used for this run.
- **Dhan rejects multi-day `fromDate`/`toDate` ranges (error DH-905).** The default is therefore one-day provider requests. Dhan sometimes returns rows of the exclusive end session as well. Those rows are clipped, or reused only when the end session is the expected consecutive session, and the ledger records `clipped provider rows on exclusive end date`. Batches that had to be split to one day are `split_transport` in the request results (not gaps).
- **The same strike-minute can be returned under two offsets.** When values differ, both are quarantined in the ledger instead of choosing one. This is the cause of every quarantine count. The number of conflicting batches is 2,404 (monthly) and 47 (weekly).
- **Foreign-index rows.** At far strikes, notably ATM-10, Dhan sometimes returns rows of another index (a `spot` field in the 13,9xx or 67,9xx range in a NIFTY series). A row whose `spot` differs from the NIFTY spot close of that minute by more than 1 % (`FOREIGN_SPOT_TOLERANCE`) is dropped. Real rows differ by less than 0.03 %. Counts: 20,784 monthly rows and 22 weekly rows.
- **Rate limits and tokens.** Dhan answers HTTP 429 or error DH-904 when requests come too fast. The client defaults to 2 requests a second (1 to 4 workers), retries network errors, 5xx and rate-limit answers up to 4 attempts with a delay of at least `2^attempt` seconds or the `Retry-After` header, and stops when `Retry-After` exceeds 60 seconds. Access tokens expire periodically (HTTP 401, error DH-901). The run then stops cleanly: resupply `DHAN_ACCESS_TOKEN` and rerun. No token is stored in the repository.
- **April and May 2025 overrides.** Dhan's `MONTH` series serves 30 April 2025 as the April monthly and has no 24 April contract. [backfill/dhan-overrides.csv](backfill/dhan-overrides.csv) therefore fetches the NSE monthly **2025-04-24** through `expiryFlag=WEEK` (codes 5 down to 1 as it nears expiry) and the May monthly **2025-05-29** through `MONTH` code 2 until 30 April, then code 1. The 30 April contract is fetched as an ordinary weekly. The evidence is [coverage/dhan-flag-evidence.csv](coverage/dhan-flag-evidence.csv) and [coverage/dhan-flag-checks.csv](coverage/dhan-flag-checks.csv) (37 pass, 5 unavailable, 0 fail).
- **2025-04-24 data exist only from 2025-04-04.** Dhan's `WEEK` series does not serve that contract earlier: codes 5 and 4 on 27 Mar to 3 Apr return no rows. The monthly folder `data/monthly/2025-04-24` therefore starts 4 April 2025 (`first_candle` in the manifest).
- **Expiry-day check.** Both Dhan calendars were checked with an expiry-day test (the straddle's time value has decayed at the close and a fresh contract opens the next session). Monthly: 60 pass and 1 `no_data` (23 Nov 2026, no later session in the window). Weekly: 199 pass and 2 fail on thresholds only (12 Sep 2024 and 8 May 2025). See [coverage/dhan-expiry-day-check-monthly.csv](coverage/dhan-expiry-day-check-monthly.csv) and [coverage/dhan-expiry-day-check-weekly.csv](coverage/dhan-expiry-day-check-weekly.csv). This is evidence, not NSE verification.

## Expiry weekday history and holiday shifts

- NIFTY weekly expiry was **Thursday until 28 August 2025** and **Tuesday from 2 September 2025**. A nominal day that is not a regular trading session moves to the previous regular session.
- Wednesday shifts (a Thursday holiday): 13 Apr 2022, 25 Jan 2023, 29 Mar 2023, 28 Jun 2023, 10 Apr 2024, 14 Aug 2024, 9 Apr 2025 and 30 Apr 2025.
- Monday shifts (a Tuesday holiday): 20 Oct 2025, 2 Mar 2026, 30 Mar 2026, 13 Apr 2026, 19 Oct 2026, 9 Nov 2026 and 23 Nov 2026.
- **20 Oct 2025.** Nominal Tuesday 21 October 2025 was the Muhurat session only, not a regular session, so the expiry moved to Monday 20 October (NSE-verified). Other non-regular days, never expiry days: 24 Oct 2022, 12 Nov 2023, 2 Mar 2024, 18 May 2024 and 1 Nov 2024.
- **24 Nov 2026** is outside the window and has no Dhan candles, so the November 2026 monthly is the Monday 23 November 2026 expiry, which the Bank Nifty repository had verified against NSE.
- Monthly expiry weekdays: 43 Thursdays, 3 Wednesdays (25 Jan, 29 Mar and 28 Jun 2023), 13 Tuesdays (30 Sep 2025 onwards) and 2 Mondays (30 Mar and 23 Nov 2026). Do not infer them: use [backfill/expiries-monthly.csv](backfill/expiries-monthly.csv) and [backfill/expiries-weekly.csv](backfill/expiries-weekly.csv).

## Derived versus verified calendar

Every date is generated by `python -m backfill.build_calendar` as `derived`. `backfill/verify_expiries.py` flips a date to `verified` only when an NSE F&O bhavcopy lists a NIFTY option expiring on it, and for a monthly date a NIFTY future too. A listing counts only within a **100-day horizon** of the file's date, because NSE lists long-dated June and December options years ahead on nominal dates that it re-dates later. A file that contradicts the calendar aborts the run without writing. Six bhavcopies were supplied: 1 Nov 2021, 12 Sep 2024, 24 Apr 2025, 20 Oct 2025 and, from the Bank Nifty repository, 27 Oct 2026 and 23 Nov 2026. They verified **14 monthly dates and 34 weekly-calendar rows**. The 34 rows include the 14 monthly dates (the weekly calendar lists every date) and the pre-window anchor 18 Nov 2021, so the in-window verified dates are 14 monthly plus 19 weekly-only, 33 of 262. The rest are `derived` and were exported with `--accept-derived-calendar`, which records `calendar_verified=false`. They are checked against Dhan only (the expiry-day test above). 61 monthly bhavcopy snapshots (`bhavcopies-needed.csv`) would verify the lot.

## Decisions and their reasons

1. **Quarantine instead of tie-breaking.** The Bank Nifty handover required it. A lowest-offset tie-break would recover minutes, but it guesses which series is correct, and correctness was chosen over coverage.
2. **NSE's monthly definition over Dhan's.** The monthly contract follows NSE (last nominal expiry of the month), not Dhan's `MONTH` flag, which differs only in April and May 2025. That is why the override file exists.
3. **Derived calendar with Dhan checks, verified where NSE files exist.** NSE downloads are not automated, so only six bhavcopies were supplied. Verified dates are 14 monthly and 34 weekly rows (above). Others stay `derived`, not silently promoted.
4. **7 spot months with invalid OHLC kept `unresolved`.** The offending minutes were excluded after a retry and the months were not relabelled. Option minutes with no validated spot candle are held back.
5. **Direct pushes to `main`** as normal fast-forward commits, approved by the owner.
6. **Commit `d1a5b33` lacks co-author trailers, history not rewritten.** It is the commit "Publish 62 validated NIFTY partitions" (the spot partitions and the daily file). The root commit also has none. A rewrite would change every later commit hash and invalidate the `commit` column of [coverage/lfs-publications.csv](coverage/lfs-publications.csv). Every other commit carries them.

## How to resume or re-run

```sh
python -m pip install -r backfill/requirements.txt
git lfs install --local && git lfs pull
export DHAN_ACCESS_TOKEN=...             # never commit it
export BACKFILL_STATE=../nifty-state     # checkpoint directory outside the checkout
python -m backfill.download --repo . --state "$BACKFILL_STATE" status
python -m backfill.download --repo . --state "$BACKFILL_STATE" --expiry-type monthly report
python backfill/resume_expiries.py monthly    # or weekly: fetch, report, publish every expiry with no Parquets
```

`resume_expiries.py` skips any `data/<type>/<expiry>` folder that already holds Parquets, so a finished repository is a no-op. A run stopped part-way can leave an incomplete folder. Move it aside before restarting. It pushes to `origin/main` after every expiry. See [backfill/README.md](backfill/README.md) for the individual commands (spot, calendars, verifiers, publishing, audit with `verify_lfs`).

Checkpoint: [checkpoint/candles.sqlite.xz](checkpoint/README.md) is a `VACUUM INTO` copy of the ledger. It is 860,593,116 bytes compressed, 4,855,238,656 bytes (4.9 GB) decompressed, SHA-256 `3750eee7156a5b261f3ae2c2307df49777cd75edd2e11aabf5eff5cf084c2113`. It holds 38,129,254 option observations (19,411 quarantined, the sum of the monthly 19,292 and weekly 119), 38,652,857 `observations` rows, 115,357 request batches, 504,156 one-minute spot rows, 1,241 daily spot rows and 19,633 recorded conflicts. `.lfsconfig` excludes it from a normal `git lfs pull`. Fetch it on purpose with `git lfs pull --include="checkpoint/*" --exclude=""`, decompress it with `xz -dc` into a folder outside the checkout and pass that folder as `BACKFILL_STATE`. The committed `coverage/` files are the durable summary.

## Known limits and open items

- Quarantined minutes cannot be recovered without either a better source or a guess.
- The derived calendar is not NSE-verified for 229 of the 262 in-window expiry dates. [backfill/bhavcopies-needed.csv](backfill/bhavcopies-needed.csv) lists the 61 monthly snapshot files that would verify all of them.
- The 2025-04-24 monthly has no data before 4 April 2025, and its WEEK code-5 and code-4 days are unavailable.
- Special sessions (Muhurat, Saturday sessions) and a few source minutes are missing from spot, which marks 12 spot months `source_limited`.
- [coverage/request-results-monthly.csv](coverage/request-results-monthly.csv) and [coverage/request-results-weekly.csv](coverage/request-results-weekly.csv) are about 7 to 8 MB each in ordinary Git. Move them to LFS if they grow past about 20 MB.
- The supplied NSE bhavcopy ZIPs are not stored here. The calendar's `source` column names the file that verified each date.
- The 504,156 ledger spot rows reconcile exactly with the 463,250 published candles: 40,303 are minutes outside the regular or special session times (for example pre-open and post-close candles), and 603 are on six 2022 Saturdays that were not exchange sessions (2022-01-08, 01-22, 02-05, 04-09, 04-23 and 04-30). Neither kind is published.
- The `source` column of [backfill/sessions.csv](backfill/sessions.csv) names the NSE `NIFTY BANK` index history. The session list is the exchange calendar shared by both indices, and it matches the 1,241 daily NIFTY candles Dhan returns for the window. The file was copied from the Bank Nifty repository and left unchanged.
