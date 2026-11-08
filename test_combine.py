"""Tests for combine.py: label correctness, causality, controls, search honesty.

Run:  python test_combine.py
"""
from __future__ import annotations

import random

import combine


def _synthetic_pair(n=400, edge=0.0, seed=0):
    """Rows with a chosen feature->label relationship (edge=0 -> coin flip)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        x = rng.gauss(0, 1)
        p = 0.5 + edge * x                      # edge=0 -> p=0.5 exactly
        y = 1 if rng.random() < min(max(p, 0.0), 1.0) else 0
        # feature 0 carries the signal; the rest are noise
        feat = [x] + [rng.gauss(0, 1) for _ in range(5)]
        rows.append((i, i, feat, y))
    return rows


def test_label_is_next_close_higher():
    """pair_rows label must equal 'next candle closed higher'."""
    from candle_store import CandleStore
    store = CandleStore("data/candles.db")
    for asset, per in store.series(min_candles=62, periods=[5]):
        candles = store.load(asset, per, limit=200)
        rows = combine.pair_rows(store, asset, per, max_candles=200,
                                 with_of=False)
        if not rows:
            continue
        for _t, i, _f, label in rows[:20]:
            want = 1 if candles[i + 1][2] > candles[i][2] else 0
            assert label == want, (asset, i, label, want)
        print("PASS test_label_is_next_close_higher")
        return
    print("SKIP test_label_is_next_close_higher (no data)")


def test_features_are_causal():
    """Perturbing candles AFTER index i must not change the row at i."""
    from candle_store import CandleStore
    store = CandleStore("data/candles.db")
    for asset, per in store.series(min_candles=80, periods=[5]):
        candles = store.load(asset, per, limit=160)
        if len(candles) < 120:
            continue
        base = combine.pair_rows(store, asset, per, max_candles=160,
                                 with_of=False)
        if len(base) < 40:
            continue
        cut = len(candles) // 2
        tampered = [list(c) for c in candles]
        for k in range(cut + 1, len(tampered)):
            tampered[k][2] = tampered[k][2] * 1.5   # blow up future closes
            tampered[k][3] = tampered[k][3] * 1.5
            tampered[k][4] = tampered[k][4] * 1.5
        orig = store.load
        store.load = lambda a, p, limit=200: tampered if (a, p) == (asset, per) else orig(a, p, limit)
        try:
            pert = combine.pair_rows(store, asset, per, max_candles=160,
                                     with_of=False)
        finally:
            store.load = orig
        by_idx = {r[1]: r[2] for r in base}
        checked = 0
        for _t, i, feat, _lab in pert:
            if i in by_idx and i <= cut - 1:
                assert feat == by_idx[i], f"future leak at {asset} i={i}"
                checked += 1
        assert checked > 0
        print(f"PASS test_features_are_causal ({checked} rows checked)")
        return
    print("SKIP test_features_are_causal (no data)")


def test_coinflip_control_is_near_half():
    rows = {"A": _synthetic_pair(4000, edge=0.0, seed=1)}
    ctrl = combine.coinflip_control(rows)
    assert abs(ctrl.hit - 0.5) < 0.03, ctrl.hit
    print(f"PASS test_coinflip_control_is_near_half ({ctrl.hit:.3f})")


def test_no_edge_data_yields_no_edge():
    """Pooled evaluation on a pure coin flip must not beat breakeven."""
    rows = {f"P{k}": _synthetic_pair(3000, edge=0.0, seed=k) for k in range(4)}
    r = combine.evaluate(rows, family="logit", n_splits=3)
    assert r["ok"]
    assert r["test"].ci_low <= r["breakeven"], r["test"]
    print(f"PASS test_no_edge_data_yields_no_edge "
          f"(test {r['test'].hit*100:.1f}%)")


def test_learnable_edge_is_exploited():
    """If a real feature->label edge exists, the pipeline finds it (control)."""
    rows = {f"P{k}": _synthetic_pair(4000, edge=0.9, seed=10 + k)
            for k in range(4)}
    r = combine.evaluate(rows, family="logit", n_splits=3)
    assert r["ok"]
    assert r["test"].hit > 0.60, r["test"].hit
    print(f"PASS test_learnable_edge_is_exploited "
          f"(test {r['test'].hit*100:.1f}%)")


if __name__ == "__main__":
    test_label_is_next_close_higher()
    test_features_are_causal()
    test_coinflip_control_is_near_half()
    test_no_edge_data_yields_no_edge()
    test_learnable_edge_is_exploited()
    print("\nall combine tests passed")
