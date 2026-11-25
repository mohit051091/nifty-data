#!/usr/bin/env python3
"""Publish validated index units on main and record independent LFS verification."""

import argparse
import csv
from datetime import date
import json
from pathlib import Path
import re
import sys
import tempfile

from . import config
from .verify_lfs import (
    DAILY_SPOT, LFS_POINTER, OPTION_PATH, SPOT_PATH, VerificationError,
    dataset_identity, git, inspect_parquet, verify,
)


RECEIPT = "coverage/lfs-publications.csv"
FIELDS = ("kind", "unit", "state", "commit")


def publication_unit(scope):
    if scope == DAILY_SPOT:
        return ("daily", "spot")
    if match := SPOT_PATH.fullmatch(scope):
        return ("spot", match[1])
    if match := re.fullmatch(r"data/(monthly|weekly)/(\d{4}-\d{2}-\d{2})", scope):
        date.fromisoformat(match[2])
        return (f"options-{match[1]}", match[2])
    raise ValueError(f"Expected data/<monthly|weekly>/<expiry> or a spot partition, got {scope}")


def read_receipts(repo):
    path = repo / RECEIPT
    if path.exists():
        with path.open(newline="", encoding="utf-8") as source:
            return {(item["kind"], item["unit"]): item for item in csv.DictReader(source)}
    return {}


def save_receipts(repo, scopes, state, commit):
    receipts = read_receipts(repo)
    for scope in scopes:
        kind, unit = publication_unit(scope)
        key = (kind, unit)
        if receipts.get(key, {}).get("commit") == commit and receipts[key]["state"] == "verified":
            continue
        receipts[key] = dict(zip(FIELDS, (kind, unit, state, commit)))
    path = repo / RECEIPT
    path.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, newline="", encoding="utf-8", delete=False) as out:
        pending = Path(out.name)
        writer = csv.DictWriter(out, FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(receipts[key] for key in sorted(receipts))
    pending.replace(path)


def publish(repo, scopes, state_dir):
    for scope in scopes:
        publication_unit(scope)

    def run(*arguments):
        return git(list(arguments), cwd=repo, stage="git " + arguments[0])

    if run("branch", "--show-current").strip() != b"main":
        raise VerificationError("Publication requires a checkout of main")
    if run("diff", "--cached", "--name-only").strip():
        raise VerificationError("Commit or unstage the current index before publication")

    # Reconcile already-published remote commits before staging any dataset changes.
    run("fetch", "origin", "main")
    try:
        run("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD")
    except VerificationError as exc:
        raise VerificationError("Remote main advanced. Reconcile the local checkout before retrying.") from exc

    local_counts = {}
    payloads = []
    for scope in scopes:
        path = repo / scope
        files = sorted(path.glob("*.parquet")) if path.is_dir() else [path]
        if not files or any(not file.is_file() for file in files):
            raise VerificationError(f"No local data files for {scope}")
        for file in files:
            relative = file.relative_to(repo).as_posix()
            if not (OPTION_PATH.fullmatch(relative) or SPOT_PATH.fullmatch(relative) or relative == DAILY_SPOT):
                raise VerificationError(f"Unexpected data filename {relative}")
            _, _, schema = dataset_identity(relative)
            local_counts[relative] = inspect_parquet(file, schema)
            if local_counts[relative] <= 0:
                raise VerificationError(f"Refusing to publish empty file {relative}")
            payloads.append(relative)

    run("add", "--", *payloads)
    for path in payloads:
        if not LFS_POINTER.fullmatch(run("show", f":{path}")):
            raise VerificationError(f"Index does not contain an LFS pointer for {path}")
    if run("diff", "--cached", "--name-only").strip():
        run("diff", "--cached", "--check")
        symbol = config.underlying().symbol
        subject = f"Publish {symbol} data for {scopes[0]}" if len(scopes) == 1 else (
            f"Publish {len(scopes)} validated {symbol} partitions"
        )
        run("commit", "-m", subject)
    commit = run("rev-parse", "HEAD").decode().strip()
    run("lfs", "push", "origin", "main")
    run("push", "origin", "HEAD:refs/heads/main")
    save_receipts(repo, scopes, "pushed", commit)
    print(f"Pushed {len(payloads):,} LFS files at {commit}", flush=True)

    state_dir.mkdir(parents=True, exist_ok=True)
    report = {"commit": commit, "paths": list(scopes), "errors": [],
              "verified_files": {}, "options": {}, "spot": {}}
    batch = "-".join(publication_unit(scope)[1] for scope in scopes)
    report_path = state_dir / f"lfs-verification-{commit}-{batch[:120]}.json"
    try:
        remote = run("remote", "get-url", "origin").decode().strip()
        verify(remote, commit, list(scopes), local_counts, report)
    except (OSError, ValueError, VerificationError) as exc:
        report["errors"].append(str(exc))
    report["verified_file_count"] = len(report["verified_files"])
    report["verified"] = not report["errors"] and report["verified_file_count"] == len(payloads)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["verified"]:
        raise VerificationError(f"Independent download failed. See {report_path}")
    save_receipts(repo, scopes, "verified", commit)
    print(f"Independently verified {len(payloads):,} LFS files at {commit}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, required=True, help="Directory outside the repository for verification reports")
    parser.add_argument("--path", action="append", required=True, help="data/<monthly|weekly>/<expiry> or a spot partition")
    args = parser.parse_args()
    repo = args.repo.resolve()
    state = args.state.resolve()
    if state.is_relative_to(repo):
        parser.error("--state must be outside the repository")
    try:
        publish(repo, tuple(dict.fromkeys(args.path)), state)
    except (VerificationError, ValueError, OSError) as exc:
        print(f"Publication stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
