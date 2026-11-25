"""Upgrade derived expiry dates to verified using NSE F&O bhavcopies placed in --cache-dir.

Any bhavcopy counts, whatever day it is for: a snapshot lists every outstanding contract, so one file per month is
enough. A calendar date E is verified when it appears as a NIFTY option expiry (OPTIDX in old files, IDO in UDiFF
files) in a supplied file dated on or before E. A date flagged monthly (and every date in expiries-monthly.csv) must
also appear as a NIFTY index future expiry (FUTIDX / IDF) in such a file, which is NSE's own monthly series.

The run aborts, writing nothing, when a supplied file contradicts the calendar:
  * it lists a NIFTY option expiry inside the window [2021-11-24, 2026-11-24) that is not a calendar date,
  * it lists a NIFTY index future expiry inside the window that is not a monthly date,
  * a date the calendar flags as weekly-only is a future expiry (so it is really the month's monthly).
Dates no file confirms yet stay `derived`. --list-missing prints those dates (and the daily file that would settle
each) without writing, so a few top-up files can be requested.

Both file-name styles are accepted: fo05JAN2023bhav.csv.zip and BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from . import config
from .nse_reference import bhavcopy_name_date, bhavcopy_url, read_listing

ROOT = Path(__file__).resolve().parent
START, STOP = date(2021, 11, 24), date(2026, 11, 24)


def load(cache_dir):
    listings = []
    for path in sorted(cache_dir.iterdir()) if cache_dir.is_dir() else ():
        if bhavcopy_name_date(path.name) is not None:
            listings.append((*read_listing(path), path.name))
    return sorted(listings, key=lambda item: (item[0], item[3]))


def read_calendar(path):
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    return rows, list(rows[0])


def check(listings, weekly_rows, monthly_rows):
    weekly = {date.fromisoformat(r["expiry"]) for r in weekly_rows}
    monthly = {date.fromisoformat(r["expiry"]) for r in monthly_rows}
    weekly_only = {date.fromisoformat(r["expiry"]) for r in weekly_rows if r["monthly"].lower() != "true"}
    for trade_date, options, futures, name in listings:
        unknown = sorted(x for x in options if START <= x < STOP and x not in weekly)
        if unknown:
            raise ValueError(f"{name} lists NIFTY option expiries not in the calendar: "
                             f"{', '.join(map(str, unknown))}. A derived date is wrong.")
        unknown = sorted(x for x in futures if START <= x < STOP and x not in monthly)
        if unknown:
            raise ValueError(f"{name} lists NIFTY future expiries that are not monthly dates in the calendar: "
                             f"{', '.join(map(str, unknown))}.")
        wrong = sorted(x for x in futures if x in weekly_only)
        if wrong:
            raise ValueError(f"{name}: {', '.join(map(str, wrong))} is a future expiry (NSE monthly) but the calendar "
                             f"files it as weekly only.")


def verify(rows, listings, monthly_kind):
    """Set verified/source/status on rows confirmed by a file; return (changed, still_derived_dates)."""
    changed, missing = 0, []
    for row in rows:
        day = date.fromisoformat(row["expiry"])
        needs_future = monthly_kind or row.get("monthly", "").lower() == "true"
        option_source = next((name for d, options, _, name in listings if d <= day and day in options), None)
        future_ok = not needs_future or any(d <= day and day in futures for d, _, futures, _ in listings)
        if option_source is None or not future_ok:
            if row["verified"].lower() != "true" and day >= START:  # the pre-window anchor cannot be fetched
                missing.append(day)
            continue
        if row["verified"].lower() != "true":
            row.update(source=option_source, verified="true", status="verified")
            changed += 1
    return changed, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY", choices=sorted(config.UNDERLYINGS))
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--calendar-dir", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--list-missing", action="store_true")
    args = parser.parse_args()
    config.select(args.underlying)
    try:
        listings = load(args.cache_dir)
        weekly_rows, weekly_fields = read_calendar(args.calendar_dir / "expiries-weekly.csv")
        monthly_rows, monthly_fields = read_calendar(args.calendar_dir / "expiries-monthly.csv")
        check(listings, weekly_rows, monthly_rows)
    except (OSError, ValueError) as exc:
        print(f"Verification stopped, no calendar written: {exc}", file=sys.stderr)
        return 1
    results = {}
    for name, rows, fields, monthly_kind in (("expiries-weekly.csv", weekly_rows, weekly_fields, False),
                                             ("expiries-monthly.csv", monthly_rows, monthly_fields, True)):
        results[name] = (rows, fields, *verify(rows, listings, monthly_kind))
    print(f"{len(listings)} bhavcopy files read")
    for name, (rows, fields, changed, missing) in results.items():
        print(f"{name}: {changed} dates newly verified, {len(missing)} still derived")
        if args.list_missing:
            for day in missing:
                print(f"  {day}  needs a file dated on or before it that lists it, e.g. {bhavcopy_url(day)}")
            continue
        with (args.calendar_dir / name).open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
