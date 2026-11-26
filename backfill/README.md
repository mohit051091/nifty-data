# Run the NIFTY backfill

The frozen interval is `[2021-11-24, 2026-11-24)` in Asia/Kolkata. Dhan is the source of the minute candles. The governing expiry dates, exchange sessions and NSE evidence are in [the NSE reference](NSE-REFERENCE.md). This is the finished NIFTY run: all 61 monthly expiries, 201 weekly expiries and 61 spot months are published. The commands below resume, audit or repeat it. See [HANDOVER.md](../HANDOVER.md) for why the data looks as it does.

Use Python 3.11 or newer, Git LFS, the authorized GitHub identity, and a Dhan access token supplied to the running process as `DHAN_ACCESS_TOKEN`. Never commit the token. Keep the checkpoint and verification output outside the checkout, for example `../nifty-state`. The checkpoint can be restored from [checkpoint/](../checkpoint/README.md).

```sh
python -m pip install -r backfill/requirements.txt
git lfs install --local
git lfs pull
state=../nifty-state
python -m backfill.download --repo . --state "$state" status
```

The underlying (`--underlying NIFTY|BANKNIFTY`, default NIFTY, Dhan security id 13 for NIFTY and 25 for Bank Nifty, both `IDX_I`) and the expiry type (`--expiry-type monthly|weekly`, Dhan `expiryFlag` `MONTH` or `WEEK`, default `monthly` for `download`) are parameters defined in `config.py`. Nothing else hard-codes either.

## Calendars

```sh
python -m backfill.build_calendar                                  # regenerates expiries-*.csv, rollovers-*.csv, bhavcopies-needed.csv, dhan-overrides.csv
python -m backfill.verify_expiries --cache-dir NSE_CACHE --list-missing   # dates no supplied bhavcopy confirms yet
python -m backfill.verify_expiries --cache-dir NSE_CACHE           # upgrade derived to verified
python -m unittest backfill.test_verify_expiries                   # 7 tests
```

`build_calendar` writes every date as `derived` and overwrites the verified marks, so rerun `verify_expiries` afterwards with every bhavcopy that was supplied. `verify_expiries` accepts both bhavcopy name styles, counts a listing only within 100 days of the file, and aborts without writing when a file contradicts the calendar. Details and the rules are in [NSE-REFERENCE.md](NSE-REFERENCE.md). `sessions.csv` is the exchange session list shared with the Bank Nifty repository. Its `source` column therefore names the NSE `NIFTY BANK` index history. The 1,241 sessions match the daily NIFTY candles Dhan returns.

## Dhan flag overrides

`dhan-overrides.csv` is written by `build_calendar`. It lists, for monthly contracts that Dhan's `MONTH` flag does not serve as NSE defines them, the flag and relative code per date range: the 24 April 2025 monthly through `WEEK` codes 5 down to 1, and the 29 May 2025 monthly through `MONTH` code 2 until 30 April then code 1. The downloader reads it automatically. Dhan serves 24 April only from 4 April 2025, so 27 March to 3 April are `unavailable`. To re-collect the Dhan evidence:

```sh
python -m backfill.verify_flag_overrides --repo .
```

It writes [coverage/dhan-flag-evidence.csv](../coverage/dhan-flag-evidence.csv) and [coverage/dhan-flag-checks.csv](../coverage/dhan-flag-checks.csv) (default 4 requests a second, about 4 requests per covered day).

## Code mapping and expiry-day evidence

```sh
python -m backfill.verify_dhan_codes --expiry-type weekly --repo . --trade-date 2026-01-06 --output "$state/mapping-weekly.csv"
python -m backfill.verify_dhan_codes --expiry-type weekly --repo . --expiry-days --output "$state/expiry-day-check-weekly.csv"
```

The first run gathers the Dhan-only evidence that the relative codes map as documented (written as `coverage/rollover-mapping-<type>.csv`, which gets an `expiry` column). `--expiry-days` requests the ATM straddle on every calendar date and is the check behind `coverage/dhan-expiry-day-check-<type>.csv`. It is evidence, not verification. `python -m backfill.verify_mapping --cache-dir NSE_CACHE --output ...` is the NSE open-price alternative once bhavcopies exist. It was not used for this run.

## Fetch, report and publish

Dhan rejects multi-day `fromDate`/`toDate` ranges (error DH-905), so one-day provider requests that keep a confirmed consecutive session are the default (`--no-provider-day-pairs` disables it). Requests go out at 2 a second by default (`--requests-per-second`, `--workers` 1 to 4). Rate-limit answers (HTTP 429, DH-904) are retried with backoff, and an expired token (DH-901) stops the run cleanly.

```sh
python -m backfill.download --repo . --state "$state" spot --month 2026-01
python -m backfill.download --repo . --state "$state" --expiry-type weekly --accept-derived-calendar \
    --mapping-evidence coverage/rollover-mapping-weekly.csv options --expiry 2026-01-13
python -m backfill.download --repo . --state "$state" --expiry-type weekly report
python -m backfill.download --repo . --state "$state" --expiry-type weekly --accept-derived-calendar \
    --mapping-evidence coverage/rollover-mapping-weekly.csv export-options --expiry 2026-01-13   # re-export from the ledger, no Dhan request
```

- A date with `monthly=true` in `expiries-weekly.csv` is the monthly contract. `download --expiry-type weekly --expiry <that date>` stops with an error.
- Exports of dates not verified against NSE need `--accept-derived-calendar` and are recorded as `calendar_verified=false`.
- Where a session's spot minutes are unavailable, the downloader checks the exchange session and contract listing for admissible observations. Rows whose `spot` differs from NIFTY by more than 1 % are dropped as another index's rows.
- The ledger keeps the expiry flag with every request, so the checkpoint preserves accepted requests on retry.

Publish validated files using the exact data folders or partitions. This uploads LFS objects, pushes a normal fast-forward update to `main`, verifies a clean checkout and updates the receipt [coverage/lfs-publications.csv](../coverage/lfs-publications.csv). If `main` has advanced, reconcile it before retrying.

```sh
python -m backfill.publish --repo . --state "$state" --path data/weekly/2026-01-13
python -m backfill.publish --repo . --state "$state" --path spot/NIFTY_SPOT_2026-01.parquet
```

## Whole-run helpers

```sh
python -m backfill.run --repo . --state "$state" --expiry-type monthly --accept-derived-calendar --pilot-only
BACKFILL_STATE="$state" python backfill/resume_expiries.py monthly --accept-derived-calendar   # or weekly
```

`run.py` fetches spot and the pilot expiry (30 December 2021 monthly, 2 December 2021 weekly), then every remaining expiry, and publishes. `resume_expiries.py` fetches, reports, merges coverage, publishes and pushes each expiry whose `data/<type>/<expiry>` folder has no Parquets. A finished repository makes it a no-op. A run stopped part-way can leave an incomplete folder. Move it aside before restarting. Both push to `origin/main`, so use them only with the owner's approval to do so.

## Coverage files and audits

Coverage files are per type: `expiry-manifest-<type>.csv`, `option-session-offsets-<type>.csv` and `request-results-<type>.csv`, with the shared `spot-inventory.csv` and `request-results-spot.csv`. They are described in [coverage/README.md](../coverage/README.md). For a read-only audit of any published commit use `python -m backfill.verify_lfs --commit FULL_COMMIT_SHA`. It accepts optional scopes and expected row counts and records the results as JSON.

Tests: `python -m unittest backfill.test_verify_expiries` and `python -m compileall backfill`.
