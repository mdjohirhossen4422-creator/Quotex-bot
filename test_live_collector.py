#!/usr/bin/env python3
"""Tests for the live collector (Phase 2.5).

Every property that matters is tested with a valid input AND an intentionally
invalid one, so a check that cannot fail is exposed:

  * a replayed tick is dropped, but a *genuinely different* tick is kept,
  * a partial candle from across a reconnect is never written,
  * existing rows are never modified (append-only),
  * real and OTC stay separated in the stored `source`,
  * the schema is only ever extended additively.

The tests drive `LiveCollector._on_tick` / `_candle_cb` directly, so no network
connection is needed.

Run:  python test_live_collector.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from live_collector import LiveCollector, CANDLE_PERIODS
from market_qx_candles import CandleBuilder


def _coll(db: str, pairs: list[str] | None = None) -> LiveCollector:
    c = LiveCollector(db, pairs or [], db + ".state.json", log_every=999)
    c._assets = ["EURUSD_otc", "BTCUSD_otc", "EURUSD", "XAUUSD"]
    c._asset_set = set(c._assets)
    c._builders = {(a, p): CandleBuilder(p, on_closed=c._candle_cb(a, p))
                   for a in c._assets for p in CANDLE_PERIODS}
    return c


def _ticks(db: str, asset: str | None = None) -> list[tuple]:
    conn = sqlite3.connect(db)
    if asset:
        rows = conn.execute("SELECT asset,ts,price,dir FROM ticks WHERE asset=? "
                            "ORDER BY ts,price", (asset,)).fetchall()
    else:
        rows = conn.execute("SELECT asset,ts,price,dir FROM ticks "
                            "ORDER BY asset,ts,price").fetchall()
    conn.close()
    return [tuple(r) for r in rows]


# --------------------------------------------------------------------------- #
def test_replayed_tick_dropped_but_real_tick_kept():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db)
        c._on_tick("EURUSD_otc", 1000.0, 1.10, 1)
        c._on_tick("EURUSD_otc", 1000.0, 1.10, 1)          # exact replay -> drop
        c._on_tick("EURUSD_otc", 1000.0, 1.11, 1)          # same ts, new price -> keep
        c._on_tick("EURUSD_otc", 1001.0, 1.10, 1)          # new ts -> keep
        c._flush(wait=True)
        stored = _ticks(db, "EURUSD_otc")
        assert len(stored) == 3, stored
        assert c.stats["ticks_duplicate"] == 1, c.stats["ticks_duplicate"]
        assert ("EURUSD_otc", 1000.0, 1.11, 1) in stored
        print("PASS test_replayed_tick_dropped_but_real_tick_kept")


def test_dedup_survives_restart_reseed():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c1 = _coll(db)
        c1._on_tick("EURUSD_otc", 500.0, 2.0, 1)
        c1._on_tick("EURUSD_otc", 501.0, 2.1, 1)
        c1._flush(wait=True)
        # simulate a restart: brand-new collector seeds its dedup from the tail
        c2 = _coll(db)
        c2._on_tick("EURUSD_otc", 501.0, 2.1, 1)           # the last tick again -> drop
        c2._on_tick("EURUSD_otc", 502.0, 2.2, 1)           # genuinely new -> keep
        c2._flush(wait=True)
        stored = _ticks(db, "EURUSD_otc")
        assert len(stored) == 3, stored
        assert c2.stats["ticks_duplicate"] == 1
        print("PASS test_dedup_survives_restart_reseed")


def test_partial_candle_not_written_across_reconnect():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db)
        # a tick opens a 1m candle at t=120 but it never closes (reconnect)
        c._on_tick("EURUSD_otc", 120.0, 1.50, 1)
        c._flush(wait=True)
        assert c.conn.execute("SELECT COUNT(*) FROM candles WHERE period=60").fetchone()[0] == 0
        # reconnect: builders are rebuilt, the half-built candle is discarded
        before = c.stats["candles_dropped_partial"]
        c._reset_builders()
        assert c.stats["candles_dropped_partial"] > before, "partial candle not counted"
        # the next candle only appears once a later tick closes it
        c._on_tick("EURUSD_otc", 180.0, 1.60, 1)   # opens a candle at 180
        c._flush(wait=True)
        assert c.conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0, \
            "the dropped 120 candle was written anyway"
        c._on_tick("EURUSD_otc", 240.0, 1.70, 1)   # closes the 180 candle
        c._flush(wait=True)
        rows = c.conn.execute(
            "SELECT time,open,close,high,low FROM candles WHERE period=60").fetchall()
        assert rows == [(180, 1.60, 1.60, 1.60, 1.60)], rows
        print("PASS test_partial_candle_not_written_across_reconnect")


def test_candle_primary_key_ignores_duplicate():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db)
        cb = c._candle_cb("EURUSD_otc", 60)
        cb([120, 1.0, 1.1, 1.2, 0.9])
        cb([120, 1.0, 1.1, 1.2, 0.9])   # same (asset,period,time) -> ignored
        assert c.conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1
        assert c.stats["candles_saved"] == 1
        print("PASS test_candle_primary_key_ignores_duplicate")


def test_existing_rows_never_modified():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE ticks(asset TEXT, ts REAL, price REAL, dir INTEGER)")
        conn.execute("CREATE TABLE candles(asset TEXT, period INTEGER, time INTEGER,"
                     " open REAL, close REAL, high REAL, low REAL,"
                     " PRIMARY KEY(asset,period,time))")
        conn.execute("INSERT INTO ticks VALUES('EURUSD_otc',1.0,1.0,1)")
        conn.execute("INSERT INTO candles VALUES('EURUSD_otc',60,60,1,1,1,1)")
        conn.commit(); conn.close()

        c = _coll(db)
        c._on_tick("EURUSD_otc", 999.0, 3.0, 1)     # new tick appended
        c._flush(wait=True)
        c._candle_cb("EURUSD_otc", 60)([120, 2.0, 2.0, 2.0, 2.0])
        # the pre-existing rows are byte-identical, and new rows were appended
        assert _ticks(db, "EURUSD_otc")[0] == ("EURUSD_otc", 1.0, 1.0, 1)
        assert c.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 2
        assert c.conn.execute(
            "SELECT open FROM candles WHERE time=60").fetchone()[0] == 1
        assert c.conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 2
        print("PASS test_existing_rows_never_modified")


def test_market_separation_in_stored_source():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db)
        c._on_tick("EURUSD_otc", 10.0, 1.0, 1)
        c._on_tick("EURUSD", 10.0, 1.0, 1)
        c._on_tick("XAUUSD", 10.0, 1.0, 1)
        c._flush(wait=True)
        src = dict(c.conn.execute(
            "SELECT asset, source FROM ticks").fetchall())
        assert src["EURUSD_otc"] == "otc", src
        assert src["EURUSD"] == "real", src
        assert src["XAUUSD"] == "real", src
        print("PASS test_market_separation_in_stored_source")


def test_schema_is_extended_additively_only():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE ticks(asset TEXT, ts REAL, price REAL, dir INTEGER)")
        conn.commit(); conn.close()
        _coll(db)   # runs _ensure_schema
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ticks)")]
        conn.close()
        # the original four columns are intact and only additive ones appeared
        assert cols[:4] == ["asset", "ts", "price", "dir"], cols
        assert "source" in cols and "feed_id" in cols
        print("PASS test_schema_is_extended_additively_only")


def test_store_ticks_false_writes_candles_not_ticks():
    """--no-ticks mode: candles are built and stored, raw ticks are not."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = LiveCollector(db, [], db + ".state.json", log_every=999,
                          store_ticks=False)
        c._assets = ["EURUSD_otc"]
        c._asset_set = set(c._assets)
        c._builders = {(a, p): CandleBuilder(p, on_closed=c._candle_cb(a, p))
                       for a in c._assets for p in CANDLE_PERIODS}
        for k in range(120):
            c._on_tick("EURUSD_otc", 1000.0 + k, 1.0 + k * 1e-4, 1)
        c._flush(wait=True)
        assert _ticks(db, "EURUSD_otc") == [], "ticks were stored in --no-ticks mode"
        candles = c.conn.execute(
            "SELECT COUNT(*) FROM candles WHERE period=60").fetchone()[0]
        assert candles >= 1, "no candle built in --no-ticks mode"
        print("PASS test_store_ticks_false_writes_candles_not_ticks")


