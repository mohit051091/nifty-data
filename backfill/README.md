# Run the NIFTY backfill

Frozen interval `[2021-11-24, 2026-11-24)` in Asia/Kolkata. Python 3.11+, `pip install -r backfill/requirements.txt`,
`DHAN_ACCESS_TOKEN` in the environment, state outside the checkout (for example `/tmp/claude-0/nifty-state`).
The underlying (`--underlying NIFTY|BANKNIFTY`, default NIFTY, Dhan id 13 `IDX_I`) and the expiry type
(`--expiry-type monthly|weekly`, Dhan `expiryFlag` MONTH or WEEK) are parameters. See `config.py`.

```sh
S=/tmp/claude-0/nifty-state
python -m backfill.build_calendar                                    # regenerate calendars (derived dates)
python -m backfill.verify_expiries --cache-dir NSE_CACHE             # derived -> verified, needs NSE bhavcopies (monthly snapshots suffice)
python -m backfill.verify_dhan_codes --expiry-type weekly --repo . --trade-date 2026-01-06 --output $S/mapping-weekly.csv
python -m backfill.download --repo . --state $S spot --month 2026-01
python -m backfill.download --repo . --state $S --expiry-type weekly --accept-derived-calendar \
    --mapping-evidence $S/mapping-weekly.csv options --expiry 2026-01-13
python -m backfill.download --repo . --state $S --expiry-type weekly report
```

What differs from the Bank Nifty pipeline:

- Dhan rejects multi-day `fromDate`/`toDate` ranges (DH-905). One-day provider requests that keep a confirmed
  consecutive session are therefore the default (`--no-provider-day-pairs` disables it).
- Calendars are per type. The monthly contract is NSE's: the last expiry whose nominal date falls in the month. A weekly
  date with `monthly=true` is the monthly contract and is stored only in `data/monthly/`.
  `download --expiry-type weekly --expiry <that date>` stops with an error. A weekly pulled into an earlier month by a
  holiday stays weekly (30 Apr 2025).
- `dhan-overrides.csv` (written by `build_calendar`) lists, for monthly contracts that Dhan's MONTH flag does not serve
  as NSE defines them, the flag and relative code per date range. April 2025: 24 Apr via WEEK (codes 5..1), May 2025
  (29 May) via MONTH code 2 until 30 Apr. `python -m backfill.verify_flag_overrides --repo .` re-collects the Dhan
  evidence (`coverage/dhan-flag-evidence.csv`, `coverage/dhan-flag-checks.csv`, about 4 requests a day covered, 4 req/s
  max). 24 Apr is not served by Dhan before 4 Apr 2025, so 27 Mar to 3 Apr are `unavailable` for that monthly.
- Exports of dates not verified against NSE need `--accept-derived-calendar`.
- Mapping evidence is per type (`coverage/rollover-mapping-<type>.csv`, written by `verify_dhan_codes` or
  `verify_mapping`); the ledger keeps the expiry flag with every request. `coverage/dhan-expiry-day-check-<type>.csv`
  is the Dhan-only check that every calendar date really is an expiry day (see NSE-REFERENCE.md).
- Coverage files are per type: `expiry-manifest-<type>.csv`, `option-session-offsets-<type>.csv`,
  `request-results-<type>.csv`, and shared `spot-inventory.csv`, `request-results-spot.csv`.
- Publishing uses `data/<type>/<expiry>` scopes. `run.py` and `resume_expiries.py` push to `origin/main`; they were
  not exercised in the pilot.
