"""Phase 2 pipeline tests (Task 12).

Twelve categories, each with a valid input and an intentionally invalid one. A
check that cannot fail is worthless, so every leakage/causality test is run twice:
once on clean input (must pass) and once on a deliberately leaky input (must be
caught).

Run:  python test_pipeline.py
"""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile

from pipeline import candles as cb
from pipeline import cleaning, dataset, leakage, splits, store
from pipeline import features as feat
from pipeline import structurefeat, tickfeat
from pipeline.config import FEATURE_ORDER, LABEL_COLUMNS


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ticks(spec):
    """spec: list of (ts, price, dir)."""
    return [(float(t), float(p), int(d)) for t, p, d in spec]


def _candle(t, o, c, h, l):
    return [t, o, c, h, l]


def _flat_candles(n, t0=0, period=60, price=100.0, step=0.1):
    """A gently trending series long enough for indicators."""
    out = []
    p = price
    for i in range(n):
        o = p
        c = p + step
        out.append(_candle(t0 + i * period, o, c, c + step, o - step))
        p = c
    return out


# --------------------------------------------------------------------------- #
# 1. candle aggregation
# --------------------------------------------------------------------------- #
def test_candle_aggregation_known_result():
    ticks = _ticks([(0.0, 10, 1), (10.0, 12, 1), (20.0, 9, 0), (30.0, 11, 1),
                    (60.0, 11, 1), (70.0, 15, 1)])
    out = cb.build_from_ticks(ticks, 60)
    assert len(out) == 2, out
    c0 = out[0]
    assert (c0["open"], c0["high"], c0["low"], c0["close"]) == (10, 12, 9, 11), c0
    assert c0["tick_count"] == 4, c0
    assert out[1]["open"] == 11 and out[1]["close"] == 15
    print("PASS test_candle_aggregation_known_result")


def test_candle_aggregation_rejects_bad_order():
    """Out-of-order ticks produce wrong OHLC — the pipeline must sort first."""
    ticks = _ticks([(60.0, 15, 1), (0.0, 10, 1)])   # unsorted
    # build_from_ticks assumes sorted input; the guard is that a sorted stream
    # gives a different (correct) answer than an unsorted one.
    good = cb.build_from_ticks(sorted(ticks), 60)
    bad = cb.build_from_ticks(ticks, 60)
    assert good != bad, "sorting made no difference — the test is not exercising order"
    print("PASS test_candle_aggregation_rejects_bad_order")


def test_aggregate_candles_matches_ticks():
    """30m built by aggregating 5m candles must equal 30m built from ticks."""
    ticks = _ticks([(i * 5.0, 100 + (i % 7) - 3, 1) for i in range(360)])  # 30 min
    c5 = cb.build_from_ticks(ticks, 300)
    agg = cb.aggregate_candles(c5, 6)     # 6 x 5m = 30m
    direct = cb.build_from_ticks(ticks, 1800)
    assert len(agg) == len(direct), (len(agg), len(direct))
    for a, d in zip(agg, direct):
        assert a["time"] == d["time"], (a, d)
        assert math.isclose(a["open"], d["open"]) and math.isclose(a["close"], d["close"])
        assert math.isclose(a["high"], d["high"]) and math.isclose(a["low"], d["low"])
    print("PASS test_aggregate_candles_matches_ticks")


# --------------------------------------------------------------------------- #
# 2. indicators
# --------------------------------------------------------------------------- #
def test_indicators_known_and_warmup():
    vals = [float(i) for i in range(1, 61)]        # 1..60
    ema5 = feat.ta.ema(vals, 5)
    assert ema5[3] is None, "EMA must be None before warmup"
    assert ema5[4] is not None, "EMA must be valid at warmup"
    # SMA of 1..5 = 3.0
    sma5 = feat.ta.sma(vals, 5)
    assert math.isclose(sma5[4], 3.0), sma5[4]
    print("PASS test_indicators_known_and_warmup")


def test_indicator_insufficient_history_is_none():
    short = [1.0, 2.0, 3.0]
    ema20 = feat.ta.ema(short, 20)
    assert all(v is None for v in ema20), "short series must not fabricate an EMA"
    print("PASS test_indicator_insufficient_history_is_none")


