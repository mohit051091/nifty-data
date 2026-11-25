"""Resume one expiry type: fetch, report, publish to LFS and push each expiry that has no Parquets yet.

Usage: BACKFILL_STATE=/tmp/claude-0/nifty-state DHAN_ACCESS_TOKEN=... \
       python backfill/resume_expiries.py monthly|weekly [--accept-derived-calendar]
Pushes to origin/main after every expiry. Skips expiries whose data/<type>/<expiry> folder already holds Parquets;
move a partial folder aside first. The mapping evidence is coverage/rollover-mapping-<type>.csv.
"""
import argparse, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backfill import config
from backfill.download import load_calendar

REPO = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("expiry_type", choices=sorted(config.EXPIRY_TYPES))
parser.add_argument("--accept-derived-calendar", action="store_true")
args = parser.parse_args()
STATE = os.environ.get("BACKFILL_STATE") or sys.exit("Set BACKFILL_STATE to the checkpoint directory (outside the checkout)")
KIND = args.expiry_type
EVIDENCE = f"coverage/rollover-mapping-{KIND}.csv"
common = ["--repo", ".", "--state", STATE, "--expiry-type", KIND, "--mapping-evidence", EVIDENCE]
if args.accept_derived_calendar:
    common.append("--accept-derived-calendar")


def sh(*a):
    r = subprocess.run(a, cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"FAILED {a}: {r.stdout[-500:]} {r.stderr[-800:]}")
    return r.stdout


for expiry in load_calendar(REPO / "backfill" / config.expiry_type(KIND).calendar, KIND):
    folder = REPO / "data" / KIND / expiry.isoformat()
    if folder.is_dir() and any(folder.glob("*.parquet")):
        continue
    print("START", KIND, expiry, flush=True)
    sh(sys.executable, "-m", "backfill.download", *common, "options", "--expiry", expiry.isoformat())
    sh(sys.executable, "-m", "backfill.download", *common, "report")
    sh("git", "fetch", "origin", "main")
    sh(sys.executable, "-m", "backfill.publish", "--repo", ".", "--state", STATE, "--path", f"data/{KIND}/{expiry}")
    sh("git", "add", "coverage")
    sh("git", "commit", "-q", "-m", f"Record coverage and LFS receipt for the {KIND} {expiry} expiry")
    sh("git", "push", "origin", "main")
    print("DONE", KIND, expiry, sh("git", "rev-parse", "HEAD").strip(), flush=True)
print("ALL DONE")
