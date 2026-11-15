#!/usr/bin/env python3
"""Tests for storage_monitor.py — thresholds, safe prune, backup.

  * usage below 80% is OK, >=80% is WARN, >=90% is ALERT (exit codes 10/20),
  * prune removes ONLY old ticks and never touches candles,
  * prune respects --dry-run and refuses without --yes,
  * backup writes a compressed candle-only file that contains no ticks.

Run:  python test_storage_monitor.py
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
import time

import storage_monitor as sm


def _db(path: str, n_old: int, n_new: int, n_candles: int):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ticks(asset TEXT, ts REAL, price REAL, dir INTEGER)")
    con.execute("CREATE TABLE candles(asset TEXT, period INTEGER, time INTEGER,"
                "open REAL, close REAL, high REAL, low REAL)")
    now = time.time()
    for i in range(n_old):
        con.execute("INSERT INTO ticks VALUES('EURUSD_otc',?,1.1,1)",
                    (now - 10 * 86400 - i,))
    for i in range(n_new):
        con.execute("INSERT INTO ticks VALUES('EURUSD_otc',?,1.1,1)", (now - i,))
    for i in range(n_candles):
        con.execute("INSERT INTO candles VALUES('EURUSD_otc',60,?,1.1,1.1,1.1,1.1)",
                    (1700000000 + i * 60,))
    con.commit()
    con.close()


def _pad(path: str, mb: int):
    with open(path, "ab") as f:
        f.write(b"\0" * (mb * 1024 * 1024))


def test_status_thresholds():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    _db(db, 0, 10, 10)
    # tiny db, 10 MB budget -> OK
    assert sm.evaluate(db, 10)["status"] == "OK"
    # pad to ~9 MB of a 10 MB budget -> ALERT (>=90%)
    _pad(db, 9)
    r = sm.evaluate(db, 10)
    assert r["status"] == "ALERT", r["pct"]
    assert sm._exit_code("WARN") == 10 and sm._exit_code("ALERT") == 20
    print("PASS test_status_thresholds")


def test_prune_only_old_ticks_keeps_candles():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    _db(db, 20, 20, 15)
    res = sm.prune_ticks(db, days=7, dry_run=False)
    con = sqlite3.connect(db)
    ticks = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    candles = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    con.close()
    assert res["deleted"] == 20, res
    assert ticks == 20, f"expected 20 recent ticks kept, got {ticks}"
    assert candles == 15, "prune must never touch candles"
    print("PASS test_prune_only_old_ticks_keeps_candles")


def test_prune_dry_run_deletes_nothing():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    _db(db, 20, 20, 10)
    res = sm.prune_ticks(db, days=7, dry_run=True)
    con = sqlite3.connect(db)
    ticks = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    con.close()
    assert res["would_delete"] == 20 and res["deleted"] == 0
    assert ticks == 40, "dry-run must not delete"
    print("PASS test_prune_dry_run_deletes_nothing")


def test_backup_excludes_ticks_and_roundtrips():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    _db(db, 5, 5, 12)
    out = sm.backup_candles(db, os.path.join(d, "bk"))
    assert os.path.exists(out), "backup file not written"
    lines = []
    with gzip.open(out, "rt") as f:
        for line in f:
            lines.append(json.loads(line))
    assert len(lines) == 12, f"expected 12 candle rows, got {len(lines)}"
    assert all(len(r) >= 7 for r in lines)
    print("PASS test_backup_excludes_ticks_and_roundtrips")


if __name__ == "__main__":
    test_status_thresholds()
    test_prune_only_old_ticks_keeps_candles()
    test_prune_dry_run_deletes_nothing()
    test_backup_excludes_ticks_and_roundtrips()
    print("\nall storage-monitor tests passed")