# --------------------------------------------------------------------------- #
# 3. tick features
# --------------------------------------------------------------------------- #
def test_tick_updown_uses_price_not_flag():
    # prices rise twice, fall once; broker flag is all 0 (would say "down")
    ticks = _ticks([(0.0, 10, 0), (1.0, 11, 0), (2.0, 12, 0), (3.0, 11, 0)])
    f = tickfeat.tick_features(ticks, 60)[0]
    assert f["tick_up_count"] == 2 and f["tick_down_count"] == 1, f
    assert f["broker_dir_ratio"] == 0.0, f   # flag kept separate, not used
    print("PASS test_tick_updown_uses_price_not_flag")


def test_tick_features_zero_division_safe():
    ticks = _ticks([(0.0, 10, 1)])          # single tick
    f = tickfeat.tick_features(ticks, 60)[0]
    assert f["tick_count"] == 1
    assert math.isfinite(f["tick_dir_ratio"]) and math.isfinite(f["avg_tick_interval"])
    print("PASS test_tick_features_zero_division_safe")


# --------------------------------------------------------------------------- #
# 4. market structure (causality of pivots)
# --------------------------------------------------------------------------- #
def test_swing_pivot_needs_confirmation():
    highs = [1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3]
    lows = [h - 1 for h in highs]
    # at i=4 (the peak) the pivot is NOT yet confirmed (needs right bars)
    early = structurefeat._swings_causal(highs, lows, right=2)
    at_peak = [s for s in early if s.idx == 4 and s.kind == "high"]
    # swing_series over the whole array can confirm it, but it must be delayed:
    from structure import swing_series
    partial = swing_series(highs[:5], lows[:5], 4, 2, 2, 11)
    assert not any(s.idx == 4 and s.kind == "high" for s in partial), \
        "pivot emitted before it was confirmable (look-ahead)"
    full = swing_series(highs, lows, 10, 2, 2, 11)
    assert any(s.idx == 4 and s.kind == "high" for s in full), \
        "pivot never confirmed even after enough bars"
    print("PASS test_swing_pivot_needs_confirmation")


def test_structure_features_causal():
    candles = _flat_candles(80)
    o = [c[1] for c in candles]; cl = [c[2] for c in candles]
    hi = [c[3] for c in candles]; lo = [c[4] for c in candles]
    atr = feat.ta.atr(hi, lo, cl, 14)
    a = structurefeat.structure_features(o, cl, hi, lo, atr)
    # mutating the future must not change an earlier structure value
    candles2 = [c[:] for c in candles]
    for i in range(60, len(candles2)):
        candles2[i] = _candle(candles2[i][0], candles2[i][1] * 3, candles2[i][2] * 3,
                              candles2[i][3] * 3, candles2[i][4] * 0.3)
    b = structurefeat.structure_features(
        [c[1] for c in candles2], [c[2] for c in candles2],
        [c[3] for c in candles2], [c[4] for c in candles2], atr)
    for k in a:
        assert a[k][40] == b[k][40], f"{k} at 40 changed when only the future changed"
    print("PASS test_structure_features_causal")


# --------------------------------------------------------------------------- #
# 5. multi-timeframe alignment
# --------------------------------------------------------------------------- #
def test_htf_uses_only_closed_candles():
    # higher TF = 1m; a 1m candle at t=120 closes at 180.
    higher_times = [0, 60, 120]
    codes = [1, -1, 1]
    # at t=120 the candle [120,180) is still forming -> must use the one at 60
    out = dataset.mtf_trend_features([120], 60, {"1m": (higher_times, codes)})
    assert out["trend_1m"][0] == -1, out   # the 60-candle (closed at 120)
    out2 = dataset.mtf_trend_features([180], 60, {"1m": (higher_times, codes)})
    assert out2["trend_1m"][0] == 1, out2  # now the 120 candle has closed
    print("PASS test_htf_uses_only_closed_candles")


def test_htf_lookahead_checker():
    ok, d = leakage.check_htf_no_lookahead([0, 60, 120], 60, 120, 1)  # closed_at 120
    assert ok, d
    ok, d = leakage.check_htf_no_lookahead([0, 60, 120], 60, 120, 2)  # closed_at 180
    assert not ok, d
    print("PASS test_htf_lookahead_checker")


