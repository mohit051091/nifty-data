"""Synthetic-file tests for verify_expiries.py (old and UDiFF bhavcopy formats). Run: python -m unittest backfill.test_verify_expiries"""
import csv
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from backfill import verify_expiries
from backfill.nse_reference import MONTH_ABBREVIATIONS

ROOT = Path(__file__).resolve().parent
OLD_HEADER = "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,CONTRACTS,TIMESTAMP"
NEW_HEADER = "TradDt,FinInstrmTp,TckrSymb,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol"


def old_date(d):
    return f"{d.day:02d}-{MONTH_ABBREVIATIONS[d.month - 1].title()}-{d.year}"


def write_file(directory, trade_date, options=(), futures=(), udiff=None, symbol="NIFTY", extra=()):
    udiff = trade_date >= date(2024, 7, 8) if udiff is None else udiff
    if udiff:
        name = f"BhavCopy_NSE_FO_0_0_0_{trade_date:%Y%m%d}_F_0000.csv"
        lines = [NEW_HEADER]
        lines += [f"{trade_date},IDO,{symbol},{e},{e},22000,CE,1,1,1,1,10" for e in options]
        lines += [f"{trade_date},IDF,{symbol},{e},{e},,,1,1,1,1,10" for e in futures]
        lines += [f"{trade_date},IDO,NIFTYNXT50,{e},{e},70000,CE,1,1,1,1,10" for e in extra]
        lines += [f"{trade_date},STO,RELIANCE,{e},{e},1000,CE,1,1,1,1,10" for e in extra]
    else:
        name = f"fo{trade_date.day:02d}{MONTH_ABBREVIATIONS[trade_date.month - 1]}{trade_date.year}bhav.csv"
        lines = [OLD_HEADER]
        lines += [f"OPTIDX,{symbol},{old_date(e)},22000,CE,1,1,1,1,10,{old_date(trade_date)}," for e in options]
        lines += [f"FUTIDX,{symbol},{old_date(e)},0,XX,1,1,1,1,10,{old_date(trade_date)}," for e in futures]
        lines += [f"OPTIDX,NIFTYNXT50,{old_date(e)},70000,CE,1,1,1,1,10,{old_date(trade_date)}," for e in extra]
    path = directory / (name + ".zip")
    with ZipFile(path, "w") as archive:
        archive.writestr(name, "\n".join(lines) + "\n")
    return path


def read(name, directory):
    with (directory / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


class VerifyExpiries(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cal, self.cache = self.tmp / "cal", self.tmp / "cache"
        self.cal.mkdir(), self.cache.mkdir()
        for name in ("expiries-weekly.csv", "expiries-monthly.csv"):
            shutil.copy(ROOT / name, self.cal / name)
        self.weekly = [date.fromisoformat(r["expiry"]) for r in read("expiries-weekly.csv", self.cal)]
        self.monthly = [date.fromisoformat(r["expiry"]) for r in read("expiries-monthly.csv", self.cal)]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["verify_expiries", "--cache-dir", str(self.cache), "--calendar-dir", str(self.cal), *extra]
        with redirect_stdout(out), redirect_stderr(err):
            old, verify_expiries.sys.argv = verify_expiries.sys.argv, argv
            try:
                code = verify_expiries.main()
            finally:
                verify_expiries.sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def status(self, name="expiries-weekly.csv"):
        return {r["expiry"]: r["status"] for r in read(name, self.cal)}

    def test_snapshots_in_both_formats_verify_listed_dates(self):
        # Old-format snapshot on 2023-01-02 and a UDiFF snapshot on 2024-08-01.
        old_day, new_day = date(2023, 1, 2), date(2024, 8, 1)
        old_listed = [d for d in self.weekly if old_day <= d <= date(2023, 1, 26)]
        new_listed = [d for d in self.weekly if new_day <= d <= date(2024, 8, 29)]
        write_file(self.cache, old_day, old_listed, [d for d in self.monthly if d == date(2023, 1, 25)], extra=[old_day])
        write_file(self.cache, new_day, new_listed, [date(2024, 8, 29)], extra=[new_day])
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        weekly = self.status()
        self.assertTrue(all(weekly[d.isoformat()] == "verified" for d in old_listed + new_listed))
        self.assertEqual(weekly["2023-02-02"], "derived")
        self.assertEqual(self.status("expiries-monthly.csv")["2023-01-25"], "verified")
        self.assertEqual(self.status("expiries-monthly.csv")["2024-08-29"], "verified")
        row = next(r for r in read("expiries-weekly.csv", self.cal) if r["expiry"] == "2023-01-05")
        self.assertEqual(row["source"], "fo02JAN2023bhav.csv.zip")

    def test_monthly_needs_future_expiry(self):
        day = date(2023, 1, 2)
        write_file(self.cache, day, [date(2023, 1, 25)], [])
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.status()["2023-01-25"], "derived")
        write_file(self.cache, date(2023, 1, 3), [date(2023, 1, 25)], [date(2023, 1, 25)])
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.status()["2023-01-25"], "verified")

    def test_file_dated_after_expiry_does_not_verify_it(self):
        write_file(self.cache, date(2023, 1, 9), [date(2023, 1, 5), date(2023, 1, 12)], [])
        self.run_main()
        self.assertEqual(self.status()["2023-01-05"], "derived")
        self.assertEqual(self.status()["2023-01-12"], "verified")

    def test_unknown_option_expiry_aborts_without_writing(self):
        before = (self.cal / "expiries-weekly.csv").read_bytes()
        write_file(self.cache, date(2023, 1, 2), [date(2023, 1, 5), date(2023, 1, 6)], [])
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("2023-01-06", err)
        self.assertEqual((self.cal / "expiries-weekly.csv").read_bytes(), before)

    def test_future_expiry_filed_as_weekly_aborts(self):
        write_file(self.cache, date(2025, 4, 1), [date(2025, 4, 3)], [date(2025, 4, 30)])
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("2025-04-30", err)

    def test_nse_monthly_april_2025_confirmed(self):
        write_file(self.cache, date(2025, 4, 1), [date(2025, 4, 24), date(2025, 4, 30)], [date(2025, 4, 24)])
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.status("expiries-monthly.csv")["2025-04-24"], "verified")
        self.assertEqual(self.status()["2025-04-30"], "verified")

    def test_list_missing_prints_uncovered_dates_and_writes_nothing(self):
        before = (self.cal / "expiries-weekly.csv").read_bytes()
        write_file(self.cache, date(2023, 1, 2), [date(2023, 1, 5)], [])
        code, out, _ = self.run_main("--list-missing")
        self.assertEqual(code, 0)
        self.assertIn("2023-01-12", out)
        self.assertNotIn("2023-01-05 ", out)
        self.assertEqual((self.cal / "expiries-weekly.csv").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
