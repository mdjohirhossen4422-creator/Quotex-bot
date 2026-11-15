#!/usr/bin/env python3
"""Tests for signal_backtest.py — honest metrics, no pooling, controls.

  * a real pattern (momentum) shows a positive OOS verdict,
  * a random walk shows 'indistinguishable' (negative control),
  * a coin-flip control is always reported alongside the model,
  * too little data is refused, not scored.

Run:  python test_signal_backtest.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from provenance import FEED_ID, classify_source
from tick_summary import SUMMARY_FIELDS
from test_ai_signal import _make, _summary, PERIOD


def _seed(db: str, asset: str, candles: list[list]):
    con = sqlite3.connect(db)
    cols = ["asset TEXT", "period INTEGER", "time INTEGER", "open REAL",
            "close REAL", "high REAL", "low REAL", "source TEXT",
            "feed_id TEXT"] + [f"{c} REAL" for c in SUMMARY_FIELDS]
    con.execute(f"CREATE TABLE IF NOT EXISTS candles({', '.join(cols)}, "
                f"PRIMARY KEY(asset, period, time))")
    con.execute("CREATE TABLE IF NOT EXISTS ticks("
                "asset TEXT, ts REAL, price REAL, dir INTEGER)")
    names = ("asset,period,time,open,close,high,low,source,feed_id,"
             + ",".join(SUMMARY_FIELDS))
    ph = ",".join(["?"] * (9 + len(SUMMARY_FIELDS)))
    for c in candles:
        con.execute(f"INSERT OR IGNORE INTO candles({names}) VALUES({ph})",
                    (asset, PERIOD, c[0], c[1], c[2], c[3], c[4],
                     classify_source(asset), FEED_ID) + _summary(c))
    con.commit()
    con.close()


def _tmpdb() -> str:
    return os.path.join(tempfile.mkdtemp(), "b.db")


def test_momentum_backtest_shows_edge_random_does_not():
    from signal_backtest import backtest
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(3000, 0.7, seed=11))     # learnable
    _seed(db, "EURUSD", _make(3000, 0.0, seed=12))         # random walk
    otc = backtest("otc", PERIOD, db, 0, False, 5, 0.4, 2, 0.85, 200)
    real = backtest("real", PERIOD, db, 0, False, 5, 0.4, 2, 0.85, 200)
    assert otc["status"] == "ok" and real["status"] == "ok"
    assert otc["oos"]["hit"] > 0.56, f"momentum not detected: {otc['oos']['hit']}"
    # a random walk must not show an edge; 'below' or 'indistinguishable' are
    # both honest "no edge" outcomes, only 'above' would be a false positive
    assert real["oos"]["verdict"] != "above", \
        f"random walk faked an edge: {real['oos']['verdict']}"
    assert otc["control"]["n"] == otc["oos"]["n"], "control size mismatch"
    print("PASS test_momentum_backtest_shows_edge_random_does_not")


def test_insufficient_data_refused():
    from signal_backtest import backtest
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(150, 0.7, seed=13))
    r = backtest("otc", PERIOD, db, 0, False, 5, 0.4, 2, 0.85, 200)
    assert r["status"] == "insufficient_data", r
    print("PASS test_insufficient_data_refused")


def test_backtest_never_pools_sources():
    from signal_backtest import backtest
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(1500, 0.7, seed=14))
    _seed(db, "EURUSD", _make(1500, 0.7, seed=15))
    otc = backtest("otc", PERIOD, db, 0, False, 4, 0.4, 2, 0.85, 200)
    real = backtest("real", PERIOD, db, 0, False, 4, 0.4, 2, 0.85, 200)
    assert otc["n_pairs"] == 1 and real["n_pairs"] == 1
    print("PASS test_backtest_never_pools_sources")


if __name__ == "__main__":
    test_momentum_backtest_shows_edge_random_does_not()
    test_insufficient_data_refused()
    test_backtest_never_pools_sources()
    print("\nall backtest tests passed")