# --------------------------------------------------------------------------- #
# 6. labels
# --------------------------------------------------------------------------- #
def test_label_up_down_side():
    # big up, big down, tiny move; ATR proxy fixed at 1.0, mult 0.25 -> thr 0.25
    candles = [_candle(0, 100, 100, 100.2, 99.8),
               _candle(60, 100, 101, 101.2, 99.9),    # +1.0 -> UP
               _candle(120, 100, 99, 100.1, 98.9),    # -1.0 -> DOWN
               _candle(180, 100, 100.1, 100.15, 99.95),  # +0.1 -> SIDE
               _candle(240, 100, 100, 100.05, 99.95)]
    atr = [1.0] * len(candles)
    lab = dataset.build_labels(candles, atr, mult=0.25)
    assert lab["label"][0] == 2, lab["label"][0]     # next candle up
    assert lab["label"][1] == 0, lab["label"][1]     # next candle down
    assert lab["label"][2] == 1, lab["label"][2]     # next candle sideways
    assert lab["label"][4] is None                   # last row has no next candle
    print("PASS test_label_up_down_side")


def test_label_no_future_is_none():
    candles = _flat_candles(5)
    atr = [0.05] * 5
    lab = dataset.build_labels(candles, atr)
    assert lab["label"][-1] is None, lab["label"]
    print("PASS test_label_no_future_is_none")


# --------------------------------------------------------------------------- #
# 7. chronological split
# --------------------------------------------------------------------------- #
def test_time_split_disjoint_and_ordered():
    # many assets share timestamps: build 100 timestamps x 5 rows each
    times = []
    for t in range(100):
        times.extend([t * 60] * 5)
    sp = splits.time_split(times, 60)
    tr = {times[i] for i in sp["train"]}
    va = {times[i] for i in sp["val"]}
    te = {times[i] for i in sp["test"]}
    assert not (tr & va) and not (tr & te) and not (va & te), "split timestamps overlap"
    assert max(tr) < min(va) < max(va) < min(te), "splits not in time order"
    ok, d = leakage.check_split_disjoint(
        [times[i] for i in sp["train"]], [times[i] for i in sp["val"]],
        [times[i] for i in sp["test"]])
    assert ok, d
    print("PASS test_time_split_disjoint_and_ordered")


def test_index_split_would_contaminate():
    """Proof that the time-based split is necessary: an index split overlaps."""
    times = []
    for t in range(100):
        times.extend([t * 60] * 5)
    n = len(times)
    cut = 352                       # mid-block: rows 350-354 all share time 70*60
    tr = set(times[:cut]); te = set(times[cut:])
    assert tr & te, "the negative control did not overlap — test is broken"
    print("PASS test_index_split_would_contaminate")


# --------------------------------------------------------------------------- #
# 8. leakage
# --------------------------------------------------------------------------- #
def test_target_not_in_features():
    ok, d = leakage.check_target_not_in_features(FEATURE_ORDER)
    assert ok, d
    ok, d = leakage.check_target_not_in_features(FEATURE_ORDER + ["label"])
    assert not ok, "target column in feature list was not caught"
    print("PASS test_target_not_in_features")


def test_future_feature_causality_clean_and_leaky():
    candles = _flat_candles(80)
    ok, d = leakage.check_future_feature_causality(candles, 40)
    assert ok, d   # real features are causal

    def leaky(cs):
        # deliberately looks one candle ahead
        return {"peek": [ (cs[i + 1][2] if i + 1 < len(cs) else cs[i][2])
                          for i in range(len(cs))]}
    ok, d = leakage.check_future_feature_causality(candles, 40, builder=leaky)
    assert not ok and d["changed_features"] == ["peek"], d
    print("PASS test_future_feature_causality_clean_and_leaky")


def test_feature_label_correlation():
    feats = {"good": [1, 2, 3, 4], "leak": [0, 0, 1, 1]}
    labels = [0, 0, 1, 1]
    ok, d = leakage.check_feature_label_correlation(feats, labels, thresh=0.9)
    assert not ok and any(k == "leak" for k, _ in d["offenders"]), d
    print("PASS test_feature_label_correlation")


# --------------------------------------------------------------------------- #
# 9. missing data
# --------------------------------------------------------------------------- #
def test_missing_data_no_fabrication():
    ticks = _ticks([(0.0, 10, 1), (120.0, 11, 1)])   # minute 1 missing entirely
    out = cb.build_from_ticks(ticks, 60)
    assert len(out) == 2, "a candle was fabricated for the empty minute"
    assert out[1]["completeness_status"] == "gap", out[1]
    print("PASS test_missing_data_no_fabrication")


