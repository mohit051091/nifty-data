"""SQLite checkpoint and compatible, atomic Parquet output."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

TIMEZONE = "Asia/Kolkata"
PRICES = ("open", "high", "low", "close")
DAILY_SCHEMA = pa.schema([
    ("trade_date", pa.date32()), *((field, pa.float64()) for field in PRICES),
])
SPOT_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("ms", tz=TIMEZONE)), *DAILY_SCHEMA, ("volume", pa.float64()),
])
OPTION_SCHEMA = pa.schema([
    ("expiry", pa.date32()), ("strike", pa.int64()), ("side", pa.string()), *SPOT_SCHEMA,
    ("oi", pa.float64()), ("iv", pa.float64()), ("spot", pa.float64()),
])
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    code INTEGER NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    side TEXT NOT NULL,
    offset INTEGER NOT NULL,
    flag TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    rows INTEGER NOT NULL,
    malformed INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kind, unit, code, start, end, side, offset, flag)
);
CREATE INDEX IF NOT EXISTS batches_unit ON batches(kind, unit);
CREATE INDEX IF NOT EXISTS batches_offset ON batches(kind, unit, code, side, offset, start, end);
CREATE TABLE IF NOT EXISTS option_report_cache (
    expiry TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS options (
    expiry TEXT NOT NULL,
    strike INTEGER NOT NULL,
    side TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    oi REAL,
    iv REAL,
    spot REAL,
    baseline INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (expiry, strike, side, timestamp)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS spot (
    trade_date TEXT NOT NULL,
    timestamp INTEGER PRIMARY KEY,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    baseline INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS spot_date ON spot(trade_date);
CREATE TABLE IF NOT EXISTS daily (
    trade_date TEXT PRIMARY KEY,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    baseline INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    trade_date TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    strike INTEGER NOT NULL,
    PRIMARY KEY (batch_id, timestamp, strike)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS conflicts (
    kind TEXT NOT NULL,
    candle_key TEXT NOT NULL,
    existing_row TEXT NOT NULL,
    incoming_row TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE (kind, candle_key, existing_row, incoming_row, provenance)
);
CREATE TABLE IF NOT EXISTS fingerprints (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    trade_date TEXT PRIMARY KEY,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mapping (
    trade_date TEXT NOT NULL,
    expiry_code INTEGER NOT NULL,
    source TEXT NOT NULL,
    flag TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (trade_date, expiry_code, flag)
);
"""
KEYS = {"options": ("expiry", "strike", "side", "timestamp"), "spot": ("timestamp",), "daily": ("trade_date",)}
SCHEMAS = {"options": OPTION_SCHEMA, "spot": SPOT_SCHEMA, "daily": DAILY_SCHEMA}
BATCH_KEY = "kind=? AND unit=? AND code=? AND start=? AND end=? AND side=? AND offset=? AND flag=?"


def read_parquet(path, schema):
    table = pq.ParquetFile(path).read()
    if not table.schema.equals(schema, check_metadata=False):
        raise ValueError(f"Unexpected Parquet schema in {path}: {table.schema}")
    return table


