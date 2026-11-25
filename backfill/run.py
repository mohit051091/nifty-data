"""Run the pilot expiry, then publish each batch of one expiry type from oldest to newest.

Publishing pushes to origin/main. Do not use it until the pilot has been reviewed."""

import argparse
import csv
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow.parquet as pq

from . import config
from .download import START, load_calendar, months
from .publish import RECEIPT, read_receipts, publication_unit
from .verify_lfs import VerificationError, git


RESOLVED_COVERAGE = {"full_requested", "source_limited", "unavailable"}


def reports(kind):
    return (
        RECEIPT, f"coverage/expiry-manifest-{kind}.csv", "coverage/spot-inventory.csv",
        f"coverage/option-session-offsets-{kind}.csv", f"coverage/request-results-{kind}.csv",
        "coverage/request-results-spot.csv", f"coverage/dhan-rollover-evidence-{kind}.csv",
        "coverage/spot-gap-contract-evidence.csv",
    )


def run_module(repo, module, *args):
    return subprocess.run([sys.executable, "-m", module, *map(str, args)], cwd=repo).returncode


def read_report(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def matching_row(path, field, value):
    return next(item for item in read_report(path) if item[field] == value)


def refresh_report(repo, state, kind):
    if run_module(repo, "backfill.download", "--repo", repo, "--state", state, "--expiry-type", kind, "report"):
        raise ValueError("Coverage report failed")


def publish_reports(repo, kind):
    def run(*args):
        return git(list(args), cwd=repo, stage="git " + args[0])

    if run("diff", "--cached", "--name-only").strip():
        raise VerificationError("Unexpected staged files. Inspect the index before publishing coverage.")
    run("add", "--", *(path for path in reports(kind) if (repo / path).is_file()))
    if run("diff", "--cached", "--name-only").strip():
        run("diff", "--cached", "--check")
        run("commit", "-m", f"Record {config.underlying().symbol} {kind} backfill coverage and LFS receipts")
    run("push", "origin", "HEAD:refs/heads/main")


def has_verified_scope(repo, scope):
    receipt = read_receipts(repo).get(publication_unit(scope), {})
    unchanged = not git(["status", "--porcelain", "--untracked-files=normal", "--", scope],
                        cwd=repo, stage="Check batch changes").strip()
    if unchanged and receipt.get("state") == "verified":
        unchanged_since_verified = not git(
            ["diff", "--name-only", receipt["commit"], "HEAD", "--", scope],
            cwd=repo, stage="Compare the verified and current data trees",
        ).strip()
        if unchanged_since_verified:
            return True
    return False


def verify_scope(repo, state, scope):
    path = repo / scope
    if not (any(path.glob("*.parquet")) if path.is_dir() else path.is_file()):
        return False
    return has_verified_scope(repo, scope) or not run_module(
        repo, "backfill.publish", "--repo", repo, "--state", state, "--path", scope,
    )


def verify_final(repo, state, kind):
    commit = git(["rev-parse", "HEAD"], cwd=repo, stage="Read final published commit").decode().strip()
    counts = {
        path.relative_to(repo).as_posix(): pq.ParquetFile(path).metadata.num_rows
        for pattern in ("data/*/*/*.parquet", "spot/*.parquet")
        for path in repo.glob(pattern)
    }
    expected = state / "final-expected-counts.json"
    expected.write_text(json.dumps(counts), encoding="utf-8")
    result = state / f"lfs-verification-final-{commit}.json"
    if run_module(repo, "backfill.verify_lfs", "--commit", commit,
                  "--expected-counts", expected, "--json-output", result):
        raise VerificationError(f"Final GitHub data differs from the local inventory. See {result}")
    outstanding = []
    for file, key in ((f"expiry-manifest-{kind}.csv", "expiry"), ("spot-inventory.csv", "month")):
        outstanding.extend(row[key] for row in read_report(repo / "coverage" / file)
                           if row["coverage_status"] not in RESOLVED_COVERAGE)
    if outstanding:
        raise ValueError(f"{len(outstanding)} coverage units remain unresolved. See the published coverage reports.")
    print(f"Final LFS verification succeeded at {commit}. See {result}.", flush=True)


def run(repo, state, mapping, pilot_only, requests_per_second, workers, kind, accept_derived):
    state.mkdir(parents=True, exist_ok=True)
    refresh_report(repo, state, kind)
    if not os.environ.get("DHAN_ACCESS_TOKEN"):
        raise ValueError("DHAN_ACCESS_TOKEN is not bound to this runner")
    if not mapping.is_file():
        raise ValueError(f"Missing mapping evidence {mapping}. Create it with backfill.verify_mapping (NSE bhavcopies) "
                         "or backfill.verify_dhan_codes (Dhan only, needs --accept-derived-calendar).")
    pilot = config.expiry_type(kind).pilot
    derived = ("--accept-derived-calendar",) if accept_derived else ()

    def fetch(*args):
        return run_module(repo, "backfill.download", "--repo", repo, "--state", state, "--expiry-type", kind,
                          *derived, "--mapping-evidence", mapping, "--requests-per-second", requests_per_second, *args)

    def spot(month):
        scope = f"spot/{config.underlying().symbol}_SPOT_{month}.parquet"
        row = matching_row(repo / "coverage/spot-inventory.csv", "month", month)
        if row["coverage_status"] not in RESOLVED_COVERAGE:
            if fetch("spot", "--month", month):
                raise ValueError(f"Spot retrieval stopped during {month}")
        refresh_report(repo, state, kind)
        if (repo / scope).is_file():
            if not verify_scope(repo, state, scope):
                print(f"Spot {month} · waiting for LFS verification", flush=True)

    def options(expiry):
        scope = f"data/{kind}/{expiry}"
        row = matching_row(repo / f"coverage/expiry-manifest-{kind}.csv", "expiry", expiry)
        result = 0
        if row["coverage_status"] not in RESOLVED_COVERAGE:
            result = fetch("options", "--expiry", expiry, "--workers", workers)
        refresh_report(repo, state, kind)
        verified = verify_scope(repo, state, scope)
        if not verified:
            print(f"Options {expiry} · no verified LFS publication for this run", flush=True)
        if result:
            raise ValueError(f"Options retrieval stopped during {expiry}")
        return verified

    try:
        for first_month in ("2021-11", "2021-12"):
            spot(first_month)
        pilot_verified = has_verified_scope(repo, f"data/{kind}/{pilot}") or options(pilot)
        pilot_row = matching_row(repo / f"coverage/expiry-manifest-{kind}.csv", "expiry", pilot)
        attempted = sum(int(pilot_row[field]) for field in (
            "returned_batches", "empty_batches", "unsupported_batches", "aged_out_batches",
        ))
        aged_out = (
            pilot_row["coverage_status"] == "unavailable"
            and attempted > 0 and int(pilot_row["aged_out_batches"]) == attempted
        )
        fallback = aged_out and verify_scope(repo, state, "data/monthly/2021-11-25")
        if not (pilot_verified or fallback):
            raise ValueError(f"Pilot {pilot} needs a fresh LFS verification or evidenced retention fallback")
        if pilot_only:
            return

        completed_spot = {"2021-11", "2021-12"}
        for expiry, period in load_calendar(repo / "backfill" / config.expiry_type(kind).calendar, kind).items():
            expiry_key = expiry.isoformat()
            if expiry_key == pilot:
                continue
            previous = max(START, period.previous)
            for month in months():
                begin = date.fromisoformat(month + "-01")
                if month not in completed_spot and previous.replace(day=1) <= begin <= expiry:
                    spot(month)
                    completed_spot.add(month)
            options(expiry_key)
            publish_reports(repo, kind)
        for month in months():
            if month not in completed_spot:
                spot(month)
    finally:
        refresh_report(repo, state, kind)
        publish_reports(repo, kind)
    verify_final(repo, state, kind)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--mapping-evidence", type=Path)
    parser.add_argument("--pilot-only", action="store_true")
    parser.add_argument("--underlying", default="NIFTY", choices=sorted(config.UNDERLYINGS))
    parser.add_argument("--expiry-type", required=True, choices=sorted(config.EXPIRY_TYPES))
    parser.add_argument("--accept-derived-calendar", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--requests-per-second", type=float, default=2,
                        help="Limit Dhan chart requests to this rate (maximum 5)")
    args = parser.parse_args()
    repo, state = args.repo.resolve(), args.state.resolve()
    if state.is_relative_to(repo):
        parser.error("--state must be outside the repository")
    config.select(args.underlying)
    mapping = args.mapping_evidence or state / f"rollover-mapping-{args.expiry_type}.csv"
    try:
        run(repo, state, mapping.resolve(), args.pilot_only, args.requests_per_second, args.workers,
            args.expiry_type, args.accept_derived_calendar)
    except (OSError, ValueError, VerificationError) as exc:
        print(f"Backfill stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
