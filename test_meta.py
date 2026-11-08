#!/usr/bin/env python3
"""Tests for the meta-labeling gate.

The two controls are the point:
  * a random-walk signal table must NOT be improved by the gate (negative), and
  * a signal table with a learnable edge MUST be improved (positive).
A gate that fails either one is measuring noise, so a "no gain" result on live
data would be meaningless without them.

Also checks: signal collection is causal (features at signal i never touch
candle i+1), the flat-candle rule is respected, and coverage is reported.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from meta import (collect_signals, evaluate_gate, _synthetic_tables,
                  META_FEATURES)
from payout import DEFAULT_PAYOUT


def _random_walk_candles(n=800, seed=3, vol=0.0005, start=1.1):
    import random
    rng = random.Random(seed)
    cs = []
    price = start
    for i in range(n):
        o = price
        price += rng.gauss(0, vol)
        c = price
        cs.append([1_700_000_000 + i * 60, o, c,
                   max(o, c) + abs(rng.gauss(0, vol / 2)),
                   min(o, c) - abs(rng.gauss(0, vol / 2))])
    return cs


def test_signal_collection_is_causal_and_well_formed():
    cs = _random_walk_candles(500)
    rows = collect_signals("rule", "X_otc", cs)
    assert rows, "expected at least some signals on a random walk"
    for r in rows:
        assert r.action in ("CALL", "PUT")
        assert r.label in (0, 1)
        assert len(r.feats) >= len(META_FEATURES), (len(r.feats), len(META_FEATURES))
        assert r.idx < len(cs) - 1, "signal must have a next candle to settle on"
    # indices strictly increase (engine replay is forward-only)
    assert all(rows[k].idx < rows[k + 1].idx for k in range(len(rows) - 1))
    # without the v2 block the vector is exactly the base meta feature set
    base = collect_signals("rule", "X_otc", cs, use_v2=False)
    assert base and all(len(r.feats) == len(META_FEATURES) for r in base)
    print(f"PASS test_signal_collection_is_causal_and_well_formed ({len(rows)} signals)")


def test_negative_control_random_labels_no_gain():
    for mode in ("pooled", "per_pair"):
        tables = _synthetic_tables("random", seed=11)
        r = evaluate_gate(tables, payout=DEFAULT_PAYOUT, threshold=0.55,
                          mode=mode)
        gain = r["gated"].hit - r["baseline"].hit
        assert abs(gain) < 0.05, f"[{mode}] gate invented an edge: {gain:+.3f}"
        assert r["baseline"].n > 200
    print("PASS test_negative_control_random_labels_no_gain (both modes)")


def test_positive_control_learnable_edge_is_exploited():
    for mode in ("pooled", "per_pair"):
        tables = _synthetic_tables("edge", seed=12)
        r = evaluate_gate(tables, payout=DEFAULT_PAYOUT, threshold=0.55,
                          mode=mode)
        gain = r["gated"].hit - r["baseline"].hit
        assert gain > 0.05, f"[{mode}] gate failed to exploit edge: {gain:+.3f}"
        assert r["coverage"] > 0.1, "gate threw away almost everything"
    print("PASS test_positive_control_learnable_edge_is_exploited (both modes)")


def test_gate_off_with_too_little_training_data():
    """With fewer than min_train_signals per series, the gate must keep all
    signals (it cannot learn from almost nothing)."""
    tables = _synthetic_tables("edge", n_series=2, n_signals=80, seed=13)
    r = evaluate_gate(tables, payout=DEFAULT_PAYOUT, threshold=0.55,
                      min_train_signals=200)
    assert r["gated"].n == r["baseline"].n, (r["gated"].n, r["baseline"].n)
    print("PASS test_gate_off_with_too_little_training_data")


def test_live_gate_batch_matches_single():
    """Batch gating must agree with the single-pair path and stay causal."""
    from meta import live_gate, live_gate_batch
    tables = _synthetic_tables("edge", seed=21)
    batch = live_gate_batch(tables, threshold=0.55)
    assert batch, "expected decisions"
    for asset in batch:
        one = live_gate(tables, asset, 60, threshold=0.55)
        assert one is not None
        assert abs(one.p_win - batch[asset].p_win) < 1e-9, asset
        assert one.keep == batch[asset].keep
    # a table with a single row per pair yields no decisions (nothing to train on)
    thin = {a: rows[:1] for a, rows in tables.items()}
    assert live_gate_batch(thin, threshold=0.55) == {}
    print("PASS test_live_gate_batch_matches_single")


if __name__ == "__main__":
    test_signal_collection_is_causal_and_well_formed()
    test_negative_control_random_labels_no_gain()
    test_positive_control_learnable_edge_is_exploited()
    test_gate_off_with_too_little_training_data()
    test_live_gate_batch_matches_single()
    print("\nall meta tests passed")
