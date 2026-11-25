"""Build the NIFTY monthly and weekly expiry calendars, rollover tables and the list of NSE files needed to verify them.

Rules (derived from NSE circulars, not looked up date by date):

* Weekly expiry is Thursday until 31 Aug 2025 and Tuesday from 1 Sep 2025.
* Monthly expiry follows NSE's definition: it is the last expiry whose NOMINAL date (the scheduled Thursday or
  Tuesday, before any holiday shift) falls in the calendar month. A weekly pulled back into a month by a holiday stays
  a weekly. The only month where this differs from "last expiry by actual date" is April 2025: the weekly nominally
  due 1 May was shifted to Wednesday 30 Apr, so the April monthly is 24 Apr and 30 Apr is a weekly.
* Dhan's expiryFlag=MONTH series does NOT follow that rule: it serves 30 Apr 2025 as the April "monthly" (last
  expiry by actual date) and has no 24 Apr contract. dhan-overrides.csv (written here; evidence in
  coverage/dhan-flag-evidence.csv) therefore fetches 24 Apr via expiryFlag=WEEK and May 2025 (29 May) via MONTH
  code 2 until 30 Apr. 30 Apr is fetched as a normal weekly (WEEK). The overrides are computed, not typed: Dhan's
  MONTH series is `dhan_month_dates` and its WEEK series is every weekly date.
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
    # Dhan MONTH series: the last expiry by actual date in each calendar month. NSE monthlies are chosen below.
    last_by_month = {}
    for actual, _, _ in weekly:
        last_by_month[actual.year, actual.month] = actual
    dhan_month_dates = set(last_by_month.values())
    last_by_nominal = {}
    for actual, _, nominal in weekly:
        last_by_nominal[nominal.year, nominal.month] = actual
    return weekly, set(last_by_nominal.values()), dhan_month_dates


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


OVERRIDE_FIELDS = ["expiry", "type", "flag", "from_date", "to_date_exclusive", "code", "reason"]


def series_segments(start, stop, series, target):
    """(from, to_exclusive, code) pieces of [start, stop) where code = 1 + number of series dates in [day, target)."""
    cuts = sorted({start, stop} | {d + DAY for d in series if start <= d < stop})
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        if a < stop:
            pieces.append((a, min(b, stop), 1 + sum(1 for d in series if a <= d < target)))
    return pieces


def write_overrides(path, weekly_dates, monthly_dates, dhan_month_dates):
    """Per-expiry Dhan flag/code segments for monthly contracts that the default MONTH mapping gets wrong.

    Default: contract T with previous monthly P is MONTH code 2 on P and MONTH code 1 on (P, T]. That holds only while
    Dhan's MONTH series equals the NSE monthly series. Where it does not, each monthly T is fetched from the flag whose
    series contains it: MONTH if T is a Dhan MONTH date, else WEEK (every weekly date is in the WEEK series). The code
    on day d is 1 + the number of that series' dates in [d, T).
    """
    weekly = [date.fromisoformat(x) for x in weekly_dates]
    months = [date.fromisoformat(x) for x in monthly_dates]
    dhan_month = sorted(dhan_month_dates | {d for d in months if d < START})
    rows = []
    for previous, target in zip(months, months[1:]):
        flag, series = ("MONTH", dhan_month) if target in dhan_month_dates else ("WEEK", weekly)
        pieces = series_segments(previous, target + DAY, [d for d in series], target)
        default = [(previous, previous + DAY, 2), (previous + DAY, target + DAY, 1)]
        if flag == "MONTH" and pieces == default:
            continue
        reason = ("NSE monthly missing from Dhan's MONTH series; served by the WEEK series" if flag == "WEEK" else
                  "Dhan MONTH series holds an extra expiry inside the window, so the contract stays code 2 longer")
        rows += [{"expiry": target.isoformat(), "type": "monthly", "flag": flag, "from_date": a.isoformat(),
                  "to_date_exclusive": b.isoformat(), "code": code, "reason": reason} for a, b, code in pieces]
    write_csv(path, OVERRIDE_FIELDS, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    weekly, monthly_dates, dhan_month_dates = build()

    def row(day, basis, **extra):
        return {"expiry": day.isoformat(), **extra, "source": "", "verified": "false", "status": "derived",
                "basis": basis, "bhavcopy_url": bhavcopy_url(day)}

    weekly_rows = []
    for day, basis, _ in weekly:
        weekly_rows.append(row(day, basis + ("; NSE monthly (last nominal expiry of the month), also the monthly contract"
                                             if day in monthly_dates else ""),
                               monthly=str(day in monthly_dates).lower()))
    monthly_rows = [row(MONTHLY_ANCHOR, "anchor before the window (last Thursday of Oct 2021)")]
    monthly_rows += [row(day, "NSE monthly: last expiry whose nominal date falls in the month"
                         + ("" if day in dhan_month_dates else "; Dhan MONTH flag serves another date, "
                            "fetched via dhan-overrides.csv"))
                     for day, _, _ in weekly if day in monthly_dates and day >= START]
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

    write_overrides(out / "dhan-overrides.csv", [r["expiry"] for r in weekly_rows], months, dhan_month_dates)
    # One snapshot bhavcopy a month lists every outstanding contract, so it verifies every date expiring after it.
    first_session = {}
    for day in sorted(regular_sessions()):
        first_session.setdefault((day.year, day.month), day)
    snapshots = [date(2021, 11, 15)] + [first_session[y, m] for y, m in
                                        sorted(k for k in first_session if k > (2021, 11))]
    needed = [{"trade_date": d.isoformat(), "url": bhavcopy_url(d),
               "serves": "monthly snapshot: verifies every calendar date expiring on or after it"} for d in snapshots]
    write_csv(out / "bhavcopies-needed.csv", ["trade_date", "url", "serves"], needed)
    print(f"{len(weekly_rows)-1} weekly dates in the window ({sum(flags.values())} of them monthly), "
          f"{len(monthly_rows)-1} monthly dates, {len(needed)} monthly snapshot files needed")


if __name__ == "__main__":
    main()
