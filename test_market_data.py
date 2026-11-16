#!/usr/bin/env python3
"""Tests for market_data: aggregation, cleaning, the open==close defect gate,
and a synthetic end-to-end fetch. Network calls are exercised once, guarded, so
the suite still passes offline.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_data as md


def test_aggregate_ohlc():
    # four 1h bars -> one 4h bar: open=first open, close=last close,
    # high=max high, low=min low
    rows = [[0, 1.0, 1.2, 1.3, 0.9],
            [3600, 1.2, 1.1, 1.25, 1.05],
            [7200, 1.1, 1.4, 1.5, 1.1],
            [10800, 1.4, 1.35, 1.45, 1.3]]
    out = md.aggregate(rows, 14400)
    assert len(out) == 1, out
    t, o, c, h, lo = out[0]
    assert o == 1.0 and c == 1.35, (o, c)
    assert h == 1.5 and lo == 0.9, (h, lo)
    print("PASS test_aggregate_ohlc")


def test_aggregate_buckets_and_order():
    rows = [[i * 60, 1, 1, 1, 1] for i in range(10)]
    out = md.aggregate(rows, 300)
    assert [b[0] for b in out] == [0, 300], out
    assert all(b[0] % 300 == 0 for b in out)
    print("PASS test_aggregate_buckets_and_order")


def test_clean_drops_bad_bars():
    rows = [[100, 1.0, 1.1, 1.2, 0.9],
            [100, 1.0, 1.1, 1.2, 0.9],          # duplicate timestamp
            [200, 1.0, 1.1, 0.8, 0.9],          # high < low
            [300, 0.0, 1.1, 1.2, 0.9],          # non-positive price
            [400, 1.0, float("nan"), 1.2, 0.9], # NaN
            [500, 1.0, 1.1, 1.2, 0.9]]          # ok
    out, q = md.clean(rows)
    assert [r[0] for r in out] == [100, 500], out
    assert q.dropped == 4, q.dropped
    print("PASS test_clean_drops_bad_bars")


def test_clean_flags_broken_open_close():
    # a long run of bars where open==close is the Yahoo daily-FX defect; the
    # quality gate must mark the series not-ok so it is never trained on.
    rows = []
    for i in range(300):
        o = 1.0 + i * 0.001
        c = o                       # open == close (broken)
        h = o + 0.01
        lo = o - 0.01
        rows.append([i * 86400, o, c, h, lo])
    _, q = md.clean(rows)
    assert q.oc_equal_ratio > 0.45, q.oc_equal_ratio
    assert q.ok is False, q.as_dict()
    assert any("open==close" in n for n in q.notes), q.notes
    print("PASS test_clean_flags_broken_open_close")


def test_clean_accepts_healthy_series():
    # a healthy series (open != close on most bars) must pass
    rows = []
    p = 1.0
    for i in range(300):
        o = p
        c = p + 0.001
        h = max(o, c) + 0.002
        lo = min(o, c) - 0.002
        rows.append([i * 3600, o, c, h, lo])
        p = c
    _, q = md.clean(rows)
    assert q.oc_equal_ratio == 0.0, q.oc_equal_ratio
    assert q.ok is True, q.as_dict()
    print("PASS test_clean_accepts_healthy_series")


def test_daily_plan_aggregates_from_hourly():
    # the fix: '1d' must be built by aggregating hourly bars, not native daily
    interval, rng, agg = md.TIMEFRAME_PLAN["1d"]
    assert interval == "1h" and agg == 86400, md.TIMEFRAME_PLAN["1d"]
    # and no timeframe should ever request a broken native daily feed
    for tf, (iv, _r, _a) in md.TIMEFRAME_PLAN.items():
        assert not (iv == "1d"), f"{tf} still requests native daily bars"
    print("PASS test_daily_plan_aggregates_from_hourly")


def test_catalog_shape():
    groups = md.catalog()
    assert groups and all({"type", "symbols"} <= set(g) for g in groups)
    syms = [s for g in groups for s in g["symbols"]]
    assert "EURUSD" in syms and "BTCUSD" in syms, syms[:5]
    assert len(syms) == len(set(syms)), "duplicate instruments in catalog"
    print("PASS test_catalog_shape")


def test_unknown_pair_raises():
    try:
        md.download("NOTAPAIR", "1h")
    except md.DataError:
        print("PASS test_unknown_pair_raises")
        return
    raise AssertionError("expected DataError for unknown pair")


def test_live_fetch_optional():
    """Real network fetch. Skipped (not failed) when the network is unavailable."""
    try:
        cs, q = md.fetch("EURUSD", "1h", use_cache=False)
    except Exception as exc:  # noqa: BLE001 - offline is fine
        print(f"SKIP test_live_fetch_optional ({type(exc).__name__})")
        return
    assert len(cs) > 500, len(cs)
    assert q.ok, q.as_dict()
    # daily must now be built from hourly and therefore be clean
    d, dq = md.fetch("EURUSD", "1d", use_cache=False)
    assert dq.oc_equal_ratio < 0.45, dq.as_dict()
    print(f"PASS test_live_fetch_optional ({len(cs)} 1h bars, "
          f"{len(d)} daily bars, oc={dq.oc_equal_ratio:.3f})")


if __name__ == "__main__":
    test_aggregate_ohlc()
    test_aggregate_buckets_and_order()
    test_clean_drops_bad_bars()
    test_clean_flags_broken_open_close()
    test_clean_accepts_healthy_series()
    test_daily_plan_aggregates_from_hourly()
    test_catalog_shape()
    test_unknown_pair_raises()
    test_live_fetch_optional()
    print("\nAll market_data tests passed.")
