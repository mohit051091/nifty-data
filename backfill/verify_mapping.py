"""Verify Dhan's relative expiry codes (monthly or weekly) against NSE rollover opens.

Needs the NSE F&O bhavcopy of each rollover day in --cache-dir. See verify_dhan_codes.py for the
Dhan-only check used while those files cannot be downloaded."""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, time
from math import isclose
from pathlib import Path

from . import config
from .download import Batch, DAY, Dhan, IST, ProviderError, rows_from_csv, source_rows
from .nse_reference import contract_candles


ROOT = Path(__file__).resolve().parent
OFFSETS = (0, -1, 1, -2, 2, -3, 3)
SIDES = ('CE', 'PE')
OPEN = time(9, 15)
MIN_MATCHES_PER_SIDE = 2
OPTIONAL_PROVIDER_ERRORS = {'aged_out', 'unsupported', 'rejected', 'date_range'}


def check_openings(openings, exchange, expected):
    """Count unique exchange expiry matches and flag any contradictory prices."""
    daily_opens = defaultdict(dict)
    for (expiry, strike, side), ohlc in exchange.items():
        daily_opens[strike, side][expiry] = ohlc[0]

    matched = Counter()
    contradictions = 0
    for (strike, side), prices in openings.items():
        if len(prices) != 1:
            contradictions += 1
            continue
        price, = prices
        candidates = {expiry for expiry, daily_open in daily_opens[strike, side].items()
                      if isclose(price, daily_open, rel_tol=0, abs_tol=0.005)}
        if candidates == {expected}:
            matched[side] += 1
        elif len(candidates) == 1:
            contradictions += 1
    return matched, contradictions


def collect_openings(client, trade_date, expiry, code, flag):
    openings = defaultdict(set)
    for side in SIDES:
        for offset in OFFSETS:
            batch = Batch.make('options', expiry, trade_date, trade_date + DAY, code, side, offset, flag)
            try:
                payload = client.fetch(batch)
            except ProviderError as error:
                if error.status not in OPTIONAL_PROVIDER_ERRORS:
                    raise
                print(f'{trade_date} code {code} {side} offset {offset:+d}: {error.status}', file=sys.stderr)
                continue
            rows, diagnostics, _ = source_rows(payload, batch)
            if diagnostics:
                print(f'{trade_date} code {code} {side} offset {offset:+d}: '
                      f'{diagnostics} response diagnostics', file=sys.stderr)
            for row in rows:
                timestamp = datetime.fromtimestamp(row['timestamp'], IST)
                if timestamp.date() == trade_date and timestamp.time() == OPEN:
                    openings[row['strike'], side].add(row['open'])
    return openings


def mapped_rollover(client, rollover, cache_dir, flag):
    trade_date = date.fromisoformat(rollover['trade_date'])
    incoming = date.fromisoformat(rollover['incoming_expiry'])
    incoming_as_listed = date.fromisoformat(rollover['incoming_as_listed'])
    source, exchange = contract_candles(trade_date, cache_dir)
    verified = []
    for code, expiry, as_listed in ((1, trade_date, trade_date), (2, incoming, incoming_as_listed)):
        openings = collect_openings(client, trade_date, expiry, code, flag)
        matches, contradictions = check_openings(openings, exchange, as_listed)
        print(f'{trade_date} code {code} → {expiry}: '
              f'{matches["CE"]} CE, {matches["PE"]} PE unique NSE openings, '
              f'{contradictions} contradictions', file=sys.stderr)
        if not contradictions and all(matches[side] >= MIN_MATCHES_PER_SIDE for side in SIDES):
            verified.append((trade_date, expiry, code, source))
    return verified if len(verified) == 2 else []


def save_mapping(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                     dir=path.parent, delete=False) as output:
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(('trade_date', 'expiry', 'expiry_code', 'source'))
        writer.writerows(rows)
    Path(output.name).replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--underlying', default='NIFTY', choices=sorted(config.UNDERLYINGS))
    parser.add_argument('--expiry-type', default='monthly', choices=sorted(config.EXPIRY_TYPES))
    parser.add_argument('--rollovers', type=Path, help='Defaults to backfill/rollovers-<expiry-type>.csv')
    parser.add_argument('--trade-date', type=date.fromisoformat, action='append',
                        help='Probe a specific rollover date; defaults to up to three recent rollovers')
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--requests-per-second', type=float, default=2)
    args = parser.parse_args()

    try:
        config.select(args.underlying)
        flag = config.expiry_type(args.expiry_type).flag
        path = args.rollovers or ROOT / f'rollovers-{args.expiry_type}.csv'
        rollovers = {date.fromisoformat(row['trade_date']): row for row in rows_from_csv(path)}
        trade_dates = args.trade_date or sorted(rollovers, reverse=True)[:3]
        if not trade_dates or any(day not in rollovers for day in trade_dates):
            parser.error('Each trade date must be a known rollover in the NSE reference.')
        client = Dhan(args.requests_per_second)
        for day in trade_dates:
            verified = mapped_rollover(client, rollovers[day], args.cache_dir, flag)
            if verified:
                save_mapping(args.output, verified)
                return 0
    except ProviderError as error:
        print(f'Dhan request blocked ({error.status}): {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f'Mapping verification could not start or complete ({type(error).__name__}): {error}',
              file=sys.stderr)
        return 1

    print('Dhan rollover mapping remains unverified. Both codes require corroborated CE and PE opens.',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
