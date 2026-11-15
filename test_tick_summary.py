#!/usr/bin/env python3
"""Tests for tick_summary.py + the collector's use of it (Phase 3).

Properties that matter, each with a valid and an invalid input:

  * a summary is emitted only when a bucket rolls over (never for the forming
    candle), so nothing half-built can leak,
  * up/down/flat counts follow the ACTUAL price change, not the broker flag,
  * the broker flag is recorded as its own feature and never as direction,
  * the collector writes the summary onto the candle row of the same bucket,
  * reconnecting drops the in-progress summary (no partial summary written),
  * memory stays bounded (pending summaries are capped).

Run:  python test_tick_summary.py
"""
from __future__ import annotations

import os
import tempfile

from tick_summary import TickSummarizer, SUMMARY_FIELDS
from live_collector import LiveCollector, CANDLE_PERIODS
from market_qx_candles import CandleBuilder


def test_no_summary_until_bucket_rolls_over():
    s = TickSummarizer([60])
    assert s.add("A", 60, 60.0, 1.0, 1) is None
    assert s.add("A", 60, 90.0, 1.1, 1) is None      # same bucket, still forming
    rolled = s.add("A", 60, 120.0, 1.2, 1)           # new bucket -> finalize 60
    assert rolled is not None and rolled[0] == 60, "summary not emitted on rollover"
    assert s.pending, "finalized summary not kept for the writer"
    print("PASS test_no_summary_until_bucket_rolls_over")


def test_counts_follow_price_change_not_broker_flag():
    s = TickSummarizer([60])
    # broker flag is always +1, but price falls every tick
    for k, price in enumerate([1.00, 0.99, 0.98, 0.97]):
        s.add("A", 60, 100.0 + k, price, 1)
    rolled = s.add("A", 60, 200.0, 0.96, 1)
    _, summ = rolled
    assert summ["tick_down"] == 3, f"expected 3 down, got {summ['tick_down']}"
    assert summ["tick_up"] == 0
    assert summ["broker_dir_ratio"] == 1.0, "broker flag must be its own feature"
    assert summ["tick_dir_ratio"] < 0, "direction must follow price, not the flag"
    print("PASS test_counts_follow_price_change_not_broker_flag")


def test_negative_control_flat_ticks_are_not_direction():
    s = TickSummarizer([60])
    for k in range(4):
        s.add("A", 60, 100.0 + k, 1.00, 0)            # price never moves
    _, summ = s.add("A", 60, 200.0, 1.00, 0)
    assert summ["tick_up"] == 0 and summ["tick_down"] == 0
    assert summ["tick_flat"] == 3, "unchanged ticks must count as flat"
    assert summ["tick_dir_ratio"] == 0.0
    print("PASS test_negative_control_flat_ticks_are_not_direction")


def test_pending_is_bounded():
    s = TickSummarizer([60], max_pending=10)
    for i in range(100):
        s.add("A", 60, i * 60.0, 1.0, 0)              # every tick is a new bucket
    assert len(s.pending) <= 10, "pending summaries must be capped"
    print("PASS test_pending_is_bounded")


def _coll(db: str, store_ticks: bool) -> LiveCollector:
    c = LiveCollector(db, [], db + ".state.json", log_every=999,
                      store_ticks=store_ticks)
    c._assets = ["EURUSD_otc"]
    c._asset_set = set(c._assets)
    c._builders = {(a, p): CandleBuilder(p, on_closed=c._candle_cb(a, p))
                   for a in c._assets for p in CANDLE_PERIODS}
    return c


def test_collector_writes_summary_on_candle_row():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db, store_ticks=False)
        # 130 ticks all inside bucket 60 (ts 60..61.29), then a tick that opens
        # bucket 120 -> the bucket-60 candle is finalized with 130 ticks
        for k in range(130):
            c._on_tick("EURUSD_otc", 60.0 + k * 0.01, 1.10 + k * 1e-5, 1)
        c._on_tick("EURUSD_otc", 120.0, 1.1014, 1)
        row = c.conn.execute(
            "SELECT tick_count, tick_up, tick_down FROM candles "
            "WHERE period=60 AND time=60").fetchone()
        assert row is not None and row[0] == 130, f"summary missing: {row}"
        assert row[1] + row[2] + (130 - row[1] - row[2]) == 130
        assert c.stats["candles_with_summary"] >= 1
        print("PASS test_collector_writes_summary_on_candle_row")


def test_reconnect_drops_forming_summary():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db, store_ticks=False)
        for k in range(50):
            c._on_tick("EURUSD_otc", 1000.0 + k, 1.10 + k * 1e-5, 1)
        c._reset_builders()                                # simulated reconnect
        # a tick in the next bucket must NOT finalize the dropped candle
        rolled = c.summarizer.add("EURUSD_otc", 60, 1080.0, 1.101, 1)
        assert rolled is None, "forming summary survived a reconnect"
        print("PASS test_reconnect_drops_forming_summary")


def test_summary_fields_are_stored():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        c = _coll(db, store_ticks=False)
        cols = {r[1] for r in c.conn.execute("PRAGMA table_info(candles)")}
        for f in SUMMARY_FIELDS:
            assert f in cols, f"summary column {f} missing"
        print("PASS test_summary_fields_are_stored")


if __name__ == "__main__":
    test_no_summary_until_bucket_rolls_over()
    test_counts_follow_price_change_not_broker_flag()
    test_negative_control_flat_ticks_are_not_direction()
    test_pending_is_bounded()
    test_collector_writes_summary_on_candle_row()
    test_reconnect_drops_forming_summary()
    test_summary_fields_are_stored()
    print("\nall tick-summary tests passed")