def atomic_parquet(path, rows, schema):
    table = pa.Table.from_pylist(rows, schema=schema)
    if path.exists() and read_parquet(path, schema).equals(table):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".", suffix=".parquet.tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        pq.write_table(table, temporary, compression="zstd")
        if not read_parquet(temporary, schema).equals(table):
            raise ValueError(f"Parquet did not round trip: {path}")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Ledger:
    def __init__(self, state):
        state.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state / "candles.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def batch(self, batch):
        return self.db.execute(f"SELECT * FROM batches WHERE {BATCH_KEY}", batch).fetchone()

    def record(self, batch, status, detail, rows, malformed, *, attempted=True):
        with self.db:
            self.db.execute("""
                INSERT INTO batches(kind, unit, code, start, end, side, offset, flag, status, detail, rows,
                                    malformed, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, unit, code, start, end, side, offset, flag) DO UPDATE SET
                    status=excluded.status, detail=excluded.detail, rows=excluded.rows,
                    malformed=excluded.malformed, attempts=attempts + excluded.attempts
            """, (*batch, status, detail, len(rows), malformed, int(attempted)))
            request_id = self.batch(batch)["id"]
            conflicts = 0
            for row in rows:
                values = row.copy()
                if batch.kind == "options":
                    values.update(expiry=batch.unit, side=batch.side)
                conflicts += self.write_row(batch.kind, values, f"batch {request_id}")
                if batch.kind != "daily":
                    self.db.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?)",
                                    (request_id, row["trade_date"], row["timestamp"], row.get("strike", 0)))
            if conflicts:
                self.db.execute("UPDATE batches SET status='conflicting', detail=? WHERE id=?",
                                ((detail + "; " if detail else "") + f"{conflicts} conflicting rows", request_id))

    def write_row(self, kind, row, provenance, baseline=False):
        names = SCHEMAS[kind].names
        values = tuple(row[name] for name in names)
        columns = ", ".join(names)
        parameters = ", ".join("?" for _ in names)
        statement = f"INSERT OR IGNORE INTO {kind} ({columns}, baseline) VALUES ({parameters}, ?)"
        if self.db.execute(statement, (*values, int(baseline))).rowcount:
            return False
        keys = KEYS[kind]
        key = tuple(row[name] for name in keys)
        where = " AND ".join(f"{name}=?" for name in keys)
        existing = self.db.execute(f"SELECT * FROM {kind} WHERE {where}", key).fetchone()
        if tuple(existing[name] for name in names) == values:
            if baseline and not existing["baseline"]:
                self.db.execute(f"UPDATE {kind} SET baseline=1 WHERE {where}", key)
            return False
        self.db.execute("INSERT OR IGNORE INTO conflicts VALUES (?, ?, ?, ?, ?)",
                        (kind, json.dumps(key), json.dumps(dict(existing)), json.dumps(row), provenance))
        if baseline and not existing["baseline"]:
            assignments = ", ".join(f"{name}=?" for name in names)
            self.db.execute(f"UPDATE {kind} SET {assignments}, baseline=1, quarantined=1 WHERE {where}",
                            (*values, *key))
        else:
            self.db.execute(f"UPDATE {kind} SET quarantined=1 WHERE {where}", key)
        return True

    def fingerprint(self, path):
        stat = path.stat()
        name = str(path.resolve())
        saved = self.db.execute("SELECT * FROM fingerprints WHERE path=?", (name,)).fetchone()
        if saved and (stat.st_size, stat.st_mtime_ns) == (saved["size"], saved["mtime_ns"]):
            return name, stat.st_size, stat.st_mtime_ns, saved["digest"]
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return name, stat.st_size, stat.st_mtime_ns, digest

    def file_seen(self, path):
        name, _, _, digest = self.fingerprint(path)
        return self.db.execute("SELECT 1 FROM fingerprints WHERE path=? AND digest=?",
                               (name, digest)).fetchone() is not None

    def remember_file(self, path):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO fingerprints VALUES (?, ?, ?, ?)", self.fingerprint(path))

    def seed(self, path, kind, table, contract=None):
        from .download import START, STOP, numeric, option_strike, valid_prices

        with self.db:
            previous = None
            for row in table.to_pylist():
                day = row["trade_date"]
                if not isinstance(day, date) or not START <= day < STOP or not valid_prices(row):
                    raise ValueError(f"Unexpected date or OHLC in {path}.")
                for field, value in row.items():
                    if field in PRICES or (field in {"volume", "oi", "iv", "spot"} and value is not None):
                        numeric(value)
                row["trade_date"] = day.isoformat()
                if kind == "daily":
                    key = day
                else:
                    timestamp = row["timestamp"]
                    if not isinstance(timestamp, datetime) or timestamp.astimezone(ZoneInfo(TIMEZONE)).date() != day:
                        raise ValueError(f"Unexpected timestamp in {path}.")
                    key = int(timestamp.timestamp())
                    if timestamp.microsecond or key % 60:
                        raise ValueError(f"Non-minute timestamp in {path}.")
                    row["timestamp"] = key
                if contract:
                    row["expiry"] = row["expiry"].isoformat()
                    if (row["expiry"], option_strike(row["strike"]), row["side"]) != contract:
                        raise ValueError(f"Option contract key does not match {path}.")
                if kind == "spot" and row["trade_date"][:7] != path.stem[-7:]:
                    raise ValueError(f"Spot row is outside the month named by {path}.")
                if previous is not None and key <= previous:
                    raise ValueError(f"Unordered or repeated candle in {path}.")
                previous = key
                self.write_row(kind, row, str(path), baseline=True)
            self.db.execute("INSERT OR REPLACE INTO fingerprints VALUES (?, ?, ?, ?)", self.fingerprint(path))

    def session_days(self):
        return [row[0] for row in self.db.execute("SELECT trade_date FROM daily WHERE baseline OR NOT quarantined")]

    def daily_rows(self):
        return self.db.execute(
            "SELECT trade_date, open, high, low, close FROM daily WHERE baseline OR NOT quarantined ORDER BY trade_date")

    def spot_rows(self, month):
        return self.db.execute("""
            SELECT timestamp, trade_date, open, high, low, close, volume FROM spot
            WHERE trade_date LIKE ? AND (baseline OR NOT quarantined) ORDER BY timestamp
        """, (month + "-%",))

    def spot_timestamps(self):
        groups = defaultdict(list)
        for day, timestamp in self.db.execute("SELECT trade_date, timestamp FROM spot WHERE baseline OR NOT quarantined"):
            groups[day].append(timestamp)
        return groups

    def spot_closes(self):
        return dict(self.db.execute("SELECT timestamp, close FROM spot WHERE baseline OR NOT quarantined"))

    def contracts(self, expiry):
        return self.db.execute("SELECT DISTINCT strike, side FROM options WHERE expiry=? ORDER BY strike, side",
                               (expiry,)).fetchall()

    def contract_rows(self, expiry, strike, side):
        return self.db.execute("""
            SELECT * FROM options WHERE expiry=? AND strike=? AND side=? AND (baseline OR NOT quarantined)
            ORDER BY timestamp
        """, (expiry, strike, side))

    def record_evidence(self, day, source):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?)", (day, source))

    def record_mapping(self, mapping, flag):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO mapping VALUES (?, ?, ?, ?)",
                                ((day, code, source, flag)
                                 for day, codes in mapping.items() for code, source in codes.items()))

    def mapping_sources(self, flag):
        return [tuple(row) for row in self.db.execute(
            "SELECT trade_date, expiry_code, source FROM mapping WHERE flag=? ORDER BY trade_date, expiry_code",
            (flag,))]
