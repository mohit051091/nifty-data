# NIFTY 50 1-minute historical data

**AI assistants and new readers:** read this file and then [HANDOVER.md](./HANDOVER.md) before doing anything else. See also [AGENTS.md](./AGENTS.md). The canonical series are [one-minute NIFTY 50 spot](./spot/) and the Dhan options, [monthly](./data/monthly/) and [weekly](./data/weekly/). See the folder dictionaries for their schemas and [coverage](./coverage/) for the latest publication checks and known gaps. The target is 24 November 2021 through 23 November 2026, inclusive, in Asia/Kolkata. The [backfill instructions](./backfill/) document the source and verification workflow.

This is the NIFTY adaptation of the finished Bank Nifty pipeline (a sibling repository, `bank-nifty-data`, which holds monthly options only). The source is the Dhan Data API. The data is **partial and source-limited**: nothing was guessed, and conflicting source values were quarantined rather than published. Read the [coverage summary](./coverage/README.md) before relying on a month.

To retrieve the Parquets, use an authorized GitHub account and Git LFS.

```sh
git lfs install
git clone https://github.com/mohit051091/nifty-data.git
cd nifty-data
git lfs pull
```

## What the repository holds (24 November 2021 to 23 November 2026)

- [One-minute NIFTY 50 spot](./spot/): 61 monthly Parquet partitions, 463,250 candles over 1,241 sessions, plus a daily file.
- [Monthly options](./data/monthly/): one Zstandard Parquet per expiry, strike and side. 61 expiries, 5,788 files, 20,094,841 rows.
- [Weekly options](./data/weekly/): the same layout for the weekly contracts that are not also the monthly contract. 201 expiries, 13,359 files, 17,892,021 rows.
- [Coverage](./coverage/): per-expiry manifests, spot inventory, request outcomes, Dhan evidence and Git LFS publication receipts.
- [Checkpoint](./checkpoint/README.md): compressed SQLite of every Dhan observation, including quarantined rows. Skipped by a normal `git lfs pull`.
- [Backfill instructions](./backfill/): how the data was fetched, validated and published.

Combined, the options are 262 expiries, 19,147 files and 37,986,862 rows.

## Layout

| Path | Contents |
| --- | --- |
| [`data/monthly/`](./data/README.md) `YYYY-MM-DD/` | `NIFTY_<expiry>_<strike>_<CE\|PE>.parquet`, one file per strike and side, for each NSE monthly expiry. |
| [`data/weekly/`](./data/README.md) `YYYY-MM-DD/` | The same file layout for weekly expiries that are **not** a monthly expiry. |
| [`spot/`](./spot/README.md) | `NIFTY_SPOT_YYYY-MM.parquet` monthly minute candles and `NIFTY_SPOT_DAILY.parquet`. |
| [`coverage/`](./coverage/README.md) | Manifests (one per expiry type), per-session offset coverage, request outcomes, LFS receipts and Dhan evidence. |
| [`checkpoint/`](./checkpoint/README.md) | `candles.sqlite.xz`, 861 MB, not downloaded by default. |
| [`backfill/`](./backfill/README.md) | Downloader, ledger, calendar builder, verifiers, publisher, NSE reference and the resume script. |
| [`HANDOVER.md`](./HANDOVER.md) | Why the data looks the way it does, decisions, resume steps and known limits. |

All Parquets and the checkpoint are stored in Git LFS (`.gitattributes`).

## Monthly versus weekly

An expiry is stored under exactly one of the two folders, never both.

- **Monthly** is NSE's monthly contract: the last expiry whose **nominal** date (the scheduled Thursday, or Tuesday from September 2025, before any holiday shift) falls in the month. It lives only in `data/monthly/`.
- **Weekly** is every other expiry. The last weekly expiry of a month is the monthly contract, so it exists **only** under `data/monthly/` and has no folder under `data/weekly/`. A weekly that a holiday pulls back into an earlier month stays a weekly.

The one month where this differs from "last expiry by actual date" is April 2025. The weekly nominally due Thursday 1 May 2025 (a holiday) moved back to Wednesday 30 April, after the NSE monthly of Thursday 24 April. So the April 2025 monthly is `data/monthly/2025-04-24` and the weekly is `data/weekly/2025-04-30`. Dhan labels these the other way round, so these contracts need the special fetch rules in [HANDOVER.md](./HANDOVER.md) (`backfill/dhan-overrides.csv`). The 24 April 2025 contract is available from Dhan only from 4 April 2025.

Each folder holds the contract from the day after the previous expiry of the same type, plus the previous expiry day itself, when it is the incoming contract (the rollover day). A weekly folder is therefore about one week of trading and a monthly folder about a month, and the rollover day appears under both the outgoing and the incoming contract.

