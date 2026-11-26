# Instructions for AI assistants

Before answering questions about this repository or analysing its data, read these files in order:

1. [README.md](README.md): what the data is, how to retrieve it with Git LFS, the monthly versus weekly rule, and a tested research quick start.
2. [HANDOVER.md](HANDOVER.md): why the data looks the way it does, what is missing and why, and how to resume the backfill.
3. [coverage/README.md](coverage/README.md) and the manifests [coverage/expiry-manifest-monthly.csv](coverage/expiry-manifest-monthly.csv) and [coverage/expiry-manifest-weekly.csv](coverage/expiry-manifest-weekly.csv): how reliable each expiry is. Check the manifest before relying on any expiry.

Rules:

- The data files are in Git LFS. If a Parquet is a small text pointer starting with `version https://git-lfs`, run `git lfs pull`. Never treat a pointer as data.
- The window is 24 Nov 2021 to 23 Nov 2026, in Asia/Kolkata. The underlying is NIFTY 50. Monthly and weekly options are separate: the last weekly of a month is the monthly contract and lives only under `data/monthly/`.
- Missing minutes are absent rows. Do not fill them with guessed prices. Quarantined conflicts are deliberately left out.
- Most expiry dates are `derived`, not NSE-verified. See `calendar_verified` in the manifests and [HANDOVER.md](HANDOVER.md). Never infer expiry dates from a weekday rule: use `backfill/expiries-monthly.csv` and `backfill/expiries-weekly.csv`.
- Do not force-push, rewrite history or commit secrets such as the Dhan token. Do not edit Parquets or the coverage CSVs by hand.
- `checkpoint/` holds an 861 MB database and is excluded from a normal `git lfs pull`. Fetch it only to audit quarantined rows or resume the backfill. See [checkpoint/README.md](checkpoint/README.md).
