"""NSE F&O bhavcopy readers for the selected underlying (see config.py): listings, daily OHLC and expiries."""

import csv
import re
import math
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO, TextIOWrapper
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

from . import config


ARCHIVES = 'https://nsearchives.nseindia.com'
UDIFF_START = date(2024, 7, 8)
DAY = timedelta(days=1)
MONTH_ABBREVIATIONS = 'JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split()


def nse_date(value: str) -> date:
    """Parse an archive date without relying on the machine's month locale."""
    if value[4:5] == '-':
        return date.fromisoformat(value)
    day, month, year = value.upper().split('-')
    return date(int(year), MONTH_ABBREVIATIONS.index(month) + 1, int(day))


def bhavcopy_url(trade_date: date) -> str:
    if trade_date >= UDIFF_START:
        return f'{ARCHIVES}/content/fo/BhavCopy_NSE_FO_0_0_0_{trade_date:%Y%m%d}_F_0000.csv.zip'

    month = MONTH_ABBREVIATIONS[trade_date.month - 1]
    filename = f'fo{trade_date.day:02d}{month}{trade_date.year}bhav.csv.zip'
    return f'{ARCHIVES}/content/historical/DERIVATIVES/{trade_date.year}/{month}/{filename}'


def read_bhavcopy(trade_date: date, cache_dir: Path):
    source = bhavcopy_url(trade_date)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / source.rsplit('/', 1)[-1]
    if cached.exists():
        payload = cached.read_bytes()
    else:
        with urlopen(source, timeout=30) as response:
            payload = response.read()

    modern = trade_date >= UDIFF_START
    symbol_field = 'TckrSymb' if modern else 'SYMBOL'
    date_field = 'TradDt' if modern else 'TIMESTAMP'
    with ZipFile(BytesIO(payload)) as archive:
        files = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if len(files) != 1:
            raise ValueError(f'Expected one CSV in {source}')
        with archive.open(files[0]) as member:
            rows = [
                row for row in csv.DictReader(TextIOWrapper(member, encoding='utf-8-sig'))
                if row[symbol_field] == config.underlying().symbol
            ]
    if not rows or any(nse_date(row[date_field]) != trade_date for row in rows):
        raise ValueError(f'Invalid {config.underlying().symbol} trade date or empty archive at {source}')

    if not cached.exists():
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as temporary:
            temporary.write(payload)
        Path(temporary.name).replace(cached)

    return source, rows


def expiry_of(row: dict) -> date:
    if 'XpryDt' in row:
        return nse_date(row['FininstrmActlXpryDt'] or row['XpryDt'])
    return nse_date(row['EXPIRY_DT'])


def option_key(row: dict):
    if row.get('FinInstrmTp') == 'IDO':
        raw_strike, side = row['StrkPric'], row['OptnTp']
    elif row.get('INSTRUMENT') == 'OPTIDX':
        raw_strike, side = row['STRIKE_PR'], row['OPTION_TYP']
    else:
        return None

    strike = Decimal(raw_strike)
    if not strike.is_finite() or strike <= 0 or strike != int(strike) or side not in {'CE', 'PE'}:
        raise ValueError(f'Unexpected option strike or side: {raw_strike}, {side}')
    return expiry_of(row), int(strike), side


def contract_listings(trade_date: date, cache_dir: Path) -> tuple[str, set[tuple[date, int, str]]]:
    """Return exchange-listed expiry, strike and side keys from that session's archive."""
    source, rows = read_bhavcopy(trade_date, cache_dir)
    return source, {key for row in rows if (key := option_key(row)) is not None}


def contract_candles(trade_date: date, cache_dir: Path) -> tuple[str, dict]:
    """Return official daily OHLC for option keys with a traded contract that session."""
    source, rows = read_bhavcopy(trade_date, cache_dir)
    modern = trade_date >= UDIFF_START
    volume_field = 'TtlTradgVol' if modern else 'CONTRACTS'
    fields = ('OpnPric', 'HghPric', 'LwPric', 'ClsPric') if modern else ('OPEN', 'HIGH', 'LOW', 'CLOSE')
    candles = {}
    for row in rows:
        key = option_key(row)
        if key is None or not row[volume_field] or Decimal(row[volume_field]) <= 0:
            continue
        ohlc = tuple(float(row[field]) for field in fields)
        if not all(math.isfinite(price) for price in ohlc):
            raise ValueError(f'Invalid daily OHLC for {key} in {source}')
        if key in candles and candles[key] != ohlc:
            raise ValueError(f'Conflicting daily OHLC for {key} in {source}')
        candles[key] = ohlc
    return source, candles


def traded_option_expiries(trade_date: date, cache_dir: Path) -> tuple[str, set[date]]:
    """Option expiries that traded on the session, from the NSE F&O bhavcopy."""
    source, candles = contract_candles(trade_date, cache_dir)
    return source, {expiry for expiry, _, _ in candles}


_OLD_NAME = re.compile(r'^fo(\d{2})([A-Za-z]{3})(\d{4})bhav\.csv(?:\.zip)?$', re.IGNORECASE)
_UDIFF_NAME = re.compile(r'^BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv(?:\.zip)?$', re.IGNORECASE)


def bhavcopy_name_date(name: str):
    """Trade date encoded in an NSE F&O bhavcopy file name (old `fo05JAN2023bhav.csv.zip` or UDiFF), else None."""
    match = _OLD_NAME.match(name)
    if match:
        try:
            return date(int(match[3]), MONTH_ABBREVIATIONS.index(match[2].upper()) + 1, int(match[1]))
        except ValueError:
            return None
    match = _UDIFF_NAME.match(name)
    if match:
        try:
            return date(int(match[1][:4]), int(match[1][4:6]), int(match[1][6:]))
        except ValueError:
            return None
    return None


def read_listing(path: Path):
    """Outstanding contracts of the selected underlying in any F&O bhavcopy file.

    Returns (trade_date, option_expiries, future_expiries). The trade date comes from the file name and every row's own
    date must agree. Both formats are read by their column names: old files (INSTRUMENT OPTIDX/FUTIDX, SYMBOL,
    EXPIRY_DT, TIMESTAMP) and UDiFF files (FinInstrmTp IDO/IDF, TckrSymb, XpryDt, TradDt). Only the exact symbol counts,
    so NIFTYNXT50 and the like are ignored.
    """
    trade_date = bhavcopy_name_date(path.name)
    if trade_date is None:
        raise ValueError(f'{path.name} is not a recognised NSE F&O bhavcopy file name')
    symbol = config.underlying().symbol
    options, futures, seen = set(), set(), 0
    with ZipFile(path) as archive:
        files = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if len(files) != 1:
            raise ValueError(f'Expected one CSV in {path.name}')
        with archive.open(files[0]) as member:
            for row in csv.DictReader(TextIOWrapper(member, encoding='utf-8-sig')):
                if (row.get('TckrSymb') or row.get('SYMBOL') or '').strip() != symbol:
                    continue
                stamp = row.get('TradDt') or row.get('TIMESTAMP')
                if stamp is None or nse_date(stamp.strip()) != trade_date:
                    raise ValueError(f'{path.name}: row trade date {stamp!r} differs from the file date {trade_date}')
                kind = (row.get('FinInstrmTp') or row.get('INSTRUMENT') or '').strip()
                if kind in ('IDO', 'OPTIDX'):
                    options.add(expiry_of(row))
                elif kind in ('IDF', 'FUTIDX'):
                    futures.add(expiry_of(row))
                else:
                    continue
                seen += 1
    if not seen:
        raise ValueError(f'{path.name}: no {symbol} index contracts found')
    return trade_date, options, futures
