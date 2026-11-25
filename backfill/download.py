"""Download and validate Dhan index option and spot candles with a resumable local ledger.

The underlying (default NIFTY) and the expiry type (monthly or weekly) are parameters, see config.py.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import requests

from . import config
from .ledger import Ledger, OPTION_SCHEMA, SPOT_SCHEMA, DAILY_SCHEMA, atomic_parquet, read_parquet

START = date(2021, 11, 24)
STOP = date(2026, 11, 24)
ANCHOR = date(2021, 10, 28)
IST = ZoneInfo("Asia/Kolkata")
DAY = timedelta(days=1)
# NSE circulars MSD60677 and MSD61893 document these split trading sessions.
RECOVERY_HOURS = ((clock_time(9, 15), clock_time(10)), (clock_time(11, 30), clock_time(12, 30)))
SPECIAL_HOURS = dict.fromkeys(("2024-03-02", "2024-05-18"), RECOVERY_HOURS)
PRICES = ("open", "high", "low", "close")
# Dhan sometimes returns rows of another index (spot 13,9xx or 67,9xx in a NIFTY series) at ATM-10. The option
# `spot` field then differs from the NIFTY candle by far more than this. Real rows differ by less than 0.03%.
FOREIGN_SPOT_TOLERANCE = 0.01
OPTIONAL = ("volume", "oi", "iv", "spot")
RESOLVED = {"returned", "empty", "unsupported", "aged_out"}
SPLIT = {"split", "split_boundary", "split_generic", "split_transport"}
PROVIDER_STOPS = {"authorization", "network", "rate_limit", "provider_unavailable", "interrupted"}


def months():
    return tuple(f"{2021 + (10 + i) // 12:04d}-{(10 + i) % 12 + 1:02d}" for i in range(61))


def month_range(month):
    beginning = date.fromisoformat(month + "-01")
    following = date(beginning.year + (beginning.month == 12), beginning.month % 12 + 1, 1)
    return max(START, beginning), min(STOP, following)


def dates(start, end):
    return (start + i * DAY for i in range((end - start).days))


def windows(start, end, days):
    while start < end:
        following = min(end, start + timedelta(days=days))
        yield start, following
        start = following


def regular(timestamp):
    return clock_time(9, 15) <= timestamp.time() < clock_time(15, 30)


def exchange_minute(timestamp):
    periods = SPECIAL_HOURS.get(timestamp.date().isoformat())
    if periods:
        return any(start <= timestamp.time() < end for start, end in periods)
    return regular(timestamp)


def expected_minute_count(day):
    if day in SPECIAL_HOURS:
        return sum((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
                   for start, end in SPECIAL_HOURS[day])
    return 375


def rows_from_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


@dataclass(frozen=True)
class Expiry:
    current: date
    previous: date
    current_verified: bool
    previous_verified: bool
    source: str
    kind: str = "monthly"   # folder under data/
    flag: str = "MONTH"     # Dhan expiryFlag
    # Per-expiry override from dhan-overrides.csv: ((from, to_exclusive, flag, code), ...). Empty = default mapping.
    segments: tuple = ()

    def code_for(self, day):
        """Relative Dhan code under which this contract is served on `day`.

        Code 1 is the nearest expiry on or after the session (so the contract is code 1 on its own
        expiry day), code 2 the next one. This holds for MONTH (monthly) and WEEK (weekly) alike, where
        `previous` is the previous expiry of the same flag.
        """
        if self.segments:
            return next((code for start, stop, _, code in self.segments if start <= day < stop), None)
        if day == self.previous:
            return 2
        if self.previous < day <= self.current:
            return 1
        return None


def load_overrides(path, kind):
    """{expiry: segments} from dhan-overrides.csv next to the calendar (absent file = no overrides)."""
    if not path.exists():
        return {}
    result = {}
    for row in rows_from_csv(path):
        if row["type"] == kind:
            result.setdefault(date.fromisoformat(row["expiry"]), []).append(
                (date.fromisoformat(row["from_date"]), date.fromisoformat(row["to_date_exclusive"]),
                 row["flag"], int(row["code"])))
    return {key: tuple(sorted(value)) for key, value in result.items()}


def load_calendar(path, kind="monthly"):
    """Fetchable expiries of one type, each linked to its predecessor of the same Dhan flag.

    The first row is the pre-window anchor. For weekly calendars a row with monthly=true is the monthly
    contract: it keeps its place as a predecessor but is never fetched or stored as a weekly.
    """
    etype = config.expiry_type(kind)
    rows = rows_from_csv(path)
    listed = [date.fromisoformat(row["expiry"]) for row in rows]
    if listed != sorted(set(listed)) or listed[0] >= START or any(not START <= x < STOP for x in listed[1:]):
        raise ValueError(f"{path}: expected an ascending pre-window anchor followed by in-window expiries.")
    if kind == "monthly" and tuple(x.isoformat()[:7] for x in listed[1:]) != months():
        raise ValueError("Monthly calendar must include all 61 expiry months in order.")
    def checked(row):
        return row["verified"].lower() == "true" and bool(row["source"])
    overrides = load_overrides(Path(path).with_name("dhan-overrides.csv"), kind)
    if set(overrides) - set(listed[1:]):
        raise ValueError(f"dhan-overrides.csv names expiries absent from {path}.")
    result = {}
    for previous, current, old, new in zip(listed, listed[1:], rows, rows[1:]):
        if kind == "weekly" and new.get("monthly", "").lower() == "true":
            continue  # the monthly contract is stored under data/monthly only
        result[current] = Expiry(current, previous, checked(new), checked(old), new["source"],
                                 etype.name, etype.flag, overrides.get(current, ()))
    return result


class Batch(NamedTuple):
    kind: str
    unit: str
    code: int
    start: str
    end: str
    side: str
    offset: int
    flag: str = ""

    @classmethod
    def make(cls, kind, unit, start, end, code=0, side="", offset=0, flag=""):
        return cls(kind, str(unit), code, str(start), str(end), side, offset, flag)

    def payload(self):
        if self.kind == "options":
            strike = "ATM" if self.offset == 0 else f"ATM{self.offset:+d}"
            return "rollingoption", {
                "exchangeSegment": "NSE_FNO", "interval": "1", "securityId": config.underlying().security_id,
                "instrument": "OPTIDX", "expiryFlag": self.flag, "expiryCode": self.code,
                "strike": strike, "drvOptionType": {"CE": "CALL", "PE": "PUT"}[self.side],
                "requiredData": ["strike", *PRICES, *OPTIONAL], "fromDate": self.start, "toDate": self.end,
            }
        base = {"securityId": str(config.underlying().security_id), "exchangeSegment": config.underlying().segment,
                "instrument": "INDEX", "oi": False}
        if self.kind == "daily":
            return "historical", {**base, "expiryCode": 0, "fromDate": self.start, "toDate": self.end}
        return "intraday", {
            **base, "interval": "1", "fromDate": f"{self.start} 00:00:00",
            "toDate": f"{self.end} 00:00:00",
        }


class ProviderError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def error_outcome(body, http_status, batch):
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("errorCode", body.get("error_code", "")))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code):
        code = ""
    message = str(body.get("errorMessage", body.get("message", ""))).lower()
    reason = f"HTTP {http_status}" + (f", Dhan {code}" if code else "")
    if http_status in {401, 403} or code in {"DH-901", "DH-902"}:
        return ProviderError("authorization", reason)
    if http_status == 429 or code == "DH-904":
        return ProviderError("rate_limit", reason)
    if http_status >= 500 or code == "DH-908":
        return ProviderError("provider_unavailable", reason)
    if ("older than" in message or "outside" in message or "beyond" in message) and (
        "retention" in message or "5 years" in message or "five years" in message
    ):
        return ProviderError("aged_out", f"{reason}, provider stated retention limit")
    if batch.kind == "options" and "strike" in message and (
        "unsupported" in message or "not support" in message or "offset out of range" in message
    ):
        return ProviderError("unsupported", f"{reason}, provider explicitly rejected strike offset")
    if "date" in message and ("range" in message or "days" in message):
        return ProviderError("date_range", reason)
    return ProviderError("rejected", reason)


class Dhan:
    def __init__(self, requests_per_second):
        if not 0 < requests_per_second <= 5:
            raise ValueError("--requests-per-second must be greater than zero and no more than 5.")
        token = os.environ.get("DHAN_ACCESS_TOKEN")
        if not token:
            raise ValueError("DHAN_ACCESS_TOKEN is not bound. Configure it in Vault before a Dhan request.")
        self.token = token
        self.local = threading.local()
        self.interval = 1 / requests_per_second
        self.next_request = 0.0
        self.rate_lock = threading.Lock()
        self.stopped = threading.Event()

    @property
    def http(self):
        if not hasattr(self.local, "http"):
            self.local.http = requests.Session()
            self.local.http.headers.update({"access-token": self.token, "Accept": "application/json"})
        return self.local.http

    def fetch(self, batch):
        endpoint, payload = batch.payload()
        for attempt in range(4):
            with self.rate_lock:
                if self.stopped.wait(max(0, self.next_request - time.monotonic())):
                    raise ProviderError("interrupted", "Dhan request canceled")
                self.next_request = time.monotonic() + self.interval
            retry_after = 0
            try:
                response = self.http.post(f"https://api.dhan.co/v2/charts/{endpoint}", json=payload,
                                          timeout=(15, 45), allow_redirects=False)
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if 200 <= response.status_code < 300 and isinstance(body, dict) and (
                    not body.get("errorCode") and not body.get("error_code") and body.get("status") != "failure"
                ):
                    return body
                error = error_outcome(body, response.status_code, batch)
                retry = response.headers.get("Retry-After", "0")
                try:
                    retry_after = float(retry)
                except ValueError:
                    try:
                        retry_after = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        pass
            except requests.RequestException:
                error = ProviderError("network", "Dhan HTTPS request failed")
            if error.status not in {"network", "rate_limit", "provider_unavailable"} or attempt == 3:
                raise error
            if retry_after > 60:
                raise error
            delay = max(retry_after, 2 ** attempt)
            if error.status == "rate_limit":
                with self.rate_lock:
                    self.next_request = max(self.next_request, time.monotonic() + delay)
            elif self.stopped.wait(delay):
                raise ProviderError("interrupted", "Dhan request canceled")
        raise AssertionError("unreachable")


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("non-finite or non-numeric")
    return float(value)


def option_strike(value):
    price = numeric(value)
    if not price.is_integer() or price <= 0:
        raise ValueError("invalid strike")
    return int(price)


def source_rows(body, batch):
    option = batch.kind == "options"
    if option:
        wrapped = body.get("data")
        side = batch.side.lower()
        if not isinstance(wrapped, dict) or side not in wrapped:
            return [], 1, "missing option side in response"
        body = wrapped[side]
        if body is None:
            return [], 0, "source returned null side"
    if not isinstance(body, dict) or not isinstance(body.get("timestamp"), list):
        return [], 1, "missing timestamp array"
    timestamps = body["timestamp"]
    required = (*PRICES, "strike") if option else PRICES
    optional = OPTIONAL if option else ("volume",)
    columns = {}
    diagnostics = Counter()
    for field in (*required, *optional):
        values = body.get(field)
        if values is None and field in optional:
            values = []
        if not isinstance(values, list):
            diagnostics[f"invalid {field} array"] += 1
            values = []
        if len(values) != len(timestamps) and (field in required or values):
            diagnostics[f"unequal {field} array length"] += 1
        columns[field] = values
    start, stop = date.fromisoformat(batch.start), date.fromisoformat(batch.end)
    rows = []
    clipped_end_rows = 0
    for index, raw_timestamp in enumerate(timestamps):
        try:
            seconds = numeric(raw_timestamp)
            if not seconds.is_integer() or (batch.kind != "daily" and int(seconds) % 60):
                raise ValueError("timestamp is not the start of a source candle")
            timestamp = datetime.fromtimestamp(int(seconds), IST)
            trade_date = timestamp.date()
            if trade_date == stop:
                clipped_end_rows += 1
                continue
            if not (START <= trade_date < STOP and start <= trade_date < stop):
                raise ValueError("out of requested range")
            values = {field: numeric(columns[field][index]) for field in PRICES}
            if not valid_prices(values):
                raise ValueError("invalid OHLC")
            if option:
                values["strike"] = option_strike(columns["strike"][index])
        except (ValueError, TypeError, IndexError, OSError, OverflowError) as exc:
            diagnostics[str(exc)] += 1
            continue
        for field in optional:
            source = columns[field]
            value = source[index] if index < len(source) else None
            try:
                values[field] = numeric(value) if value is not None else None
            except ValueError:
                diagnostics[f"invalid optional {field}"] += 1
                values[field] = None
        rows.append({"timestamp": int(seconds), "trade_date": trade_date.isoformat(), **values})
    details = [f"{key}={count}" for key, count in sorted(diagnostics.items())]
    if clipped_end_rows:
        details.append(f"clipped provider rows on exclusive end date={clipped_end_rows}")
    detail = "; ".join(details)
    return rows, sum(diagnostics.values()), detail


def valid_prices(values):
    return values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]


def option_batches(expiry):
    if expiry.segments:
        # Dhan serves this contract under another flag or code than the default: one batch family per segment.
        # Only code 1 is fetched as a window (it is split to single days on rejection); other codes day by day.
        return [
            Batch.make("options", expiry.current, a, b, code, side, offset, flag)
            for start, stop, flag, code in expiry.segments
            for a, b in (windows(start, stop, 29) if code == 1 else [(d, d + DAY) for d in dates(start, stop) if d.weekday() < 5])
            for side in ("CE", "PE")
            for offset in (0, *range(-1, -11, -1), *range(1, 11))
        ]
    start = max(START, expiry.previous + DAY)
    stop = min(STOP, expiry.current + DAY)
    ranges = [(1, a, b) for a, b in windows(start, stop, 29)]
    if START <= expiry.previous < STOP:
        ranges.append((2, expiry.previous, expiry.previous + DAY))
    return [
        Batch.make("options", expiry.current, a, b, code, side, offset, expiry.flag)
        for code, a, b in ranges
        for side in ("CE", "PE")
        for offset in (0, *range(-1, -11, -1), *range(1, 11))
    ]


def fetch_batch(ledger, client, batch, sessions, provider_boundary=None, pair_provider_days=False):
    previous = ledger.batch(batch)
    if previous and previous["status"] in RESOLVED:
        return
    start, end = date.fromisoformat(batch.start), date.fromisoformat(batch.end)
    if batch.kind == "options" and provider_boundary and start < provider_boundary:
        reason = f"Before known working provider date {provider_boundary}; data availability unresolved"
        if end <= provider_boundary:
            if not previous or previous["status"] in SPLIT:
                ledger.record(batch, "before_provider_probe", reason, [], 0, attempted=False)
            return
        if not previous or previous["status"] != "split_boundary":
            if previous and previous["detail"]:
                reason += f"; earlier range outcome: {previous['detail']}"
            ledger.record(batch, "split_boundary", reason, [], 0, attempted=False)
        fetch_batch(ledger, client, batch._replace(end=provider_boundary.isoformat()),
                    sessions, provider_boundary, pair_provider_days)
        fetch_batch(ledger, client, batch._replace(start=provider_boundary.isoformat()),
                    sessions, provider_boundary, pair_provider_days)
        return
    days = (end - start).days
    if pair_provider_days and batch.kind == "options" and batch.code == 1 and days > 1:
        trading = [day for day in dates(start, end) if day.isoformat() in sessions]
        if not previous or previous["status"] != "split_transport":
            prior = f"; prior range outcome: {previous['detail']}" if previous and previous["detail"] else ""
            ledger.record(batch, "split_transport", f"One-calendar-day provider transport{prior}", [], 0,
                          attempted=False)
        plan_provider_days(ledger, client, batch, sessions, trading)
        return
    generic = batch.kind == "options" and batch.code == 1 and days > 1
    try:
        if previous and previous["status"] == "split":
            raise ProviderError("date_range", previous["detail"])
        if generic and previous and (previous["status"] == "split_generic" or (
            previous["status"] == "rejected" and "HTTP 400, Dhan DH-905" in previous["detail"]
        )):
            raise ProviderError("already_rejected", previous["detail"])
        response = client.fetch(batch)
        rows, malformed, detail = source_rows(response, batch)
        outcome = "malformed" if malformed else "returned" if rows else "empty"
        ledger.record(batch, outcome, detail, rows, malformed)
    except ProviderError as exc:
        if generic and (exc.status == "already_rejected" or (
            exc.status == "rejected" and "HTTP 400, Dhan DH-905" in str(exc)
        )):
            trading = [day for day in dates(start, end) if day.isoformat() in sessions]
            if trading:
                midpoint = (len(trading) + 1) // 2
                if not previous or previous["status"] != "split_generic":
                    ledger.record(batch, "split_generic", str(exc), [], 0,
                                  attempted=exc.status != "already_rejected")
                for portion in (trading[:midpoint], trading[midpoint:]):
                    if portion:
                        child = batch._replace(start=portion[0].isoformat(), end=(portion[-1] + DAY).isoformat())
                        fetch_batch(ledger, client, child, sessions, provider_boundary)
                return
        if exc.status == "date_range" and days > 1:
            if not previous or previous["status"] != "split":
                ledger.record(batch, "split", str(exc), [], 0)
            for day in dates(date.fromisoformat(batch.start), date.fromisoformat(batch.end)):
                if day.isoformat() in sessions or day.weekday() < 5:
                    child = batch._replace(start=day.isoformat(), end=(day + DAY).isoformat())
                    fetch_batch(ledger, client, child, sessions, provider_boundary)
            return
        reused_rejection = exc.status == "already_rejected"
        ledger.record(batch, "rejected" if reused_rejection else exc.status, str(exc), [], 0,
                      attempted=not reused_rejection)
        if exc.status in PROVIDER_STOPS:
            raise


def plan_provider_days(ledger, client, batch, sessions, trading):
    position = 0
    while position < len(trading):
        day = trading[position]
        first = batch._replace(start=day.isoformat(), end=(day + DAY).isoformat())
        if position + 1 < len(trading) and trading[position + 1] == day + DAY:
            second = first._replace(start=first.end, end=(day + 2 * DAY).isoformat())
            fetch_provider_pair(ledger, client, first, second, sessions)
            position += 2
        else:
            fetch_batch(ledger, client, first, sessions)
            position += 1


def fetch_provider_pair(ledger, client, first, second, sessions):
    children = (first, second)
    existing = [ledger.batch(batch) for batch in children]
    if any(row and row["status"] in RESOLVED for row in existing):
        for batch in children:
            fetch_batch(ledger, client, batch, sessions)
        return
    detail = f"One-day wire request {first.start} to {first.end} included expected session {second.start}"
    try:
        body = client.fetch(first)
    except ProviderError as exc:
        ledger.record(first, exc.status, str(exc), [], 0)
        if exc.status in PROVIDER_STOPS:
            raise
        fetch_batch(ledger, client, second, sessions)
        return
    rows, malformed, notes = source_rows(body, first._replace(end=second.end))
    detail += f"; {notes}" if notes else ""
    for position, batch in enumerate(children):
        selected = [row for row in rows if row["trade_date"] == batch.start]
        status = "malformed" if malformed else "returned" if selected else "missing_paired_day"
        ledger.record(batch, status, detail, selected, malformed, attempted=position == 0)
        if not selected or malformed:
            fetch_batch(ledger, client, batch, sessions)


def source_sessions(path, ledger):
    days = {day: "Dhan daily spot" for day in ledger.session_days()}
    if path and path.exists():
        for row in rows_from_csv(path):
            if row["verified"].lower() == "true" and row["source"]:
                day = date.fromisoformat(row["trade_date"])
                if START <= day < STOP:
                    days[day.isoformat()] = row["source"]
    return days


def read_mapping(path, calendar, allow_derived=False):
    """One independently checked outgoing/incoming pairing supports the relative mapping."""
    if not path:
        return {}
    matches = {}
    for row in rows_from_csv(path):
        day = date.fromisoformat(row["trade_date"])
        target = date.fromisoformat(row["expiry"])
        code = int(row["expiry_code"])
        expiry = calendar[target]
        if not row["source"] or (not allow_derived and not (expiry.current_verified and expiry.previous_verified)):
            raise ValueError("Mapping evidence must cite a source and use verified calendar dates "
                             "(or pass --accept-derived-calendar).")
        if expiry.code_for(day) != code or (code == 1 and target != day):
            raise ValueError("Mapping evidence must compare both relative codes on an expiry date.")
        matches.setdefault(day, {})[code] = row["source"]
    paired = {day.isoformat(): value for day, value in matches.items() if set(value) == {1, 2}}
    if not paired:
        raise ValueError("Mapping evidence must contain an outgoing and incoming pair on the same date.")
    return paired


def spot_stamps(ledger, sessions):
    result = {}
    for day, stamps in ledger.spot_timestamps().items():
        if day in sessions:
            regular_stamps = {ts for ts in stamps if regular(datetime.fromtimestamp(ts, IST))}
            result[day] = regular_stamps or set(stamps)
    return result


def outside_evidenced_option_session(stamp, day, timestamps, regular_spot_days):
    if day in SPECIAL_HOURS:
        return not exchange_minute(stamp)
    if day in regular_spot_days and not regular(stamp):
        return stamp.timestamp() not in timestamps[day]
    return False


def foreign_spot(row, closes):
    close = closes.get(row["timestamp"])
    return bool(close and row["spot"] is not None and abs(row["spot"] - close) / close > FOREIGN_SPOT_TOLERANCE)


def option_row_allowed(ledger, row, expiry, mapping, timestamps, sessions, reference_cache, evidence,
                       allow_derived=False, closes=None):
    if row["baseline"]:
        return True
    if closes and foreign_spot(row, closes):
        return False
    day = row["trade_date"]
    calendar_ok = allow_derived or (expiry.current_verified and expiry.previous_verified)
    if not mapping or not calendar_ok or day not in sessions:
        return False
    stamp = datetime.fromtimestamp(row["timestamp"], IST)
    if day in SPECIAL_HOURS and not exchange_minute(stamp):
        return False
    if row["timestamp"] in timestamps.get(day, ()):
        return True
    if sessions[day] == "Dhan daily spot" or not exchange_minute(stamp):
        return False
    observed = timestamps.get(day)
    if observed and not min(observed) <= row["timestamp"] <= max(observed):
        return False
    if day not in evidence:
        from .nse_reference import contract_listings
        trade_date = date.fromisoformat(day)
        try:
            source, contracts = contract_listings(trade_date, reference_cache)
            listed_expiry = expiry.current
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            evidence[day] = set()
            print(f"NSE contract evidence unavailable for {day} ({type(exc).__name__}).", file=sys.stderr)
        else:
            evidence[day] = {(strike, side) for listed, strike, side in contracts if listed == listed_expiry}
            ledger.record_evidence(day, source)
    return (row["strike"], row["side"]) in evidence[day]


def export_options(ledger, repo, expiry, mapping, sessions, reference_cache, allow_derived=False):
    target = repo / "data" / expiry.kind / expiry.current.isoformat()
    stamps = spot_stamps(ledger, sessions)
    closes = ledger.spot_closes()
    evidence = {}
    results = {}
    for strike, side in ledger.contracts(str(expiry.current)):
        rows = []
        for row in ledger.contract_rows(str(expiry.current), strike, side):
            if option_row_allowed(ledger, row, expiry, mapping, stamps, sessions, reference_cache, evidence,
                                  allow_derived, closes):
                data = {key: row[key] for key in OPTION_SCHEMA.names}
                data["expiry"] = expiry.current
                data["trade_date"] = date.fromisoformat(data["trade_date"])
                data["timestamp"] = datetime.fromtimestamp(data["timestamp"], IST)
                rows.append(data)
        if rows:
            path = target / config.option_name(expiry.current, strike, side)
            atomic_parquet(path, rows, OPTION_SCHEMA)
            ledger.remember_file(path)
            results[path.name] = len(rows)
    # The ledger is the authority: drop files for contracts that no longer export any row (for example after a
    # sanity filter was tightened) so a rerun never leaves stale Parquets behind.
    for stale in target.glob("*.parquet") if target.is_dir() else ():
        if stale.name not in results:
            stale.unlink()
            print(f"Removed stale {stale.name}", flush=True)
    return results


def export_spot(ledger, spot_dir, month, sessions):
    if month == "daily":
        path = spot_dir / f"{config.underlying().symbol}_SPOT_DAILY.parquet"
        rows = [dict(row) for row in ledger.daily_rows()]
        schema = DAILY_SCHEMA
    else:
        path = spot_dir / f"{config.underlying().symbol}_SPOT_{month}.parquet"
        groups = {}
        for row in ledger.spot_rows(month):
            if row["trade_date"] in sessions:
                groups.setdefault(row["trade_date"], []).append(dict(row))
        rows = []
        for observations in groups.values():
            for row in observations:
                row["timestamp"] = datetime.fromtimestamp(row["timestamp"], IST)
            rows.extend([row for row in observations if regular(row["timestamp"])] or observations)
        schema = SPOT_SCHEMA
    for row in rows:
        row["trade_date"] = date.fromisoformat(row["trade_date"])
    if rows:
        atomic_parquet(path, rows, schema)
        ledger.remember_file(path)
    return len(rows)


def import_existing(ledger, repo, spot_dir):
    symbol = config.underlying().symbol
    for path in sorted(spot_dir.glob(f"{symbol}_SPOT_*.parquet")):
        match = config.spot_file_pattern().fullmatch(path.name)
        if path.name == f"{symbol}_SPOT_DAILY.parquet":
            kind, schema = "daily", DAILY_SCHEMA
        elif match and match[1] in months():
            kind, schema = "spot", SPOT_SCHEMA
        else:
            continue
        if not ledger.file_seen(path):
            ledger.seed(path, kind, read_parquet(path, schema))
    for path in sorted((repo / "data").glob("*/*/*.parquet")):
        match = config.file_option_pattern().fullmatch(path.name)
        if not match or match[1] != path.parent.name or path.parent.parent.name not in config.EXPIRY_TYPES:
            raise ValueError(f"Invalid existing option path: {path}")
        if not ledger.file_seen(path):
            ledger.seed(path, "options", read_parquet(path, OPTION_SCHEMA),
                        (match[1], int(match[2]), match[3]))


def run_spot(args, ledger, client, spot_dir, sessions_path):
    work = (args.month,) if args.month else months()
    all_sessions = source_sessions(sessions_path, ledger)
    daily_path = spot_dir / f"{config.underlying().symbol}_SPOT_DAILY.parquet"
    if not daily_path.exists() or args.refresh_daily:
        for start, end in windows(START, STOP, 365):
            fetch_batch(ledger, client, Batch.make("daily", "daily", start, end), all_sessions)
        export_spot(ledger, spot_dir, "daily", all_sessions)
    all_sessions = source_sessions(sessions_path, ledger)
    for month in work:
        if month not in months():
            raise ValueError("Spot month outside the frozen window.")
        start, end = month_range(month)
        stamps = spot_stamps(ledger, all_sessions)
        expected = {day.isoformat() for day in dates(start, end) if day.isoformat() in all_sessions}
        if not expected or any(len(stamps.get(day, ())) < expected_minute_count(day) for day in expected):
            fetch_batch(ledger, client, Batch.make("spot", month, start, end), all_sessions)
        count = export_spot(ledger, spot_dir, month, all_sessions)
        print(f"Spot {month} · {count:,} saved rows", flush=True)


def run_options(args, ledger, client, repo, calendar, mapping, sessions, reference_cache):
    if date.fromisoformat(args.expiry) not in calendar:
        raise ValueError(f"{args.expiry} is not a fetchable {args.expiry_type} expiry. A weekly-calendar date flagged "
                         "monthly=true is stored only under data/monthly: fetch it with --expiry-type monthly.")
    selected = date.fromisoformat(args.expiry)
    expiry = calendar[selected]
    work = option_batches(expiry)
    def fetch(current_ledger, batch):
        fetch_batch(current_ledger, client, batch, sessions, args.first_provider_date, args.provider_day_pairs)
        return current_ledger.batch(batch)

    def isolated_fetch(batch):
        worker_ledger = Ledger(args.state.resolve())
        try:
            return fetch(worker_ledger, batch)
        finally:
            worker_ledger.close()

    def update(position, batch, result):
        print(f"Options {expiry.current} · {position}/{len(work)} planned batches · "
              f"code {batch.code} {batch.side} {batch.offset:+d} · "
              f"{result['status']} · {result['rows']:,} latest source rows", flush=True)

    try:
        if args.workers == 1:
            for position, batch in enumerate(work, 1):
                update(position, batch, fetch(ledger, batch))
        else:
            with ThreadPoolExecutor(args.workers) as pool:
                pending = {pool.submit(isolated_fetch, batch): batch for batch in work}
                try:
                    for position, future in enumerate(as_completed(pending), 1):
                        update(position, pending[future], future.result())
                except BaseException:
                    client.stopped.set()
                    for future in pending:
                        future.cancel()
                    raise
    finally:
        output = export_options(ledger, repo, expiry, mapping, sessions, reference_cache, args.accept_derived_calendar)
        print(f"Options {expiry.kind} {expiry.current} · {sum(output.values()):,} saved rows · {len(output)} contract files",
              flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path, help="Persistent directory outside the checkout.")
    parser.add_argument("--spot-dir", type=Path, help="Defaults to <repo>/spot.")
    parser.add_argument("--underlying", default="NIFTY", choices=sorted(config.UNDERLYINGS))
    parser.add_argument("--expiry-type", default="monthly", choices=sorted(config.EXPIRY_TYPES),
                        help="monthly uses expiryFlag=MONTH, weekly uses expiryFlag=WEEK.")
    parser.add_argument("--accept-derived-calendar", action="store_true",
                        help="Export rows for expiries whose date is derived, not verified against NSE.")
    parser.add_argument("--calendar", type=Path, help="Defaults to <repo>/backfill/expiries-<expiry-type>.csv.")
    parser.add_argument("--sessions", type=Path, help="Defaults to <repo>/backfill/sessions.csv when present.")
    parser.add_argument("--mapping-evidence", type=Path,
                        help="CSV with trade_date,expiry,expiry_code,source for a checked rollover pair.")
    parser.add_argument("--requests-per-second", type=float, default=2)
    commands = parser.add_subparsers(dest="command", required=True)
    spot = commands.add_parser("spot")
    spot.add_argument("--month")
    spot.add_argument("--refresh-daily", action="store_true")
    options = commands.add_parser("options")
    options.add_argument("--expiry", required=True)
    options.add_argument("--workers", type=int, choices=range(1, 5), default=1,
                         help="Parallel streams sharing one Dhan pacing limit")
    options.add_argument("--first-provider-date", type=date.fromisoformat,
                         help="Skip earlier windows as unresolved and fetch the tail of crossing windows.")
    options.add_argument("--no-provider-day-pairs", dest="provider_day_pairs", action="store_false",
                         help="Disable the default one-day requests that also keep a confirmed consecutive "
                              "session. Dhan rejects multi-day ranges (DH-905) for NIFTY.")
    export = commands.add_parser("export-options", help="Export saved observations without a Dhan request.")
    export.add_argument("--expiry", required=True)
    commands.add_parser("status")
    commands.add_parser("report")
    return parser.parse_args()


def main():
    from .reporting import report

    args = parse_args()
    repo, state = args.repo.resolve(), args.state.resolve()
    if state == repo or repo in state.parents:
        raise ValueError("State must be outside the Git checkout.")
    spot_dir = (args.spot_dir or repo / "spot").resolve()
    sessions_path = args.sessions or repo / "backfill" / "sessions.csv"
    config.select(args.underlying)
    args.calendar = args.calendar or repo / "backfill" / config.expiry_type(args.expiry_type).calendar
    calendar = load_calendar(args.calendar, args.expiry_type)
    mapping = read_mapping(args.mapping_evidence, calendar, args.accept_derived_calendar)
    ledger = Ledger(state)
    try:
        import_existing(ledger, repo, spot_dir)
        flag = config.expiry_type(args.expiry_type).flag
        if mapping:
            ledger.record_mapping(mapping, flag)
        mapping_is_verified = bool(ledger.mapping_sources(flag))
        if args.command == "spot":
            run_spot(args, ledger, Dhan(args.requests_per_second), spot_dir, sessions_path)
        if args.command == "options":
            run_options(args, ledger, Dhan(args.requests_per_second), repo, calendar, mapping_is_verified,
                        source_sessions(sessions_path, ledger), state / "nse")
        if args.command == "export-options":
            selected = calendar[date.fromisoformat(args.expiry)]
            output = export_options(ledger, repo, selected, mapping_is_verified,
                                    source_sessions(sessions_path, ledger), state / "nse",
                                    args.accept_derived_calendar)
            print(f"Options {selected.current} · {sum(output.values()):,} saved rows · {len(output)} contract files")
    finally:
        report(ledger, repo, spot_dir, calendar, source_sessions(sessions_path, ledger),
               write=args.command == "report", kind=args.expiry_type)
        ledger.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, sqlite3.Error, ProviderError) as exc:
        print(f"Backfill stopped: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