def test_negative_control_dedup_does_not_overdrop():
    """A guard that dedup cannot be too aggressive: distinct ticks all survive."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db)
        for k in range(50):
            c._on_tick("EURUSD_otc", 100.0 + k, 1.0 + k * 1e-4, k % 3 - 1)
        c._flush(wait=True)
        assert len(_ticks(db, "EURUSD_otc")) == 50, "dedup dropped distinct ticks"
        assert c.stats["ticks_duplicate"] == 0
        print("PASS test_negative_control_dedup_does_not_overdrop")


def test_embedding_stop_from_another_thread():
    """Embedding contract: run() must not crash off the main thread and a
    host app must be able to stop() it. No network is used — _connect is
    stubbed so the loop just idles and observes the stop flag."""
    import threading
    import time
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "e.db")
        c = LiveCollector(db, ["EURUSD_otc"], db + ".state.json", log_every=999)
        c._connect = lambda assets: False          # never touch the network
        c.max_backoff = 0.05
        t = threading.Thread(target=c.run, kwargs={"minutes": 0}, daemon=True)
        t.start()                                  # run() on a NON-main thread
        time.sleep(0.3)
        c.stop()                                   # host-app shutdown request
        t.join(timeout=10)
        assert not t.is_alive(), "run() did not stop after stop()"
        print("PASS test_embedding_stop_from_another_thread")


if __name__ == "__main__":
    test_replayed_tick_dropped_but_real_tick_kept()
    test_dedup_survives_restart_reseed()
    test_partial_candle_not_written_across_reconnect()
    test_candle_primary_key_ignores_duplicate()
    test_existing_rows_never_modified()
    test_market_separation_in_stored_source()
    test_schema_is_extended_additively_only()
    test_store_ticks_false_writes_candles_not_ticks()
    test_negative_control_dedup_does_not_overdrop()
    test_embedding_stop_from_another_thread()
    print("\nall live-collector tests passed")
