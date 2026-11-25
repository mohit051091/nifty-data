# NIFTY 50 1-minute historical data (build in progress)

One-minute NIFTY 50 spot and Dhan expired-options candles, 24 November 2021 to 23 November 2026 inclusive,
Asia/Kolkata. This is the NIFTY adaptation of the finished Bank Nifty pipeline. **No data is committed yet.**
Only the pipeline, the expiry calendars and the pilot method are here. The full backfill has not been run.

## Layout

| Path | Contents |
| --- | --- |
| `data/monthly/YYYY-MM-DD/NIFTY_<expiry>_<strike>_<CE\|PE>.parquet` | Monthly contracts, one file per strike and side. |
| `data/weekly/YYYY-MM-DD/NIFTY_<expiry>_<strike>_<CE\|PE>.parquet` | Weekly contracts that are **not** also the monthly contract. |
| `spot/NIFTY_SPOT_YYYY-MM.parquet`, `spot/NIFTY_SPOT_DAILY.parquet` | One-minute spot by month, and daily spot. |
| `backfill/` | Downloader, checkpoint ledger, calendar builder, verifiers, publisher. See [backfill/README.md](backfill/README.md). |

Parquet files live in Git LFS (`.gitattributes`), Zstandard compressed. Schema, identical to Bank Nifty: `expiry` date,
`strike` int64, `side` string, `timestamp` timestamp[ms, Asia/Kolkata], `trade_date` date, `open`, `high`, `low`,
`close`, `volume`, `oi`, `iv`, `spot` (double). Spot files carry `timestamp`, `trade_date`, OHLC and `volume`.

## Monthly versus weekly: the dedupe rule

**The last weekly expiry of each calendar month is the monthly contract.** It is one instrument. It is stored only
under `data/monthly/` and never copied to `data/weekly/`. A weekly expiry folder therefore exists for every weekly
date except those 61 monthly dates. "Last" means the last expiry by actual date in the calendar month, which is
also how Dhan's `MONTH` flag behaves. It differs from NSE's own series only in April 2025: the weekly of 1 May
(a holiday) moved to Wednesday 30 Apr, after the NSE monthly of 24 Apr. Dhan serves 30 Apr as the monthly, so
`data/monthly/2025-04-30` holds it and 24 Apr is stored as a weekly. Dhan serves the same contract under either flag (checked: on 14 Jan 2026,
`WEEK` code 2 and `MONTH` code 1 return identical rows), so nothing is lost. `backfill/expiries-weekly.csv` lists all
weekly dates with a `monthly` column, and the downloader refuses to fetch a `monthly=true` date as a weekly.

Because a weekly contract is fetched only from the day after the previous weekly expiry, a weekly folder holds about
one week of trading. A monthly folder holds about a month. The weekly that follows a monthly date starts on the day
after that monthly date, and the rollover day (the previous expiry day) appears under both the outgoing and the
incoming contract, exactly as in the Bank Nifty data.

## Expiry calendar

Weekly expiry is Thursday to 31 Aug 2025 and Tuesday from 1 Sep 2025. Monthly is the last of those in the month.
A holiday moves the expiry to the previous trading day. Every date is `derived` until a NSE F&O bhavcopy for that expiry
day confirms it, and then it is `verified`. See [backfill/NSE-REFERENCE.md](backfill/NSE-REFERENCE.md).
