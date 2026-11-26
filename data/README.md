# NIFTY 50 options data dictionary

This dataset contains available 1-minute candles for NSE NIFTY 50 monthly and weekly options from the Dhan Data API. The data files are Parquet with ZSTD compression and live in Git LFS.

## File layout

```text
data/
  README.md
  monthly/
    2025-04-24/
      NIFTY_2025-04-24_23300_CE.parquet
      NIFTY_2025-04-24_23300_PE.parquet
  weekly/
    2025-04-30/
      NIFTY_2025-04-30_23350_CE.parquet
      NIFTY_2025-04-30_23350_PE.parquet
```

`monthly/` holds 61 expiry folders (5,788 files, 20,094,841 rows). `weekly/` holds 201 expiry folders (13,359 files, 17,892,021 rows). Each expiry date is in exactly one of the two folders.

- **`monthly/`** is NSE's monthly contract: the last expiry whose nominal date (the scheduled Thursday, or Tuesday from September 2025, before any holiday shift) falls in the month.
- **`weekly/`** is every other expiry. The last weekly of a month is the monthly contract and is not repeated here. A weekly that a holiday pulls back into an earlier month stays a weekly. In April 2025 the monthly is `monthly/2025-04-24` and the following weekly is `weekly/2025-04-30`.

Each file represents one contract: expiry date, fixed strike price, and CE or PE. Rows are ordered by candle timestamp. Use `trade_date` or `timestamp` to select a day or time range inside the file.

The folder and the date in the filename identify the **contract expiry**. The date in `trade_date` identifies the **session when the candle occurred**. The outgoing and incoming contracts both contribute available candles on rollover day, each under its own expiry. The incoming contract includes that one earlier trading session, followed by sessions in which it is the nearest contract of its type through its expiry. A weekly folder therefore holds about a week of trading and a monthly folder about a month. The first folders start later or earlier than that for known reasons: the window opens on 24 November 2021, and the 24 April 2025 monthly is available from Dhan only from 4 April 2025.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `expiry` | `date32` | Contract's NSE expiry date, in `YYYY-MM-DD` format. |
| `strike` | `int64` | Actual option strike price in NIFTY index points. |
| `side` | `string` | `CE` for call or `PE` for put. |
| `timestamp` | `timestamp[ms, tz=Asia/Kolkata]` | Start time of the one-minute candle, in India Standard Time. |
| `trade_date` | `date32` | Session date derived from `timestamp` in India Standard Time. |
| `open` | `float64` | Opening option premium for the minute, in INR. |
| `high` | `float64` | Highest option premium for the minute, in INR. |
| `low` | `float64` | Lowest option premium for the minute, in INR. |
| `close` | `float64` | Closing option premium for the minute, in INR. |
| `volume` | `float64` | Traded volume for the minute in the units returned by Dhan. |
| `oi` | `float64` | Open interest from Dhan for that candle, in the source units. |
| `iv` | `float64` | Implied volatility from Dhan as returned by the API. The exporter applies no scale conversion. |
| `spot` | `float64` | NIFTY 50 spot index value accompanying the option candle, in index points. |

The last four columns are optional in the schema and would be null if Dhan omitted them, but none of the files spot-checked has nulls.

## Coverage

Dhan returns historical candles by ATM offset (-10 to +10 for both types). The exporter stores each candle under the actual strike returned by Dhan. A file may have missing minutes or sessions due to the source strike range, a contract listing date, or a lack of trades. Missing candles are left absent. The row key is `expiry + strike + side + timestamp`. Where Dhan returned the same key under two ATM offsets with different values, both observations were quarantined and the minute is absent. Rows whose `spot` field differs from the NIFTY spot of the same minute by more than 1 % (rows of another index) and rows outside the exchange session are excluded. Read the [coverage summary](../coverage/README.md), then the per-expiry [monthly](../coverage/expiry-manifest-monthly.csv) and [weekly](../coverage/expiry-manifest-weekly.csv) manifests, for labels, quarantine counts and gaps.

Most expiry dates are derived from NSE's rules and not yet NSE-verified. `calendar_verified` in the manifests tells which. See [HANDOVER.md](../HANDOVER.md).

The downloader keeps its SQLite checkpoint (see [checkpoint](../checkpoint/README.md)) in a state directory outside the checkout. See the [backfill instructions](../backfill/README.md) for the current command and checkpoint location.

Source: [DhanHQ Expired Options Data API](https://dhanhq.co/docs/v2/expired-options-data/).
