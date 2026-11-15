#!/usr/bin/env python3
"""Verify data survives a collector restart (requirement 2).

Runs the real collector twice against an ISOLATED temp DB and checks that:
  * run 1 wrote candles,
  * every run-1 candle is still present after the restart (no data loss),
  * run 2 added rows without duplicating any (primary key / INSERT OR IGNORE),
  * the SQLite file itself persists on disk across the two processes.

This uses the live feed but never touches the production DB. It needs ~2x the
`--minutes` you give it.

  python verify_restart.py                 # 20s per run
  python verify_restart.py --minutes 60
  python verify_restart.py --pairs EURUSD_otc,BTCUSD_otc
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _run(db: str, pairs: str, minutes: float):
    cmd = [PY, os.path.join(ROOT, "live_collector.py"), "--db", db,
           "--no-ticks", "--minutes", str(minutes), "--log-every", "15"]
    if pairs:
        cmd += ["--pairs", pairs]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=minutes * 60 + 120)
    return r


def _snapshot(db: str) -> dict:
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT asset,period,time FROM candles "
        "GROUP BY asset,period,time HAVING COUNT(*)>1)").fetchone()[0]
    keys = {(a, p, t) for a, p, t in
            con.execute("SELECT asset,period,time FROM candles")}
    con.close()
    return {"total": total, "dupes": dup, "keys": keys}


def main():
    ap = argparse.ArgumentParser(description="Collector restart-persistence check")
    ap.add_argument("--minutes", type=float, default=0.34, help="minutes per run")
    ap.add_argument("--pairs", default="EURUSD_otc")
    args = ap.parse_args()

    d = tempfile.mkdtemp(prefix="restart_verify_")
    db = os.path.join(d, "candles.db")
    print(f"isolated db: {db}")
    print(f"run 1 ({args.minutes} min) …")
    r1 = _run(db, args.pairs, args.minutes)
    if not os.path.exists(db):
        print("FAIL: no DB after run 1")
        print(r1.stdout[-800:], r1.stderr[-800:])
        sys.exit(1)
    s1 = _snapshot(db)
    print(f"  after run 1: candles={s1['total']} dupes={s1['dupes']}")

    print(f"run 2 ({args.minutes} min) — same db, fresh process …")
    r2 = _run(db, args.pairs, args.minutes)
    s2 = _snapshot(db)
    print(f"  after run 2: candles={s2['total']} dupes={s2['dupes']}")

    lost = s1["keys"] - s2["keys"]
    added = len(s2["keys"]) - len(s1["keys"])

    ok = True
    if s1["total"] == 0:
        print("FAIL: run 1 wrote no candles.")
        print("  Most likely the feed was busy: market-qx serves ONE live session")
        print("  per source IP. Stop the production collector first, then re-run")
        print("  (./run_collector.sh --stop && python verify_restart.py ...).")
        print("  run-1 stdout tail:", (r1.stdout or "")[-400:].replace("\n", " | "))
        ok = False
    if lost:
        print(f"FAIL: {len(lost)} run-1 candles missing after restart")
        ok = False
    if s2["dupes"]:
        print(f"FAIL: {s2['dupes']} duplicate (asset,period,time) rows")
        ok = False
    if added < 0:
        print(f"FAIL: row count shrank ({s1['total']} -> {s2['total']})")
        ok = False

    print(f"  run-1 candles preserved: {len(s1['keys'] - lost)}/{len(s1['keys'])}")
    print(f"  new candles added by run 2: {added}")
    print("PASS: data survives restart, no duplicates" if ok
          else "FAIL: see messages above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
