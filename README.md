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

**The monthly contract of a month is the last expiry whose nominal date (the scheduled Thursday, or Tuesday from Sep 2025,
before any holiday shift) falls in that month: NSE's own definition.** It is one instrument. It is stored only under
`data/monthly/` and never copied to `data/weekly/`, so a weekly expiry folder exists for every weekly date except those
61 monthly dates. A weekly that a holiday pulls back into an earlier month stays a weekly. This changes one month: the
weekly nominally due 1 May 2025 (a holiday) moved to Wednesday 30 Apr, after the NSE monthly of Thursday 24 Apr 2025, so
`data/monthly/2025-04-24` and `data/weekly/2025-04-30`. Every other month is the same under either reading.

**April 2025 override.** Dhan's `expiryFlag=MONTH` does not follow NSE here: it serves 30 Apr as the April "monthly" and
has no 24 Apr contract. So the downloader fetches the NSE monthly 24 Apr through `expiryFlag=WEEK` (code 4 down to 1 as it
nears expiry; Dhan serves it only from 4 Apr 2025, so 27 Mar to 3 Apr are empty) and 30 Apr as an ordinary weekly, and the
May monthly (29 May) as MONTH code 2 until 30 Apr, then code 1. The per-day flag and code are in
`backfill/dhan-overrides.csv`; the Dhan evidence is `coverage/dhan-flag-evidence.csv` and `coverage/dhan-flag-checks.csv`
(`backfill/verify_flag_overrides.py`). `backfill/expiries-weekly.csv` lists all weekly dates with a `monthly` column, and the
downloader refuses to fetch a `monthly=true` date as a weekly.

Because a weekly contract is fetched only from the day after the previous weekly expiry, a weekly folder holds about
one week of trading. A monthly folder holds about a month. The weekly that follows a monthly date starts on the day
after that monthly date, and the rollover day (the previous expiry day) appears under both the outgoing and the
incoming contract, exactly as in the Bank Nifty data.

## Expiry calendar

Weekly expiry is Thursday to 28 Aug 2025 and Tuesday from 2 Sep 2025. Monthly is the last of those whose nominal date is
in the month (NSE's rule, see above). A holiday moves the expiry to the previous trading day. Every date is `derived`
until NSE F&O bhavcopies confirm it, and then it is `verified`. See [backfill/NSE-REFERENCE.md](backfill/NSE-REFERENCE.md).
