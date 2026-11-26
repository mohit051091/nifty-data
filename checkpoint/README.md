# Backfill checkpoint

`candles.sqlite.xz` is the xz-compressed SQLite checkpoint (a `VACUUM INTO` copy of the ledger) from the NIFTY 50 backfill of 24 November 2021 to 23 November 2026. It is 860,593,116 bytes compressed and 4,855,238,656 bytes (4.9 GB) decompressed, with SHA-256 `3750eee7156a5b261f3ae2c2307df49777cd75edd2e11aabf5eff5cf084c2113`.

It holds every option observation Dhan returned, 38,129,254 rows in the `options` table (19,411 flagged `quarantined=1`; the published Parquets hold 37,986,862 rows), 38,652,857 `observations` rows, 115,357 request batches, 504,156 one-minute spot rows, 1,241 daily spot rows and the 19,633 recorded conflicts. Rows excluded by the foreign-index filter or outside the option session are not in the Parquets.

You only need it to audit the quarantined conflicts or to resume the backfill. For research, use the Parquets in [data](../data/README.md) and [spot](../spot/README.md). The published minute count of spot (463,250) is lower than the 504,156 spot rows the ledger holds. That difference was not reconciled, so rely on the Parquets.

The repository's [.lfsconfig](../.lfsconfig) skips this file on a normal `git lfs pull`. To fetch it:

```sh
git lfs pull --include="checkpoint/*" --exclude=""
mkdir -p ../nifty-state
xz -dc checkpoint/candles.sqlite.xz > ../nifty-state/candles.sqlite
```

In SQLite, the `options` table has a `quarantined` column. Pass `../nifty-state` as `BACKFILL_STATE` to resume. See [HANDOVER.md](../HANDOVER.md) and the [backfill instructions](../backfill/README.md).
