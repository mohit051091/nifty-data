"""Upgrade derived expiry dates to verified using NSE F&O bhavcopies placed in --cache-dir.

A date E is verified when the bhavcopy of session E lists option contracts of the selected underlying that expire
on E and traded that day. A weekly-calendar date flagged monthly=true must also be the month's index future expiry.
Dates whose file is absent stay derived. A file that contradicts the calendar stops the run: nothing is rewritten.

Files are those named in bhavcopies-needed.csv, saved under their original names, for example
`BhavCopy_NSE_FO_0_0_0_20260106_F_0000.csv.zip` or `fo06JAN2022bhav.csv.zip`.
Use --list-missing to print the URLs still needed.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from . import config
from .nse_reference import bhavcopy_url, expiry_of, read_bhavcopy, traded_option_expiries

ROOT = Path(__file__).resolve().parent


def monthly_future_expiries(day, cache_dir):
    source, rows = read_bhavcopy(day, cache_dir)
    return {expiry_of(row) for row in rows if row.get("INSTRUMENT") == "FUTIDX" or row.get("FinInstrmTp") == "IDF"}


def check(path, cache_dir):
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    fields = list(rows[0])
    present, missing, changed = 0, [], 0
    for row in rows:
        day = date.fromisoformat(row["expiry"])
        if not (cache_dir / bhavcopy_url(day).rsplit("/", 1)[-1]).exists():
            missing.append(bhavcopy_url(day))
            continue
        present += 1
        source, expiries = traded_option_expiries(day, cache_dir)
        if day not in expiries:
            raise ValueError(f"{day}: no {config.underlying().symbol} option expiring {day} traded in {source}. "
                             f"The derived date is wrong or the file is for another day.")
        if row.get("monthly", "").lower() == "true" and day not in monthly_future_expiries(day, cache_dir):
            raise ValueError(f"{day}: calendar says monthly but the index future does not expire that day ({source})")
        if row["verified"].lower() != "true":
            row.update(source=source, verified="true", status="verified")
            changed += 1
    return rows, fields, present, missing, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY", choices=sorted(config.UNDERLYINGS))
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--list-missing", action="store_true")
    args = parser.parse_args()
    config.select(args.underlying)
    results = {}
    try:
        for name in ("expiries-weekly.csv", "expiries-monthly.csv"):
            results[name] = check(ROOT / name, args.cache_dir)
    except (OSError, ValueError) as exc:
        print(f"Verification stopped, no calendar written: {exc}", file=sys.stderr)
        return 1
    for name, (rows, fields, present, missing, changed) in results.items():
        print(f"{name}: {present} bhavcopies found, {changed} dates newly verified, {len(missing)} still derived")
        if args.list_missing:
            print("\n".join(missing))
            continue
        with (ROOT / name).open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
