"""Tests for data_audit.py — the read-only data-engine audit.

Every check is verified twice: clean input must pass, and a deliberately broken
input must fail. A check that never fails is worthless, so each negative case
matters as much as the positive one.

Run:  python test_data_audit.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import data_audit


def _mkdb(path, candles=(), ticks=(), with_constraints=True):
    db = sqlite3.connect(path)
    if with_constraints:
        db.execute("CREATE TABLE candles(asset TEXT, period INTEGER, time INTEGER,"
                   "open REAL, close REAL, high REAL, low REAL, source TEXT,"
                   "feed_id TEXT, PRIMARY KEY(asset,period,time))")
    else:
        db.execute("CREATE TABLE candles(asset TEXT, period INTEGER, time INTEGER,"
                   "open REAL, close REAL, high REAL, low REAL, source TEXT,"
                   "feed_id TEXT)")
    db.execute("CREATE TABLE ticks(asset TEXT, ts REAL, price REAL, dir INTEGER,"
               "source TEXT, feed_id TEXT)")
    db.executemany("INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)", candles)
    db.executemany("INSERT INTO ticks VALUES(?,?,?,?,?,?)", ticks)
    db.commit()
    db.close()


def _c(asset, period, t, o, cl, h, l, src=None):
    src = src or ("otc" if asset.endswith("_otc") else "real")
    return (asset, period, t, o, cl, h, l, src, "market-qx.info")


def _t(asset, ts, price, d=1, src=None):
    src = src or ("otc" if asset.endswith("_otc") else "real")
    return (asset, ts, price, d, src, "market-qx.info")


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def test_clean_store_passes():
    p = _tmp()
    _mkdb(p,
          candles=[_c("EURUSD_otc", 60, 960, 1.1, 1.2, 1.25, 1.05),
                   _c("EURUSD_otc", 60, 1020, 1.2, 1.15, 1.22, 1.1)],
          ticks=[_t("EURUSD_otc", 1000.0, 1.1), _t("EURUSD_otc", 1030.0, 1.2)])
    a = data_audit.run(p, now=1060)
    assert a.candles["ok"], a.candles
    assert a.ticks["ok"], a.ticks
    os.unlink(p)
    print("PASS test_clean_store_passes")


def test_invalid_ohlc_detected():
    p = _tmp()
    # high < low is impossible
    _mkdb(p, candles=[_c("EURUSD_otc", 60, 1000, 1.1, 1.2, 1.0, 1.3)])
    a = data_audit.run(p, now=1060)
    assert a.candles["invalid_ohlc"] == 1, a.candles
    assert not a.candles["ok"]
    os.unlink(p)
    print("PASS test_invalid_ohlc_detected")


def test_misaligned_time_detected():
    p = _tmp()
    _mkdb(p, candles=[_c("EURUSD_otc", 60, 1001, 1.1, 1.2, 1.25, 1.05)])
    a = data_audit.run(p, now=1060)
    assert a.candles["misaligned_time"] == 1, a.candles
    os.unlink(p)
    print("PASS test_misaligned_time_detected")


def test_source_mismatch_detected():
    p = _tmp()
    # otc symbol but tagged real
    _mkdb(p, candles=[_c("EURUSD_otc", 60, 960, 1.1, 1.2, 1.25, 1.05, src="real")])
    a = data_audit.run(p, now=1060)
    assert a.candles["source_symbol_mismatch"] == 1, a.candles
    os.unlink(p)
    print("PASS test_source_mismatch_detected")


def test_conflicting_tick_ts_detected():
    p = _tmp()
    _mkdb(p, ticks=[_t("EURUSD_otc", 1000.0, 1.1),
                    _t("EURUSD_otc", 1000.0, 1.2)])  # same ts, two prices
    a = data_audit.run(p, now=1060)
    assert a.ticks["conflicting_ts"] == 1, a.ticks
    # conflicting ts is flagged but not a hard failure of the other checks
    os.unlink(p)
    print("PASS test_conflicting_tick_ts_detected")


def test_future_tick_detected():
    p = _tmp()
    _mkdb(p, ticks=[_t("EURUSD_otc", 999999.0, 1.1)])
    a = data_audit.run(p, now=1000)
    assert a.ticks["future_ts"] == 1, a.ticks
    assert not a.ticks["ok"]
    os.unlink(p)
    print("PASS test_future_tick_detected")


def test_frozen_rate_by_source():
    p = _tmp()
    _mkdb(p, candles=[_c("EURUSD_otc", 60, 1000, 1.1, 1.1, 1.1, 1.1),
                      _c("EURUSD", 60, 1000, 1.1, 1.1, 1.1, 1.1)])
    a = data_audit.run(p, now=1060)
    assert a.candles["frozen_rate_by_source"]["otc"] == 1.0
    assert a.candles["frozen_rate_by_source"]["real"] == 1.0
    os.unlink(p)
    print("PASS test_frozen_rate_by_source")


def test_consistency_match_and_mismatch():
    p = _tmp()
    # ticks -> bucket 1000: open 1.10 close 1.20 high 1.25 low 1.05
    ticks = [_t("EURUSD_otc", 960.0, 1.10), _t("EURUSD_otc", 970.0, 1.25),
             _t("EURUSD_otc", 980.0, 1.05), _t("EURUSD_otc", 990.0, 1.20)]
    good = [_c("EURUSD_otc", 60, 960, 1.10, 1.20, 1.25, 1.05)]
    _mkdb(p, candles=good, ticks=ticks)
    s = data_audit.candle_tick_consistency(data_audit._ro(p), "EURUSD_otc", 60)
    assert s["ok"] and s["mismatch"] == 0, s
    os.unlink(p)

    # now a stored candle that disagrees with the ticks
    p = _tmp()
    bad = [_c("EURUSD_otc", 60, 960, 1.99, 1.20, 1.25, 1.05)]
    _mkdb(p, candles=bad, ticks=ticks)
    s = data_audit.candle_tick_consistency(data_audit._ro(p), "EURUSD_otc", 60)
    assert s["mismatch"] == 1 and not s["ok"], s
    os.unlink(p)
    print("PASS test_consistency_match_and_mismatch")


def test_series_gaps():
    p = _tmp()
    _mkdb(p, candles=[_c("EURUSD_otc", 60, 960, 1.1, 1.2, 1.25, 1.05),
                      _c("EURUSD_otc", 60, 1020, 1.2, 1.15, 1.22, 1.1),
                      _c("EURUSD_otc", 60, 5000, 1.1, 1.2, 1.25, 1.05)])
    g = data_audit.series_gaps(data_audit._ro(p), "EURUSD_otc", 60)
    assert g["n"] == 3 and g["gaps"] == 1, g
    assert g["max_gap_s"] == 3980, g
    assert g["recent_run"] == 1, g
    os.unlink(p)
    print("PASS test_series_gaps")


def test_audit_never_writes():
    """The audit opens the DB read-only, so a write attempt must be refused."""
    p = _tmp()
    _mkdb(p, candles=[_c("EURUSD_otc", 60, 960, 1.1, 1.2, 1.25, 1.05)])
    db = data_audit._ro(p)
    try:
        db.execute("DELETE FROM candles")
        raised = False
    except sqlite3.OperationalError:
        raised = True
    db.close()
    os.unlink(p)
    assert raised, "read-only connection allowed a write"
    print("PASS test_audit_never_writes")


if __name__ == "__main__":
    test_clean_store_passes()
    test_invalid_ohlc_detected()
    test_misaligned_time_detected()
    test_source_mismatch_detected()
    test_conflicting_tick_ts_detected()
    test_future_tick_detected()
    test_frozen_rate_by_source()
    test_consistency_match_and_mismatch()
    test_series_gaps()
    test_audit_never_writes()
    print("\nall data_audit tests passed")
