#!/usr/bin/env python3
"""Tests for ml_engine: feature causality, honest walk-forward (negative control
on a random walk, positive control on a real momentum pattern), the train bundle
shape, and the background job manager. No network: `market_data.fetch` is
monkeypatched with synthetic candles.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import market_data as md
import ml_engine as ml


def _synth(n, seed, drift=0.0, wave=0.0):
    """Synthetic OHLC. drift>0 -> trending (learnable); wave=0,drift=0 -> walk."""
    rng = np.random.default_rng(seed)
    price = 1.0
    rows = []
    for i in range(n):
        step = rng.normal(0, 0.0015)
        if wave:
            step += wave * np.sin(i / 6.0)          # a real, learnable pattern
        if drift:
            step += drift
        o = price
        c = price + step
        h = max(o, c) + abs(rng.normal(0, 0.0005))
        lo = min(o, c) - abs(rng.normal(0, 0.0005))
        rows.append([i * 3600, o, c, h, lo])
        price = c
    return rows


def test_features_shape_and_labels():
    candles = _synth(400, seed=1)
    X, y, idx = ml.build_features(candles)
    assert X.shape[1] == len(ml.FEATURES), X.shape
    # X carries one extra "live" row (the newest candle, no label yet)
    assert len(X) == len(y) + 1 == len(idx), (len(X), len(y), len(idx))
    assert set(np.unique(y)) <= {0, 1}
    # label must be "did the NEXT close rise", aligned to the labeled rows
    for k in range(len(y)):
        i = idx[k]
        assert y[k] == (1 if candles[i + 1][2] > candles[i][2] else 0), (k, i)
    # the final row is the newest candle (the live prediction row)
    assert idx[-1] == len(candles) - 1, (idx[-1], len(candles) - 1)
    assert np.isfinite(X).all(), "features must be finite"
    print(f"PASS test_features_shape_and_labels ({X.shape})")


def test_signal_targets_next_candle():
    """The live signal must be about the not-yet-closed candle, not a closed one."""
    candles = _synth(500, seed=21, wave=0.0010)
    X, y, idx = ml.build_features(candles)
    # the row used for the live call is the last feature row = newest candle
    live_row = X[-1:]
    assert live_row.shape[0] == 1
    assert idx[-1] == len(candles) - 1
    # and it is NOT a row that already has a realized label
    assert len(y) == len(idx) - 1
    print("PASS test_signal_targets_next_candle")


def test_features_are_causal():
    """Changing future candles must not change any past feature row."""
    candles = _synth(400, seed=2)
    X1, _, idx1 = ml.build_features(candles)
    # mutate everything after row 250
    mutated = [list(c) for c in candles]
    for i in range(250, len(mutated)):
        mutated[i][1] += 5.0
        mutated[i][2] += 5.0
        mutated[i][3] += 5.0
        mutated[i][4] += 5.0
    X2, _, idx2 = ml.build_features(mutated)
    # rows whose feature uses only candles [0..i] with i < 249 must be identical
    keep = [k for k, i in enumerate(idx1) if i < 248]
    assert len(keep) > 50
    for k in keep:
        assert np.allclose(X1[k], X2[k]), f"row {k} (candle {idx1[k]}) leaked future data"
    print(f"PASS test_features_are_causal ({len(keep)} rows checked)")


def test_walk_forward_negative_control():
    """A pure random walk must NOT show an edge (the pipeline must not fake one)."""
    candles = _synth(4000, seed=7)
    X, y, _ = ml.build_features(candles)
    vw = ml.walk_forward(X, y)
    assert vw["n_folds"] >= 3, vw
    assert abs(vw["mean_edge"]) < 0.05, vw
    print(f"PASS test_walk_forward_negative_control (edge={vw['mean_edge']:+.3f})")


def test_walk_forward_positive_control():
    """A real, learnable pattern MUST be detected — proves the code can learn."""
    candles = _synth(6000, seed=3, wave=0.0012)
    X, y, _ = ml.build_features(candles)
    vw = ml.walk_forward(X, y)
    assert vw["mean_edge"] > 0.10, vw
    assert vw["mean_auc"] and vw["mean_auc"] > 0.6, vw
    print(f"PASS test_walk_forward_positive_control (edge={vw['mean_edge']:+.3f}, "
          f"auc={vw['mean_auc']})")


def test_train_bundle_shape():
    md.fetch = lambda name, tf, use_cache=True: (  # monkeypatch: no network
        _synth(3000, seed=11, wave=0.0010), md.Quality(bars=3000, ok=True, score=1.0))
    ml._CACHE.clear()
    b = ml.train("EURUSD", "1h", force=True)
    assert b["signal"]["action"] in ("UP", "DOWN")
    assert 0.0 <= b["signal"]["confidence"] <= 1.0
    assert b["verdict"] in ("EDGE DETECTED", "NO EDGE (≈ random)",
                            "BELOW BASELINE", "NOT ENOUGH DATA")
    assert b["samples"] > 0 and b["recent"] and b["importances"]
    assert b["validation"]["n_folds"] >= 3
    print(f"PASS test_train_bundle_shape ({b['signal']['action']}, "
          f"verdict={b['verdict']})")


def test_train_uses_cache():
    md.fetch = lambda name, tf, use_cache=True: (
        _synth(3000, seed=12, wave=0.0010), md.Quality(bars=3000, ok=True, score=1.0))
    ml._CACHE.clear()
    ml.train("GBPUSD", "1h", force=True)
    assert ml.cache_info(), "expected a cached entry after training"
    # a second call without force must hit the cache (same trained_at)
    first = ml.train("GBPUSD", "1h", force=False)
    second = ml.train("GBPUSD", "1h", force=False)
    assert first["trained_at"] == second["trained_at"], "cache not reused"
    print("PASS test_train_uses_cache")


def test_train_rejects_bad_quality():
    md.fetch = lambda name, tf, use_cache=True: (
        _synth(500, seed=13), md.Quality(bars=500, ok=False, score=0.1,
                                         notes=["broken source bars"]))
    ml._CACHE.clear()
    try:
        ml.train("USDJPY", "1h", force=True)
    except md.DataError:
        print("PASS test_train_rejects_bad_quality")
        return
    raise AssertionError("expected DataError when quality.ok is False")


def test_job_manager():
    md.fetch = lambda name, tf, use_cache=True: (
        _synth(3000, seed=14, wave=0.0010), md.Quality(bars=3000, ok=True, score=1.0))
    ml._CACHE.clear()
    jid = ml.start_job("AUDUSD", "1h", force=True)
    import time
    for _ in range(120):
        st = ml.job_status(jid)
        if st["state"] != "running":
            break
        time.sleep(0.25)
    st = ml.job_status(jid)
    assert st["state"] == "done", st
    assert st["progress"] == 1.0
    assert st["result"]["signal"]["action"] in ("UP", "DOWN")
    assert ml.job_status("nope") is None
    print("PASS test_job_manager")


def test_consensus_all_timeframes():
    md.fetch = lambda name, tf, use_cache=True: (
        _synth(3000, seed=15, wave=0.0010), md.Quality(bars=3000, ok=True, score=1.0))
    ml._CACHE.clear()
    b = ml.train_all("EURUSD", tfs=["1h", "1d"], force=True)
    assert b["tf"] == "all"
    assert b["signal"]["action"] in ("UP", "DOWN")
    assert len(b["per_timeframe"]) == 2
    assert b["signal"]["agree"].endswith("timeframes")
    print(f"PASS test_consensus_all_timeframes ({b['signal']['agree']}, "
          f"action={b['signal']['action']})")


if __name__ == "__main__":
    test_features_shape_and_labels()
    test_signal_targets_next_candle()
    test_features_are_causal()
    test_walk_forward_negative_control()
    test_walk_forward_positive_control()
    test_train_bundle_shape()
    test_train_uses_cache()
    test_train_rejects_bad_quality()
    test_job_manager()
    test_consensus_all_timeframes()
    print("\nAll ml_engine tests passed.")
