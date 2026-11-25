#!/usr/bin/env python3
"""Verify published index Parquets from a fresh clone (underlying from BACKFILL_UNDERLYING, default NIFTY)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from . import config

DAILY_SPOT = config.daily_spot()
OPTION_PATH = config.option_pattern()  # groups: expiry type, expiry, strike, side
SPOT_PATH = config.spot_pattern()
LFS_POINTER = re.compile(
    rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\n"
    rb"size (0|[1-9][0-9]*)\n"
)

TRADE_DATE = ("trade_date", pa.date32())
TIMESTAMP = ("timestamp", pa.timestamp("ms", tz="Asia/Kolkata"))
OHLC = [(name, pa.float64()) for name in ("open", "high", "low", "close")]
VOLUME = ("volume", pa.float64())
OPTION_SCHEMA = pa.schema([
    ("expiry", pa.date32()), ("strike", pa.int64()), ("side", pa.string()),
    TIMESTAMP, TRADE_DATE, *OHLC, VOLUME,
    ("oi", pa.float64()), ("iv", pa.float64()), ("spot", pa.float64()),
])
SPOT_SCHEMA = pa.schema([TIMESTAMP, TRADE_DATE, *OHLC, VOLUME])
DAILY_SCHEMA = pa.schema([TRADE_DATE, *OHLC])


class VerificationError(Exception):
    """A remote content or verification prerequisite failed."""


def git(arguments, *, cwd=None, stage, stdin=None):
    env = os.environ.copy()
    for variable in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(variable, None)
    env.update(GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=env, input=stdin, capture_output=True, check=False,
    )
    if result.returncode:
        # Git and LFS diagnostics can contain credentialed remote or object URLs.
        raise VerificationError(f"{stage} failed (Git exit status {result.returncode})")
    return result.stdout


def in_scope(path, scopes):
    return not scopes or any(path == scope or path.startswith(scope + "/") for scope in scopes)


def is_data_path(path):
    return path.startswith(("data/", "spot/")) and path.endswith(".parquet")


def load_expected_counts(path, scopes):
    if path is None:
        return None
    with path.open(encoding="utf-8") as stream:
        counts = json.load(stream)
    if not isinstance(counts, dict) or any(
        not (OPTION_PATH.fullmatch(name) or SPOT_PATH.fullmatch(name) or name == DAILY_SPOT)
        or type(rows) is not int or rows <= 0
        for name, rows in counts.items()
    ):
        raise VerificationError("Expected counts must map Parquet paths to positive integer rows")
    return {name: rows for name, rows in counts.items() if in_scope(name, scopes)}


def dataset_identity(path):
    if match := OPTION_PATH.fullmatch(path):
        return f"{match[1]}/{match[2]}", match[4], OPTION_SCHEMA
    if match := SPOT_PATH.fullmatch(path):
        return match[1], None, SPOT_SCHEMA
    if path == DAILY_SPOT:
        return "daily", None, DAILY_SCHEMA
    raise VerificationError("Path does not match the published layout")


def inspect_parquet(path, expected_schema):
    with pq.ParquetFile(path) as parquet:
        if not parquet.schema_arrow.equals(expected_schema):
            raise VerificationError("Parquet schema differs from the published schema")
        metadata = parquet.metadata
        if any(
            metadata.row_group(group).column(column).compression != "ZSTD"
            for group in range(metadata.num_row_groups)
            for column in range(metadata.num_columns)
        ):
            raise VerificationError("Expected ZSTD-compressed Parquet")
        observed = sum(batch.num_rows for batch in parquet.iter_batches())
        if observed <= 0 or observed != metadata.num_rows:
            raise VerificationError("Parquet is empty or its decoded row count differs from metadata")
    return observed


def inspect_download(path, oid, size):
    if path.is_symlink() or not path.is_file():
        raise VerificationError("The checkout has no regular file at this path")
    if path.stat().st_size != size:
        raise VerificationError("Downloaded size differs from the committed LFS pointer")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != oid:
            raise VerificationError("Downloaded SHA-256 differs from the committed LFS pointer")


def read_pointers(checkout, commit, scopes, errors):
    tree = git(
        ["ls-tree", "-rlz", commit, "--", "data", "spot"],
        cwd=checkout, stage="Published Git tree read",
    )
    pointers = {}
    selected = set()
    for entry in tree.split(b"\0")[:-1]:
        metadata, raw_path = entry.split(b"\t", 1)
        path = os.fsdecode(raw_path)
        if not is_data_path(path) or not in_scope(path, scopes):
            continue
        selected.add(path)
        mode, kind, _, size = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755"} or int(size) > 1024:
            errors.append(f"{path}: committed Git object is not a regular LFS pointer")
            continue
        # Smudge was disabled before checkout, so these are committed pointer bytes.
        pointer = LFS_POINTER.fullmatch((checkout / path).read_bytes())
        if pointer is None:
            errors.append(f"{path}: invalid Git LFS pointer")
            continue
        pointers[path] = (pointer[1].decode("ascii"), int(pointer[2]))

    for scope in scopes:
        if not any(in_scope(path, [scope]) for path in selected):
            errors.append(f"{scope}: no data files in the published Git tree")
    if not selected:
        errors.append("The published Git tree has no data files in scope")
    if pointers:
        paths = b"".join(os.fsencode(path) + b"\0" for path in pointers)
        attributes = git(["check-attr", "-z", "--stdin", "filter"], cwd=checkout,
                         stage="Git LFS attribute check", stdin=paths).split(b"\0")
        for path, _, value in zip(attributes[::3], attributes[1::3], attributes[2::3]):
            if value != b"lfs":
                errors.append(f"{os.fsdecode(path)}: Git filter attribute is not lfs")
    return pointers, selected


def verify(remote, commit, scopes, counts, report):
    with tempfile.TemporaryDirectory(prefix="nifty-lfs-verify-") as directory:
        workspace = Path(directory)
        checkout = workspace / "checkout"
        git(["clone", "--no-local", "--no-checkout", "--", remote, str(checkout)],
            stage="Fresh Git clone")
        git(["lfs", "install", "--local", "--skip-smudge"], cwd=checkout, stage="Git LFS setup")
        git(["config", "--local", "lfs.storage", str(workspace / "lfs")],
            cwd=checkout, stage="Isolated LFS cache setup")
        git(["checkout", "--detach", commit], cwd=checkout, stage="Published commit checkout")

        errors = report["errors"]
        pointers, selected = read_pointers(checkout, commit, scopes, errors)
        report["git_files_in_scope"] = len(selected)
        if counts is not None:
            parquet_paths = selected
            errors.extend(f"{path}: missing from the published Git tree" for path in sorted(counts.keys() - parquet_paths))
            errors.extend(f"{path}: missing an expected row count" for path in sorted(parquet_paths - counts.keys()))
        if not pointers:
            return

        include = ",".join(f"/{scope}" for scope in scopes)
        if not scopes:
            include = "/data/**/*.parquet,/spot/*.parquet"
        git(["-c", "lfs.fetchrecentalways=false", "lfs", "fetch", f"--include={include}",
             "--exclude=", "origin", commit], cwd=checkout, stage="Git LFS download")
        git(["lfs", "checkout"], cwd=checkout, stage="Git LFS hydration")

        for path, (oid, size) in sorted(pointers.items()):
            try:
                local = checkout / path
                inspect_download(local, oid, size)
                result = {"sha256": oid, "bytes": size}
                partition, side, schema = dataset_identity(path)
                rows = inspect_parquet(local, schema)
                if counts is not None and path in counts and rows != counts[path]:
                    raise VerificationError(f"Read {rows} rows, expected {counts[path]}")
                dataset = report["options"] if side else report["spot"]
                partition_counts = dataset.setdefault(partition, {"files": 0, "rows": 0})
                partition_counts["files"] += 1
                partition_counts["rows"] += rows
                if side:
                    side_counts = partition_counts.setdefault(side, {"files": 0, "rows": 0})
                    side_counts["files"] += 1
                    side_counts["rows"] += rows
                result["rows"] = rows
                report["verified_files"][path] = result
            except (OSError, ValueError, VerificationError, RuntimeError) as exc:
                errors.append(f"{path}: {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Full SHA of the published commit")
    parser.add_argument("--remote", help="Git remote URL or path, defaulting to this project's origin")
    parser.add_argument("--path", action="append", default=[],
                        help="Repository-relative data file or directory, repeat to select multiple paths")
    parser.add_argument("--expected-counts", type=Path,
                        help="JSON object mapping each expected Parquet path to its row count")
    parser.add_argument("--json-output", type=Path, help="Save the verification report to this path")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.commit):
        parser.error("--commit must be a full Git SHA")
    scopes = [path.rstrip("/") for path in args.path]
    for path in scopes:
        if path not in {"data", "spot", DAILY_SPOT} and not (
            OPTION_PATH.fullmatch(path) or SPOT_PATH.fullmatch(path)
            or re.fullmatch(r"data/(monthly|weekly)(/\d{4}-\d{2}-\d{2})?", path)
        ):
            parser.error(f"Unexpected publication path {path!r}")

    report = {"commit": args.commit.lower(), "paths": scopes, "git_files_in_scope": 0,
              "verified_files": {}, "options": {}, "spot": {}, "errors": []}
    try:
        counts = load_expected_counts(args.expected_counts, scopes)
        remote = args.remote or os.fsdecode(git(
            ["remote", "get-url", "origin"], cwd=Path(__file__).resolve().parents[1],
            stage="Project origin lookup",
        )).strip()
        verify(remote, report["commit"], scopes, counts, report)
    except (OSError, ValueError, VerificationError) as exc:
        report["errors"].append(str(exc))
    report["verified_file_count"] = len(report["verified_files"])
    report["verified"] = not report["errors"] and report["verified_file_count"] > 0
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
