# NIFTY 50 spot index data

DhanHQ provides the NSE NIFTY 50 index under security ID 13 (segment `IDX_I`). The dataset separates daily index observations from one-minute candles. Files use Zstandard-compressed Parquet.

`NIFTY_SPOT_DAILY.parquet` contains the daily source series, 1,241 sessions from 2021-11-24 to 2026-11-23. `NIFTY_SPOT_YYYY-MM.parquet` contains the available one-minute candles for that calendar month in India. There are 61 monthly files, 463,250 candles in total.

| Column | Arrow type | Definition |
| --- | --- | --- |
| `trade_date` | `date32` | Trading date in Asia/Kolkata. |
| `timestamp` | `timestamp[ms, tz=Asia/Kolkata]` | Start of the one-minute candle in Asia/Kolkata. Present in minute files only. |
| `open` | `float64` | Opening index level for the interval, in index points. |
| `high` | `float64` | Highest index level for the interval, in index points. |
| `low` | `float64` | Lowest index level for the interval, in index points. |
| `close` | `float64` | Closing index level for the interval, in index points. |
| `volume` | `float64` | Minute value returned by Dhan. Minute files only. This is a spot index, so it does not represent traded volume. The daily file has no `volume` column. |

Minute files keep source observations from 09:15 through 15:29 IST on dates present in the daily series. Missing minutes are left absent. Daily values come directly from Dhan's daily endpoint. A minute whose OHLC values were inconsistent after a retry was excluded, not repaired: 11 minutes in 7 months (2022-03, 2022-04 and 2024-04 to 2024-08), which are labelled `unresolved`.

Special sessions have far fewer minutes: Muhurat and Saturday sessions such as 24 Oct 2022, 12 Nov 2023, 2 Mar 2024, 18 May 2024 and 1 Nov 2024 are present, and 21 Oct 2025 and 8 Nov 2026 record only 60 and 70 candles. These months are `source_limited`.

See the [spot inventory](../coverage/spot-inventory.csv) for observed sessions, missing minutes and status (42 `full_requested`, 12 `source_limited`, 7 `unresolved`), and the [coverage summary](../coverage/README.md). All published spot Parquets use Git LFS.

Source: [DhanHQ Historical Data](https://dhanhq.co/docs/v2/historical-data/).