## Using this data for research

Install `pyarrow` and `pandas`, clone with Git LFS as shown above, then run from the repository root:

```python
import pandas as pd
import pyarrow.dataset as ds

def load_expiry(kind, expiry):
    """All strikes and sides of one expiry. kind is 'monthly' or 'weekly'."""
    table = ds.dataset(f"data/{kind}/{expiry}", format="parquet").to_table()
    return table.to_pandas().sort_values(["timestamp", "strike", "side"], ignore_index=True)

monthly = load_expiry("monthly", "2025-04-24")   # NSE April 2025 monthly (24 Apr, not 30 Apr)
weekly = load_expiry("weekly", "2025-04-30")     # the weekly that follows it
spot = pd.read_parquet("spot/NIFTY_SPOT_2025-04.parquet")   # one month of spot minutes

# Calls of the monthly on one session, joined to the spot close of the same minute.
day = pd.Timestamp("2025-04-21").date()
calls = monthly[(monthly.trade_date == day) & (monthly.side == "CE")]
calls = calls.merge(spot[["timestamp", "close"]].rename(columns={"close": "spot_close"}),
                    on="timestamp", how="left")

# Check reliability before trusting an expiry.
manifest = pd.read_csv("coverage/expiry-manifest-monthly.csv")   # or expiry-manifest-weekly.csv
print(manifest.loc[manifest.expiry == "2025-04-24", ["coverage_status", "quarantined_keys", "rows"]])
```

Run against the committed data, this gives 178,793 monthly rows, 78,747 weekly rows, 7,875 call rows for 21 April 2025 (each with a spot close) and, from the manifest, `unresolved` with 25 quarantined keys for the monthly. Stack several expiries with `pd.concat(load_expiry("weekly", e) for e in expiries)`. The monthly and weekly folders for the same instrument never overlap, so concatenating both types does not double count contracts.

To fetch only part of the data, use for example `git lfs pull --include="data/weekly/2025-04-30/*,spot/*"`.

Things to know before analysing:

- **Gaps are absent rows**, never zero-filled. Reindex to the spot minutes if you need a regular grid.
- **Strike coverage follows the ATM.** Each session covers about ATM -10 to ATM +10 strikes (50-point steps for NIFTY), so far-from-ATM strikes only have the sessions when they were near the money.
- **Rollover day appears twice.** On the previous expiry's date, both the outgoing and the incoming contract have candles, each in its own expiry folder. For the April 2025 pair, 24 April 2025 is in `data/monthly/2025-04-24` and also the first day of `data/weekly/2025-04-30`.
- **Quarantine.** `unresolved` expiries are usable but incomplete. Where Dhan returned the same strike and minute under two ATM offsets with different values, both observations were quarantined and the minute is absent. Check `quarantined_keys` in the manifest. The monthly 29 May 2025 expiry lost the most (1,844 keys).
- **Source gaps.** `source_limited` means some minutes, offsets or sessions were never returned by Dhan. Special sessions (Muhurat, Saturday sessions) have far fewer minutes.
- **Derived versus verified calendar.** Only 33 of the 262 in-window expiry dates (the 14 monthly dates and 19 weekly dates) were checked against NSE bhavcopies. The rest of the expiry dates are derived from NSE's rules and were checked against Dhan data (decayed time value on expiry day), not against NSE files. See [HANDOVER.md](./HANDOVER.md) and `calendar_verified` in the manifests.
- **LFS pointers.** If a Parquet is a small text file starting with `version https://git-lfs`, it is an unpulled pointer, not data. Run `git lfs pull`. Never analyse a pointer.
- **Units.** `timestamp` is IST and timezone-aware. `iv` is as returned by Dhan, with no rescaling. Spot `volume` is not meaningful for an index.
- **Foreign rows.** Dhan sometimes returns rows of another index at far strikes. Those rows (20,784 monthly, 22 weekly) were excluded and are not in the Parquets.

## Data dictionary

Each folder has its own dictionary: [options](./data/README.md) and [spot](./spot/README.md). The exact column types are also listed in [HANDOVER.md](./HANDOVER.md). Timestamps are `timestamp[ms, tz=Asia/Kolkata]` and the row key for options is `expiry + strike + side + timestamp`.

## Expiry calendar

NIFTY weekly expiry is Thursday up to 28 August 2025 and Tuesday from 2 September 2025. A holiday moves an expiry to the previous regular trading session (for example Monday 20 October 2025). Each date is `derived` until NSE F&O bhavcopies confirm it and then `verified`. See [backfill/NSE-REFERENCE.md](backfill/NSE-REFERENCE.md) and [HANDOVER.md](./HANDOVER.md).