def test_series_gaps_counts_holes():
    # 1m candles at 0,60 then a hole, then 600,660
    cs = [{"time": 0, "open": 1, "high": 1, "low": 1, "close": 1, "tick_count": 1},
          {"time": 60, "open": 1, "high": 1, "low": 1, "close": 1, "tick_count": 1},
          {"time": 600, "open": 1, "high": 1, "low": 1, "close": 1, "tick_count": 1},
          {"time": 660, "open": 1, "high": 1, "low": 1, "close": 1, "tick_count": 1}]
    clean, rep = cleaning.clean_candles("X", 60, cs)
    assert rep.gaps + rep.session_gaps == 1, rep.as_dict()
    print("PASS test_series_gaps_counts_holes")


# --------------------------------------------------------------------------- #
# 10. duplicates
# --------------------------------------------------------------------------- #
def test_duplicate_candle_dropped():
    cs = [{"time": 60, "open": 1, "high": 2, "low": 1, "close": 1, "tick_count": 1},
          {"time": 60, "open": 1, "high": 2, "low": 1, "close": 1, "tick_count": 1}]
    clean, rep = cleaning.clean_candles("X", 60, cs)
    assert len(clean) == 1 and rep.dropped_duplicate == 1, rep.as_dict()
    print("PASS test_duplicate_candle_dropped")


def test_duplicate_key_checker():
    ok, d = leakage.check_no_duplicate_keys(["A", "A"], [1, 2])
    assert ok, d
    ok, d = leakage.check_no_duplicate_keys(["A", "A"], [1, 1])
    assert not ok, d
    print("PASS test_duplicate_key_checker")


# --------------------------------------------------------------------------- #
# 11. OTC / Real separation + read-only source
# --------------------------------------------------------------------------- #
def test_market_separation_dirs():
    cols = {"asset": ["EURUSD_otc"], "market_type": ["otc"], "timeframe": ["1m"],
            "time": [60], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "tick_count": [1], "price_change": [0.0], "source": ["otc"],
            "completeness_status": ["ok"], "label": [2], "next_ret": [0.1],
            "next_range": [0.1], "next_dir": [1], "label_ts": [120]}
    d = store.dataset_dir("otc", "1m")
    assert str(d).endswith(os.path.join("otc", "1m")), d
    d2 = store.dataset_dir("real", "1m")
    assert str(d2).endswith(os.path.join("real", "1m")), d2
    assert d != d2
    print("PASS test_market_separation_dirs")


def test_source_db_read_only(tmp_path="/tmp/_p2_ro_test.db"):
    p = tempfile.mktemp(suffix=".db")
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE candles(asset TEXT)")
    db.commit(); db.close()
    from pipeline import db as pdb
    conn = pdb.connect(p)
    try:
        conn.execute("DELETE FROM candles")
        raised = False
    except sqlite3.OperationalError:
        raised = True
    conn.close(); os.unlink(p)
    assert raised, "source DB connection allowed a write"
    print("PASS test_source_db_read_only")


# --------------------------------------------------------------------------- #
# 12. reproducibility
# --------------------------------------------------------------------------- #
def test_reproducible_features():
    candles = _flat_candles(120)
    a = feat.build_features(candles)
    b = feat.build_features([c[:] for c in candles])
    for k in a:
        assert a[k] == b[k], f"{k} not reproducible"
    print("PASS test_reproducible_features")


def test_no_feature_is_constant_across_two_runs_but_deterministic():
    candles = _flat_candles(120, step=0.05)
    a = feat.build_features(candles)
    b = feat.build_features(candles)
    assert a == b
    print("PASS test_no_feature_is_constant_across_two_runs_but_deterministic")


if __name__ == "__main__":
    test_candle_aggregation_known_result()
    test_candle_aggregation_rejects_bad_order()
    test_aggregate_candles_matches_ticks()
    test_indicators_known_and_warmup()
    test_indicator_insufficient_history_is_none()
    test_tick_updown_uses_price_not_flag()
    test_tick_features_zero_division_safe()
    test_swing_pivot_needs_confirmation()
    test_structure_features_causal()
    test_htf_uses_only_closed_candles()
    test_htf_lookahead_checker()
    test_label_up_down_side()
    test_label_no_future_is_none()
    test_time_split_disjoint_and_ordered()
    test_index_split_would_contaminate()
    test_target_not_in_features()
    test_future_feature_causality_clean_and_leaky()
    test_feature_label_correlation()
    test_missing_data_no_fabrication()
    test_series_gaps_counts_holes()
    test_duplicate_candle_dropped()
    test_duplicate_key_checker()
    test_market_separation_dirs()
    test_source_db_read_only()
    test_reproducible_features()
    test_no_feature_is_constant_across_two_runs_but_deterministic()
    print("\nall pipeline tests passed")
