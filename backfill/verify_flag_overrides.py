"""Dhan-only evidence for the per-expiry flag/code overrides in dhan-overrides.csv (NSE monthly vs Dhan MONTH series).

NSE's monthly is the last expiry whose nominal date is in the month. Dhan's expiryFlag=MONTH instead serves the last
expiry by actual date, so for April 2025 it serves 30 Apr and has no 24 Apr contract. For every trading day covered
by an override segment this fetches (CE, ATM, one day each, at most 4 requests a second, about 4 requests per day):

* the override's own (flag, code) series,         the next slot of the WEEK series (or MONTH code 2),
* MONTH code 1,                                    and, where useful, WEEK code 1.

and writes `coverage/dhan-flag-evidence.csv` (one fingerprint per request: strike, minutes, first and last close, last
open interest) and `coverage/dhan-flag-checks.csv` (one verdict per segment day). The identity of a contract is read
from the series structure, not guessed:

* WEEK override (24 Apr): the segment slot must differ from MONTH code 1 (the 30 Apr contract), the next WEEK slot must be
  identical to MONTH code 1, and it must be cheaper than that slot (earlier expiry). On the expiry day itself WEEK
  code 1 must have decayed to nothing while MONTH code 1 has not (so MONTH never serves it). An empty slot is reported
  `unavailable`: Dhan does not serve the contract that early (24 Apr appears in the WEEK series from 4 Apr 2025).
* MONTH override (29 May, code 2 until 30 Apr): MONTH code 1 must be identical to the weekly slot of the 30 Apr expiry
  (WEEK code 1 on 25-30 Apr), MONTH code 2 must differ and be dearer. On 29 May MONTH code 1 must have decayed.

Exit status 1 on any failed check, 2 if Dhan blocks the token.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from . import config
from .download import DAY, Dhan, ProviderError, load_overrides, rows_from_csv
from .verify_dhan_codes import day_rows

DECAYED = 1.0   # index points: close of an ATM call at the end of its own expiry day


def fingerprint(rows):
    if not rows:
        return None
    return (rows[-1]["strike"], len(rows), rows[0]["close"], rows[-1]["close"], rows[-1]["oi"])


class Probe:
    def __init__(self, client, evidence):
        self.client, self.evidence, self.cache = client, evidence, {}

    def get(self, expiry, day, flag, code, role):
        key = (day, flag, code)
        if key not in self.cache:
            rows = day_rows(self.client, flag, code, day, "CE")
            self.cache[key] = fingerprint(rows)
            fp = self.cache[key] or ("", "", "", "", "")
            self.evidence.append({"expiry": expiry, "trade_date": day, "flag": flag, "code": code, "role": role,
                                  "strike": fp[0], "minutes": fp[1], "first_close": fp[2], "last_close": fp[3],
                                  "last_oi": fp[4]})
        return self.cache[key]


def check_day(probe, expiry, day, flag, code, weekly, dhan_month):
    """Return (result, detail) for one segment day."""
    exp = str(expiry)
    own = probe.get(exp, day, flag, code, "override_series")
    month1 = probe.get(exp, day, "MONTH", 1, "month_code_1")
    if flag == "WEEK":
        if day == expiry:
            if own is None or month1 is None:
                return "fail", "no rows on the expiry day"
            ok = own[3] <= DECAYED and month1 != own and month1[3] > DECAYED
            return ("pass" if ok else "fail"), (f"WEEK code 1 closes {own[3]} (expired), MONTH code 1 is another contract "
                                                f"closing {month1[3]}")
        if own is None:
            return "unavailable", f"Dhan WEEK code {code} returns no rows: the contract is not served that early"
        following = probe.get(exp, day, "WEEK", code + 1, "next_week_slot")
        ok = following == month1 and own != month1 and own[2] < month1[2]
        return ("pass" if ok else "fail"), (f"WEEK code {code} first close {own[2]} sits before WEEK code {code + 1} "
                                            f"which is identical to MONTH code 1 ({'same' if following == month1 else 'different'}"
                                            f", first close {month1[2] if month1 else None})")
    # MONTH flag
    if code == 1:
        if day != expiry:
            return ("pass" if own is not None else "fail"), "MONTH code 1 present"
        return ("pass" if own and own[3] <= DECAYED else "fail"), f"MONTH code 1 closes {own and own[3]} on its expiry day"
    earlier = min((d for d in dhan_month if d >= day), default=None)
    slot = 1 + sum(1 for d in weekly if day <= d < earlier)
    reference = probe.get(exp, day, "WEEK", slot, "weekly_slot_of_dhan_month_date")
    ok = own is not None and month1 == reference and own != month1 and own[2] > month1[2]
    return ("pass" if ok else "fail"), (f"MONTH code 1 {'equals' if month1 == reference else 'differs from'} WEEK code "
                                        f"{slot} (the {earlier} expiry); MONTH code 2 first close {own and own[2]} is "
                                        f"{'dearer' if own and month1 and own[2] > month1[2] else 'not dearer'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY", choices=sorted(config.UNDERLYINGS))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--requests-per-second", type=float, default=4)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    config.select(args.underlying)
    if args.requests_per_second > 4:
        parser.error("at most 4 requests per second")
    base = args.repo / "backfill"
    out = args.output_dir or args.repo / "coverage"
    sessions = {date.fromisoformat(r["trade_date"]) for r in rows_from_csv(base / "sessions.csv")}
    overrides = load_overrides(base / "dhan-overrides.csv", "monthly")
    weekly = [date.fromisoformat(r["expiry"]) for r in rows_from_csv(base / "expiries-weekly.csv")]
    # Dhan's MONTH series: the last weekly-calendar expiry by actual date in each calendar month.
    last = {}
    for d in weekly:
        last[d.year, d.month] = d
    dhan_month = sorted(last.values())
    evidence, checks = [], []
    probe = Probe(Dhan(args.requests_per_second), evidence)
    try:
        for expiry, segments in sorted(overrides.items()):
            for start, stop, flag, code in segments:
                for day in sorted(d for d in sessions if start <= d < stop):
                    result, detail = check_day(probe, expiry, day, flag, code, weekly, dhan_month)
                    checks.append({"expiry": expiry.isoformat(), "trade_date": day.isoformat(), "flag": flag,
                                   "code": code, "result": result, "detail": detail})
                    print(checks[-1], flush=True)
    except ProviderError as error:
        print(f"Dhan request blocked ({error.status}): {error}", file=sys.stderr)
        return 2
    finally:
        out.mkdir(parents=True, exist_ok=True)
        for name, rows in (("dhan-flag-evidence.csv", evidence), ("dhan-flag-checks.csv", checks)):
            if rows:
                with (out / name).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, list(rows[0]), lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
    failed = [c for c in checks if c["result"] == "fail"]
    print(f"{len(checks)} segment days checked: {sum(c['result'] == 'pass' for c in checks)} pass, "
          f"{sum(c['result'] == 'unavailable' for c in checks)} unavailable, {len(failed)} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
