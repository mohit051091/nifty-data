"""Underlying and expiry-type parameters. Nothing else in the pipeline hard-codes either."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Underlying:
    symbol: str          # file-name prefix and NSE bhavcopy symbol
    security_id: int     # Dhan security id of the index
    segment: str         # Dhan segment of the spot index
    index_name: str      # NSE historical index name
    strike_step: int


@dataclass(frozen=True)
class ExpiryType:
    name: str            # folder name under data/
    flag: str            # Dhan rollingoption expiryFlag
    calendar: str        # file under backfill/
    pilot: str           # first expiry exercised by `run --pilot-only`


UNDERLYINGS = {
    "NIFTY": Underlying("NIFTY", 13, "IDX_I", "NIFTY 50", 50),
    "BANKNIFTY": Underlying("BANKNIFTY", 25, "IDX_I", "NIFTY BANK", 100),
}
EXPIRY_TYPES = {
    "monthly": ExpiryType("monthly", "MONTH", "expiries-monthly.csv", "2021-12-30"),
    "weekly": ExpiryType("weekly", "WEEK", "expiries-weekly.csv", "2021-12-02"),
}

# The one place a default is chosen. Command lines override it with --underlying.
import os  # noqa: E402

_active = UNDERLYINGS[os.environ.get("BACKFILL_UNDERLYING", "NIFTY").upper()]


def select(symbol: str) -> Underlying:
    global _active
    _active = UNDERLYINGS[symbol.upper()]
    return _active


def underlying() -> Underlying:
    return _active


def expiry_type(name: str) -> ExpiryType:
    return EXPIRY_TYPES[name]


def option_name(expiry, strike, side) -> str:
    return f"{_active.symbol}_{expiry}_{strike}_{side}.parquet"


def option_pattern() -> re.Pattern:
    """data/<monthly|weekly>/<expiry>/<SYMBOL>_<expiry>_<strike>_<CE|PE>.parquet"""
    return re.compile(rf"data/(monthly|weekly)/(\d{{4}}-\d{{2}}-\d{{2}})/{_active.symbol}_\2_([1-9]\d*)_(CE|PE)\.parquet")


def file_option_pattern() -> re.Pattern:
    return re.compile(rf"{_active.symbol}_(\d{{4}}-\d{{2}}-\d{{2}})_(\d+)_(CE|PE)\.parquet")


def spot_pattern() -> re.Pattern:
    return re.compile(rf"spot/{_active.symbol}_SPOT_(\d{{4}}-(?:0[1-9]|1[0-2]))\.parquet")


def spot_file_pattern() -> re.Pattern:
    return re.compile(rf"{_active.symbol}_SPOT_(\d{{4}}-\d{{2}})\.parquet")


def daily_spot() -> str:
    return f"spot/{_active.symbol}_SPOT_DAILY.parquet"
