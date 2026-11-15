#!/usr/bin/env python3
"""Storage guard for the collector — never let the disk fill silently.

The free Botkeep volume is 1 GB and raw ticks grow ~1.3 GB/day, so a collector
that ignores storage will die. This module is the safety net the brief asks for
(priority 11):

  * reports current usage (db + WAL + shm) against a budget,
  * **WARN at 80%**, **ALERT at 90%** (exit codes 10 / 20 so cron/CI can react),
  * `--backup` writes a **compressed candle-only archive** (ticks are excluded —
    they are the bulk and the least valuable long-term) into a backup dir,
  * `--prune-ticks DAYS` deletes raw ticks older than N days and VACUUMs, which
    is the only safe way to reclaim space once raw ticks are no longer needed;
    it refuses to run without `--yes` and prints exactly what it would remove,
  * never touches candles, never fabricates data, never deletes the newest rows.

Usage
-----
    python storage_monitor.py                     # report + exit code
    python storage_monitor.py --budget-mb 1024
    python storage_monitor.py --backup            # archive candles
    python storage_monitor.py --prune-ticks 7 --dry-run
    python storage_monitor.py --prune-ticks 7 --yes
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

WARN_PCT = 80.0
ALERT_PCT = 90.0
DEFAULT_BUDGET_MB = 1024
DEFAULT_BACKUP_DIR = "data/backups"


def _bytes(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


def usage(db: str) -> dict:
    total = _bytes(db) + _bytes(db + "-wal") + _bytes(db + "-shm")
    return {"db": db, "db_bytes": _bytes(db),
            "wal_bytes": _bytes(db + "-wal"),
            "shm_bytes": _bytes(db + "-shm"),
            "total_bytes": total}


def row_counts(db: str) -> dict:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        t = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        c = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        con.close()
        return {"ticks": t, "candles": c}
    except sqlite3.Error as e:
        return {"error": str(e)}


def evaluate(db: str, budget_mb: int) -> dict:
    u = usage(db)
    limit = budget_mb * 1024 * 1024
    pct = u["total_bytes"] / limit * 100 if limit else 0.0
    if pct >= ALERT_PCT:
        status = "ALERT"
    elif pct >= WARN_PCT:
        status = "WARN"
    else:
        status = "OK"
    u.update({"budget_mb": budget_mb, "pct": round(pct, 1), "status": status,
              "rows": row_counts(db), "checked_at": time.time()})
    return u


def _exit_code(status: str) -> int:
    return {"OK": 0, "WARN": 10, "ALERT": 20}.get(status, 0)


# --------------------------------------------------------------------------- #
def backup_candles(db: str, backup_dir: str) -> str:
    """Compressed candle-only dump. Ticks are excluded on purpose."""
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(backup_dir, f"candles-{stamp}.sql.gz")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(candles)")]
    with gzip.open(out, "wt") as f:
        for row in con.execute(f"SELECT {','.join(cols)} FROM candles"):
            f.write(json.dumps(row) + "\n")
    con.close()
    return out


def prune_ticks(db: str, days: int, dry_run: bool) -> dict:
    """Delete raw ticks older than `days` and VACUUM. Candles are untouched."""
    cutoff = time.time() - days * 86400
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM ticks WHERE ts < ?", (cutoff,)).fetchone()[0]
    before = _bytes(db)
    if dry_run or n == 0:
        con.close()
        return {"would_delete": n, "deleted": 0, "before_bytes": before,
                "after_bytes": before, "dry_run": dry_run}
    con.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
    con.commit()
    con.execute("VACUUM")
    con.close()
    return {"would_delete": n, "deleted": n, "before_bytes": before,
            "after_bytes": _bytes(db), "dry_run": False}


def _cycle(args) -> dict:
    """One storage check + optional backup/prune. Returns the report dict."""
    report = evaluate(args.db, args.budget_mb)
    if not args.json:
        r = report["rows"]
        print(f"[{time.strftime('%H:%M:%S')}] storage: "
              f"{report['total_bytes']/1e6:.1f} MB of {args.budget_mb} MB "
              f"({report['pct']}%) -> {report['status']}")
        print(f"  db={report['db_bytes']/1e6:.1f} MB  wal={report['wal_bytes']/1e6:.1f} MB"
              f"  rows: ticks={r.get('ticks')} candles={r.get('candles')}")
        if report["status"] == "WARN":
            print("  [WARN] over 80% — plan a backup/prune before the disk fills")
        elif report["status"] == "ALERT":
            print("  [ALERT] over 90% — archive now and prune raw ticks")

    if args.backup:
        path = backup_candles(args.db, args.backup_dir)
        report["backup_path"] = path
        if not args.json:
            print(f"  backup -> {path} ({_bytes(path)/1e6:.1f} MB)")

    if args.prune_ticks:
        if not args.yes and not args.dry_run:
            print("refusing to prune without --yes (use --dry-run to preview)")
            sys.exit(2)
        res = prune_ticks(args.db, args.prune_ticks, args.dry_run)
        report["prune"] = res
        if not args.json:
            verb = "would delete" if args.dry_run else "deleted"
            print(f"  {verb} {res['would_delete']:,} ticks older than "
                  f"{args.prune_ticks} days "
                  f"({res['before_bytes']/1e6:.0f} -> {res['after_bytes']/1e6:.0f} MB)")
    return report


def watch(args):
    """Loop forever, warning before capacity runs out (req 10).

    On WARN: back up candles (if --auto-backup). On ALERT: back up and, if
    --auto-prune-days is set AND --yes is given, prune ticks older than that.
    Candles are never pruned automatically.
    """
    print(f"storage watch — db={args.db} budget={args.budget_mb}MB "
          f"interval={args.interval}s  (ctrl-c to stop)")
    while True:
        report = evaluate(args.db, args.budget_mb)
        status = report["status"]
        if status == "WARN":
            print(f"[{time.strftime('%H:%M:%S')}] WARN {report['pct']}% — "
                  "backing up candles")
            if args.auto_backup:
                print("  backup ->", backup_candles(args.db, args.backup_dir))
        elif status == "ALERT":
            print(f"[{time.strftime('%H:%M:%S')}] ALERT {report['pct']}% — "
                  "archiving and pruning")
            if args.auto_backup:
                print("  backup ->", backup_candles(args.db, args.backup_dir))
            if args.auto_prune_days and args.yes:
                res = prune_ticks(args.db, args.auto_prune_days, False)
                print(f"  pruned {res['deleted']:,} ticks "
                      f"({res['before_bytes']/1e6:.0f} -> "
                      f"{res['after_bytes']/1e6:.0f} MB)")
            elif args.auto_prune_days:
                print("  (auto-prune needs --yes; skipping)")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] OK {report['pct']}% "
                  f"({report['total_bytes']/1e6:.0f} MB)")
        time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser(description="Storage guard for the collector")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--budget-mb", type=int, default=DEFAULT_BUDGET_MB)
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--prune-ticks", type=int, default=0,
                    help="delete raw ticks older than N days (needs --yes)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--watch", action="store_true", help="loop, warning before full")
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--auto-backup", action="store_true",
                    help="with --watch: back up candles on WARN/ALERT")
    ap.add_argument("--auto-prune-days", type=int, default=0,
                    help="with --watch --yes: prune ticks older than N days on ALERT")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}")
        sys.exit(2)

    if args.watch:
        try:
            watch(args)
        except KeyboardInterrupt:
            print("\nstopped.")
        return

    report = _cycle(args)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    sys.exit(_exit_code(report["status"]))


if __name__ == "__main__":
    main()
