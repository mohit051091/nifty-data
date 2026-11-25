"""Coverage artifacts and progress derived from checkpoint and publication receipts."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import pyarrow.parquet as pq

from . import config
from .download import (
    FOREIGN_SPOT_TOLERANCE, IST, RESOLVED, SPECIAL_HOURS, SPLIT, dates, expected_minute_count, month_range, months,
    option_batches, outside_evidenced_option_session, regular, rows_from_csv,
)


def local_option_files(repo, kind):
    files = defaultdict(dict)
    for path in sorted((repo / "data" / kind).glob("*/*.parquet")):
        parquet = pq.ParquetFile(path)
        timestamp = parquet.schema_arrow.get_field_index("timestamp")
        metadata = parquet.metadata
        first = []
        last = []
        for group in range(metadata.num_row_groups):
            statistics = metadata.row_group(group).column(timestamp).statistics
            if statistics and statistics.has_min_max:
                first.append(statistics.min)
                last.append(statistics.max)
        parts = path.stem.split("_")
        files[path.parent.name][(int(parts[-2]), parts[-1])] = (
            metadata.num_rows, min(first) if first else None, max(last) if last else None)
    return files


def batch_statuses(ledger):
    batches = defaultdict(list)
    for row in ledger.db.execute("SELECT * FROM batches ORDER BY kind,unit,code,start,end,side,offset"):
        batches[row["kind"], row["unit"]].append(row)
    return batches


def coverage_status(entries, day, dates_with_rows):
    covering = [row for row in entries if row["start"] <= day < row["end"] and row["status"] not in SPLIT]
    if not covering:
        return "pending"
    observation_count = sum(dates_with_rows.get((row["id"], day), 0) for row in covering)
    outcomes = {row["status"] for row in covering}
    if outcomes - RESOLVED:
        return sorted(outcomes - RESOLVED)[0]
    if observation_count:
        return "returned"
    if outcomes == {"aged_out"}:
        return "aged_out"
    if outcomes == {"unsupported"}:
        return "unsupported"
    return "accepted_no_rows"


def offsets_by_session(expiry, expected, unit_batches, observed_days, spot):
    entries = defaultdict(list)
    for batch in unit_batches:
        entries[batch["code"], batch["side"], batch["offset"]].append(batch)
    outcomes = Counter()
    records = []
    for day in expected:
        code = expiry.code_for(date.fromisoformat(day))
        for side in ("CE", "PE"):
            groups = defaultdict(list)
            raw_returned = []
            minute_deficits = {}
            for offset in range(-10, 11):
                batches = entries[code, side, offset]
                status = coverage_status(batches, day, observed_days)
                outcomes[status] += 1
                groups[status].append(str(offset))
                source_minutes = sum(observed_days.get((batch["id"], day), 0) for batch in batches)
                if source_minutes:
                    raw_returned.append(str(offset))
                if day in spot and status in RESOLVED | {"accepted_no_rows"}:
                    if source_minutes < len(spot[day]):
                        minute_deficits[offset] = len(spot[day]) - source_minutes
            records.append({
                "expiry": str(expiry.current), "trade_date": day, "expiry_code": code, "side": side,
                "raw_offsets_with_rows": ",".join(raw_returned),
                "returned_offsets": ",".join(groups.pop("returned", ())),
                "accepted_empty_offsets": ",".join(groups.pop("accepted_no_rows", ())),
                "provider_unsupported_offsets": ",".join(groups.pop("unsupported", ())),
                "aged_out_offsets": ",".join(groups.pop("aged_out", ())),
                "unresolved_offsets": ";".join(f"{key}={','.join(value)}" for key, value in sorted(groups.items())),
                "source_minute_deficit_floor_vs_spot": ",".join(f"{offset}:{gap}"
                                                               for offset, gap in minute_deficits.items()),
            })
            outcomes["spot_minute_deficit_floor"] += sum(minute_deficits.values())
    return outcomes, records


def classify(rows, unresolved, limited, attempted):
    if unresolved or not attempted:
        return "unresolved" if attempted else ("unresolved_baseline" if rows else "missing")
    if not rows:
        return "unavailable"
    return "source_limited" if limited else "full_requested"


def options_manifest(ledger, calendar, sessions, spot, option_files, batches):
    fingerprints = defaultdict(list)
    for path, digest in ledger.db.execute("SELECT path, digest FROM fingerprints ORDER BY path"):
        file = Path(path)
        unit = "spot" if file.name.startswith(f"{config.underlying().symbol}_SPOT_") else file.parent.name
        fingerprints[unit].append(digest)
    spot_version = hashlib.sha256(repr((fingerprints["spot"], sorted(sessions.items()),
        [tuple(batch) for (kind, _), rows in batches.items() if kind in {"spot", "daily"} for batch in rows],
    )).encode()).hexdigest()
    options = []
    by_session = []
    regular_spot_days = {day for day, timestamps in spot.items()
                         if any(regular(datetime.fromtimestamp(ts, IST)) for ts in timestamps)}
    for expiry in calendar.values():
        unit = str(expiry.current)
        expected = [day for day in sorted(sessions) if expiry.code_for(date.fromisoformat(day)) is not None]
        rollover = str(expiry.previous)
        unit_batches = batches["options", unit]
        files = option_files[unit]
        version = hashlib.sha256(repr((4, expiry, spot_version, fingerprints[unit], files,
            [tuple(batch) for batch in unit_batches],
        )).encode()).hexdigest()
        cached = ledger.db.execute("SELECT payload FROM option_report_cache WHERE expiry=? AND version=?",
                                   (unit, version)).fetchone()
        if cached:
            summary, session_rows = json.loads(cached[0])
            options.append(summary)
            by_session.extend(session_rows)
            continue
        observed_days = {(row[0], row[1]): row[2] for row in ledger.db.execute("""
            SELECT o.batch_id, o.trade_date, COUNT(DISTINCT o.timestamp) FROM batches b
            JOIN observations o ON o.batch_id=b.id
            WHERE b.kind='options' AND b.unit=? GROUP BY o.batch_id, o.trade_date
        """, (unit,))}
        offset_outcomes, session_rows = offsets_by_session(expiry, expected, unit_batches, observed_days, spot)
        by_session.extend(session_rows)
        counts = Counter()
        for (_, side), (total, _, _) in files.items():
            counts[side] += total
        row_total = counts.total()
        first = [item[1] for item in files.values() if item[1]]
        last = [item[2] for item in files.values() if item[2]]
        strikes = [strike for strike, _ in files]
        batch_outcomes = Counter(row["status"] for row in unit_batches)
        present = {(row["code"], row["start"], row["end"], row["side"], row["offset"], row["flag"]) for row in unit_batches}
        pending = sum(tuple(batch[2:]) not in present for batch in option_batches(expiry))
        considered = ledger.db.execute(
            "SELECT COUNT(*) FROM options WHERE expiry=? AND (baseline OR NOT quarantined)", (unit,)).fetchone()[0]
        quarantine = ledger.db.execute(
            "SELECT COUNT(*) FROM options WHERE expiry=? AND quarantined", (unit,)).fetchone()[0]
        baseline_rows = ledger.db.execute(
            "SELECT COUNT(*) FROM options WHERE expiry=? AND baseline", (unit,)).fetchone()[0]
        sanity = option_sanity(ledger, unit, spot, regular_spot_days, bool(unit_batches))
        local_hold = max(0, considered - row_total - sanity["excluded_outside_option_session_rows"]
                         - sanity["excluded_foreign_spot_rows"])
        unresolved = pending + sum(value for key, value in offset_outcomes.items()
                                   if key not in RESOLVED | {"accepted_no_rows", "spot_minute_deficit_floor"})
        unresolved += sum(value for key, value in batch_outcomes.items() if key not in RESOLVED | SPLIT)
        unresolved += local_hold + quarantine
        limited = (any(value for key, value in offset_outcomes.items() if key != "returned")
                   or any(len(spot.get(day, ())) < expected_minute_count(day) for day in expected)
                   or sanity["missing_spot_minute_option_rows"] > 0)
        status = classify(row_total, unresolved or not expected, limited, bool(unit_batches))
        observed_atm = defaultdict(set)
        for row in session_rows:
            if row["returned_offsets"]:
                observed_atm[row["expiry_code"], row["side"]].update(map(int, row["returned_offsets"].split(",")))
        summary = {
            "expiry": unit, "expiry_type": expiry.kind, "expiry_flag": expiry.flag,
            "calendar_verified": expiry.current_verified, "calendar_source": expiry.source,
            "rollover_date": rollover if rollover in expected else "",
            "expected_sessions": len(expected),
            "observed_spot_sessions": sum(day in spot for day in expected),
            "first_candle": min(first).astimezone(IST).isoformat() if first else "",
            "last_candle": max(last).astimezone(IST).isoformat() if last else "",
            "sides": ",".join(sorted(counts)),
            "strike_min": min(strikes) if strikes else "", "strike_max": max(strikes) if strikes else "",
            "contracts": len(files), "ce_rows": counts["CE"], "pe_rows": counts["PE"], "rows": row_total,
            "baseline_rows": min(row_total, baseline_rows), "new_dhan_rows": max(0, row_total - baseline_rows),
            **sanity, "source_minute_deficit_floor_vs_spot": offset_outcomes["spot_minute_deficit_floor"],
            "front_ce_offsets": format_offsets(observed_atm[1, "CE"]),
            "front_pe_offsets": format_offsets(observed_atm[1, "PE"]),
            "rollover_ce_offsets": format_offsets(observed_atm[2, "CE"]),
            "rollover_pe_offsets": format_offsets(observed_atm[2, "PE"]),
            "returned_batches": batch_outcomes["returned"], "empty_batches": batch_outcomes["empty"],
            "unsupported_batches": batch_outcomes["unsupported"], "aged_out_batches": batch_outcomes["aged_out"],
            "split_batches": sum(batch_outcomes[key] for key in SPLIT),
            "rejected_batches": sum(batch_outcomes[key] for key in ("rejected", "date_range")),
            "other_unresolved_batches": sum(value for key, value in batch_outcomes.items()
                                            if key not in RESOLVED | SPLIT | {"rejected", "date_range"}),
            "pending_batches": pending, "held_rows": local_hold, "quarantined_keys": quarantine,
            "coverage_status": status,
        }
        options.append(summary)
        with ledger.db:
            ledger.db.execute("INSERT OR REPLACE INTO option_report_cache VALUES (?, ?, ?)",
                              (unit, version, json.dumps((summary, session_rows))))
    return options, by_session


def option_sanity(ledger, expiry, spot, regular_spot_days, attempted):
    counts = Counter()
    for row in ledger.db.execute("""
        SELECT o.timestamp, o.trade_date, o.spot, s.low, s.high, o.baseline, o.quarantined, s.close
        FROM options o LEFT JOIN spot s ON s.timestamp=o.timestamp AND (s.baseline OR NOT s.quarantined)
        WHERE o.expiry=?
    """, (expiry,)):
        stamp, day, provider_spot, low, high, baseline, quarantined, close = row
        if (not baseline and not quarantined and close and provider_spot is not None
                and abs(provider_spot - close) / close > FOREIGN_SPOT_TOLERANCE):
            counts["excluded_foreign_spot_rows"] += 1
            continue
        if not baseline and not quarantined and outside_evidenced_option_session(
            datetime.fromtimestamp(stamp, IST), day, spot, regular_spot_days
        ):
            counts["excluded_outside_option_session_rows"] += 1
            continue
        if day not in spot:
            counts["missing_spot_session_option_rows"] += 1
        elif stamp not in spot[day]:
            counts["missing_spot_minute_option_rows"] += 1
        elif provider_spot is not None:
            counts["paired_option_spot_rows"] += 1
            counts["option_spot_outside_minute_ohlc_rows"] += not (low - 1e-8 <= provider_spot <= high + 1e-8)
    if attempted:
        ordered_checks, inversions = ledger.db.execute("""
            SELECT COUNT(*), COUNT(CASE WHEN (b.offset > 0 AND o.strike <= atm.strike)
                                         OR (b.offset < 0 AND o.strike >= atm.strike) THEN 1 END)
            FROM batches b
            JOIN observations o ON o.batch_id=b.id
            JOIN batches a ON a.kind=b.kind AND a.unit=b.unit AND a.code=b.code
                          AND a.side=b.side AND a.offset=0 AND a.start <= o.trade_date AND o.trade_date < a.end
            JOIN observations atm ON atm.batch_id=a.id AND atm.timestamp=o.timestamp
            WHERE b.kind='options' AND b.unit=? AND b.offset<>0
        """, (expiry,)).fetchone()
        counts["offset_order_checks"] = ordered_checks
        counts["offset_order_inversions"] = inversions
    return {field: counts[field] for field in (
        "missing_spot_session_option_rows", "missing_spot_minute_option_rows", "paired_option_spot_rows",
        "option_spot_outside_minute_ohlc_rows", "offset_order_checks", "offset_order_inversions",
        "excluded_outside_option_session_rows", "excluded_foreign_spot_rows",
    )}


def format_offsets(offsets):
    return ",".join(map(str, sorted(offsets)))


def spot_inventory(ledger, spot_dir, sessions, spot, batches):
    result = []
    baseline = Counter()
    for day, timestamp in ledger.db.execute("SELECT trade_date, timestamp FROM spot WHERE baseline"):
        if day in spot and timestamp in spot[day]:
            baseline[day[:7]] += 1
    for month in months():
        start, end = month_range(month)
        expected = {day.isoformat() for day in dates(start, end)} & sessions.keys()
        observed = {day for day in expected if day in spot}
        special = sorted(day for day in observed if day in SPECIAL_HOURS or (
            spot[day] and not any(regular(datetime.fromtimestamp(ts, IST)) for ts in spot[day])
        ))
        regular_gaps = {
            day: expected_minute_count(day) - len(spot[day]) for day in observed
            if (day not in special or day in SPECIAL_HOURS) and len(spot[day]) < expected_minute_count(day)
        }
        path = spot_dir / f"{config.underlying().symbol}_SPOT_{month}.parquet"
        count = pq.ParquetFile(path).metadata.num_rows if path.exists() else 0
        unit_batches = batches["spot", month]
        invalid = any(row["status"] not in RESOLVED | {"split"} for row in unit_batches)
        attempted = bool(unit_batches) or bool(count and observed == expected and not regular_gaps)
        coverage = classify(count, invalid or not expected or (count and count != sum(len(spot[d]) for d in observed)),
                            expected != observed or bool(regular_gaps)
                            or any(day not in SPECIAL_HOURS for day in special), attempted)
        result.append({
            "month": month, "expected_sessions": len(expected), "observed_sessions": len(observed), "minute_rows": count,
            "baseline_rows": min(count, baseline[month]), "new_dhan_rows": max(0, count - baseline[month]),
            "missing_sessions": ",".join(sorted(expected - observed)),
            "regular_minute_gaps": ",".join(f"{day}:{missing}" for day, missing in sorted(regular_gaps.items())),
            "special_sessions": ",".join(special),
            "source_batches": len(unit_batches), "coverage_status": coverage,
        })
    return result


def write_csv(path, rows, fields=None):
    if not fields and rows:
        fields = tuple(dict.fromkeys(key for row in rows for key in row))
    if not fields:
        return
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    text = stream.getvalue()
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    temporary.replace(path)


def bar(completed, total=61):
    total = total or 1
    fill = min(20, completed * 20 // total)
    return f"[{'#' * fill}{'-' * (20 - fill)}] {completed}/{total}"


def published(repo, kind, option_units, spot_units):
    receipts = repo / "coverage" / "lfs-publications.csv"
    states = {}
    if receipts.exists():
        for row in rows_from_csv(receipts):
            if row["state"] in {"pushed", "verified"}:
                states[row["kind"], row["unit"]] = row["state"]
    for label, units in ((f"options-{kind}", option_units), ("spot", spot_units)):
        pushed = sum(states.get((label, unit)) in {"pushed", "verified"} for unit in units)
        verified = sum(states.get((label, unit)) == "verified" for unit in units)
        print(f"GitHub LFS · {label}: {bar(verified, len(units))} verified · {pushed}/{len(units)} pushed")
    print(f"GitHub LFS · daily: {states.get(('daily', 'spot'), 'unverified')}")
    result = subprocess.run(["git", "rev-parse", "--verify", "refs/remotes/origin/main"], cwd=repo,
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        print(f"origin/main at last fetch: {result.stdout.strip()}")


def report(ledger, repo, spot_dir, calendar, sessions, write=False, kind="monthly"):
    from .download import spot_stamps

    flag = config.expiry_type(kind).flag
    batches = batch_statuses(ledger)
    spot = spot_stamps(ledger, sessions)
    options, by_session = options_manifest(ledger, calendar, sessions, spot, local_option_files(repo, kind), batches)
    spot_rows = spot_inventory(ledger, spot_dir, sessions, spot, batches)
    for label, records in ((f"options-{kind}", options), ("spot", spot_rows)):
        states = Counter(row["coverage_status"] for row in records)
        done = states["full_requested"] + states["source_limited"]
        baseline = sum(record["baseline_rows"] > 0 for record in records)
        new_rows = sum(record["new_dhan_rows"] > 0 for record in records)
        local = sum(record["baseline_rows"] + record["new_dhan_rows"] > 0 for record in records)
        print(f"Data gathered · {label}: {bar(done, len(records))} · {states['full_requested']} full · "
              f"{states['source_limited']} source-limited · {states['unavailable']} unavailable · "
              f"{states['unresolved'] + states['unresolved_baseline']} unresolved · {states['missing']} missing")
        print(f"Local candle files · {label}: {local}/{len(records)} · {baseline} with reused rows · "
              f"{new_rows} with newly fetched Dhan rows")
    published(repo, kind, [row["expiry"] for row in options], [row["month"] for row in spot_rows])
    if not write:
        return
    coverage = repo / "coverage"
    coverage.mkdir(parents=True, exist_ok=True)
    write_csv(coverage / f"expiry-manifest-{kind}.csv", options)
    write_csv(coverage / "spot-inventory.csv", spot_rows)
    write_csv(coverage / f"option-session-offsets-{kind}.csv", by_session)
    request_fields = ("kind", "unit", "flag", "code", "start", "end", "side", "offset", "status",
                      "rows", "malformed", "attempts", "detail")
    mine = [row for rows in batches.values() for row in rows if row["kind"] != "options" or row["flag"] == flag]
    write_csv(coverage / f"request-results-{kind}.csv",
              [{key: row[key] for key in request_fields} for row in mine if row["kind"] == "options"], request_fields)
    write_csv(coverage / "request-results-spot.csv",
              [{key: row[key] for key in request_fields} for row in mine if row["kind"] != "options"], request_fields)
    fields = ("trade_date", "expiry_code", "source")
    write_csv(coverage / f"dhan-rollover-evidence-{kind}.csv",
              [dict(zip(fields, row)) for row in ledger.mapping_sources(flag)], fields)
    evidence_fields = ("trade_date", "source")
    evidence_rows = [dict(row) for row in ledger.db.execute("SELECT * FROM evidence ORDER BY trade_date")]
    write_csv(coverage / "spot-gap-contract-evidence.csv", evidence_rows, evidence_fields)
