"""Build the NIFTY monthly and weekly expiry calendars, rollover tables and the list of NSE files needed to verify them.

Rules (derived from NSE circulars, not looked up date by date):

* Weekly expiry is Thursday until 31 Aug 2025 and Tuesday from 1 Sep 2025.
* Monthly expiry is the last weekly expiry of the calendar month, taken by its actual date, so it is the last
  Thursday (to Aug 2025) and the last Tuesday (from Sep 2025). There is no separate monthly contract date.
  This is also how Dhan's MONTH flag behaves. April 2025 is the one month where it differs from NSE's series: the
  weekly of 1 May 2025 (a holiday) moved to 30 Apr, after NSE's 24 Apr monthly. Dhan serves 30 Apr as MONTH code 1
  (checked: on 22, 24 and 25 Apr 2025 MONTH code 1 returns the same rows as WEEK code 2 or 1 of 30 Apr), so 30 Apr is the
  monthly here and 24 Apr is stored as a weekly.
* If the nominal day is not a regular trading session, the expiry moves to the previous regular session.

Every date is written with verified=false and status=derived. `verify_expiries.py` flips a date to verified only when
the NSE F&O bhavcopy for that expiry day shows contracts expiring on it. Nothing here queries NSE.

Trading days come from sessions.csv (the same exchange calendar as the Bank Nifty repository). Sessions that are not
full regular days cannot host an expiry: the Saturday special sessions of 2 Mar and 18 May 2024 and the Muhurat
evening sessions. 24 Nov 2026 is not in sessions.csv simply because the window ends on 23 Nov. It is a closed day
(no Dhan candles, and the Bank Nifty repository's NSE-verified monthly expiry for Nov 2026 is Monday 23 Nov), so it
is passed in CLOSED_BEYOND_SESSIONS rather than guessed.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path

from .nse_reference import bhavcopy_url

ROOT = Path(__file__).resolve().parent
START, STOP = date(2021, 11, 24), date(2026, 11, 24)
WEEKLY_ANCHOR, MONTHLY_ANCHOR = date(2021, 11, 18), date(2021, 10, 28)
TUESDAY_FROM = date(2025, 9, 1)
NON_REGULAR = {date(2022, 10, 24), date(2023, 11, 12), date(2024, 3, 2), date(2024, 5, 18),
               date(2024, 11, 1), date(2025, 10, 21)}
CLOSED_BEYOND_SESSIONS = {date(2026, 11, 24)}
DAY = timedelta(days=1)


def regular_sessions():
    with (ROOT / "sessions.csv").open(newline="", encoding="utf-8") as source:
        days = {date.fromisoformat(row["trade_date"]) for row in csv.DictReader(source)}
    return days - NON_REGULAR


def nominal_weeklies():
    day = WEEKLY_ANCHOR
    while day < STOP + 7 * DAY:
        weekday = 3 if day < TUESDAY_FROM else 1
        if day.weekday() == weekday:
            yield day
        day += DAY


def build():
    sessions = regular_sessions()
    last_known = max(sessions)
    weekly = []
    for nominal in nominal_weeklies():
        if nominal >= STOP and nominal not in CLOSED_BEYOND_SESSIONS:
            continue
        actual, basis = nominal, ""
        if nominal != WEEKLY_ANCHOR and (nominal not in sessions or nominal > last_known):
            if nominal > last_known and nominal not in CLOSED_BEYOND_SESSIONS:
                raise ValueError(f"{nominal} is beyond sessions.csv and not declared closed")
            actual = nominal - DAY
            while actual not in sessions:
                actual -= DAY
            basis = f"holiday shift from {nominal}"
        if not basis:
            basis = f"{'Thursday' if nominal < TUESDAY_FROM else 'Tuesday'} weekly rule"
        if actual < STOP:
            weekly.append((actual, basis, nominal))
    # Monthly: the last weekly expiry, by actual date, in each calendar month.
    last_by_month = {}
    for actual, _, _ in weekly:
        last_by_month[actual.year, actual.month] = actual
    monthly_dates = set(last_by_month.values())
    return weekly, monthly_dates


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    weekly, monthly_dates = build()

    def row(day, basis, **extra):
        return {"expiry": day.isoformat(), **extra, "source": "", "verified": "false", "status": "derived",
                "basis": basis, "bhavcopy_url": bhavcopy_url(day)}

    weekly_rows = []
    for day, basis, _ in weekly:
        weekly_rows.append(row(day, basis + ("; last weekly of the month, also the monthly contract"
                                             if day in monthly_dates else ""),
                               monthly=str(day in monthly_dates).lower()))
    monthly_rows = [row(MONTHLY_ANCHOR, "anchor before the window (last Thursday of Oct 2021)")]
    monthly_rows += [row(day, "last weekly expiry of the month") for day, _, _ in weekly if day in monthly_dates
                     and day >= START]
    weekly_rows[0]["basis"] = "anchor before the window; " + weekly_rows[0]["basis"]
    weekly_rows[0]["monthly"] = "false"

    out = args.output_dir
    write_csv(out / "expiries-weekly.csv", ["expiry", "monthly", "source", "verified", "status", "basis",
                                           "bhavcopy_url"], weekly_rows)
    write_csv(out / "expiries-monthly.csv", ["expiry", "source", "verified", "status", "basis", "bhavcopy_url"],
              monthly_rows)

    # Rollover tables: day P (an expiry day) and the incoming expiry E, between two contracts of the same type.
    flags = {r["expiry"]: r["monthly"] == "true" for r in weekly_rows}
    fields = ["trade_date", "incoming_expiry", "incoming_as_listed", "source"]
    weekly_roll, monthly_roll = [], []
    ordered = [r["expiry"] for r in weekly_rows]
    for p, e in zip(ordered, ordered[1:]):
        if not flags[p] and not flags[e]:
            weekly_roll.append({"trade_date": p, "incoming_expiry": e, "incoming_as_listed": e,
                                "source": bhavcopy_url(date.fromisoformat(p))})
    months = [r["expiry"] for r in monthly_rows]
    for p, e in zip(months, months[1:]):
        monthly_roll.append({"trade_date": p, "incoming_expiry": e, "incoming_as_listed": e,
                             "source": bhavcopy_url(date.fromisoformat(p))})
    write_csv(out / "rollovers-weekly.csv", fields, weekly_roll)
    write_csv(out / "rollovers-monthly.csv", fields, monthly_roll)

    needed = [{"trade_date": r["expiry"], "url": r["bhavcopy_url"],
               "serves": "calendar check; rollover check" + ("; monthly" if flags[r["expiry"]] else "; weekly")}
              for r in weekly_rows]
    write_csv(out / "bhavcopies-needed.csv", ["trade_date", "url", "serves"], needed)
    print(f"{len(weekly_rows)-1} weekly dates in the window ({sum(flags.values())} of them monthly), "
          f"{len(monthly_rows)-1} monthly dates, {len(needed)} NSE files needed")


if __name__ == "__main__":
    main()
