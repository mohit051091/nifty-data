"""Dhan-only evidence for the relative expiry codes, used while NSE bhavcopies cannot be downloaded.

For a rollover day D (an expiry day of the chosen type) with incoming expiry E and next session D', it checks
with real Dhan responses that

* code 1 on D is the contract expiring that day: its ATM straddle has almost no time value at the last candle,
* code 2 on D is a live contract: the straddle keeps clearly more time value than code 1,
* code 1 on D' is that same live contract: the strike seen last under code 2 on D opens on D' within 50% of its
  previous close, and much nearer to it than to the (near zero or intrinsic) close of the expired contract.
  Open interest is reported for information only: it moves too much at the open to discriminate.

When all hold on both sides it writes the two-row evidence CSV that `download --mapping-evidence` accepts. This
is weaker than the NSE open-price match in verify_mapping.py (it never names the expiry date from an exchange
source), so every row's `source` says "dhan-internal" and the calendar must be accepted as derived.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from . import config
from .download import Batch, DAY, Dhan, IST, ProviderError, load_calendar, regular, rows_from_csv, source_rows
from .build_calendar import CLOSED_BEYOND_SESSIONS, NON_REGULAR
from .verify_mapping import save_mapping

EXPIRING_MAX_TV = 0.004    # straddle time value / spot at the last candle of the expiry day
LIVE_MIN_TV = 0.004
LIVE_VS_EXPIRING = 4.0
MAX_OVERNIGHT_GAP = 0.5
OFFSETS = (0, 1, -1, 2, -2, 3, -3, 4, -4)


def day_rows(client, flag, code, day, side, offset=0, expiry="probe"):
    batch = Batch.make("options", expiry, day, day + DAY, code, side, offset, flag)
    rows, malformed, _ = source_rows(client.fetch(batch), batch)
    return [r for r in rows if regular(datetime.fromtimestamp(r["timestamp"], IST)) and r["trade_date"] == str(day)]


def straddle_tv(client, flag, code, day):
    """Time value of the ATM straddle at the day's last regular candle, as a fraction of spot, and the legs."""
    legs = {}
    for side in ("CE", "PE"):
        rows = day_rows(client, flag, code, day, side)
        if not rows:
            return None
        legs[side] = rows[-1]
    ce, pe = legs["CE"], legs["PE"]
    if ce["strike"] != pe["strike"] or ce["timestamp"] != pe["timestamp"] or not ce["spot"]:
        return None
    intrinsic = abs(ce["spot"] - ce["strike"])
    return (ce["close"] + pe["close"] - intrinsic) / ce["spot"], legs


def next_open(client, flag, day_next, strike, side):
    """The 09:15 candle of `strike` under code 1 on the next session, found by scanning ATM offsets."""
    for offset in OFFSETS:
        rows = day_rows(client, flag, 1, day_next, side, offset)
        if rows and rows[0]["strike"] == strike:
            return rows[0]
    return None


def check(client, flag, day, next_session):
    expiring, expiring_legs = straddle_tv(client, flag, 1, day) or (None, None)
    live, live_legs = straddle_tv(client, flag, 2, day) or (None, None)
    if expiring is None or live is None:
        return False, "straddle data missing"
    notes = [f"{day} straddle time value {expiring:.4%} of spot under code 1, {live:.4%} under code 2"]
    ok = expiring <= EXPIRING_MAX_TV and live >= LIVE_MIN_TV and live >= LIVE_VS_EXPIRING * max(expiring, 1e-6)
    for side in ("CE", "PE"):
        leg = live_legs[side]
        row = next_open(client, flag, next_session, leg["strike"], side)
        if row is None:
            return False, f"{side} strike {leg['strike']} not found under code 1 on {next_session}"
        gap_live = abs(row["open"] - leg["close"])
        gap_expired = abs(row["open"] - expiring_legs[side]["close"])
        notes.append(f"{side} {leg['strike']} opens {row['open']} on {next_session} under code 1 against a "
                     f"{leg['close']} close under code 2 and {expiring_legs[side]['close']} for the expired contract "
                     f"(OI {row['oi']:.0f} vs {leg['oi']:.0f} and {expiring_legs[side]['oi']:.0f})")
        ok = ok and gap_live <= MAX_OVERNIGHT_GAP * leg["close"] and gap_live < gap_expired
    return ok, "; ".join(notes)


def open_straddle_tv(client, flag, day):
    """Time value of the ATM straddle under code 1 at the first candle of `day`, as a fraction of spot."""
    legs = {}
    for side in ("CE", "PE"):
        rows = day_rows(client, flag, 1, day, side)
        if not rows:
            return None
        legs[side] = rows[0]
    ce, pe = legs["CE"], legs["PE"]
    if ce["strike"] != pe["strike"] or not ce["spot"]:
        return None
    return (ce["close"] + pe["close"] - abs(ce["spot"] - ce["strike"])) / ce["spot"]


def expiry_day_check(client, flag, day, next_session):
    """Does code 1 expire on `day`? Its straddle has decayed by the close, and a fresh contract opens next session."""
    close = straddle_tv(client, flag, 1, day)
    opened = open_straddle_tv(client, flag, next_session)
    if close is None or opened is None:
        return "no_data", close[0] if close else None, opened
    tv = close[0]
    ok = tv <= EXPIRING_MAX_TV and opened >= LIVE_MIN_TV and opened >= LIVE_VS_EXPIRING * max(tv, 1e-6)
    return ("pass" if ok else "fail"), tv, opened


def next_regular_session(sessions, day):
    """Next full trading day. Muhurat and Saturday special sessions have no 09:15 candle, so they are skipped."""
    for candidate in sessions:
        if candidate > day and candidate not in NON_REGULAR:
            return candidate
    candidate = day + DAY  # beyond the last listed session: first weekday not known to be closed
    while candidate.weekday() >= 5 or candidate in CLOSED_BEYOND_SESSIONS:
        candidate += DAY
    return candidate


def run_expiry_days(args, calendar, sessions, flag):
    import csv
    client = Dhan(args.requests_per_second)
    rows = []
    for expiry in calendar.values():
        day = expiry.current
        following = next_regular_session(sessions, day)
        result, tv, opened = ("no_next_session", None, None) if following is None else expiry_day_check(
            client, flag, day, following)
        rows.append({"expiry": day.isoformat(), "flag": flag, "result": result,
                     "expiry_day_close_tv_pct": "" if tv is None else f"{tv:.4%}",
                     "next_open_tv_pct": "" if opened is None else f"{opened:.4%}"})
        print(rows[-1], flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    return 0 if all(r["result"] == "pass" for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY", choices=sorted(config.UNDERLYINGS))
    parser.add_argument("--expiry-type", required=True, choices=sorted(config.EXPIRY_TYPES))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--trade-date", type=date.fromisoformat, action="append", default=[],
                        help="Rollover day: an expiry day whose incoming expiry is also fetchable.")
    parser.add_argument("--expiry-days", action="store_true",
                        help="Instead of rollovers, check every fetchable expiry of the type decays on its own day "
                             "(about four requests each) and write a pass/fail CSV to --output.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests-per-second", type=float, default=2)
    args = parser.parse_args()
    config.select(args.underlying)
    flag = config.expiry_type(args.expiry_type).flag
    calendar = load_calendar(args.repo / "backfill" / config.expiry_type(args.expiry_type).calendar,
                             args.expiry_type)
    sessions = sorted(date.fromisoformat(r["trade_date"]) for r in rows_from_csv(args.repo / "backfill/sessions.csv"))
    if args.expiry_days:
        return run_expiry_days(args, calendar, sessions, flag)
    if not args.trade_date:
        parser.error("--trade-date is required unless --expiry-days is given")
    client = Dhan(args.requests_per_second)
    out = []
    try:
        for day in args.trade_date:
            incoming = next((e for e in calendar.values() if e.previous == day), None)
            if day not in calendar or incoming is None:
                print(f"{day}: not a rollover between two fetchable {args.expiry_type} expiries", file=sys.stderr)
                continue
            following = next_regular_session(sessions, day)
            ok, notes = check(client, flag, day, following)
            print(f"{day} -> {incoming.current}: {'PASS' if ok else 'FAIL'}: {notes}", file=sys.stderr)
            if ok:
                source = f"dhan-internal {flag}: {notes}"
                out += [(day, day, 1, source), (day, incoming.current, 2, source)]
    except ProviderError as error:
        print(f"Dhan request blocked ({error.status}): {error}", file=sys.stderr)
        return 1
    if out:
        save_mapping(args.output, out)
        return 0
    print("No rollover passed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
